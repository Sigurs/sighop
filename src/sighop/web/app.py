"""The application, and the service that runs it (design D1, D2).

Three things live here and they are deliberately separate:

* `create_app` builds the ASGI application from the read seam. It touches no
  socket and starts nothing, so a test can construct one with a stub state, no
  runtime and no database and request every page.
* `WebInterface.bind` takes the listening socket. It happens **before** the run
  starts, because a port that cannot be bound is a startup failure in the same
  way a configured database that cannot be reached is (`web-server`): reported
  before the platform begins processing traffic, never discovered later as a
  silently absent interface.
* `WebInterface.service` is the callable `Runtime.services` carries. It runs
  `uvicorn.Server(...).serve()` on the already-bound socket, inside the
  runtime's own event loop.

**Never `uvicorn.run()`** (design D1). It builds its own event loop, and the
loop this process has is the one the radio's serial transport is running on.

**And never uvicorn's signal handling either.** In this version there is no
`install_signal_handlers` flag: `Server.serve()` wraps itself in
`capture_signals()`, which calls `signal.signal()` for SIGINT and SIGTERM and so
would replace the handlers `Runtime.install_signal_handlers` installed. Ctrl-C
would then stop the web server and leave the run going. `_UnsignalledServer`
below is the whole of the difference — the public `serve()` with that one
context manager made a no-op, so the process's signal handling is exactly what a
run without the interface has.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import errno
import secrets
import socket
import time
from collections.abc import Awaitable, Callable, Generator, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sighop.db.engine import Succeeded
from sighop.logging import Logger, get_logger
from sighop.web.chat import ConversationLog
from sighop.web.deps import Panel
from sighop.web.feed import Connection, EntityTraffic, FeedHub
from sighop.web.guard import RequestGuard
from sighop.web.render import (
    REGULATORY_NOTE,
    contact_rows,
    meter_for,
    modem_readings,
    persistence_view,
    queue_rows,
)
from sighop.web.routes import admin, chat, keys, rooms
from sighop.web.serialize import logged_packet
from sighop.web.state import PanelState

FEED_SEND_TIMEOUT_SECONDS = 10.0
"""How long one message may take to reach a browser before the connection is
closed. A browser that has stopped reading fills its socket buffer and then
never drains; without a bound here, that connection would hold a task for the
life of the process (`web-dashboard`: "the connection is eventually closed")."""

DEFAULT_WEB_HOST = "127.0.0.1"
"""Loopback, and it is the default because the alternative is a transmit-capable,
key-revealing surface with no authentication reachable from the network. An
operator may choose otherwise; the choice is announced (`web-server`)."""

DEFAULT_WEB_PORT = 8080

SHUTDOWN_GRACE_SECONDS = 5.0
"""How long in-flight requests have to finish once the run is stopping. Bounded
rather than unbounded: a shutdown that waits on a browser is a shutdown a
browser can prevent."""

PACKAGE_DIR = Path(__file__).parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

NO_AUTHENTICATION = (
    "this build has no authentication: anything that can reach the port can "
    "transmit and reveal private key material"
)
"""Said in full wherever the interface's exposure is reported. Milestone 9 adds
authentication; until then the gap is stated rather than implied."""


class WebBindError(RuntimeError):
    """The interface could not listen. Names the address, port and reason."""


# --- The application --------------------------------------------------------


def new_token() -> str:
    """One provenance token, minted per process (design D9).

    Not a session and not a credential: it identifies a page *this process
    served*, which is the whole of what it claims. It never leaves the pages
    this process renders and it dies with the process.
    """
    return secrets.token_urlsafe(32)


def create_app(
    state: PanelState,
    *,
    feed: FeedHub | None = None,
    logger: Logger | None = None,
    token: str | None = None,
    hosts: frozenset[str] | None = None,
    conversations: ConversationLog | None = None,
    sealing_secret: bytes | None = None,
) -> FastAPI:
    """Build the panel over one read seam.

    Takes the state and the feed hub; the repositories arrive with the state,
    because `PanelState.persistence` is where they live and a second way to
    reach them would be a second answer to "is there a database".

    `sealing_secret` is `SIGHOP_SECRET_KEY`, which `cli.py` already reads and is
    the one module that composes both sides (design D1). It is needed only to
    open a stored seed for an export; `None` is the default and every page that
    would need it says so instead of failing.

    `hosts` is the set of `Host` header values this application will answer to.
    `None` turns that check off and is for an application that is not being
    served on a socket — a test client's, where there is no configured address
    for a host header to disagree with. `WebInterface.build_app` always supplies
    the real set.

    Nothing here binds, listens or connects. The application is a value.
    """
    log = logger or get_logger(component="web")
    issued = token or new_token()
    app = FastAPI(
        title="sighop",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # No lifespan events: the platform this serves is already started, and
        # the application must never be a second place where startup happens.
        lifespan=None,
    )
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.panel = Panel(
        state=state,
        templates=templates,
        token=issued,
        logger=log,
        feed=feed,
        sealing_secret=sealing_secret,
        chat=conversations or ConversationLog(),
    )
    app.state.feed = feed
    app.state.templates = templates
    app.state.log = log
    app.state.token = issued
    app.state.hosts = hosts

    app.add_middleware(RequestGuard, token=issued, hosts=hosts, logger=log)
    app.add_exception_handler(Exception, _error_page)
    app.include_router(admin.router)
    app.include_router(keys.router)
    app.include_router(rooms.router)
    app.include_router(chat.router)

    def page(request: Request, name: str, **extra: object) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={**page_context(state, token=issued), **extra},
        )

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> HTMLResponse:
        """The panel's front page: what the platform is doing right now."""
        status = state.scheduler.status()
        return page(
            request,
            "overview.html",
            queues=queue_rows(status),
            dedup=state.pipeline.dedup.stats,
            delivered=state.pipeline.delivered,
            paths=state.pipeline.paths.destination_count,
            contacts=len(state.contacts),
            entities=entity_rows(state, feed),
        )

    @app.get("/contacts", response_class=HTMLResponse)
    async def contacts(request: Request) -> HTMLResponse:
        """Who we have heard, with the route a message to them would take."""
        return page(
            request,
            "contacts.html",
            rows=contact_rows(state.contacts, state.pipeline.paths),
        )

    @app.get("/modem", response_class=HTMLResponse)
    async def modem(request: Request) -> HTMLResponse:
        """The board's own readback, absences included (§4.1)."""
        return page(
            request,
            "modem.html",
            readings=modem_readings(state.probe_result, state.radio),
            probe=state.probe_result,
        )

    @app.websocket("/feed")
    async def feed_socket(websocket: WebSocket) -> None:
        """The live packet feed (design D4).

        Paints the recorded history first, marks where it ends, then streams.
        Nothing on the reception path awaits this socket: the hub's `offer` puts
        a row into a bounded deque and returns, and everything below happens in
        this connection's own task.
        """
        await serve_feed(websocket, state=state, feed=feed, logger=log)

    return app


async def serve_feed(
    websocket: WebSocket,
    *,
    state: PanelState,
    feed: FeedHub | None,
    logger: Logger,
) -> None:
    """One feed connection, from accept to its closing event.

    Written as a function rather than inline so it can be driven by a test with
    no browser: the socket is the only thing a browser brings, and everything
    interesting here is about what is sent and in what order.
    """
    await websocket.accept()
    started = time.perf_counter()
    hub = feed or FeedHub(logger=logger)
    connection = hub.connect(at=dt.datetime.now(dt.UTC))
    reason = "closed"
    try:
        await _paint_history(websocket, state=state, connection=connection)
        await _stream(websocket, connection)
    except WebSocketDisconnect:
        reason = "disconnected"
    except TimeoutError:
        # The browser stopped reading. The grace is a bound, not a hope: a
        # connection that will not drain does not get to hold a task open.
        reason = "stalled"
        with contextlib.suppress(Exception):
            await websocket.close(code=1013)
    except asyncio.CancelledError:
        reason = "cancelled"
        with contextlib.suppress(Exception):
            await websocket.close(code=1001)
        raise
    finally:
        hub.disconnect(connection)
        # §9: one event per closed connection, carrying what it managed to
        # deliver and what it lost. A feed with a gap in it is a fact about the
        # session, not only about the browser.
        emit = logger.info if reason in ("closed", "disconnected") else logger.error
        emit(
            "web_feed_closed",
            outcome="success" if reason in ("closed", "disconnected") else reason,
            reason=reason,
            delivered=connection.delivered,
            dropped=connection.dropped,
            incomplete=connection.incomplete,
            duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
        )


async def _paint_history(
    websocket: WebSocket, *, state: PanelState, connection: Connection
) -> None:
    """The recent recorded packets, newest first, before anything live.

    With no database — or one that cannot be read — the feed starts empty and
    says that only live records are shown, rather than looking like a mesh that
    has been quiet (`web-dashboard`).
    """
    persistence = state.persistence
    rows: list[dict[str, object]] = []
    note = ""
    if persistence is None:
        note = "no database is configured, so only records from now on are shown"
    else:
        recent = await persistence.packet_log.recent()
        if isinstance(recent, Succeeded):
            rows = [logged_packet(row) for row in recent.value]
        else:
            note = (
                "the recorded history could not be read, so only records from "
                "now on are shown"
            )
    await _send(websocket, {"kind": "history", "records": rows, "note": note})
    # The boundary itself, as its own message: "this happened before you
    # connected" and "this is happening" are different claims.
    await _send(
        websocket,
        {"kind": "boundary", "at": dt.datetime.now(dt.UTC).isoformat(), "painted": len(rows)},
    )


async def _stream(websocket: WebSocket, connection: Connection) -> None:
    """Live records, in batches, until the connection closes."""
    await _send(websocket, connection.status())
    dropped = connection.dropped
    while not connection.closed:
        batch = await connection.drain()
        if batch:
            await _send(websocket, {"kind": "records", "records": batch})
        if connection.dropped != dropped:
            dropped = connection.dropped
            await _send(websocket, connection.status())


async def _send(websocket: WebSocket, message: dict[str, object]) -> None:
    """One message, bounded. A browser that will not read does not block a task."""
    async with asyncio.timeout(FEED_SEND_TIMEOUT_SECONDS):
        await websocket.send_json(message)


async def _error_page(request: Request, exc: Exception) -> HTMLResponse:
    """One generic page for an unhandled failure (`web-server`).

    No traceback, no path, no exception text, no internal detail: a page that
    explained the failure to a browser would be explaining it to whoever reached
    the port, and on this build that is anyone who can. What actually happened
    is in the request's wide event, which is where an operator looks anyway.
    """
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={"token": getattr(request.app.state, "token", "")},
        status_code=500,
    )


def entity_rows(state: PanelState, feed: FeedHub | None) -> list[dict[str, object]]:
    """Per-entity TX/RX counters (§8), from the traffic the hub has seen.

    With no hub attached the counts are zero and the identities are still
    listed: "this run holds three identities and none has transmitted" is a
    different screen from "this run holds no identities".
    """
    traffic = feed.traffic if feed is not None else EntityTraffic()
    rows: list[dict[str, object]] = []
    for stub in state.adverts.stubs:
        counts = traffic.for_entity(stub.entity_id, stub.node_hash)
        rows.append(
            {
                "name": stub.name,
                "entity_id": stub.entity_id,
                "public_key": stub.identity.public_key.hex(),
                "node_hash": stub.node_hash,
                "node_type": stub.node_type.name,
                "persistent": stub.persistent,
                "adverts_sent": stub.adverts_sent,
                **counts,
            }
        )
    return rows


def page_context(state: PanelState, *, token: str = "") -> dict[str, object]:
    """What every page is given, whatever else it also reads.

    The duty-cycle meter, the transmit gate and the persistence state are on
    every page by construction (design D12), so they are assembled once here
    rather than remembered per route. The provenance token rides along for the
    same reason: a page that forgot it would be a page whose forms silently
    stopped working.
    """
    status = state.scheduler.status()
    return {
        "state": state,
        "scheduler": state.scheduler,
        "status": status,
        "meter": meter_for(status),
        "durability": persistence_view(state.persistence),
        "regulatory_note": REGULATORY_NOTE,
        "persistence": state.persistence,
        "token": token,
    }


# --- The service ------------------------------------------------------------


class _UnsignalledServer(uvicorn.Server):
    """`uvicorn.Server` with its signal capture removed.

    See the module docstring: `serve()` otherwise installs its own SIGINT and
    SIGTERM handlers over the ones `Runtime.install_signal_handlers` set, and
    Ctrl-C would stop the interface instead of the run.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


@dataclass(slots=True)
class WebInterface:
    """A bound socket, and the server that will serve the panel on it."""

    state: PanelState
    host: str
    port: int
    listener: socket.socket
    feed: FeedHub | None = None
    conversations: ConversationLog | None = None
    logger: Logger | None = None
    sealing_secret: bytes | None = None
    """Handed in by `cli.py`, which reads it anyway (design D1). Never logged,
    never rendered, and not part of `PanelState`: a secret is not panel state."""

    shutdown_grace: float = SHUTDOWN_GRACE_SECONDS
    _server: uvicorn.Server | None = field(default=None, init=False, repr=False)

    @classmethod
    def bind(
        cls,
        state: PanelState,
        *,
        host: str = DEFAULT_WEB_HOST,
        port: int = DEFAULT_WEB_PORT,
        feed: FeedHub | None = None,
        conversations: ConversationLog | None = None,
        logger: Logger | None = None,
        sealing_secret: bytes | None = None,
    ) -> WebInterface:
        """Take the listening socket now, or fail startup saying why.

        Binding here rather than inside the server is what makes a port clash a
        *startup* failure: it happens before the pipeline exists, so a run that
        cannot serve the interface it was asked for does not quietly become a
        run without one.
        """
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            listener = socket.create_server(
                (host, port), family=family, backlog=128, reuse_port=False
            )
        except OSError as exc:
            raise WebBindError(_bind_message(host, port, exc)) from exc
        bound = listener.getsockname()
        return cls(
            state=state,
            host=host,
            # `--web-port 0` asks the OS to choose; what is reported has to be
            # the port somebody can actually open, not the zero that was asked.
            port=int(bound[1]),
            listener=listener,
            feed=feed,
            conversations=conversations,
            logger=logger or get_logger(component="web"),
            sealing_secret=sealing_secret,
        )

    @property
    def loopback(self) -> bool:
        """Whether this bind is reachable only from this host.

        Judged from the address itself rather than from the string: `localhost`,
        `127.0.0.1` and `::1` are all loopback, and `0.0.0.0` is not, however it
        was spelled.
        """
        return _is_loopback(self.host)

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def startup_lines(self) -> list[str]:
        """What the run's output says about the interface. Never empty.

        The non-loopback warning has no off switch and no quiet mode, which is
        the requirement rather than an oversight: there is no option that serves
        that bind without saying what it exposes (`web-server`).
        """
        lines = [f"web: {self.url} — the panel is served from inside this run"]
        if self.loopback:
            lines.append(f"     reachable from this host only; {NO_AUTHENTICATION}")
        else:
            lines.append(
                f"     REACHABLE FROM THE NETWORK on {self.host}: {NO_AUTHENTICATION}. "
                "Authentication arrives in milestone 9"
            )
        return lines

    def report(self) -> None:
        """Emit the interface's own startup event. Not suppressible either."""
        assert self.logger is not None
        self.logger.info(
            "web_interface_listening",
            outcome="success",
            web_host=self.host,
            web_port=self.port,
            loopback=self.loopback,
            authenticated=False,
            detail=(
                NO_AUTHENTICATION
                if self.loopback
                else f"reachable from the network; {NO_AUTHENTICATION}"
            ),
        )

    def build_app(self) -> FastAPI:
        """The application, with the host set this bind actually answers to.

        Always supplied here, never left to the default: an interface on a
        socket has a configured address, and a request declaring some other host
        name is a rebinding attempt whatever else it might be (design D9).
        """
        return create_app(
            self.state,
            feed=self.feed,
            logger=self.logger,
            hosts=allowed_hosts(self.host, self.port),
            conversations=self.conversations,
            sealing_secret=self.sealing_secret,
        )

    def service(self) -> Callable[[], Awaitable[None]]:
        """The callable `Runtime.services` carries."""
        return self.serve

    async def serve(self) -> None:
        """Serve until cancelled, then stop accepting and drain within the bound.

        The server runs as its own task and the cancellation is caught here, so
        a stopping run gets uvicorn's graceful shutdown — stop accepting, let
        in-flight requests finish, close the connections — rather than a socket
        torn out from under a half-written response.
        """
        config = uvicorn.Config(
            self.build_app(),
            # Neither is used: the socket is already bound and is handed in.
            host=self.host,
            port=self.port,
            # Logging is this project's, structured, one event per request
            # (§9). uvicorn's own logger would write a second, unstructured
            # line for the same work.
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=int(self.shutdown_grace),
        )
        server = _UnsignalledServer(config)
        self._server = server
        running = asyncio.create_task(
            server.serve(sockets=[self.listener]), name="web-server"
        )
        try:
            await asyncio.shield(running)
        except asyncio.CancelledError:
            server.should_exit = True
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(running, timeout=self.shutdown_grace + 1.0)
            if not running.done():
                # The grace period is a bound, not a hope: a connection that
                # will not close does not keep the process alive.
                running.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await running
            raise
        finally:
            self._server = None
            with contextlib.suppress(OSError):
                self.listener.close()

    def close(self) -> None:
        """Release the socket without ever having served. For a failed startup."""
        with contextlib.suppress(OSError):
            self.listener.close()


LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A name we cannot resolve to an address here. Treated as *not* loopback
        # on purpose: the warning is the safe error to make.
        return False


def _bind_message(host: str, port: int, exc: OSError) -> str:
    """Why the interface could not listen, in the terms an operator can act on."""
    reason = {
        errno.EADDRINUSE: "the address and port are already in use",
        errno.EACCES: "permission denied (ports below 1024 need privileges)",
        errno.EADDRNOTAVAIL: "no interface on this host carries that address",
    }.get(exc.errno or 0, str(exc))
    return (
        f"the web interface could not listen on {host}:{port}: {reason}. "
        "Choose another address or port with --web-host/--web-port, or run "
        "without --web"
    )


def allowed_hosts(host: str, port: int) -> frozenset[str]:
    """The `Host` header values this interface will answer to (design D9).

    Milestone 8's rebinding defence is built on this set; it is computed here,
    beside the bind, because it is a fact about what was bound rather than a
    policy a request handler gets to decide.
    """
    names: Iterable[str] = (host,) if not _is_loopback(host) else ("127.0.0.1", "::1", "localhost")
    values: set[str] = set()
    for name in names:
        bracketed = f"[{name}]" if ":" in name else name
        values.add(bracketed)
        values.add(f"{bracketed}:{port}")
    return frozenset(values)
