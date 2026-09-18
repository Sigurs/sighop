"""First-run setup as the application serves it (web-first-run-setup 3.3-3.5, 4.3).

`tests/test_web_auth.py` tests `complete_setup`'s decisions; this drives the
real application through the real guard, over an in-memory account store and a
hasher that counts — and, for the race with a terminal, over the database.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sighop.web.app import allowed_hosts, create_app
from sighop.web.auth import (
    LOGIN_FAILED,
    SESSION_COOKIE,
    SETUP_BAD_CODE,
    SETUP_PASSWORD_MISMATCH,
    Authenticator,
    FirstRunSetup,
)
from sighop.web.guard import TOKEN_FIELD
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    CountingHasher,
    MemoryAccounts,
    StubState,
    authenticator,
    stub_state,
)

HOSTS = allowed_hosts("127.0.0.1", 8080)
BASE = "http://127.0.0.1:8080"
USERNAME = "dev-first"
PASSWORD = "a-first-password"


class _Setup:
    """One served application in setup mode, and what a test reads back."""

    def __init__(
        self,
        *,
        accounts: MemoryAccounts | None = None,
        setup: FirstRunSetup | None = None,
        state: StubState | None = None,
    ) -> None:
        self.accounts = accounts if accounts is not None else MemoryAccounts()
        self.setup = setup if setup is not None else FirstRunSetup()
        self.state = state or stub_state(stub_names=("dev-panel-identity",))
        self.logger = RecordingLogger()
        self.said: list[str] = []
        self.auth: Authenticator = authenticator(
            self.accounts, logger=self.logger, setup=self.setup
        )
        hasher = self.auth.hasher
        assert isinstance(hasher, CountingHasher)
        self.hasher = hasher
        self.app: FastAPI = create_app(
            self.state,
            auth=self.auth,
            hosts=HOSTS,
            logger=self.logger,
            announce=self.said.append,
        )

    def client(self) -> TestClient:
        return TestClient(self.app, base_url=BASE, follow_redirects=False)

    def form(self, **fields: str) -> dict[str, str]:
        return {
            TOKEN_FIELD: self.auth.login_token,
            "setup_code": self.setup.display,
            "username": USERNAME,
            "password": PASSWORD,
            "password_again": PASSWORD,
            **fields,
        }


def _form_token(page: str) -> str:
    found = re.search(r'name="_token" value="([^"]+)"', page)
    assert found is not None, "the setup form carries no provenance token"
    return found.group(1)


# --- 3.3 GET /setup ---------------------------------------------------------


def test_the_setup_form_is_served_while_setup_is_pending_and_carries_nothing() -> None:
    served = _Setup()
    stub = served.state.adverts.stubs[0]
    with served.client() as client:
        response = client.get("/setup")
    assert response.status_code == 200
    body = response.text
    assert response.headers["cache-control"] == "no-store"
    assert _form_token(body) == served.auth.login_token
    for name in ("setup_code", "username", "password", "password_again"):
        assert f'name="{name}"' in body
    assert body.count('autocomplete="new-password"') == 2
    assert "sighop web user add" in body and "docker compose logs sighop" in body
    assert served.setup.code not in body and served.setup.display not in body
    assert stub.identity.private_key.hex() not in body
    assert stub.identity.public_key.hex() not in body
    assert "dev-panel-identity" not in body
    assert 'aria-label="duty cycle"' not in body and "transmit enabled" not in body


def test_without_a_setup_the_form_sends_the_browser_to_sign_in() -> None:
    app = create_app(stub_state(), auth=authenticator(), hosts=HOSTS, logger=RecordingLogger())
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        response = client.get("/setup")
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_a_closed_setup_sends_the_browser_to_sign_in() -> None:
    served = _Setup()
    served.setup.close()
    with served.client() as client:
        response = client.get("/setup")
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_an_account_that_appeared_meanwhile_closes_setup_on_the_form_request() -> None:
    served = _Setup()
    served.accounts.add("dev-terminal", "from-the-terminal")
    with served.client() as client:
        response = client.get("/setup")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert not served.setup.pending


def test_a_count_that_cannot_be_read_still_renders_the_form() -> None:
    served = _Setup()
    served.accounts.degraded = True
    with served.client() as client:
        response = client.get("/setup")
    assert response.status_code == 200
    assert 'name="setup_code"' in response.text
    assert served.setup.pending


def test_the_sign_in_form_sends_the_browser_to_setup_while_pending() -> None:
    served = _Setup()
    with served.client() as client:
        response = client.get("/login?next=%2Fcontacts")
    assert response.status_code == 303 and response.headers["location"] == "/setup"


def test_a_sign_in_submitted_during_setup_fails_as_an_unknown_user() -> None:
    served = _Setup()
    with served.client() as client:
        response = client.post(
            "/login",
            data={TOKEN_FIELD: served.auth.login_token, "username": USERNAME, "password": PASSWORD},
        )
    assert response.status_code == 200 and LOGIN_FAILED in response.text
    assert served.logger.named("web_login")[-1]["reason"] == "unknown_user"


# --- 3.4 POST /setup --------------------------------------------------------


def test_a_successful_setup_lands_on_the_overview_signed_in_and_is_announced() -> None:
    served = _Setup()
    with served.client() as client:
        token = _form_token(client.get("/setup").text)
        response = client.post("/setup", data=served.form(**{TOKEN_FIELD: token}))
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert SESSION_COOKIE in response.cookies
        overview = client.get("/")
        assert overview.status_code == 200
        assert f'signed in as <span class="mono">{USERNAME}</span>' in overview.text
        assert client.get("/setup").headers["location"] == "/login"
    assert set(served.accounts.accounts) == {USERNAME}
    assert not served.setup.pending
    assert served.said == [f"web: first-run setup completed; account '{USERNAME}' created"]
    requests = served.logger.named("web_request")
    assert any(e["path"] == "/setup" and e["actor"] == USERNAME for e in requests)
    for name, fields in served.logger.events:
        for secret in (served.setup.code, served.setup.display, PASSWORD):
            assert all(secret not in str(value) for value in fields.values()), name


def test_a_wrong_code_re_renders_one_fixed_message_without_echoing_the_username() -> None:
    served = _Setup()
    with served.client() as client:
        response = client.post(
            "/setup",
            data=served.form(setup_code="0000000000000000000A", username="a-guessed-name"),
        )
    assert response.status_code == 200
    assert SETUP_BAD_CODE in response.text
    assert "a-guessed-name" not in response.text
    assert served.setup.code not in response.text
    assert SESSION_COOKIE not in response.cookies
    assert not served.accounts.accounts and served.hasher.hashes == 0
    assert served.said == []


def test_differing_passwords_then_the_same_code_again_succeeds() -> None:
    served = _Setup()
    with served.client() as client:
        refused = client.post("/setup", data=served.form(password_again=PASSWORD + "x"))
        assert refused.status_code == 200 and SETUP_PASSWORD_MISMATCH in refused.text
        assert USERNAME not in refused.text
        assert not served.accounts.accounts
        accepted = client.post("/setup", data=served.form())
    assert accepted.status_code == 303 and accepted.headers["location"] == "/"
    assert set(served.accounts.accounts) == {USERNAME}


def test_a_second_submission_after_success_creates_nothing() -> None:
    served = _Setup()
    with served.client() as client:
        assert client.post("/setup", data=served.form()).status_code == 303
    with served.client() as other:
        response = other.post("/setup", data=served.form(username="dev-second"))
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert set(served.accounts.accounts) == {USERNAME}
    assert served.hasher.hashes == 1
    assert [e["reason"] for e in served.logger.named("web_setup")] == ["success", "setup_closed"]


@pytest.mark.parametrize("token", [None, "not-the-process-token"])
def test_a_submission_without_the_process_token_is_refused_before_anything_is_checked(
    token: str | None,
) -> None:
    served = _Setup()
    data = served.form()
    if token is None:
        del data[TOKEN_FIELD]
    else:
        data[TOKEN_FIELD] = token
    with served.client() as client:
        response = client.post("/setup", data=data)
    assert response.status_code == 403
    assert not served.accounts.accounts
    assert served.hasher.hashes == 0
    assert served.setup.pending
    assert served.logger.named("web_setup") == [], "no setup decision ran"
    assert served.logger.named("web_request")[-1]["outcome"] == "no_token"


# --- 3.5 An account added from a terminal while setup is served -------------


@pytest.mark.database
async def test_an_account_added_through_the_repository_closes_setup(database: object) -> None:
    import httpx2

    from sighop.db.persistence import Persistence
    from sighop.passwords import hash_password

    persistence = Persistence(database=database)  # type: ignore[arg-type]
    users = persistence.web_users
    assert (await users.count()).value == 0

    def serve() -> tuple[FastAPI, FirstRunSetup, CountingHasher]:
        setup = FirstRunSetup()
        hasher = CountingHasher()
        auth = Authenticator(accounts=users, hasher=hasher, logger=RecordingLogger(), setup=setup)
        app = create_app(
            stub_state(persistence=persistence), auth=auth, hosts=HOSTS, logger=RecordingLogger()
        )
        return app, setup, hasher

    for_form, form_setup, _ = serve()
    for_post, post_setup, post_hasher = serve()
    await users.add("dev-terminal", password_hash=hash_password("from-the-terminal"))

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=for_form), base_url=BASE
    ) as client:
        response = await client.get("/setup")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert not form_setup.pending

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=for_post), base_url=BASE
    ) as client:
        response = await client.post(
            "/setup",
            data={
                TOKEN_FIELD: for_post.state.auth.login_token,
                "setup_code": post_setup.code,
                "username": USERNAME,
                "password": PASSWORD,
                "password_again": PASSWORD,
            },
        )
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert not post_setup.pending
    assert post_hasher.hashes == 1, "the race is decided by add_first, after hashing"
    listed = (await users.list()).value
    assert [record.username for record in listed] == ["dev-terminal"]
