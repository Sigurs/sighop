"""The twelfth table and its migration (webhook-notifications task 2.2, design D5)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import CheckConstraint, Table, UniqueConstraint
from sqlalchemy.exc import IntegrityError

from sighop.config import DatabaseConfig
from sighop.db import migrations
from sighop.db.engine import Database
from sighop.db.models import Webhook
from tests.dbfixtures import SCHEMA_PREFIX, _connect, _create_schema, _drop_schema

MIGRATION = "0006_webhooks.py"
TABLE: Table = Webhook.__table__  # type: ignore[assignment]


def _row(**overrides: object) -> Webhook:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "name": "dev-hook",
        "sealed_url": b"\x02sealed",
        "url_host": "https://example.invalid",
        "format": "json",
        "triggers": ["new_repeater"],
        "max_hops": None,
        "enabled": True,
        "created_at": dt.datetime.now(dt.UTC),
    }
    fields.update(overrides)
    return Webhook(**fields)


def test_the_model_declares_the_named_constraints() -> None:
    names = {constraint.name for constraint in TABLE.constraints}
    assert {"uq_webhook_name", "ck_webhook_format", "ck_webhook_max_hops"} <= names
    unique = {
        constraint.name: {column.name for column in constraint.columns}
        for constraint in TABLE.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique["uq_webhook_name"] == {"name"}
    assert sum(isinstance(c, CheckConstraint) for c in TABLE.constraints) == 2


def test_the_migration_states_what_a_downgrade_costs() -> None:
    source = (migrations.migrations_dir() / "versions" / MIGRATION).read_text()
    assert "``webhook``" in source
    assert "twelfth" in source
    assert "deletes every" in source and "webhook" in source


@pytest.mark.parametrize(
    "overrides",
    [
        {"format": "xml"},
        {"max_hops": -1},
    ],
)
async def test_the_server_enforces_the_checks(
    database: Database, overrides: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        async with database.sessions() as session:
            session.add(_row(**overrides))
            await session.commit()


async def test_the_server_refuses_a_second_webhook_with_the_same_name(
    database: Database,
) -> None:
    async with database.sessions() as session:
        session.add(_row(max_hops=0))
        await session.commit()
    with pytest.raises(IntegrityError):
        async with database.sessions() as session:
            session.add(_row(format="discord"))
            await session.commit()


async def test_0006_upgrade_downgrade_upgrade_leaves_no_leftover_objects(
    database_url: str,
) -> None:
    schema = f"{SCHEMA_PREFIX}webhook_cycle"
    config = DatabaseConfig(url=database_url, schema=schema)
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    try:
        await migrations.upgrade_async(config)
        assert await _objects_named_webhook(database_url, schema)

        await migrations.downgrade_async(config, revision="0005")
        assert await _objects_named_webhook(database_url, schema) == set(), (
            "a downgrade to 0005 must leave no table, index or constraint behind"
        )

        await migrations.upgrade_async(config)
        handle = Database(config=config)
        try:
            assert await handle.read_applied_revision() == migrations.expected_revision()
        finally:
            await handle.dispose()
    finally:
        await _drop_schema(database_url, schema)


async def _objects_named_webhook(url: str, schema: str) -> set[str]:
    connection = await _connect(url)
    try:
        rows = await connection.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname LIKE '%webhook%'",
            schema,
        )
        constraints = await connection.fetch(
            "SELECT conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = $1 AND conname LIKE '%webhook%'",
            schema,
        )
        return {row[0] for row in rows} | {row[0] for row in constraints}
    finally:
        await connection.close()
