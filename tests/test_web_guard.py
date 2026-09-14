"""Request provenance and the request's wide event (design D9, §9).

Every assertion here held before milestone 9 added authentication, and holds
beside it: a page in the operator's browser can reach a loopback port and ride a
signed-in browser, and a hostname an attacker controls can be made to resolve to
one — so a state-changing request has to be attributable to a page this process
served *to this session*, and a request declaring somebody else's host name has
to be refused before a handler sees it.

The safe-method sweep is the other half, and it is enumerated over the route
table rather than written once per route: a rule asserted route by route is a
rule that stops holding the first time somebody adds a route.
"""

from __future__ import annotations

import re
import secrets

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.routing import Route

from sighop.web.app import allowed_hosts, create_app
from sighop.web.auth import FirstRunSetup
from sighop.web.guard import REBINDING_STATUS, TOKEN_FIELD, TOKEN_HEADER
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    MemoryAccounts,
    StubState,
    authenticator,
    csrf,
    registered_routes,
    signed_client,
    stub_state,
)

HOSTS = allowed_hosts("127.0.0.1", 8080)


class Probe:
    """A route that records whether it ran. What "no handler was invoked" means."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> dict[str, int]:
        self.calls += 1
        return {"calls": self.calls}


def _app_with_probe(
    *, hosts: frozenset[str] | None = HOSTS, logger: RecordingLogger | None = None
) -> tuple[FastAPI, Probe, StubState]:
    """The real application, plus one state-changing route to aim at.

    A probe rather than one of the panel's own actions: what is under test is
    the guard, and a test that had to enable transmission to check a token
    would be testing two things and reporting one.
    """
    state = stub_state()
    app = create_app(
        state, auth=authenticator(), hosts=hosts, logger=logger or RecordingLogger()
    )
    probe = Probe()
    app.post("/probe")(probe)
    return app, probe, state


def _client(app: FastAPI) -> TestClient:
    """Signed in, with no token sent by default — the token is what is under test.

    The host the guard was configured for, so a request is ordinary unless a
    test deliberately makes it otherwise.
    """
    return signed_client(app, send_token=False, base_url="http://127.0.0.1:8080")


# --- 8.1 The provenance token -----------------------------------------------


def test_a_post_without_the_token_changes_nothing_and_is_rejected() -> None:
    """8.1: the CSRF case — a page on another origin posting to loopback."""
    app, probe, _state = _app_with_probe()

    with _client(app) as client:
        response = client.post("/probe")

    assert response.status_code == 403
    assert probe.calls == 0, "the handler ran despite the request being refused"


def test_a_post_carrying_the_token_as_a_header_is_served() -> None:
    """8.1: what HTMX sends, because the base template sets `hx-headers`."""
    app, probe, _state = _app_with_probe()

    with _client(app) as client:
        response = client.post("/probe", headers={TOKEN_HEADER: csrf(client)})

    assert response.status_code == 200
    assert probe.calls == 1


def test_a_post_carrying_the_token_as_a_form_field_is_served() -> None:
    """8.1: what a plain `<form method="post">` sends, with no script running."""
    app, probe, _state = _app_with_probe()

    with _client(app) as client:
        response = client.post("/probe", data={TOKEN_FIELD: csrf(client)})

    assert response.status_code == 200
    assert probe.calls == 1


def test_the_handler_still_reads_the_body_the_browser_sent() -> None:
    """8.1: the guard reads the body to find the token and replays it.

    Consuming a request body in middleware is the classic way to make a handler
    see an empty form. The guard hands the body back down; this is what says so.
    """
    state = stub_state()
    app = create_app(state, auth=authenticator(), hosts=HOSTS, logger=RecordingLogger())
    seen: dict[str, str] = {}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, str]:
        form = await request.form()
        seen.update({key: str(value) for key, value in form.items()})
        return dict(seen)

    with _client(app) as client:
        response = client.post("/echo", data={TOKEN_FIELD: csrf(client), "text": "hello"})

    assert response.status_code == 200
    assert seen["text"] == "hello"


def test_a_wrong_token_is_refused() -> None:
    """8.1: the token is compared, not merely required to be present."""
    app, probe, _state = _app_with_probe()

    with _client(app) as client:
        response = client.post("/probe", headers={TOKEN_HEADER: secrets.token_urlsafe(32)})

    assert response.status_code == 403
    assert probe.calls == 0


def test_a_token_from_another_session_is_refused_like_none() -> None:
    """6.2, `web-server`: bound to the session its page was served to."""
    app, probe, _state = _app_with_probe()

    with _client(app) as client, _client(app) as other:
        response = client.post("/probe", headers={TOKEN_HEADER: csrf(other)})
        form = client.post("/probe", data={TOKEN_FIELD: csrf(other)})

    assert response.status_code == 403
    assert form.status_code == 403
    assert probe.calls == 0


def test_the_process_token_is_not_a_session_token() -> None:
    """6.2: the sign-in form's token authorises the sign-in form, nothing else."""
    app, probe, _state = _app_with_probe()

    with _client(app) as client:
        response = client.post("/probe", headers={TOKEN_HEADER: app.state.auth.login_token})

    assert response.status_code == 403
    assert probe.calls == 0


def test_a_cross_site_post_is_refused_even_carrying_a_token() -> None:
    """8.1: `Sec-Fetch-Site` is checked where the browser sends it."""
    app, probe, _state = _app_with_probe()

    with _client(app) as client:
        response = client.post(
            "/probe",
            headers={TOKEN_HEADER: csrf(client), "Sec-Fetch-Site": "cross-site"},
        )

    assert response.status_code == 403
    assert probe.calls == 0


def test_the_rejection_appears_in_the_requests_event() -> None:
    """8.1: a refusal nobody can see is a refusal nobody will investigate."""
    logger = RecordingLogger()
    app, _probe, _state = _app_with_probe(logger=logger)

    with _client(app) as client:
        client.post("/probe")

    events = logger.named("web_request")
    assert len(events) == 1
    assert events[0]["outcome"] == "no_token"
    assert events[0]["status"] == 403
    assert events[0]["method"] == "POST"


def test_every_served_page_carries_the_sessions_token() -> None:
    """8.1: embedded in the page, so a form and an HTMX request both have it."""
    app, _probe, _state = _app_with_probe()

    with _client(app) as client:
        page = client.get("/").text
        token = csrf(client)

    assert f'content="{token}"' in page
    assert app.state.auth.login_token not in page


# --- 8.2 The declared host --------------------------------------------------


def test_a_rebinding_shaped_host_is_rejected_before_any_handler_runs() -> None:
    """8.2: the DNS rebinding case, which is what makes reading responses possible."""
    app, probe, _state = _app_with_probe()

    with _client(app) as client:
        refused = client.post(
            "/probe",
            headers={TOKEN_HEADER: csrf(client), "Host": "rebind.attacker.example"},
        )
        page = client.get("/", headers={"Host": "rebind.attacker.example"})

    assert refused.status_code == REBINDING_STATUS
    assert page.status_code == REBINDING_STATUS
    assert probe.calls == 0


def test_the_host_check_is_configured_from_what_was_actually_bound() -> None:
    """8.2: the set is a fact about the bind, not a policy a handler chooses."""
    import socket

    from sighop.web.app import WebInterface

    interface = WebInterface.bind(
        stub_state(), auth=authenticator(), accounts_enabled=1, host="127.0.0.1", port=0
    )
    try:
        app = interface.build_app()
        assert app.state.hosts == allowed_hosts("127.0.0.1", interface.port)
        assert f"127.0.0.1:{interface.port}" in app.state.hosts
    finally:
        interface.close()
    assert isinstance(interface.listener, socket.socket)


def test_an_application_off_a_socket_has_no_host_to_check_against() -> None:
    """8.2: `None` turns off the rebinding check only — never authentication."""
    app, probe, _state = _app_with_probe(hosts=None)

    with _client(app) as client:
        response = client.post(
            "/probe", headers={TOKEN_HEADER: csrf(client), "Host": "anything.example"}
        )
    with TestClient(app, base_url="http://127.0.0.1:8080") as anonymous:
        refused = anonymous.post("/probe", headers={"Host": "anything.example"})

    assert response.status_code == 200
    assert refused.status_code == 401
    assert probe.calls == 1


# --- 8.3 Safe methods, enumerated over the route table ----------------------


HEX = re.compile(r"[0-9a-f]{16,}")


def _safe_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) a browser could issue by navigating or prefetching.

    Through `registered_routes`, which walks into included routers: this
    FastAPI version does not flatten them into `app.routes`, so the obvious
    loop would sweep three pages out of thirty-one and this whole assertion
    would be a fraction of what it claims.
    """
    found: list[tuple[str, str]] = []
    for route in registered_routes(app):
        if not isinstance(route, Route) or "{" in route.path:
            continue
        for method in sorted((route.methods or set()) & {"GET", "HEAD"}):
            found.append((method, route.path))
    return found


def test_no_safe_request_transmits_or_changes_state() -> None:
    """8.3: enumerated over the route table, so a new route joins the rule."""
    state = stub_state(stub_names=("panel-identity",))
    app = create_app(state, auth=authenticator(), hosts=HOSTS, logger=RecordingLogger())
    routes = _safe_routes(app)
    assert routes, "there are no safe routes to sweep; the assertion would be vacuous"

    before = state.scheduler.status().as_json()
    contacts = len(state.contacts)
    paths = state.pipeline.paths.destination_count

    with _client(app) as client:
        for method, path in routes:
            response = client.request(method, path)
            assert response.status_code < 500, f"{method} {path} failed"

    assert state.scheduler.status().as_json() == before, "a safe request transmitted"
    assert len(state.contacts) == contacts
    assert state.pipeline.paths.destination_count == paths


def test_no_safe_request_reveals_private_key_material() -> None:
    """8.3: the seeds this run holds appear in no page reachable by navigation.

    Design D10 puts key material in exactly one response body, behind a guarded
    action's confirmation. Everything else is swept here.
    """
    state = stub_state(stub_names=("panel-identity",))
    app = create_app(state, auth=authenticator(), hosts=HOSTS, logger=RecordingLogger())
    seeds = [stub.identity.seed for stub in state.adverts.stubs]
    assert seeds, "the state holds no identity; the assertion would be vacuous"

    with _client(app) as client:
        for method, path in _safe_routes(app):
            body = client.request(method, path).text
            for seed in seeds:
                assert seed.hex() not in body, f"{method} {path} leaked a seed"
                assert _base64ish(seed) not in body


def _base64ish(seed: bytes) -> str:
    import base64

    return base64.b64encode(seed).decode()


# --- 8.4 One event per request ----------------------------------------------


def test_exactly_one_event_is_emitted_per_request() -> None:
    """8.4, §9: one unit of work, one event — never two, never none."""
    logger = RecordingLogger()
    app, _probe, _state = _app_with_probe(logger=logger)

    with _client(app) as client:
        client.get("/")
        client.post("/probe", headers={TOKEN_HEADER: csrf(client)})

    events = logger.named("web_request")
    assert len(events) == 2
    assert [event["method"] for event in events] == ["GET", "POST"]
    assert [event["status"] for event in events] == [200, 200]
    assert all(event["outcome"] == "success" for event in events)
    assert all(isinstance(event["duration_ms"], float) for event in events)
    assert all(event["route"] for event in events)


def test_the_event_carries_the_route_rather_than_only_the_path() -> None:
    """8.4: a route is what a reader compares across requests."""
    logger = RecordingLogger()
    state = stub_state()
    app = create_app(state, auth=authenticator(), hosts=HOSTS, logger=logger)

    @app.get("/thing/{name}")
    async def thing(name: str) -> dict[str, str]:
        return {"name": name}

    with _client(app) as client:
        client.get("/thing/abc")

    event = logger.named("web_request")[0]
    assert event["route"] == "/thing/{name}"
    assert event["path"] == "/thing/abc"


def test_no_event_carries_the_query_string() -> None:
    """8.4: the one part of a request a password could reach by mistake."""
    logger = RecordingLogger()
    app, _probe, _state = _app_with_probe(logger=logger)

    with _client(app) as client:
        client.get("/?password=hunter2")

    event = logger.named("web_request")[0]
    assert "hunter2" not in repr(event)


# --- 8.5 An unhandled failure -----------------------------------------------


def test_a_failing_route_serves_a_generic_page_and_records_the_failure() -> None:
    """8.5: no traceback, no path, no internal detail — and one event saying so."""
    logger = RecordingLogger()
    state = stub_state()
    app = create_app(state, auth=authenticator(), hosts=HOSTS, logger=logger)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("the secret internal detail")

    with signed_client(
        app, base_url="http://127.0.0.1:8080", raise_server_exceptions=False
    ) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    body = response.text
    assert "the secret internal detail" not in body
    assert "Traceback" not in body
    assert "/boom" not in body
    assert "sighop/web" not in body
    assert "something failed" in body

    events = logger.named("web_request")
    assert len(events) == 1
    assert events[0]["outcome"] == "error"
    assert events[0]["status"] == 500


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_a_missing_page_is_a_refusal_not_a_failure(method: str) -> None:
    """8.4: a 404 is a completed request with a refused outcome, and one event."""
    logger = RecordingLogger()
    app, _probe, _state = _app_with_probe(logger=logger)

    with _client(app) as client:
        response = client.request(method, "/nowhere")

    assert response.status_code == 404
    events = logger.named("web_request")
    assert len(events) == 1
    assert events[0]["outcome"] == "refused"


# --- web-first-run-setup 3.1 Where a refused page request is sent ------------


def test_a_refused_page_goes_to_sign_in_when_no_setup_is_pending() -> None:
    app = create_app(stub_state(), auth=authenticator(), hosts=HOSTS, logger=RecordingLogger())
    with TestClient(app, base_url="http://127.0.0.1:8080", follow_redirects=False) as client:
        response = client.get("/contacts")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fcontacts"


def test_a_refused_page_goes_to_setup_while_setup_is_pending() -> None:
    setup = FirstRunSetup()
    logger = RecordingLogger()
    auth = authenticator(MemoryAccounts(), setup=setup)
    app = create_app(stub_state(), auth=auth, hosts=HOSTS, logger=logger)
    with TestClient(app, base_url="http://127.0.0.1:8080", follow_redirects=False) as client:
        for path in ("/", "/contacts", "/admin/identities?filter=x"):
            response = client.get(path)
            assert response.status_code == 303
            assert response.headers["location"] == "/setup", path
            assert response.content == b""
        assert client.post("/admin/transmit").status_code == 401
        setup.close()
        response = client.get("/contacts")
    assert response.headers["location"] == "/login?next=%2Fcontacts"
    assert all(setup.code not in str(fields) for _, fields in logger.events)


# --- webhook-notifications 8.1: the webhook writes are guarded too ----------


def test_every_webhook_write_refuses_a_post_without_the_token() -> None:
    """Enumerated over the route table, so a webhook write added later joins it."""
    from fastapi.routing import APIRoute

    state = stub_state()
    app = create_app(state, auth=authenticator(), hosts=HOSTS, logger=RecordingLogger())
    writes = sorted(
        route.path.replace("{webhook_id}", "00000000-0000-0000-0000-000000000000")
        for route in registered_routes(app)
        if isinstance(route, APIRoute)
        and route.path.startswith("/admin/webhooks")
        and "POST" in (route.methods or set())
    )
    assert len(writes) == 6, writes

    with _client(app) as client:
        for path in writes:
            response = client.post(path, data={"url": "https://h/x", "confirm": "yes"})
            assert response.status_code == 403, path
