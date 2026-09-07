"""Request provenance, and one wide event per unit of work (design D9, §9).

This exists *because* there is no authentication, not in spite of it. Two
attacks work against an unauthenticated loopback service with no further effort:

* **CSRF.** Any page open in the operator's browser can
  `POST http://127.0.0.1:8080/transmit/enable`. It cannot read the response, and
  it does not need to — enabling transmission is the whole of the attack.
* **DNS rebinding.** A hostname the attacker controls resolves to `127.0.0.1`,
  which makes their JavaScript same-origin with the panel and able to *read*
  responses. Private key material is in that set.

So every state-changing request must carry a token this process issued into a
page it served, and every request's declared host must be one the interface was
configured to answer to. Neither is authentication and neither is presented as
such: they are the difference between "reachable by anything that can route to
the port" and "reachable by anything that can render a page in the operator's
browser", and the second is a very much larger set.

The guard is plain ASGI middleware rather than Starlette's
`BaseHTTPMiddleware`, for one concrete reason: a token submitted in a form is in
the request *body*, and reading a body in `BaseHTTPMiddleware` consumes the
stream the handler was going to read. Here the body is read once and replayed
downstream, so a handler sees exactly what the browser sent.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import cast
from urllib.parse import parse_qsl

from sighop.logging import Logger, get_logger

Scope = MutableMapping[str, object]
Receive = Callable[[], Awaitable[MutableMapping[str, object]]]
Send = Callable[[MutableMapping[str, object]], Awaitable[None]]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""What a browser may issue by navigating, prefetching or reloading. None of
these may change state, transmit, or reveal key material — ever (design D9)."""

TOKEN_HEADER = "x-sighop-token"
TOKEN_FIELD = "_token"
"""Header first, form field second. HTMX sends the header for every request from
a served page; the field is what a plain `<form method="post">` carries, and the
panel must keep working when a script does not run."""

MAX_GUARDED_BODY = 1 << 20
"""One mebibyte. The guard reads the body to find a form token, so the body has
a bound — an unbounded read here would be a way to make the panel hold memory
before any handler had agreed to."""

REBINDING_STATUS = 421
"""`Misdirected Request`: this server is not the one for that host name. The
honest code, and it is not 400 — nothing about the request is malformed."""

FORBIDDEN_STATUS = 403


class RequestGuard:
    """Host check, provenance token, and the request's own wide event.

    Ordered deliberately: the host is checked first, because a rebinding request
    must be refused *before any handler runs*; the token second; and the event
    is emitted for all three outcomes, because a refusal nobody can see is a
    refusal nobody will investigate.
    """

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        *,
        token: str,
        hosts: frozenset[str] | None,
        logger: Logger | None = None,
    ) -> None:
        self.app = app
        self.token = token
        self.hosts = hosts
        self.log = logger or get_logger(component="web")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind == "websocket":
            # A socket is a read of live state from a page, and rebinding
            # reaches it as easily as it reaches a page. Its *event* is the
            # connection's own, emitted when it closes, so only the host is
            # checked here.
            if not self._host_allowed(scope):
                await _reject_websocket(send)
                return
            await self.app(scope, receive, send)
            return
        if kind != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        status = 500
        outcome = "error"

        async def capture(message: MutableMapping[str, object]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = cast("int", message.get("status", 0))
            await send(message)

        if not self._host_allowed(scope):
            status, outcome = REBINDING_STATUS, "host_rejected"
            await _refuse(
                send,
                REBINDING_STATUS,
                b"This server does not answer to that host name.",
            )
            self._emit(scope, method, path, status, outcome, started)
            return

        if method not in SAFE_METHODS:
            refusal, receive = await self._provenance(scope, receive)
            if refusal is not None:
                status, outcome = FORBIDDEN_STATUS, refusal
                await _refuse(
                    send,
                    FORBIDDEN_STATUS,
                    b"This request did not come from a page this process served.",
                )
                self._emit(scope, method, path, status, outcome, started)
                return

        try:
            await self.app(scope, receive, capture)
        except Exception:
            # Reported here and re-raised: the error page is Starlette's
            # server-error handler, which is outside this middleware, and it is
            # what turns the failure into a response with no traceback in it.
            self._emit(scope, method, path, 500, "error", started)
            raise
        self._emit(
            scope,
            method,
            path,
            status,
            "success" if status < 400 else "refused",
            started,
        )

    # --- The two checks ----------------------------------------------------

    def _host_allowed(self, scope: Scope) -> bool:
        """Whether the declared host is one this interface was configured for.

        `None` means the check is off, which is the case for an application that
        is not being served on a socket at all — a test client's, where there is
        no configured address for a host header to disagree with.
        """
        if self.hosts is None:
            return True
        return _header(scope, "host") in self.hosts

    async def _provenance(
        self, scope: Scope, receive: Receive
    ) -> tuple[str | None, Receive]:
        """Whether this state-changing request came from a page we served.

        Returns the refusal reason (or `None`) and the receive channel to use
        downstream — a replay of the body when one was read, so consuming it
        here costs the handler nothing.
        """
        site = _header(scope, "sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            # Sent by every current browser and trivially absent otherwise, so
            # it is checked where present and never relied on alone.
            return "cross_origin", receive

        if _header(scope, TOKEN_HEADER) == self.token:
            return None, receive

        body, replay = await _read_body(receive)
        if _form_token(scope, body) == self.token:
            return None, replay
        return "no_token", replay

    # --- The event ---------------------------------------------------------

    def _emit(
        self,
        scope: Scope,
        method: str,
        path: str,
        status: int,
        outcome: str,
        started: float,
    ) -> None:
        """Exactly one structured event per completed request (§9).

        The matched route rather than the path where one is known: a path
        carries identifiers and a route is what a reader compares across
        requests. The query string is never carried at all — it is the one part
        of a request a password could end up in by mistake.
        """
        route = scope.get("route")
        emit = self.log.info if outcome == "success" else self.log.error
        emit(
            "web_request",
            outcome=outcome,
            method=method,
            route=getattr(route, "path", None) or path,
            path=path,
            status=status,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
        )


# --- Helpers ----------------------------------------------------------------


def _header(scope: Scope, name: str) -> str | None:
    wanted = name.encode("latin-1")
    headers = cast("Iterable[tuple[bytes, bytes]]", scope.get("headers") or ())
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


async def _read_body(receive: Receive) -> tuple[bytes, Receive]:
    """Drain the request body, and hand back a channel that replays it."""
    chunks: list[bytes] = []
    total = 0
    more = True
    while more:
        message = await receive()
        if message.get("type") != "http.request":
            break
        chunk = cast("bytes", message.get("body") or b"")
        total += len(chunk)
        if total <= MAX_GUARDED_BODY:
            chunks.append(chunk)
        more = bool(message.get("more_body", False))
    body = b"".join(chunks)

    delivered = False

    async def replay() -> MutableMapping[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return body, replay


def _form_token(scope: Scope, body: bytes) -> str | None:
    content_type = (_header(scope, "content-type") or "").split(";")[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        # Anything else — multipart, JSON — carries the token in the header.
        # Parsing a multipart body here to find one would be a second, weaker
        # implementation of a parser the handler already has.
        return None
    for key, value in parse_qsl(body.decode("latin-1"), keep_blank_values=True):
        if key == TOKEN_FIELD:
            return value
    return None


async def _refuse(send: Send, status: int, message: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(message)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": message})


async def _reject_websocket(send: Send) -> None:
    await send({"type": "websocket.close", "code": 1008})
