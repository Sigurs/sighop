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
import re
import signal
import socket

import pytest

from sighop.cli import main
from sighop.config import DATABASE_SCHEMA_VARIABLE, DatabaseConfig
from sighop.db.engine import Database, Succeeded
from sighop.db.repositories import WebUserRepository
from sighop.runtime import Runtime, RuntimeConfig
from sighop.web.app import (
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    NO_DATABASE_FOR_WEB,
    NO_ENABLED_ACCOUNT,
    PLAIN_HTTP_WARNING,
    WebBindError,
    WebInterface,
    WebStartupError,
    allowed_hosts,
    create_app,
    validate_allowed_hosts,
)
from sighop.web.auth import FirstRunSetup
from tests.protocol.corpus import CAPTURES_DIR
from tests.test_web_state import RecordingLogger, _empty_source, _startup
from tests.webfixtures import MemoryAccounts, authenticator, signed_client, stub_state

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
    """7.1: constructed from stubs and an in-memory account store, requested
    in-process by a signed-in client, nothing running."""
    state = stub_state()
    assert state.persistence is None

    with signed_client(create_app(state, auth=authenticator())) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_the_application_serves_its_own_assets() -> None:
    """7.1, design D3: every asset comes from the application itself."""
    with signed_client(create_app(stub_state(), auth=authenticator())) as client:
        page = client.get("/").text
        assert "/static/htmx.min.js" in page
        assert client.get("/static/htmx.min.js").status_code == 200
        assert client.get("/static/panel.css").status_code == 200


def test_building_the_application_opens_no_socket() -> None:
    """7.1: the app is a value; the socket belongs to the interface."""
    before = socket.socket
    create_app(stub_state(), auth=authenticator())
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

    interface = _bind(runtime, host="127.0.0.1", port=0)
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
    interface = _bind(runtime, host="127.0.0.1", port=0)
    serving = asyncio.create_task(interface.serve())
    try:
        await _wait_until(lambda: _is_serving(interface), "the server never started")
        body = await _fetch(interface.host, interface.port, "/login")
        assert body.startswith("HTTP/1.1 200")
        # And everything else wants a session, over the real socket too.
        page = await _fetch(interface.host, interface.port, "/")
        assert page.startswith("HTTP/1.1 303")
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
    # The startup line about webhooks (outbound HTTP, not the interface) is the
    # one legitimate occurrence of the letters.
    assert "web" not in printed.lower().replace("webhook", ""), printed


def test_a_run_with_the_flag_and_no_database_refuses_before_binding(
    capsys, monkeypatch
) -> None:
    """9.1: the accounts live in the database, so there is no panel without one.

    Refused before any socket exists and before the replay is consumed: the bind
    fails the test outright if it is reached, and the output carries no frame.
    """

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a run with no database took a listening socket")

    monkeypatch.setattr("sighop.cli.WebInterface.bind", _refuse)
    code = main(
        ["run", "--replay", str(CAPTURE), "--status-interval", "3600", "--web", "--web-port", "0"]
    )

    assert code == 2
    captured = capsys.readouterr()
    assert NO_DATABASE_FOR_WEB in captured.err
    assert "stored in the database" in captured.err
    assert "DATABASE_URL" in captured.err
    assert captured.out == "", "something was received before the refusal"


def _database_run_argv(url: str, *extra: str) -> list[str]:
    return [
        "run",
        "--replay",
        str(CAPTURE),
        "--status-interval",
        "3600",
        "--database-url",
        url,
        "--web",
        *extra,
    ]


@pytest.fixture
def run_database(database_config: DatabaseConfig, monkeypatch) -> str:
    assert database_config.schema is not None
    monkeypatch.setenv(DATABASE_SCHEMA_VARIABLE, database_config.schema)
    return database_config.url


@pytest.mark.database
def test_a_run_whose_accounts_are_all_disabled_refuses_before_binding(
    database: Database, database_config: DatabaseConfig, run_database: str, capsys, monkeypatch
) -> None:
    """9.1, web-first-run-setup D1 row two: nobody could sign in, and setup must
    not undo a deliberate lockout — so nothing is served and nothing is bound."""
    _add_account(database_config, enabled=False)

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a run with only disabled accounts took a listening socket")

    monkeypatch.setattr("sighop.cli.WebInterface.bind", _refuse)
    code = main(_database_run_argv(run_database, "--web-port", "0"))

    assert code == 2
    captured = capsys.readouterr()
    assert NO_ENABLED_ACCOUNT in captured.err
    assert "sighop web user enable <username>" in captured.err
    assert "sighop web user add <username>" in captured.err
    assert "SETUP" not in captured.out
    assert "frames=" not in captured.out


@pytest.mark.database
def test_a_run_with_no_account_at_all_serves_first_run_setup(
    database: Database, run_database: str, capsys
) -> None:
    """web-first-run-setup D1 row one: served, with the code in the output only."""
    code = main(_database_run_argv(run_database, "--web-port", "0"))

    assert code == 0
    printed = capsys.readouterr().out
    assert "FIRST-RUN SETUP PENDING: no account exists" in printed, printed
    found = re.search(
        r"open (http://127\.0\.0\.1:\d+)/setup and enter setup code "
        r"([0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5})",
        printed,
    )
    assert found is not None, printed
    assert "reachable from this host only" in printed
    assert "sign-in required" not in printed


def _add_account(config: DatabaseConfig, *, enabled: bool = True) -> None:
    """One enabled account, written on a loop of its own.

    The run under test installs signal handlers, which only the main thread may
    do, so these tests are synchronous and cannot share the fixture's loop.
    """

    async def add() -> None:
        handle = Database(config=config)
        await handle.open()
        try:
            added = await WebUserRepository(database=handle).add(
                "dev-operator", password_hash="$argon2id$test$unused", enabled=enabled
            )
            assert isinstance(added, Succeeded)
        finally:
            await handle.dispose()

    asyncio.run(add())


@pytest.mark.database
def test_a_run_with_the_flag_reports_where_it_is_listening(
    database: Database, database_config: DatabaseConfig, run_database: str, capsys
) -> None:
    """7.3 / 7.4 / 9.3: the address, the port and the account count.

    web-first-run-setup D1 row three: accounts enabled, so no setup at all.
    """
    _add_account(database_config)
    code = main(_database_run_argv(run_database, "--web-port", "0"))

    assert code == 0
    printed = capsys.readouterr().out
    assert "web: http://127.0.0.1:" in printed, printed
    assert "sign-in required; 1 enabled account(s)" in printed
    assert "SETUP" not in printed and "setup code" not in printed


@pytest.mark.database
def test_the_run_fails_rather_than_continuing_without_the_interface(
    database: Database, database_config: DatabaseConfig, run_database: str, capsys
) -> None:
    """7.5: reported before any traffic is processed, and the run does not start."""
    _add_account(database_config)
    with socket.create_server(("127.0.0.1", 0)) as held:
        port = held.getsockname()[1]
        code = main(_database_run_argv(run_database, "--web-port", str(port)))

    assert code == 2
    error = capsys.readouterr().err
    assert "could not listen" in error
    assert str(port) in error


# --- 7.4 / 9.3 What startup says about the exposure --------------------------


def test_a_loopback_bind_says_sign_in_is_required_and_counts_accounts() -> None:
    """9.3: the loopback line states authentication and the account count."""
    interface = _bound(host="127.0.0.1", accounts_enabled=2)
    try:
        lines = interface.startup_lines()
        assert f"http://127.0.0.1:{interface.port}" in lines[0]
        assert "sign-in required; 2 enabled account(s)" in lines[0]
        assert "reachable from this host only" in lines[1]
        assert PLAIN_HTTP_WARNING not in "\n".join(lines)
    finally:
        interface.close()


def test_a_non_loopback_bind_says_plain_http_and_unencrypted_credentials() -> None:
    """9.3: the operator's call, announced — in the output and in an event."""
    logger = RecordingLogger()
    interface = _bound(host="0.0.0.0", logger=logger)
    try:
        assert interface.loopback is False
        warning = "\n".join(interface.startup_lines())
        assert "REACHABLE FROM THE NETWORK" in warning
        assert "plain HTTP" in warning
        assert "passwords and session cookies cross the network unencrypted" in warning
        assert "tunnel" in warning
        assert "sign-in required" in warning

        interface.report()
        listening = logger.named("web_interface_listening")
        assert listening, "the interface did not emit its own event"
        assert listening[0]["loopback"] is False
        assert listening[0]["authenticated"] is True
        assert listening[0]["encrypted"] is False
        assert listening[0]["accounts_enabled"] == 1
        assert listening[0]["setup_pending"] is False
        assert listening[0]["web_port"] == interface.port
        assert "unencrypted" in str(listening[0]["detail"])
    finally:
        interface.close()


def test_there_is_no_option_that_serves_a_wide_bind_quietly() -> None:
    """9.3, `web-server`: the warning is not suppressible.

    Asserted twice over: the parser offers no option that could suppress it, and
    `startup_lines()` has no argument and no branch that omits it.
    """
    import inspect

    from sighop.cli import build_parser

    parser = build_parser()
    for action in parser._subparsers._group_actions[0].choices["run"]._actions:
        for option in action.option_strings:
            assert "quiet" not in option
            assert "no-warn" not in option
            assert "insecure" not in option
            assert "no-auth" not in option

    assert list(inspect.signature(WebInterface.startup_lines).parameters) == ["self"]
    interface = _bound(host="0.0.0.0")
    try:
        assert PLAIN_HTTP_WARNING in "\n".join(interface.startup_lines())
    finally:
        interface.close()


def test_the_no_authentication_wording_is_gone() -> None:
    """9.3: milestone 8's statement would now be false, so it no longer exists."""
    import sighop.web.app as app_module

    assert not hasattr(app_module, "NO_AUTHENTICATION")


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


def _bind(state: object, **kwargs: object) -> WebInterface:
    auth = kwargs.pop("auth", None) or authenticator()
    return WebInterface.bind(
        state,  # type: ignore[arg-type]
        auth=auth,  # type: ignore[arg-type]
        accounts_enabled=kwargs.pop("accounts_enabled", 1),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _bound(
    *,
    host: str,
    port: int = 0,
    logger: RecordingLogger | None = None,
    accounts_enabled: int = 1,
) -> WebInterface:
    return _bind(
        stub_state(),
        host=host,
        port=port,
        logger=logger or RecordingLogger(),
        accounts_enabled=accounts_enabled,
    )


# --- 7.5 A bind that fails is a startup failure -----------------------------


def test_a_port_already_in_use_is_a_startup_failure_naming_why() -> None:
    """7.5: the run fails, and the message says what to do about it."""
    with socket.create_server(("127.0.0.1", 0)) as held:
        port = held.getsockname()[1]
        with pytest.raises(WebBindError) as excinfo:
            _bind(stub_state(), host="127.0.0.1", port=port)

    message = str(excinfo.value)
    assert "127.0.0.1" in message
    assert str(port) in message
    assert "already in use" in message
    assert "--web-port" in message


# --- 7.6 Bounded shutdown ---------------------------------------------------


async def test_the_run_exits_with_a_connected_socket_open() -> None:
    """7.6: a browser holding a connection does not hold the process.

    The connection is deliberately left open and idle — the shape a WebSocket
    or a keep-alive has — and the run is stopped. It must come down inside the
    bound rather than waiting on somebody else's socket.
    """
    runtime = _runtime()
    interface = _bind(runtime, host="127.0.0.1", port=0)
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
    interface = _bind(runtime, host="127.0.0.1", port=0)
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


# --- 9.2 Allowed host names beyond the bind ----------------------------------


def test_an_all_addresses_bind_answers_to_a_named_host_and_refuses_others() -> None:
    """9.2: the container case — bound to 0.0.0.0, reached as localhost:8080."""
    hosts = allowed_hosts("0.0.0.0", 8080, extra=validate_allowed_hosts(["localhost:8080"]))
    app = create_app(stub_state(), auth=authenticator(), hosts=hosts, logger=RecordingLogger())

    with signed_client(app, base_url="http://localhost:8080") as client:
        served = client.get("/", headers={"Host": "localhost:8080"})
        unnamed = client.get("/", headers={"Host": "example.test:8080"})
        bare_other = client.get("/", headers={"Host": "127.0.0.1:8080"})

    assert served.status_code == 200
    assert unnamed.status_code == 421
    assert bare_other.status_code == 421, "an extra host extends; it grants no other name"


def test_a_bare_allowed_host_is_added_with_and_without_the_bound_port() -> None:
    hosts = allowed_hosts("0.0.0.0", 8080, extra=("panel.lan",))
    assert {"panel.lan", "panel.lan:8080"} <= hosts
    assert {"0.0.0.0", "0.0.0.0:8080"} <= hosts, "the bind-derived set is kept"


@pytest.mark.parametrize(
    "value", ["*", "", "  ", "*.example", "http://localhost:8080", "localhost/path", "a b", "::1"]
)
def test_an_unusable_allowed_host_is_refused_naming_it(value: str) -> None:
    with pytest.raises(WebStartupError) as excinfo:
        validate_allowed_hosts([value])
    assert repr(value) in str(excinfo.value)
    assert "No port was listened on" in str(excinfo.value)


@pytest.mark.parametrize("value", ["localhost", "localhost:8080", "127.0.0.1:8080", "[::1]:8080"])
def test_ordinary_allowed_hosts_are_accepted(value: str) -> None:
    assert validate_allowed_hosts([value]) == (value,)


def test_a_wildcard_allowed_host_fails_startup_with_no_port_bound(capsys, monkeypatch) -> None:
    """9.2: refused at startup, before the database is consulted or a socket exists."""

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a wildcard allowed host took a listening socket")

    monkeypatch.setattr("sighop.cli.WebInterface.bind", _refuse)
    code = main(
        [
            "run",
            "--replay",
            str(CAPTURE),
            "--status-interval",
            "3600",
            "--web",
            "--web-allowed-host",
            "*",
        ]
    )

    assert code == 2
    assert "--web-allowed-host '*' is refused" in capsys.readouterr().err


def test_bind_refuses_a_wildcard_before_taking_the_socket() -> None:
    with pytest.raises(WebStartupError):
        _bind(stub_state(), host="127.0.0.1", port=0, allowed=["*"])


def test_bind_refuses_zero_enabled_accounts_before_taking_the_socket() -> None:
    with pytest.raises(WebStartupError) as excinfo:
        _bind(stub_state(), host="127.0.0.1", port=0, accounts_enabled=0)
    assert "sighop web user add" in str(excinfo.value)
    assert "sighop web user enable" in str(excinfo.value)


# --- web-first-run-setup 4.2 / 4.4 Setup at startup -------------------------


def _setup_bound(host: str, logger: RecordingLogger | None = None) -> WebInterface:
    return _bind(
        stub_state(),
        host=host,
        port=0,
        logger=logger or RecordingLogger(),
        accounts_enabled=0,
        auth=authenticator(MemoryAccounts(), setup=FirstRunSetup()),
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_a_setup_bind_prints_the_code_before_the_exposure_line(host: str) -> None:
    logger = RecordingLogger()
    interface = _setup_bound(host, logger)
    try:
        setup = interface.auth.setup
        assert setup is not None
        lines = interface.startup_lines()
        assert lines[0] == f"web: {interface.url} — FIRST-RUN SETUP PENDING: no account exists"
        assert lines[1] == (
            f"     open {interface.url}/setup and enter setup code {setup.display}"
        )
        if host == "127.0.0.1":
            assert lines[2] == "     reachable from this host only"
        else:
            assert "REACHABLE FROM THE NETWORK" in lines[2]
            assert PLAIN_HTTP_WARNING in lines[2]

        interface.report()
        [listening] = logger.named("web_interface_listening")
        assert listening["setup_pending"] is True
        assert listening["accounts_enabled"] == 0
        for secret in (setup.code, setup.display):
            assert all(secret not in str(value) for value in listening.values())
            assert all(secret not in str(f) for _, f in logger.events)
    finally:
        interface.close()


def test_bind_refuses_zero_accounts_when_setup_is_closed() -> None:
    setup = FirstRunSetup()
    setup.close()
    with pytest.raises(WebStartupError):
        _bind(
            stub_state(),
            host="127.0.0.1",
            port=0,
            accounts_enabled=0,
            auth=authenticator(MemoryAccounts(), setup=setup),
        )


async def test_a_restart_prints_a_new_code_and_refuses_the_old_one() -> None:
    accounts = MemoryAccounts()
    first = _setup_bound("127.0.0.1")
    first.close()
    second = _bind(
        stub_state(),
        host="127.0.0.1",
        port=0,
        logger=RecordingLogger(),
        accounts_enabled=0,
        auth=authenticator(accounts, setup=FirstRunSetup()),
    )
    try:
        old, new = first.auth.setup, second.auth.setup
        assert old is not None and new is not None
        assert old.code != new.code
        assert old.display not in "\n".join(second.startup_lines())
        refused = await second.auth.complete_setup(
            old.display, "dev-first", "a-password", "a-password", client="192.0.2.10"
        )
        assert refused.reason == "bad_code"
        assert not accounts.accounts
    finally:
        second.close()
