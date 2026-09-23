"""The thirteenth and fourteenth tables and their migration (channel-messaging task 5.1)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import CheckConstraint, Table, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError

from sighop.config import DatabaseConfig
from sighop.db import migrations
from sighop.db.engine import Database
from sighop.db.models import Channel, ChannelMessage
from sighop.protocol.crypto import PUBLIC_CHANNEL_KEY
from tests.dbfixtures import SCHEMA_PREFIX, _connect, _create_schema, _drop_schema

MIGRATION = "0007_channels.py"
CHANNEL: Table = Channel.__table__  # type: ignore[assignment]
MESSAGE: Table = ChannelMessage.__table__  # type: ignore[assignment]
NOW = dt.datetime(2026, 9, 14, 18, 0, tzinfo=dt.UTC)


def test_the_models_declare_the_named_constraints() -> None:
    assert {
        "uq_channel_name",
        "ck_channel_kind",
        "ck_channel_hashtag",
        "ck_channel_sealed_key",
        "ck_channel_channel_hash",
    } <= {c.name for c in CHANNEL.constraints}
    unique = {
        c.name: {column.name for column in c.columns}
        for c in MESSAGE.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert unique["uq_channel_message_channel_id_ref"] == {"channel_id", "ref"}
    assert sum(isinstance(c, CheckConstraint) for c in MESSAGE.constraints) == 4
    [foreign] = MESSAGE.c.channel_id.foreign_keys
    assert foreign.ondelete == "CASCADE"


def test_the_seeded_hash_is_the_public_key_hash() -> None:
    source = (migrations.migrations_dir() / "versions" / MIGRATION).read_text()
    assert "PUBLIC_CHANNEL_HASH = 0x11" in source
    assert PUBLIC_CHANNEL_KEY.channel_hash == 0x11


def test_the_migration_states_unencrypted_text_and_what_a_downgrade_costs() -> None:
    source = (migrations.migrations_dir() / "versions" / MIGRATION).read_text()
    assert "``channel``" in source and "``channel_message``" in source
    assert "thirteenth and fourteenth" in source
    assert "not encrypted at rest" in source
    assert "deletes every" in source and "channel history" in source


async def test_an_upgraded_database_holds_exactly_the_public_channel(database: Database) -> None:
    async with database.sessions() as session:
        rows = (await session.execute(select(Channel))).scalars().all()
    assert [(row.name, row.kind, row.channel_hash, row.sealed_key) for row in rows] == [
        ("Public", "public", 0x11, None)
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "secret"},
        {"kind": "hashtag"},
        {"kind": "psk"},
        {"kind": "public", "hashtag": "#nope"},
        {"channel_hash": 256},
    ],
)
async def test_the_server_enforces_the_channel_checks(
    database: Database, fields: dict[str, object]
) -> None:
    row = {"name": "x", "kind": "public", "channel_hash": 1, "created_at": NOW, **fields}
    with pytest.raises(IntegrityError):
        async with database.sessions() as session:
            session.add(Channel(**row))
            await session.commit()


async def test_removing_a_channel_cascades_to_its_messages(database: Database) -> None:
    async with database.sessions() as session:
        channel = Channel(
            name="#dev", kind="hashtag", hashtag="#dev", channel_hash=3, created_at=NOW
        )
        session.add(channel)
        await session.flush()
        session.add(
            ChannelMessage(
                channel_id=channel.id,
                direction="in",
                ref="p1",
                text=b"hi",
                wire_timestamp=1,
                handled_at=NOW,
                outcome="received",
            )
        )
        await session.commit()
        await session.delete(channel)
        await session.commit()
        remaining = (await session.execute(select(ChannelMessage))).scalars().all()
    assert remaining == []


async def test_0007_upgrade_downgrade_upgrade_leaves_no_leftover_objects(
    database_url: str,
) -> None:
    schema = f"{SCHEMA_PREFIX}channel_cycle"
    config = DatabaseConfig(url=database_url, schema=schema)
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    try:
        await migrations.upgrade_async(config)
        assert await _objects_named_channel(database_url, schema)

        await migrations.downgrade_async(config, revision="0006")
        assert await _objects_named_channel(database_url, schema) == set(), (
            "a downgrade to 0006 must leave no table, index or constraint behind"
        )

        await migrations.upgrade_async(config)
        handle = Database(config=config)
        try:
            assert await handle.read_applied_revision() == migrations.expected_revision()
        finally:
            await handle.dispose()
    finally:
        await _drop_schema(database_url, schema)


async def _objects_named_channel(url: str, schema: str) -> set[str]:
    connection = await _connect(url)
    try:
        rows = await connection.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname LIKE '%channel%'",
            schema,
        )
        constraints = await connection.fetch(
            "SELECT conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = $1 AND conname LIKE '%channel%'",
            schema,
        )
        return {row[0] for row in rows} | {row[0] for row in constraints}
    finally:
        await connection.close()
