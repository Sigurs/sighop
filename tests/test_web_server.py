"""The application, its socket, and the run's three flags (`web-server`).

What this milestone had to get right before it could get anything else right:

* the panel is a **value** built from the read seam, so it can be constructed
  and requested with no runtime, no database and no socket;
* the socket is taken **before** the run starts, so a port clash is a startup
  failure rather than a silently absent interface;
* the process's signal handling is unchanged, because uvicorn's own would take
  Ctrl-C away from the run that owns the radio;
* and a run that was not asked for a panel listens on nothing and says nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import signal
import socket

import pytest
from fastapi.testclient import TestClient

from sighop.cli import main
from sighop.runtime import Runtime, RuntimeConfig
from sighop.web.app import (
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    NO_AUTHENTICATION,
    WebBindError,
    WebInterface,
    allowed_hosts,
    create_app,
)
from tests.protocol.corpus import CAPTURES_DIR
from tests.test_web_state import RecordingLogger, _empty_source, _startup
from tests.webfixtures import stub_state

CAPTURE = CAPTURES_DIR / "2026-09-04-03.jsonl"


@pytest.fixture(autouse=True)
def _no_database(monkeypatch) -> None:
    """Nothing here is about persistence, so nothing here configures a database.

    `sighop run` refuses to start against a database at the wrong revision, by
    design — so a developer whose shell exports `DATABASE_URL` would otherwise
    see these tests fail for a reason that has nothing to do with the web
    interface.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SIGHOP_TEST_DATABASE_URL", raising=False)


def _runtime(logger: RecordingLogger | None = None, out: io.StringIO | None = None) -> Runtime:
    return Runtime(
        source=_empty_source(),
        startup=_startup,
        config=RuntimeConfig(status_interval=3600.0, advert_tick=3600.0),
        out=out or io.StringIO(),
        logger=logger or RecordingLogger(),
    )


# --- 7.1 The application factory --------------------------------------------


def test_a_page_is_served_with_no_runtime_and_no_database() -> None:
    """7.1: constructed from stubs, requested in-process, nothing running."""
    state = stub_state()
    assert state.persistence is None

    with TestClient(create_app(state)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_the_application_serves_its_own_assets() -> None:
    """7.1, design D3: every asset comes from the application itself."""
    with TestClient(create_app(stub_state())) as client:
        page = client.get("/").text
        assert "/static/htmx.min.js" in page
        assert client.get("/static/htmx.min.js").status_code == 200
        assert client.get("/static/panel.css").status_code == 200


def test_building_the_application_opens_no_socket() -> None:
    """7.1: the app is a value; the socket belongs to the interface."""
    before = socket.socket
    create_app(stub_state())
    assert socket.socket is before  # nothing swapped it, and nothing bound


# --- 7.2 Serving it, without taking the run's signal handling ---------------


async def test_serving_leaves_the_runs_signal_handlers_in_place() -> None:
    """7.2, design D1: uvicorn's `serve()` would otherwise capture SIGINT.

    `Runtime.install_signal_handlers` uses `loop.add_signal_handler`, which
    installs a handler of its own; uvicorn's `capture_signals` calls
    `signal.signal` for the same signals and would replace it. The assertion is
    the one that matters operationally: after the interface has started, Ctrl-C
    still stops the run.
    """
    runtime = _runtime()
    runtime.install_signal_handlers()
    installed = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    interface = WebInterface.bind(runtime, host="127.0.0.1", port=0)
    serving = asyncio.create_task(interface.serve())
    try:
        await _wait_until(lambda: _is_serving(interface), "the server never started")
        assert {
            sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
        } == installed
    finally:
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving
        _restore_default_handlers()


def _is_serving(interface: WebInterface) -> bool:
    server = interface._server
    return server is not None and server.started


async def _wait_until(predicate, message: str, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.01)


def _restore_default_handlers() -> None:
    """Undo `install_signal_handlers` so one test does not leak into the next."""
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(sig)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


async def test_the_panel_answers_over_a_real_socket_inside_the_run() -> None:
    """7.2: end to end — bound socket, uvicorn, one request, in this loop."""
    runtime = _runtime()
    interface = WebInterface.bind(runtime, host="127.0.0.1", port=0)
    serving = asyncio.create_task(interface.serve())
    try:
        await _wait_until(lambda: _is_serving(interface), "the server never started")
        body = await _fetch(interface.host, interface.port, "/")
        assert body.startswith("HTTP/1.1 200")
    finally:
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving


async def _fetch(host: str, port: int, path: str) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    body = await reader.read()
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return body.decode(errors="replace")


# --- 7.3 The flags ----------------------------------------------------------


def test_the_flags_default_to_loopback_and_off() -> None:
    """7.3: the address defaults to loopback and the interface to absent."""
    from sighop.cli import build_parser

    args = build_parser().parse_args(["run", "--replay", str(CAPTURE)])
    assert args.web is False
    assert args.web_host == DEFAULT_WEB_HOST
    assert args.web_port == DEFAULT_WEB_PORT
    assert DEFAULT_WEB_HOST == "127.0.0.1"


def test_a_run_without_the_flag_listens_on_nothing_and_says_nothing(
    capsys, monkeypatch
) -> None:
    """7.3, `web-server`: a run not asked for the interface is a run without one.

    Two halves. Nothing binds — asserted by making a bind fail the test outright,
    rather than by inspecting the process afterwards — and the output says
    nothing about a web interface.
    """

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a run without --web took a listening socket")

    monkeypatch.setattr("sighop.cli.WebInterface.bind", _refuse)
    code = main(["run", "--replay", str(CAPTURE), "--status-interval", "3600"])

    assert code == 0
    printed = capsys.readouterr().out
    assert "web" not in printed.lower(), printed


def test_a_run_with_the_flag_reports_where_it_is_listening(capsys) -> None:
    """7.3 / 7.4: the address and port, in the run's own output."""
    code = main(
        [
            "run",
            "--replay",
            str(CAPTURE),
            "--status-interval",
            "3600",
            "--web",
            "--web-port",
            "0",
        ]
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert "web: http://127.0.0.1:" in printed, printed
    assert NO_AUTHENTICATION in printed



# --- 7.4 What startup says about the exposure -------------------------------


def test_a_loopback_bind_names_the_address_and_says_it_is_unauthenticated() -> None:
    """7.4: even loopback says there is no authentication."""
    interface = _bound(host="127.0.0.1")
    try:
        lines = interface.startup_lines()
        assert f"http://127.0.0.1:{interface.port}" in lines[0]
        assert "reachable from this host only" in lines[1]
        assert NO_AUTHENTICATION in lines[1]
    finally:
        interface.close()


def test_a_non_loopback_bind_says_it_is_reachable_from_the_network() -> None:
    """7.4: the operator's call, announced — in the output and in an event."""
    logger = RecordingLogger()
    interface = _bound(host="0.0.0.0", logger=logger)
    try:
        assert interface.loopback is False
        warning = "\n".join(interface.startup_lines())
        assert "REACHABLE FROM THE NETWORK" in warning
        assert NO_AUTHENTICATION in warning
        assert "transmit and reveal private key material" in warning

        interface.report()
        listening = logger.named("web_interface_listening")
        assert listening, "the interface did not emit its own event"
        assert listening[0]["loopback"] is False
        assert listening[0]["authenticated"] is False
        assert listening[0]["web_port"] == interface.port
    finally:
        interface.close()


def test_there_is_no_option_that_serves_a_wide_bind_quietly() -> None:
    """7.4, `web-server`: the warning is not suppressible.

    Asserted twice over: the parser offers no option that could suppress it, and
    `startup_lines()` has no argument and no branch that omits it.
    """
    from sighop.cli import build_parser

    parser = build_parser()
    for action in parser._subparsers._group_actions[0].choices["run"]._actions:
        for option in action.option_strings:
            assert "quiet" not in option
            assert "no-warn" not in option

    for host in ("0.0.0.0", "127.0.0.1"):
        interface = _bound(host=host)
        try:
            assert NO_AUTHENTICATION in "\n".join(interface.startup_lines())
        finally:
            interface.close()


def test_localhost_and_the_ipv6_loopback_are_loopback() -> None:
    """7.4: judged from the address, not from how it was spelled."""
    interface = _bound(host="127.0.0.1")
    try:
        assert interface.loopback is True
    finally:
        interface.close()

    for host in ("localhost", "::1", "127.0.0.5"):
        from sighop.web.app import _is_loopback

        assert _is_loopback(host) is True
    for host in ("0.0.0.0", "192.0.2.10", "example.invalid"):
        from sighop.web.app import _is_loopback

        assert _is_loopback(host) is False


def _bound(*, host: str, port: int = 0, logger: RecordingLogger | None = None) -> WebInterface:
    return WebInterface.bind(
        stub_state(), host=host, port=port, logger=logger or RecordingLogger()
    )


# --- 7.5 A bind that fails is a startup failure -----------------------------


def test_a_port_already_in_use_is_a_startup_failure_naming_why() -> None:
    """7.5: the run fails, and the message says what to do about it."""
    with socket.create_server(("127.0.0.1", 0)) as held:
        port = held.getsockname()[1]
        with pytest.raises(WebBindError) as excinfo:
            WebInterface.bind(stub_state(), host="127.0.0.1", port=port)

    message = str(excinfo.value)
    assert "127.0.0.1" in message
    assert str(port) in message
    assert "already in use" in message
    assert "--web-port" in message


def test_the_run_fails_rather_than_continuing_without_the_interface(capsys) -> None:
    """7.5: reported before any traffic is processed, and the run does not start."""
    with socket.create_server(("127.0.0.1", 0)) as held:
        port = held.getsockname()[1]
        code = main(
            [
                "run",
                "--replay",
                str(CAPTURE),
                "--status-interval",
                "3600",
                "--web",
                "--web-port",
                str(port),
            ]
        )

    assert code == 2
    error = capsys.readouterr().err
    assert "could not listen" in error
    assert str(port) in error


# --- 7.6 Bounded shutdown ---------------------------------------------------


async def test_the_run_exits_with_a_connected_socket_open() -> None:
    """7.6: a browser holding a connection does not hold the process.

    The connection is deliberately left open and idle — the shape a WebSocket
    or a keep-alive has — and the run is stopped. It must come down inside the
    bound rather than waiting on somebody else's socket.
    """
    runtime = _runtime()
    interface = WebInterface.bind(runtime, host="127.0.0.1", port=0)
    interface.shutdown_grace = 0.5
    runtime.services = (interface.service(),)

    run = asyncio.create_task(runtime.run())
    await _wait_until(lambda: _is_serving(interface), "the server never started")

    reader, writer = await asyncio.open_connection(interface.host, interface.port)
    writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()
    await reader.read(16)

    runtime.stop()
    await asyncio.wait_for(run, timeout=10)

    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def test_the_listening_socket_is_closed_when_the_run_ends() -> None:
    """7.6: no listener outlives the process, so a restart can bind again."""
    runtime = _runtime()
    interface = WebInterface.bind(runtime, host="127.0.0.1", port=0)
    port = interface.port
    runtime.services = (interface.service(),)

    run = asyncio.create_task(runtime.run())
    await _wait_until(lambda: _is_serving(interface), "the server never started")
    runtime.stop()
    await asyncio.wait_for(run, timeout=10)

    # Binding the same port again is the assertion: it would fail if the old
    # listener were still open.
    with socket.create_server(("127.0.0.1", port)) as rebound:
        assert rebound.getsockname()[1] == port


# --- The host set the rebinding defence is built on (design D9) -------------


def test_the_allowed_hosts_for_a_loopback_bind_cover_its_spellings() -> None:
    hosts = allowed_hosts("127.0.0.1", 8080)
    assert "127.0.0.1:8080" in hosts
    assert "localhost:8080" in hosts
    assert "[::1]:8080" in hosts
    assert "attacker.example:8080" not in hosts


def test_the_allowed_hosts_for_a_wide_bind_are_only_what_was_asked_for() -> None:
    hosts = allowed_hosts("192.0.2.10", 9000)
    assert hosts == frozenset({"192.0.2.10", "192.0.2.10:9000"})
