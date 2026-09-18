"""One HTTP attempt against a real local server (webhook-notifications task 5.1)."""

from __future__ import annotations

import http.server
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from sighop.webhooks.transport import (
    MAX_RETRY_AFTER_SECONDS,
    AttemptOutcome,
    parse_retry_after,
    post,
)


@dataclass
class Receiver:
    base: str
    requests: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)


def _handler(receiver: Receiver) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            receiver.requests.append((self.path, dict(self.headers.items()), body))
            match self.path.split("?")[0]:
                case "/ok":
                    self.send_response(204)
                    self.end_headers()
                case "/busy":
                    self.send_response(503)
                    self.end_headers()
                case "/limited":
                    self.send_response(429)
                    self.send_header("Retry-After", "2")
                    self.end_headers()
                case "/moved":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                    self.end_headers()
                case "/slow":
                    time.sleep(1.0)
                    self.send_response(204)
                    self.end_headers()
                case _:
                    self.send_response(404)
                    self.end_headers()

    return Handler


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    holder = Receiver(base="")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(holder))
    server.daemon_threads = True
    holder.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield holder
    finally:
        server.shutdown()
        server.server_close()


def test_2xx_is_delivered_with_headers_and_body(receiver: Receiver) -> None:
    result = post(f"{receiver.base}/ok", b'{"a":1}', "application/json")
    assert result.outcome is AttemptOutcome.DELIVERED
    assert result.status == 204
    assert result.summary == "HTTP 204"
    [(_path, headers, body)] = receiver.requests
    assert body == b'{"a":1}'
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"].startswith("sighop/")


def test_5xx_is_retryable(receiver: Receiver) -> None:
    result = post(f"{receiver.base}/busy", b"{}", "application/json")
    assert result.outcome is AttemptOutcome.RETRYABLE
    assert result.status == 503


def test_429_is_retryable_with_retry_after(receiver: Receiver) -> None:
    result = post(f"{receiver.base}/limited", b"{}", "application/json")
    assert result.outcome is AttemptOutcome.RETRYABLE
    assert result.status == 429
    assert result.retry_after == 2.0


def test_404_is_final(receiver: Receiver) -> None:
    result = post(f"{receiver.base}/missing", b"{}", "application/json")
    assert result.outcome is AttemptOutcome.FINAL
    assert result.status == 404


def test_a_redirect_is_not_followed(receiver: Receiver) -> None:
    result = post(f"{receiver.base}/moved", b"{}", "application/json")
    assert result.outcome is AttemptOutcome.FINAL
    assert result.status == 302
    assert [path for path, _, _ in receiver.requests] == ["/moved"]


def test_a_timeout_is_retryable_and_bounded(receiver: Receiver) -> None:
    started = time.monotonic()
    result = post(f"{receiver.base}/slow", b"{}", "application/json", timeout=0.2)
    assert time.monotonic() - started < 0.9
    assert result.outcome is AttemptOutcome.RETRYABLE
    assert result.status is None
    assert "timed out" in result.reason


def test_a_refused_connection_is_retryable_and_names_no_url() -> None:
    result = post("http://127.0.0.1:9/secret-path?token=x", b"{}", "application/json", timeout=1)
    assert result.outcome is AttemptOutcome.RETRYABLE
    assert "secret-path" not in result.reason and "token" not in result.reason


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, None), ("2", 2.0), ("0", 0.0), ("100000", MAX_RETRY_AFTER_SECONDS), ("soon", None)],
)
def test_retry_after_parsing(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


def test_retry_after_as_an_http_date() -> None:
    assert parse_retry_after("Sun, 13 Sep 2026 12:00:05 GMT", now=1_789_300_800.0) == pytest.approx(
        5.0
    )
