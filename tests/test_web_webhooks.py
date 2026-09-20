"""Webhooks through the interface (webhook-notifications task 8.1, `web-admin`)."""

from __future__ import annotations

import base64
import datetime as dt
import html

import httpx2
import pytest
from fastapi import FastAPI

from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import WebhookRecord
from sighop.web.app import allowed_hosts, create_app
from sighop.web.guard import TOKEN_FIELD
from sighop.webhooks.config import WebhookConfigError, parse_url
from tests.test_web_state import RecordingLogger
from tests.test_webhooks_transport import Receiver, receiver  # noqa: F401 - fixture
from tests.webfixtures import (
    StubState,
    authenticator,
    csrf,
    signed_async_client,
    signed_client,
    stub_state,
)

HOSTS = allowed_hosts("127.0.0.1", 8080)
SECRET = base64.b64decode(generate_secret_key())
URL = "https://discord.com/api/webhooks/42/s3cr3t-token?wait=true"
SECRET_PARTS = ("/api/webhooks", "s3cr3t-token", "wait=true")


def _built(state: StubState, *, sealing_secret: bytes | None = SECRET) -> FastAPI:
    return create_app(
        state,
        auth=authenticator(),
        hosts=HOSTS,
        logger=RecordingLogger(),
        sealing_secret=sealing_secret,
    )


def _live(app: FastAPI) -> httpx2.AsyncClient:
    return signed_async_client(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    )


async def _apost(client: httpx2.AsyncClient, path: str, data: dict[str, object]):
    return await client.post(path, data={TOKEN_FIELD: csrf(client), **data})


def _no_secret(body: str, *parts: str) -> None:
    for part in (*SECRET_PARTS, *parts):
        assert part not in body, part


async def _stored(
    persistence: Persistence, name: str = "dev-hook", url: str = URL
) -> WebhookRecord:
    created = await persistence.webhooks.create(
        name=name, url=url, format="discord", triggers=["new_repeater"], secret=SECRET
    )
    assert isinstance(created, Succeeded)
    return created.value


async def _get(persistence: Persistence, name: str = "dev-hook") -> WebhookRecord | None:
    found = await persistence.webhooks.get_by_name(name)
    assert isinstance(found, Succeeded)
    return found.value


def test_with_no_database_the_page_says_storage_is_required_and_offers_no_controls() -> None:
    app = _built(stub_state())
    with signed_client(app, base_url="http://127.0.0.1:8080") as client:
        body = client.get("/admin/webhooks").text

    assert "require durable storage" in body
    assert 'action="/admin/webhooks' not in body


def test_the_navigation_links_the_page() -> None:
    app = _built(stub_state())
    with signed_client(app, base_url="http://127.0.0.1:8080") as client:
        body = client.get("/").text
    assert 'href="/admin/webhooks"' in body


@pytest.mark.database
async def test_the_page_shows_scheme_and_host_and_outcomes_but_no_path(database: Database) -> None:
    persistence = Persistence(database=database)
    record = await _stored(persistence)
    await persistence.webhooks.record_delivery(
        record.id, dt.datetime(2026, 9, 13, 10, tzinfo=dt.UTC)
    )
    await persistence.webhooks.record_failure(
        record.id, dt.datetime(2026, 9, 13, 11, tzinfo=dt.UTC), "HTTP 404"
    )
    app = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        body = (await client.get("/admin/webhooks")).text

    assert "https://discord.com" in body
    assert "new_repeater" in body and "discord" in body
    # Each time is relative in the browser and carries its exact UTC value; the
    # failure's reason sits beside the time rather than inside it.
    assert 'datetime="2026-09-13T10:00:00+00:00"' in body
    assert 'datetime="2026-09-13T11:00:00+00:00"' in body
    assert "(HTTP 404)" in body
    assert 'type="password"' in body and 'autocomplete="off"' in body
    _no_secret(body)


@pytest.mark.database
async def test_adding_through_the_page_stores_an_enabled_webhook(database: Database) -> None:
    persistence = Persistence(database=database)
    app = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        response = await _apost(
            client,
            "/admin/webhooks/create",
            {
                "name": "dev-hook",
                "url": URL,
                "format": "json",
                "triggers": ["new_repeater", "new_companion"],
                "max_hops": "2",
            },
        )
        assert response.status_code == 303
        body = (await client.get("/admin/webhooks")).text

    stored = await _get(persistence)
    assert stored is not None and stored.enabled
    assert stored.triggers == ("new_repeater", "new_companion") and stored.max_hops == 2
    _no_secret(body)


@pytest.mark.database
async def test_a_refused_url_gives_the_command_lines_reason_and_is_not_shown_back(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    app = _built(stub_state(persistence=persistence))
    submitted = "file:///etc/secret-path?token=hunter2"
    with pytest.raises(WebhookConfigError) as cli_reason:
        parse_url(submitted)

    async with _live(app) as client:
        response = await _apost(
            client,
            "/admin/webhooks/create",
            {"name": "dev-kept", "url": submitted, "format": "json", "triggers": ["new_repeater"]},
        )

    assert response.status_code == 400
    assert str(cli_reason.value) in html.unescape(response.text)
    assert 'value="dev-kept"' in response.text, "the name is carried back"
    _no_secret(response.text, "secret-path", "hunter2")
    listed = await persistence.webhooks.list_all()
    assert isinstance(listed, Succeeded) and listed.value == []


@pytest.mark.database
async def test_enable_settings_url_and_remove_with_confirmation(database: Database) -> None:
    persistence = Persistence(database=database)
    record = await _stored(persistence)
    app = _built(stub_state(persistence=persistence))
    base = f"/admin/webhooks/{record.id}"

    async with _live(app) as client:
        assert (await _apost(client, f"{base}/enabled", {"enabled": "false"})).status_code == 303
        assert not (await _get(persistence)).enabled

        response = await _apost(
            client,
            f"{base}/settings",
            {"format": "json", "triggers": ["new_companion"], "max_hops": ""},
        )
        assert response.status_code == 303
        changed = await _get(persistence)
        assert changed is not None and changed.format == "json"
        assert changed.triggers == ("new_companion",) and changed.max_hops is None

        refused = await _apost(client, f"{base}/settings", {"format": "json", "max_hops": "1"})
        assert refused.status_code == 400 and "at least one trigger" in refused.text

        new_url = "https://hooks.example.org/private/path?k=v"
        assert (await _apost(client, f"{base}/url", {"url": new_url})).status_code == 303
        body = (await client.get("/admin/webhooks")).text
        assert "https://hooks.example.org" in body
        _no_secret(body, "/private/path", "k=v")

        bad = await _apost(client, f"{base}/url", {"url": "ftp://elsewhere/secret-path"})
        assert bad.status_code == 400 and "http or https" in bad.text
        _no_secret(bad.text, "secret-path")

        confirm_page = (await client.get(f"{base}/remove")).text
        assert "confirm: remove dev-hook" in confirm_page
        unconfirmed = await _apost(client, f"{base}/remove", {})
        assert unconfirmed.status_code == 200
        assert await _get(persistence) is not None, "nothing removed without confirmation"
        assert (await _apost(client, f"{base}/remove", {"confirm": "yes"})).status_code == 303
        assert await _get(persistence) is None


@pytest.mark.database
async def test_testing_from_the_page_shows_the_outcome(
    database: Database,
    receiver: Receiver,  # noqa: F811
) -> None:
    persistence = Persistence(database=database)
    ok = await _stored(persistence, "dev-ok", f"{receiver.base}/ok?token=hunter2")
    missing = await _stored(persistence, "dev-missing", f"{receiver.base}/missing")
    app = _built(stub_state(persistence=persistence))

    async with _live(app) as client:
        delivered = await _apost(
            client, f"/admin/webhooks/{ok.id}/test", {"trigger": "new_companion"}
        )
        rejected = await _apost(
            client, f"/admin/webhooks/{missing.id}/test", {"trigger": "new_repeater"}
        )

    assert delivered.status_code == 200
    assert "delivered, HTTP 204" in delivered.text
    _no_secret(delivered.text, "hunter2", "/ok?")
    assert "failed, HTTP 404" in rejected.text
    assert len(receiver.requests) == 2
