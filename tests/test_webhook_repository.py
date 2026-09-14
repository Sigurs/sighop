"""`WebhookRepository` against a real server (webhook-notifications task 3.3)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import select

from sighop.db.engine import Database, Failed, Succeeded
from sighop.db.models import Webhook as WebhookRow
from sighop.db.persistence import Persistence
from sighop.db.repositories import WebhookRecord, WebhookRepository
from sighop.webhooks.config import WebhookConfigError, WebhookExistsError

SECRET = bytes(range(32))
OTHER_SECRET = bytes(range(1, 33))
URL = "https://discord.com/api/webhooks/42/s3cr3t-token?wait=true"


def _value[T](outcome: Succeeded[T] | Failed) -> T:
    assert isinstance(outcome, Succeeded), outcome
    return outcome.value


async def _create(hooks: WebhookRepository, name: str = "dev-hook", **overrides: Any) -> WebhookRecord:
    fields: dict[str, Any] = {
        "name": name,
        "url": URL,
        "format": "discord",
        "triggers": ["new_repeater"],
        "secret": SECRET,
    }
    fields.update(overrides)
    return _value(await hooks.create(**fields))


@pytest.mark.database
async def test_create_stores_an_enabled_webhook_with_no_hop_limit(database: Database) -> None:
    hooks = WebhookRepository(database=database)
    record = await _create(hooks)

    assert record.enabled
    assert record.max_hops is None
    assert record.triggers == ("new_repeater",)
    assert record.url_host == "https://discord.com"
    stored = _value(await hooks.get_by_name("dev-hook"))
    assert stored == record


@pytest.mark.database
async def test_the_stored_url_is_sealed_and_the_host_is_clear(database: Database) -> None:
    hooks = WebhookRepository(database=database)
    await _create(hooks)

    async with database.sessions() as session:
        row = (await session.execute(select(WebhookRow))).scalar_one()
    assert URL.encode() not in bytes(row.sealed_url)
    assert b"s3cr3t-token" not in bytes(row.sealed_url)
    assert row.sealed_url[0] == 2
    assert row.url_host == "https://discord.com"


@pytest.mark.database
async def test_a_duplicate_name_is_refused_naming_it(database: Database) -> None:
    hooks = WebhookRepository(database=database)
    await _create(hooks)
    with pytest.raises(WebhookExistsError, match="'dev-hook'"):
        await _create(hooks, format="json")
    assert len(_value(await hooks.list_all())) == 1


@pytest.mark.database
@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "file:///etc/passwd"},
        {"url": "ftp://host/"},
        {"triggers": ["new_chatter"]},
        {"triggers": []},
        {"format": "slack"},
        {"max_hops": -1},
    ],
)
async def test_refusals_store_nothing(database: Database, overrides: dict[str, Any]) -> None:
    hooks = WebhookRepository(database=database)
    with pytest.raises(WebhookConfigError):
        await _create(hooks, **overrides)
    assert _value(await hooks.list_all()) == []


@pytest.mark.database
async def test_update_changes_only_what_was_given(database: Database) -> None:
    hooks = WebhookRepository(database=database)
    record = await _create(hooks, max_hops=2)

    assert _value(await hooks.update(record.id, triggers=["new_companion", "new_repeater"]))
    stored = _value(await hooks.get_by_id(record.id))
    assert stored is not None
    assert stored.triggers == ("new_companion", "new_repeater")
    assert stored.max_hops == 2 and stored.format == "discord"

    assert _value(await hooks.update(record.id, format="json", max_hops=None))
    stored = _value(await hooks.get_by_id(record.id))
    assert stored is not None and stored.format == "json" and stored.max_hops is None

    with pytest.raises(WebhookConfigError):
        await hooks.update(record.id, triggers=["nope"], format="json")


@pytest.mark.database
async def test_enable_disable_and_remove(database: Database) -> None:
    hooks = WebhookRepository(database=database)
    record = await _create(hooks)

    assert _value(await hooks.set_enabled(record.id, False))
    assert _value(await hooks.list_enabled(SECRET)) == []
    assert _value(await hooks.set_enabled(record.id, True))
    assert len(_value(await hooks.list_enabled(SECRET))) == 1

    assert _value(await hooks.remove(record.id))
    assert _value(await hooks.get_by_name("dev-hook")) is None
    assert not _value(await hooks.remove(record.id))


@pytest.mark.database
async def test_list_enabled_opens_urls_and_isolates_an_unsealable_row(database: Database) -> None:
    hooks = WebhookRepository(database=database)
    await _create(hooks, name="dev-good")
    await _create(hooks, name="dev-other-key", url="https://example.org/x", secret=OTHER_SECRET)

    opened = {item.record.name: item for item in _value(await hooks.list_enabled(SECRET))}
    assert opened["dev-good"].url == URL
    assert opened["dev-good"].error is None
    assert opened["dev-other-key"].url is None
    assert "did not authenticate" in (opened["dev-other-key"].error or "")
    assert "s3cr3t" not in repr(opened["dev-good"])


@pytest.mark.database
async def test_set_url_replaces_the_target(database: Database) -> None:
    hooks = WebhookRepository(database=database)
    record = await _create(hooks)

    assert _value(await hooks.set_url(record.id, "http://10.0.0.5:5678/hook", secret=SECRET)) == (
        "http://10.0.0.5:5678"
    )
    opened = _value(await hooks.open_url(record.id, SECRET))
    assert opened is not None and opened.url == "http://10.0.0.5:5678/hook"
    stored = _value(await hooks.get_by_id(record.id))
    assert stored is not None and stored.plaintext_http


@pytest.mark.database
async def test_outcome_columns_update(database: Database) -> None:
    hooks = WebhookRepository(database=database)
    record = await _create(hooks)
    delivered = dt.datetime(2026, 9, 13, 12, tzinfo=dt.UTC)
    failed = dt.datetime(2026, 9, 13, 13, tzinfo=dt.UTC)

    assert _value(await hooks.record_delivery(record.id, delivered))
    assert _value(await hooks.record_failure(record.id, failed, "HTTP 404"))

    stored = _value(await hooks.get_by_id(record.id))
    assert stored is not None
    assert stored.last_delivered_at == delivered
    assert stored.last_failed_at == failed
    assert stored.last_failure == "HTTP 404"


@pytest.mark.database
async def test_persistence_exposes_the_repository(database: Database) -> None:
    persistence = Persistence(database=database)
    assert isinstance(persistence.webhooks, WebhookRepository)
