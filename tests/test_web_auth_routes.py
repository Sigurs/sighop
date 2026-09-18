"""Authentication as the application serves it (tasks 6.x, 7.1-7.3, 8.x, 9.4, 9.5).

`tests/test_web_auth.py` tests the decisions; this tests the doors. Every test
drives the real application through the real guard, with an in-memory account
store and a hasher that counts — the one real-Argon2id path is
`tests/test_web_login.py`'s.
"""

from __future__ import annotations

import http.cookies
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from sighop.web import guard as guard_module
from sighop.web.app import STATIC_DIR, allowed_hosts, create_app
from sighop.web.auth import FREE_FAILURES, LOGIN_FAILED, SESSION_COOKIE, UNVERIFIED
from sighop.web.guard import PUBLIC_ROUTES, TOKEN_FIELD
from sighop.web.guarded import (
    ENABLE_TRANSMIT,
    EXPORT_KEY,
    RAISE_CEILING,
    REVEAL_KEY,
)
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    OPERATOR,
    OPERATOR_PASSWORD,
    CountingHasher,
    MemoryAccounts,
    StubState,
    authenticator,
    csrf,
    registered_routes,
    session_of,
    signed_client,
    stub_state,
)

HOSTS = allowed_hosts("127.0.0.1", 8080)
BASE = "http://127.0.0.1:8080"


def _app(
    state: StubState | None = None,
    *,
    accounts: MemoryAccounts | None = None,
    logger: RecordingLogger | None = None,
    said: list[str] | None = None,
) -> FastAPI:
    return create_app(
        state or stub_state(stub_names=("dev-panel-identity",)),
        auth=authenticator(accounts, logger=logger),
        hosts=HOSTS,
        logger=logger or RecordingLogger(),
        announce=None if said is None else said.append,
    )


def _anonymous(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=BASE, follow_redirects=False)


def _signed(app: FastAPI) -> TestClient:
    return signed_client(app, base_url=BASE, follow_redirects=False)


def _login_form(client: TestClient, next_path: str = "/") -> dict[str, str]:
    page = client.get(f"/login?next={next_path}").text
    token = re.search(r'name="_token" value="([^"]+)"', page)
    assert token is not None
    return {TOKEN_FIELD: token.group(1)}


def _nonce(page: str) -> str:
    found = re.search(r'name="nonce" value="([^"]+)"', page)
    assert found is not None, "the confirmation page carries no nonce"
    return found.group(1)


# --- 6.1 The guard's order, branch by branch --------------------------------


def test_an_unauthenticated_page_request_is_sent_to_sign_in_with_nothing_in_it() -> None:
    app = _app()
    with _anonymous(app) as client:
        response = client.get("/admin/identities?filter=x")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fadmin%2Fidentities%3Ffilter%3Dx"
    assert response.content == b""


def test_an_unauthenticated_state_change_is_refused_and_recorded() -> None:
    logger = RecordingLogger()
    state = stub_state()
    app = _app(state, logger=logger)
    with _anonymous(app) as client:
        response = client.post("/admin/transmit", data={"enabled": "true"})
    assert response.status_code == 401
    assert state.scheduler.transmit_enabled is False
    event = logger.named("web_request")[-1]
    assert event["outcome"] == "unauthenticated"
    assert event["status"] == 401
    assert event["actor"] == "unauthenticated"
    assert logger.named("web_guarded_action") == [], "no handler ran"


def test_the_sign_in_form_and_static_assets_are_public() -> None:
    app = _app()
    with _anonymous(app) as client:
        assert client.get("/login").status_code == 200
        assert client.get("/static/panel.css").status_code == 200


def test_the_sign_in_page_carries_no_platform_state() -> None:
    state = stub_state(stub_names=("dev-panel-identity",))
    app = _app(state)
    with _anonymous(app) as client:
        body = client.get("/login").text
    assert 'aria-label="duty cycle"' not in body
    assert "dev-panel-identity" not in body
    assert state.adverts.stubs[0].identity.public_key.hex() not in body
    assert "TX=" not in body and "transmit enabled" not in body


@pytest.mark.parametrize(
    "next_value",
    ["https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)"],
)
def test_an_off_site_next_value_is_ignored(next_value: str) -> None:
    app = _app()
    with _anonymous(app) as client:
        form = _login_form(client, next_value)
        page = client.get("/login", params={"next": next_value}).text
        assert 'name="next" value="/"' in page
        response = client.post(
            "/login",
            data={**form, "username": OPERATOR, "password": OPERATOR_PASSWORD, "next": next_value},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_a_same_origin_next_is_where_sign_in_goes() -> None:
    app = _app()
    with _anonymous(app) as client:
        form = _login_form(client)
        response = client.post(
            "/login",
            data={
                **form,
                "username": OPERATOR,
                "password": OPERATOR_PASSWORD,
                "next": "/admin/radio",
            },
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/radio"


# --- 6.2 Provenance on the sign-in form -------------------------------------


def test_a_sign_in_without_the_process_token_is_refused_before_any_verification() -> None:
    app = _app()
    hasher = app.state.auth.hasher
    assert isinstance(hasher, CountingHasher)
    with _anonymous(app) as client:
        response = client.post("/login", data={"username": OPERATOR, "password": OPERATOR_PASSWORD})
        with_other = client.post(
            "/login",
            data={TOKEN_FIELD: "not-the-token", "username": OPERATOR, "password": "x"},
        )
    assert response.status_code == 403
    assert with_other.status_code == 403
    assert hasher.verifications == 0
    assert SESSION_COOKIE not in response.cookies


# --- 6.3 The actor on every request event -----------------------------------


def test_every_request_event_names_the_actor_or_says_unauthenticated() -> None:
    logger = RecordingLogger()
    app = _app(logger=logger)
    with _signed(app) as signed:
        signed.get("/")
    with _anonymous(app) as anonymous:
        anonymous.get("/login")
        anonymous.get("/")
    actors = [(event["path"], event["actor"]) for event in logger.named("web_request")]
    assert actors == [
        ("/", OPERATOR),
        ("/login", "unauthenticated"),
        ("/", "unauthenticated"),
    ]


def test_a_sign_in_request_event_names_who_signed_in() -> None:
    logger = RecordingLogger()
    app = _app(logger=logger)
    with _anonymous(app) as client:
        form = _login_form(client)
        client.post("/login", data={**form, "username": OPERATOR, "password": OPERATOR_PASSWORD})
    assert logger.named("web_request")[-1]["actor"] == OPERATOR


# --- 6.4 The feed's socket ---------------------------------------------------


def _feed(client: TestClient, **headers: str):
    return client.websocket_connect(
        "/feed", headers={"Host": "127.0.0.1:8080", "Origin": BASE, **headers}
    )


def test_a_feed_connection_without_a_session_is_closed_before_any_record() -> None:
    logger = RecordingLogger()
    app = _app(logger=logger)
    with (
        _anonymous(app) as client,
        pytest.raises(WebSocketDisconnect) as excinfo,
        _feed(client) as socket,
    ):
        socket.receive_json()
    assert excinfo.value.code == 1008
    closed = logger.named("web_feed_closed")
    assert closed[-1]["reason"] == "unauthenticated"
    assert closed[-1]["actor"] == "unauthenticated"
    assert closed[-1]["delivered"] == 0


def test_a_feed_connection_from_a_foreign_origin_is_closed() -> None:
    logger = RecordingLogger()
    app = _app(logger=logger)
    with (
        _signed(app) as client,
        pytest.raises(WebSocketDisconnect) as excinfo,
        _feed(client, Origin="http://evil.example") as socket,
    ):
        socket.receive_json()
    assert excinfo.value.code == 1008
    assert logger.named("web_feed_closed")[-1]["reason"] == "origin_rejected"


def test_a_feed_connection_with_no_origin_is_closed() -> None:
    app = _app()
    with (
        _signed(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/feed", headers={"Host": "127.0.0.1:8080"}) as socket,
    ):
        socket.receive_json()


def test_a_signed_in_feed_receives_records_and_its_closing_event_names_the_actor() -> None:
    logger = RecordingLogger()
    app = _app(logger=logger)
    with _signed(app) as client, _feed(client) as socket:
        assert socket.receive_json()["kind"] == "history"
        assert socket.receive_json()["kind"] == "boundary"
        assert socket.receive_json()["kind"] == "status"
    closed = logger.named("web_feed_closed")
    assert closed[-1]["actor"] == OPERATOR


# --- 6.5 Every route outside the public set refuses no session --------------


def _unprotected(app: FastAPI) -> list[str]:
    """Every route that answered a request with no session with something other
    than a refusal. Parameters are filled with a placeholder: a refusal happens
    before routing, so a real identifier would prove nothing more."""
    found: list[str] = []
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        for route in registered_routes(app):
            if isinstance(route, Mount):
                continue
            path = re.sub(r"\{[^}]+\}", "placeholder", route.path)  # type: ignore[attr-defined]
            if isinstance(route, WebSocketRoute):
                try:
                    with client.websocket_connect(
                        path, headers={"Host": "127.0.0.1:8080", "Origin": BASE}
                    ) as socket:
                        socket.receive_json()
                    found.append(f"WS {route.path}")
                except WebSocketDisconnect:
                    pass
                continue
            assert isinstance(route, APIRoute), route
            for method in sorted(route.methods or ()):
                if (method, route.path) in PUBLIC_ROUTES:
                    continue
                response = client.request(method, path)
                refused = (
                    method in ("GET", "HEAD")
                    and response.status_code == 303
                    and response.headers["location"].startswith("/login")
                ) or (method not in ("GET", "HEAD") and response.status_code == 401)
                if not refused:
                    found.append(f"{method} {route.path}")
    return found


def test_every_route_outside_the_public_set_refuses_a_request_with_no_session() -> None:
    app = _app()
    routes = registered_routes(app)
    assert len(routes) > 30, f"only {len(routes)} routes were walked; the walk is broken"
    assert _unprotected(app) == []


def test_the_sweep_catches_a_route_made_public_by_mistake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6.5: the sweep is not vacuous. A route the guard treats as public that is
    not the sign-in or setup form is exactly the mistake it exists to find."""
    monkeypatch.setattr(guard_module, "PUBLIC_ROUTES", PUBLIC_ROUTES | {("GET", "/contacts")})
    app = _app()
    # The sweep's own skip list stays the real one, so the widened set shows up.
    assert "GET /contacts" in _unprotected(app)


def test_the_public_set_is_exactly_the_sign_in_and_setup_forms() -> None:
    assert {
        ("GET", "/login"),
        ("POST", "/login"),
        ("GET", "/setup"),
        ("POST", "/setup"),
    } == PUBLIC_ROUTES


# --- 6.6 Static assets -------------------------------------------------------


def test_every_static_file_is_public_and_carries_no_state() -> None:
    state = stub_state(stub_names=("dev-panel-identity",))
    app = _app(state)
    private_key = state.adverts.stubs[0].identity.private_key.hex()
    files = sorted(path for path in STATIC_DIR.rglob("*") if path.is_file())
    assert files, "no static files to sweep"
    with _anonymous(app) as client:
        for path in files:
            relative = path.relative_to(STATIC_DIR).as_posix()
            response = client.get(f"/static/{relative}")
            assert response.status_code == 200, relative
            body = response.text
            for marker in (
                "{{",
                "{%",
                app.state.auth.login_token,
                private_key,
                SESSION_COOKIE,
            ):
                assert marker not in body, f"{relative} carries {marker!r}"


# --- 7.1 The cookie ----------------------------------------------------------


def _set_cookie(response: object) -> http.cookies.Morsel[str]:
    header = response.headers["set-cookie"]  # type: ignore[attr-defined]
    jar: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
    jar.load(header)
    return jar[SESSION_COOKIE]


def test_the_cookie_is_httponly_samesite_strict_path_root_and_not_secure() -> None:
    app = _app()
    with _anonymous(app) as client:
        form = _login_form(client)
        response = client.post(
            "/login", data={**form, "username": OPERATOR, "password": OPERATOR_PASSWORD}
        )
    header = response.headers["set-cookie"]
    attributes = {part.strip().split("=")[0].lower() for part in header.split(";")[1:]}
    assert attributes == {"httponly", "path", "samesite"}, header
    morsel = _set_cookie(response)
    assert morsel["path"] == "/"
    assert morsel["samesite"].lower() == "strict"
    assert morsel["httponly"]
    assert not morsel["secure"]
    assert not morsel["max-age"] and not morsel["expires"]


def test_a_cookie_held_before_sign_in_grants_nothing_afterwards() -> None:
    app = _app()
    with _signed(app) as client:
        old = client.cookies.get(SESSION_COOKIE)
        form = _login_form(client)
        response = client.post(
            "/login", data={**form, "username": OPERATOR, "password": OPERATOR_PASSWORD}
        )
        new = _set_cookie(response).value
    assert new != old
    with _anonymous(app) as stale:
        stale.cookies.set(SESSION_COOKIE, old or "")
        assert stale.get("/").status_code == 303


# --- 7.2 One response for every failure -------------------------------------


def test_every_failed_sign_in_gets_the_same_bytes() -> None:
    accounts = MemoryAccounts()
    accounts.add(OPERATOR, OPERATOR_PASSWORD)
    accounts.add("dev-disabled", OPERATOR_PASSWORD, enabled=False)
    app = _app(accounts=accounts)
    with _anonymous(app) as client:
        form = _login_form(client)
        responses = [
            client.post("/login", data={**form, "username": name, "password": password})
            for name, password in (
                ("dev-nobody", OPERATOR_PASSWORD),
                ("dev-disabled", OPERATOR_PASSWORD),
                (OPERATOR, "a-wrong-password"),
            )
        ]
    assert {response.status_code for response in responses} == {200}
    assert responses[0].content == responses[1].content == responses[2].content
    assert LOGIN_FAILED in responses[0].text
    assert all(SESSION_COOKIE not in response.cookies for response in responses)


# --- 7.3 Signing out ---------------------------------------------------------


def test_signing_out_ends_the_session_and_clears_the_cookie() -> None:
    logger = RecordingLogger()
    app = _app(logger=logger)
    with _signed(app) as client:
        token = client.cookies.get(SESSION_COOKIE)
        response = client.post("/logout")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert f"{SESSION_COOKIE}=" in response.headers["set-cookie"]
    with _anonymous(app) as after:
        after.cookies.set(SESSION_COOKIE, token or "")
        assert after.get("/").status_code == 303
    assert logger.named("web_session_ended")[-1]["reason"] == "logout"


def test_sign_out_needs_the_sessions_token() -> None:
    app = _app()
    with signed_client(app, send_token=False, base_url=BASE, follow_redirects=False) as client:
        assert client.post("/logout").status_code == 403
        assert client.get("/").status_code == 200, "the session was not ended"


def test_there_is_no_get_logout() -> None:
    app = _app()
    with _signed(app) as client:
        assert client.get("/logout").status_code in (404, 405)
        assert client.get("/").status_code == 200, "navigation ended a session"
    assert not any(
        isinstance(route, APIRoute)
        and route.path == "/logout"
        and "GET" in (route.methods or set())
        for route in registered_routes(app)
    )


def test_the_header_names_the_signed_in_user_and_offers_sign_out() -> None:
    app = _app()
    with _signed(app) as client:
        page = client.get("/").text
        token = csrf(client)
    assert f'signed in as <span class="mono">{OPERATOR}</span>' in page
    assert 'action="/logout"' in page
    assert f'name="_token" value="{token}"' in page


# --- 8.1 / 8.2 Guarded actions: right, wrong and missing passwords ----------

GUARDED = {
    REVEAL_KEY: ("reveal", {}),
    ENABLE_TRANSMIT: ("/admin/transmit", {"enabled": "true"}),
    RAISE_CEILING: ("/admin/ceiling", {"fraction": "0.2"}),
}


def _guarded_paths(state: StubState, action: str) -> tuple[str, dict[str, str]]:
    path, fields = GUARDED[action]
    if action == REVEAL_KEY:
        path = f"/admin/reveal/{state.adverts.stubs[0].entity_id}"
    return path, fields


def _effect(state: StubState, action: str, response: object) -> bool:
    if action == REVEAL_KEY:
        return state.adverts.stubs[0].identity.private_key.hex() in response.text  # type: ignore[attr-defined]
    if action == ENABLE_TRANSMIT:
        return state.scheduler.transmit_enabled
    return state.scheduler.budget.ceiling_fraction == 0.2


@pytest.mark.parametrize("action", sorted(GUARDED))
@pytest.mark.parametrize("password", [OPERATOR_PASSWORD, "a-wrong-password", None])
def test_a_guarded_action_needs_the_acting_users_password(
    action: str, password: str | None
) -> None:
    logger = RecordingLogger()
    state = stub_state(stub_names=("dev-panel-identity",))
    said: list[str] = []
    app = _app(state, logger=logger, said=said)
    path, fields = _guarded_paths(state, action)
    with _signed(app) as client:
        page = client.get(path).text
        assert 'type="password" name="password"' in page
        data = {**fields, "nonce": _nonce(page)}
        if password is not None:
            data["password"] = password
        response = client.post(path, data=data)

    events = logger.named("web_guarded_action")
    assert len(events) == 1
    assert events[0]["action"] == action
    assert events[0]["actor"] == OPERATOR
    if password == OPERATOR_PASSWORD:
        assert events[0]["outcome"] == "success"
        assert _effect(state, action, response)
    else:
        assert response.status_code == 403
        assert events[0]["outcome"] == "refused"
        assert events[0]["reason"] == "bad_password"
        assert not _effect(state, action, response)
        assert said == []
    for event in logger.events:
        assert "a-wrong-password" not in repr(event)
        assert OPERATOR_PASSWORD not in repr(event)


def test_a_missing_password_is_refused_exactly_as_a_wrong_one() -> None:
    bodies = []
    for password in ("a-wrong-password", None):
        state = stub_state()
        app = _app(state)
        with _signed(app) as client:
            page = client.get("/admin/transmit").text
            data = {"enabled": "true", "nonce": _nonce(page)}
            if password is not None:
                data["password"] = password
            response = client.post("/admin/transmit", data=data)
            bodies.append((response.status_code, response.text.replace(csrf(client), "")))
    assert bodies[0] == bodies[1]


@pytest.mark.database
async def test_an_export_needs_the_acting_users_password(database: object) -> None:
    """8.2 for the fourth action; its success path is in `test_web_write_parity.py`."""
    import base64

    import httpx2

    from sighop.config import generate_secret_key
    from sighop.db.persistence import Persistence
    from sighop.protocol.identity import generate_identity

    secret = base64.b64decode(generate_secret_key())
    persistence = Persistence(database=database)  # type: ignore[arg-type]
    stored = await persistence.entities.store(
        name="dev-export", identity=generate_identity(), secret=secret
    )
    record = stored.value
    logger = RecordingLogger()
    app = create_app(
        stub_state(persistence=persistence),
        auth=authenticator(logger=logger),
        hosts=HOSTS,
        logger=logger,
        sealing_secret=secret,
    )
    from tests.webfixtures import signed_in

    for password, outcome in (
        ("a-wrong-password", "refused"),
        (None, "refused"),
        (OPERATOR_PASSWORD, "success"),
    ):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE
        ) as client:
            signed_in(client)
            page = (await client.get(f"/admin/identities/{record.id}/export")).text
            data = {"nonce": _nonce(page)}
            if password is not None:
                data["password"] = password
            response = await client.post(f"/admin/identities/{record.id}/export", data=data)
        event = logger.named("web_guarded_action")[-1]
        assert event["action"] == EXPORT_KEY
        assert event["outcome"] == outcome
        assert event["actor"] == OPERATOR
        if outcome == "refused":
            assert response.status_code == 403
            assert event["reason"] == "bad_password"
            assert "private_key_hex" not in response.text
            assert "seed_hex" not in response.text


def test_audit_requires_an_actor_with_no_default() -> None:
    """8.1: keyword-only and required, so mypy refuses a call without it."""
    import inspect

    from sighop.web.guarded import audit

    parameter = inspect.signature(audit).parameters["actor"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_no_audit_call_in_the_web_package_omits_the_actor() -> None:
    """8.1, statically: every call site passes `actor=`."""
    import ast

    package = Path(STATIC_DIR).parent
    missing = []
    for path in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "audit"
                and "actor" not in {keyword.arg for keyword in node.keywords}
            ):
                missing.append(f"{path.name}:{node.lineno}")
    assert missing == []


# --- 8.3 A wrong re-authentication feeds the throttle, not the door ---------


def test_wrong_reauthentications_throttle_sign_in_but_keep_the_session() -> None:
    app = _app()
    with _signed(app) as client:
        for _ in range(FREE_FAILURES):
            page = client.get("/admin/transmit").text
            client.post(
                "/admin/transmit",
                data={"enabled": "true", "nonce": _nonce(page), "password": "wrong"},
            )
        assert client.get("/").status_code == 200, "the session still loads pages"
    result_reason = app.state.auth.throttle.retry_after(OPERATOR, "elsewhere")
    assert result_reason > 0
    with _anonymous(app) as other:
        form = _login_form(other)
        response = other.post(
            "/login", data={**form, "username": OPERATOR, "password": OPERATOR_PASSWORD}
        )
    assert response.status_code == 200 and LOGIN_FAILED in response.text


# --- 8.4 A session the database could not confirm ---------------------------


@pytest.mark.parametrize("action", sorted(GUARDED))
def test_an_unverified_session_is_refused_every_guarded_action(action: str) -> None:
    from sighop.web.auth import REVALIDATE_SECONDS

    accounts = MemoryAccounts()
    accounts.add(OPERATOR, OPERATOR_PASSWORD)
    logger = RecordingLogger()
    state = stub_state(stub_names=("dev-panel-identity",))
    app = _app(state, accounts=accounts, logger=logger)
    path, fields = _guarded_paths(state, action)
    with _signed(app) as client:
        page = client.get(path).text
        accounts.degraded = True
        clock = app.state.auth.sessions.clock
        session = session_of(client)
        session.verified_at = clock() - REVALIDATE_SECONDS
        assert client.get("/").status_code == 200, "pages stay available"
        assert session.verified is False
        response = client.post(
            path, data={**fields, "nonce": _nonce(page), "password": OPERATOR_PASSWORD}
        )
    assert response.status_code == 403
    assert UNVERIFIED in response.text
    assert not _effect(state, action, response)
    assert logger.named("web_guarded_action")[-1]["reason"] == "unverified"


# --- 8.6 The run's output names the account ---------------------------------


def test_the_runs_output_names_the_account_that_opened_the_gate_and_raised_the_ceiling() -> None:
    said: list[str] = []
    state = stub_state()
    app = _app(state, said=said)
    with _signed(app) as client:
        page = client.get("/admin/transmit").text
        client.post(
            "/admin/transmit",
            data={"enabled": "true", "nonce": _nonce(page), "password": OPERATOR_PASSWORD},
        )
        page = client.get("/admin/ceiling").text
        client.post(
            "/admin/ceiling",
            data={"fraction": "0.2", "nonce": _nonce(page), "password": OPERATOR_PASSWORD},
        )
    assert len(said) == 2
    assert "transmission ENABLED" in said[0] and f"account {OPERATOR!r}" in said[0]
    assert "10% -> 20%" in said[1] and "ABOVE" in said[1]
    assert f"account {OPERATOR!r}" in said[1]


async def test_the_runtime_says_what_it_is_told() -> None:
    import io

    from sighop.runtime import Runtime, RuntimeConfig
    from tests.test_web_state import _empty_source, _startup

    out = io.StringIO()
    runtime = Runtime(source=_empty_source(), startup=_startup, config=RuntimeConfig(), out=out)
    runtime._started = True
    runtime.say("web: transmission ENABLED (by account 'dev-operator' from the web interface)")
    assert "by account 'dev-operator'" in out.getvalue()


# --- 9.4 Forwarding headers change nothing ----------------------------------

FORWARDED = {
    "X-Forwarded-For": "203.0.113.77",
    "X-Forwarded-Proto": "https",
    "Forwarded": "for=203.0.113.77;proto=https",
}


def test_forwarding_headers_change_neither_throttle_event_nor_cookie() -> None:
    logger = RecordingLogger()
    app = _app(logger=logger)
    with _anonymous(app) as client:
        form = _login_form(client)
        client.post(
            "/login",
            data={**form, "username": OPERATOR, "password": "wrong"},
            headers=FORWARDED,
        )
        response = client.post(
            "/login",
            data={**form, "username": OPERATOR, "password": OPERATOR_PASSWORD},
            headers=FORWARDED,
        )
    throttle = app.state.auth.throttle
    assert throttle.failures(client="203.0.113.77") == 0
    assert all(event["client"] == "testclient" for event in logger.named("web_request"))
    assert all(event["client"] == "testclient" for event in logger.named("web_login"))
    assert not _set_cookie(response)["secure"], "a claimed https scheme set Secure"


# --- 9.5 No way around it ----------------------------------------------------


def test_create_app_has_no_default_authenticator() -> None:
    import inspect

    parameter = inspect.signature(create_app).parameters["auth"]
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        create_app(stub_state())  # type: ignore[call-arg]


def test_no_auth_bypass_parameter_exists_in_the_web_package() -> None:
    package = Path(STATIC_DIR).parent
    pattern = re.compile(
        r"auth\s*[:=]\s*None|auth:\s*[A-Za-z]+\s*\|\s*None|no_auth|skip_auth|disable_auth"
        r"|auth_enabled|require_auth\s*=\s*False|authenticated\s*=\s*False",
        re.IGNORECASE,
    )
    offenders = []
    for path in sorted(package.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []


# --- 10.1 Account management is named where it would be looked for ----------


def test_the_schema_page_names_the_account_command_and_why_it_is_not_here() -> None:
    app = _app()
    with _signed(app) as client:
        body = " ".join(client.get("/admin/schema").text.split())
    assert "sighop web user" in body
    assert "Accounts are not managed from here" in body
    assert "stolen session" in body


# --- 10.3 Authentication changes nothing the mesh sees ----------------------


async def test_the_corpus_replays_byte_identically_with_authentication_wired_in() -> None:
    """10.3: delivered, duplicate, considered, contact and path counts, compared as
    bytes, with the authenticated application serving signed-in and anonymous
    requests — sign-ins, refusals and revalidations included — while it replays."""
    import json

    from sighop.radio.modem import EU868_NARROW
    from tests.test_web_exercise import _counts, _replayed

    keys = ("delivered", "duplicates", "considered", "contacts", "paths")
    without = await _counts(watched=False)

    state = stub_state(radio=EU868_NARROW, stub_names=("dev-replay-identity",))
    state.contacts.subscribe(state.pipeline.bus)
    app = _app(state)
    records = list(_replayed())
    half = len(records) // 2

    with _signed(app) as signed, _anonymous(app) as anonymous:
        for record in records[:half]:
            state.pipeline.ingest(record)
        form = _login_form(anonymous)
        anonymous.post("/login", data={**form, "username": OPERATOR, "password": "wrong"})
        assert anonymous.get("/contacts").status_code == 303
        for record in records[half:]:
            state.pipeline.ingest(record)
        for path in ("/", "/contacts", "/admin/identities", "/admin/schema", "/chat"):
            assert signed.get(path).status_code == 200
    await state.pipeline.bus.aclose()

    with_auth = {
        "delivered": state.pipeline.delivered,
        "duplicates": state.pipeline.duplicates,
        "considered": state.pipeline.dedup.stats.considered,
        "contacts": len(state.contacts),
        "paths": state.pipeline.paths.destination_count,
    }
    expected = {key: without[key] for key in keys}
    assert (
        json.dumps(with_auth, sort_keys=True).encode()
        == json.dumps(expected, sort_keys=True).encode()
    )
    assert without["delivered"] > 0, "the comparison would be vacuous"
