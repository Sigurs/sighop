"""Host, session, provenance, and one wide event per unit of work (§8, §9).

Milestone 8 built this guard for a panel with no authentication, and every
reason it gave still holds now that there is some. Two attacks work against a
loopback service whatever else is in front of it:

* **CSRF.** Any page open in the operator's browser can
  `POST http://127.0.0.1:8080/transmit/enable`. It cannot read the response, and
  it does not need to — enabling transmission is the whole of the attack.
* **DNS rebinding.** A hostname the attacker controls resolves to `127.0.0.1`,
  which makes their JavaScript same-origin with the panel and able to *read*
  responses. Private key material is in that set.

So every state-changing request must carry a token issued into a page this
process served, and every request's declared host must be one the interface was
configured to answer to. Authentication does not replace either: a page on
another origin can ride a signed-in browser, and a rebound name can make its
script same-origin with the panel.

**Milestone 9 adds the session, in the same middleware** (design D5), so the one
`web_request` event can carry `actor` and every refusal is seen by the same code.
The order is: host → session from the `sighop_session` cookie → public-path
check → provenance → handler. The public set is short and fixed —
`GET`/`POST /login`, `GET`/`POST /setup` and `/static/` — and anything not named in it requires a
session, so a route added later is protected without anyone remembering to
protect it. Matching is by path, because routing happens inside the application
and the guard runs before it; `tests/test_web_auth_routes.py` walks the route
table to check the consequence rather than trusting it.

The provenance token is the session's own once there is one (design D6). The
sign-in and first-run setup forms, which by definition have no session yet,
carry the process's token — all a pre-session request can be bound to. While
setup is pending, a refused page request goes to the setup form rather than the
sign-in form (web-first-run-setup design D7).

The guard is plain ASGI middleware rather than Starlette's
`BaseHTTPMiddleware`, for one concrete reason: a token submitted in a form is in
the request *body*, and reading a body in `BaseHTTPMiddleware` consumes the
stream the handler was going to read. Here the body is read once and replayed
downstream, so a handler sees exactly what the browser sent.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qsl, quote, urlsplit

from sighop.logging import Logger, get_logger
from sighop.web.auth import SESSION_COOKIE, Authenticator, Session

if TYPE_CHECKING:  # pragma: no cover
    from starlette.requests import HTTPConnection

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
UNAUTHORIZED_STATUS = 401
SEE_OTHER_STATUS = 303

LOGIN_PATH = "/login"
SETUP_PATH = "/setup"
STATIC_PREFIX = "/static/"
PUBLIC_ROUTES = frozenset(
    {
        ("GET", LOGIN_PATH),
        ("POST", LOGIN_PATH),
        ("GET", SETUP_PATH),
        ("POST", SETUP_PATH),
    }
)
"""The whole public surface, with `/static/`: the sign-in form and the first-run
setup form, each with its submission. Nothing else is reachable without a
session, and nothing can be added here by a route declaring itself public: it is
added here, in review, or it is not public (design D5)."""

SESSION_SCOPE_KEY = "sighop.session"
"""Where the guard leaves the resolved session for handlers. `None` when the
request has none, which only a public route can observe."""

UNAUTHENTICATED = "unauthenticated"


def is_public(method: str, path: str) -> bool:
    return (method, path) in PUBLIC_ROUTES or path.startswith(STATIC_PREFIX)


def current_session(connection: HTTPConnection) -> Session | None:
    """The session the guard resolved for this request, if any."""
    return cast("Session | None", connection.scope.get(SESSION_SCOPE_KEY))


class RequestGuard:
    """Host, session, public path, provenance, and the request's own wide event.

    Ordered deliberately: the host is checked first, because a rebinding request
    must be refused *before any handler runs*; the session second, so every
    later refusal knows who asked; the public-path check third; the token
    fourth; and the event is emitted for every outcome, because a refusal nobody
    can see is a refusal nobody will investigate.
    """

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        *,
        auth: Authenticator,
        hosts: frozenset[str] | None,
        logger: Logger | None = None,
    ) -> None:
        self.app = app
        self.auth = auth
        self.hosts = hosts
        self.log = logger or get_logger(component="web")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind == "websocket":
            await self._websocket(scope, receive, send)
            return
        if kind != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        status = 500
        actor = UNAUTHENTICATED

        async def capture(message: MutableMapping[str, object]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = cast("int", message.get("status", 0))
            await send(message)

        if not self._host_allowed(scope):
            await _refuse(
                send,
                REBINDING_STATUS,
                b"This server does not answer to that host name.",
            )
            self._emit(scope, method, path, REBINDING_STATUS, "host_rejected", started, actor)
            return

        session = await self.auth.resolve(_cookie(scope, SESSION_COOKIE))
        scope[SESSION_SCOPE_KEY] = session
        if session is not None:
            actor = session.username

        if session is None and not is_public(method, path):
            if method in SAFE_METHODS:
                if self.auth.setup_pending:
                    # `next` is not carried: setup always lands on the overview.
                    await _redirect(send, SETUP_PATH)
                else:
                    await _redirect_to_login(send, scope)
                self._emit(
                    scope, method, path, SEE_OTHER_STATUS, "unauthenticated", started, actor
                )
            else:
                await _refuse(send, UNAUTHORIZED_STATUS, b"Sign in first.")
                self._emit(
                    scope, method, path, UNAUTHORIZED_STATUS, "unauthenticated", started, actor
                )
            return

        if method not in SAFE_METHODS:
            expected = (
                self.auth.login_token
                if (method, path) in PUBLIC_ROUTES or session is None
                else session.csrf_token
            )
            refusal, receive = await self._provenance(scope, receive, expected)
            if refusal is not None:
                await _refuse(
                    send,
                    FORBIDDEN_STATUS,
                    b"This request did not come from a page this process served.",
                )
                self._emit(scope, method, path, FORBIDDEN_STATUS, refusal, started, actor)
                return

        try:
            await self.app(scope, receive, capture)
        except Exception:
            # Reported here and re-raised: the error page is Starlette's
            # server-error handler, which is outside this middleware, and it is
            # what turns the failure into a response with no traceback in it.
            self._emit(scope, method, path, 500, "error", started, actor)
            raise
        # A handler may have ended or replaced the session (sign-in, sign-out);
        # the event names who the request was made by when it arrived, unless
        # it arrived with nobody and left signed in.
        after = scope.get(SESSION_SCOPE_KEY)
        if actor == UNAUTHENTICATED and isinstance(after, Session):
            actor = after.username
        self._emit(
            scope,
            method,
            path,
            status,
            "success" if status < 400 else "refused",
            started,
            actor,
        )

    # --- The live feed ------------------------------------------------------

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Host, session and `Origin`, all before `accept()` (design D5).

        A socket is a read of live state, and rebinding reaches it as easily as
        it reaches a page. `SameSite=Strict` already withholds the cookie from a
        cross-site handshake; the `Origin` check is a second, independent reason
        to refuse one. A refused socket is closed with 1008 and gets its own
        closing event, carrying who it was — which, refused, is nobody.
        """
        started = time.perf_counter()
        reason: str | None = None
        session: Session | None = None
        if not self._host_allowed(scope):
            reason = "host_rejected"
        else:
            session = await self.auth.resolve(_cookie(scope, SESSION_COOKIE))
            if session is None:
                reason = "unauthenticated"
            elif not self._origin_allowed(scope):
                reason = "origin_rejected"
        if reason is not None:
            await _reject_websocket(send)
            self.log.error(
                "web_feed_closed",
                outcome="refused",
                reason=reason,
                actor=UNAUTHENTICATED if reason != "origin_rejected" else _actor(session),
                delivered=0,
                dropped=0,
                incomplete=False,
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
            )
            return
        scope[SESSION_SCOPE_KEY] = session
        await self.app(scope, receive, send)

    def _origin_allowed(self, scope: Scope) -> bool:
        """Whether the handshake's `Origin` names a host this interface serves.

        Absent is refused: every browser sends it on a WebSocket handshake, so a
        client that does not is not a page this process served.
        """
        if self.hosts is None:
            return True
        origin = _header(scope, "origin")
        if not origin:
            return False
        parts = urlsplit(origin)
        return parts.scheme in ("http", "https") and parts.netloc in self.hosts

    # --- The checks ---------------------------------------------------------

    def _host_allowed(self, scope: Scope) -> bool:
        """Whether the declared host is one this interface was configured for.

        `None` means the check is off, which is the case for an application that
        is not being served on a socket at all — a test client's, where there is
        no configured address for a host header to disagree with. It turns off
        the rebinding check only; nothing turns off authentication.
        """
        if self.hosts is None:
            return True
        return _header(scope, "host") in self.hosts

    async def _provenance(
        self, scope: Scope, receive: Receive, expected: str
    ) -> tuple[str | None, Receive]:
        """Whether this state-changing request came from a page we served.

        Returns the refusal reason (or `None`) and the receive channel to use
        downstream — a replay of the body when one was read, so consuming it
        here costs the handler nothing. `expected` is the session's own token,
        or the process's for the sign-in form: a token from another session is
        refused exactly as no token is.
        """
        site = _header(scope, "sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            # Sent by every current browser and trivially absent otherwise, so
            # it is checked where present and never relied on alone.
            return "cross_origin", receive

        if _matches(_header(scope, TOKEN_HEADER), expected):
            return None, receive

        body, replay = await _read_body(receive)
        if _matches(_form_token(scope, body), expected):
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
        actor: str,
    ) -> None:
        """Exactly one structured event per completed request (§9).

        The matched route rather than the path where one is known: a path
        carries identifiers and a route is what a reader compares across
        requests. The query string is never carried at all — it is the one part
        of a request a password could end up in by mistake. `client` is the
        socket's own address; a forwarding header is never read (§8).
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
            actor=actor,
            client=client_address(scope),
            duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
        )


# --- Helpers ----------------------------------------------------------------


def client_address(scope: Scope) -> str:
    """The connection's own peer address. Never a forwarding header (§8)."""
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return "unknown"


def _actor(session: Session | None) -> str:
    return UNAUTHENTICATED if session is None else session.username


def _matches(presented: str | None, expected: str) -> bool:
    return presented is not None and hmac.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    )


def _cookie(scope: Scope, name: str) -> str | None:
    raw = _header(scope, "cookie")
    if not raw:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return None
    morsel = jar.get(name)
    return None if morsel is None else morsel.value


async def _redirect_to_login(send: Send, scope: Scope) -> None:
    """`303` to the sign-in form, remembering the page that was asked for.

    Only the path and query are carried, and `/login` accepts `next` only as a
    same-origin absolute path, so this cannot be turned into an open redirect.
    """
    path = str(scope.get("path", "/"))
    query = cast("bytes", scope.get("query_string") or b"").decode("latin-1")
    wanted = path + (f"?{query}" if query else "")
    location = f"{LOGIN_PATH}?next={quote(wanted, safe='')}" if wanted != "/" else LOGIN_PATH
    await _redirect(send, location)


async def _redirect(send: Send, location: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": SEE_OTHER_STATUS,
            "headers": [
                (b"location", location.encode("latin-1")),
                (b"content-length", b"0"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b""})


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
