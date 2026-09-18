"""One HTTP attempt, classified (webhook-notifications design D3).

Standard library only: traffic is a handful of POSTs a day, and a runtime HTTP
client would grow the image and the lock for no measured need. `post` blocks, so
callers run it through `asyncio.to_thread`; its timeout bounds the thread.

Redirects are not followed. A webhook endpoint that answers `3xx` is either
misconfigured or pointing somewhere the operator did not choose, and following
it would carry the payload there.
"""

from __future__ import annotations

import contextlib
import email.utils
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import IO

from sighop.logging import package_version

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RETRY_AFTER_SECONDS = 300.0


class AttemptOutcome(StrEnum):
    DELIVERED = "delivered"
    RETRYABLE = "retryable"
    FINAL = "final"


@dataclass(frozen=True, slots=True)
class AttemptResult:
    outcome: AttemptOutcome
    status: int | None = None
    """The HTTP status, when the server answered at all."""

    reason: str = ""
    """Why it failed, in words that never include the URL."""

    retry_after: float | None = None
    """Seconds a `429` asked us to wait, capped."""

    duration_ms: float = 0.0

    @property
    def delivered(self) -> bool:
        return self.outcome is AttemptOutcome.DELIVERED

    @property
    def summary(self) -> str:
        """`HTTP 204`, `HTTP 404`, or the network reason."""
        return f"HTTP {self.status}" if self.status is not None else self.reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def user_agent() -> str:
    return f"sighop/{package_version()}"


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """`Retry-After` as seconds: a number, or an HTTP date. Capped at 300 s."""
    if value is None:
        return None
    cleaned = value.strip()
    try:
        seconds = float(cleaned)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(cleaned)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - (time.time() if now is None else now)
    if seconds != seconds:  # NaN
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def classify_status(status: int, retry_after: str | None = None) -> AttemptResult:
    if 200 <= status < 300:
        return AttemptResult(AttemptOutcome.DELIVERED, status=status)
    if status == 429:
        return AttemptResult(
            AttemptOutcome.RETRYABLE,
            status=status,
            reason="rate limited",
            retry_after=parse_retry_after(retry_after),
        )
    if status >= 500:
        return AttemptResult(AttemptOutcome.RETRYABLE, status=status, reason="server error")
    if 300 <= status < 400:
        return AttemptResult(AttemptOutcome.FINAL, status=status, reason="redirect not followed")
    return AttemptResult(AttemptOutcome.FINAL, status=status, reason="rejected")


def _drain(response: IO[bytes]) -> None:
    with contextlib.suppress(OSError, ValueError):
        response.read(65536)


def post(
    url: str,
    body: bytes,
    content_type: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> AttemptResult:
    """POST once. Never raises for anything the network or the server does."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "User-Agent": user_agent()},
    )
    started = time.monotonic()

    def timed(result: AttemptResult) -> AttemptResult:
        return AttemptResult(
            outcome=result.outcome,
            status=result.status,
            reason=result.reason,
            retry_after=result.retry_after,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )

    try:
        with _OPENER.open(request, timeout=timeout) as response:
            _drain(response)
            return timed(classify_status(response.status))
    except urllib.error.HTTPError as exc:
        with exc:
            _drain(exc)
            return timed(classify_status(exc.code, exc.headers.get("Retry-After")))
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            text = f"timed out after {timeout:g} s"
        elif isinstance(reason, socket.gaierror):
            text = f"host did not resolve ({reason.strerror or reason})"
        elif isinstance(reason, OSError):
            text = f"connection failed ({reason.strerror or type(reason).__name__})"
        else:
            text = f"connection failed ({reason})"
        return timed(AttemptResult(AttemptOutcome.RETRYABLE, reason=text))
    except TimeoutError:
        return timed(
            AttemptResult(AttemptOutcome.RETRYABLE, reason=f"timed out after {timeout:g} s")
        )
    except OSError as exc:
        return timed(
            AttemptResult(
                AttemptOutcome.RETRYABLE,
                reason=f"connection failed ({exc.strerror or type(exc).__name__})",
            )
        )
    except ValueError as exc:
        # urllib refuses a URL it cannot request; stored URLs were validated, so
        # this is a row altered outside sighop. Not worth retrying.
        return timed(
            AttemptResult(AttemptOutcome.FINAL, reason=f"unusable URL ({type(exc).__name__})")
        )
