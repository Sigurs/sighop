"""Signing in through the real form with real Argon2id (task 7.4, design D16).

Every other web test signs in through `SessionStore.issue()` with a hasher that
counts, so none of them pays 64 MiB per verification. This one pays it, once per
attempt, so the whole path — form, process token, throttle, libsodium in a worker
thread, rotation, cookie, and the guard resolving that cookie — is exercised
end to end. It needs no database: the account store is in memory.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from sighop.passwords import PasswordHasher, hash_password
from sighop.web.app import allowed_hosts, create_app
from sighop.web.auth import LOGIN_FAILED, SESSION_COOKIE, Authenticator
from sighop.web.guard import TOKEN_FIELD
from tests.test_web_state import RecordingLogger
from tests.webfixtures import MemoryAccounts, stub_state

pytestmark = pytest.mark.usefixtures("default_persistence")

USERNAME = "dev-operator"
PASSWORD = "a real password, hashed for real"


def test_signing_in_with_real_argon2id() -> None:
    accounts = MemoryAccounts()
    record = accounts.add(USERNAME, PASSWORD, password_hash=hash_password(PASSWORD))
    assert record.password_hash.startswith("$argon2id$")

    logger = RecordingLogger()
    hasher = PasswordHasher()
    auth = Authenticator(accounts=accounts, hasher=hasher, logger=logger)
    state = stub_state()
    app = create_app(state, auth=auth, hosts=allowed_hosts("127.0.0.1", 8080), logger=logger)

    with TestClient(app, base_url="http://127.0.0.1:8080", follow_redirects=False) as client:
        assert client.get("/").status_code == 303
        form = client.get("/login").text
        token = re.search(r'name="_token" value="([^"]+)"', form)
        assert token is not None

        wrong = client.post(
            "/login",
            data={TOKEN_FIELD: token.group(1), "username": USERNAME, "password": "not it"},
        )
        assert wrong.status_code == 200 and LOGIN_FAILED in wrong.text

        right = client.post(
            "/login",
            data={TOKEN_FIELD: token.group(1), "username": "DEV-Operator", "password": PASSWORD},
        )
        assert right.status_code == 303
        assert SESSION_COOKIE in right.cookies

        overview = client.get("/")
        assert overview.status_code == 200
        assert f'signed in as <span class="mono">{USERNAME}</span>' in overview.text

    # The dummy hash, the wrong password and the right one — three real runs.
    assert hasher.completed == 3
    outcomes = [(event["outcome"], event["reason"]) for event in logger.named("web_login")]
    assert outcomes == [("refused", "bad_password"), ("success", "success")]
