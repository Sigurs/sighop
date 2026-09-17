"""`entity.sealed_private_key` and its migration (create-entity-with-known-key task 3.5).

The rename itself is one line of DDL. What is tested here is everything around
it: that it reads no key material and so needs no secret, that it says how many
identities it takes out of service, and that what it says carries a count and
nothing that identifies anybody.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import Table, text

from sighop.config import DatabaseConfig
from sighop.db import migrations
from sighop.db.engine import Database
from sighop.db.models import Entity
from sighop.db.sealing import SEAL_VERSION
from tests.dbfixtures import SCHEMA_PREFIX, _connect, _create_schema, _drop_schema

MIGRATION = "0008_private_key.py"
ENTITY: Table = Entity.__table__  # type: ignore[assignment]
NOW = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.UTC)
SECRET = bytes(range(32))


def _source() -> str:
    return (migrations.migrations_dir() / "versions" / MIGRATION).read_text()


def test_the_model_carries_the_renamed_column() -> None:
    assert "sealed_private_key" in ENTITY.c
    assert "sealed_seed" not in ENTITY.c


def test_the_migration_reads_no_key_material() -> None:
    """Its whole claim to running without the secret."""
    source = _source()
    assert "count(*)" in source
    assert "SELECT sealed" not in source
    assert "SecretBox" not in source and "open_private_key" not in source


def test_the_migration_states_what_stops_opening_and_what_a_downgrade_does() -> None:
    source = _source()
    assert "stop opening" in source
    assert "no key material is read" in source.lower()
    assert "keys import --private-key" in source
    assert "downgrade" in source.lower()


def test_the_report_names_nobody() -> None:
    """A count is operator-useful; a name in a deploy log is not (design D5)."""
    source = _source()
    report = source[source.index("def _report_stranded_rows") : source.index("def upgrade")]
    assert "SELECT count(*) FROM entity" in report
    for disclosing in ("name", "public_key", "sealed"):
        assert f"SELECT {disclosing}" not in report
    assert "keys list" in report, "the operator needs to be told where the names are"


async def _seed_one_entity(database: Database, name: str) -> None:
    async with database.sessions() as session:
        session.add(
            Entity(
                id=uuid.uuid4(),
                type="companion",
                name=name,
                public_key=os.urandom(32),
                node_hash=7,
                sealed_private_key=bytes([SEAL_VERSION]) + os.urandom(80),
                advert_config={},
                enabled=True,
                created_at=NOW,
            )
        )
        await session.commit()


@pytest.mark.database
async def test_the_upgraded_schema_has_the_renamed_column(database: Database) -> None:
    async with database.sessions() as session:
        columns = {
            row[0]
            for row in (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'entity' AND table_schema = current_schema()"
                    )
                )
            ).all()
        }
    assert "sealed_private_key" in columns
    assert "sealed_seed" not in columns


@pytest.mark.database
async def test_0008_renames_without_the_secret_and_reports_the_rows_it_strands(
    database_url: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade to 0007, put rows in, then apply 0008 with no secret in the environment."""
    monkeypatch.delenv("SIGHOP_SECRET_KEY", raising=False)
    schema = f"{SCHEMA_PREFIX}private_key_cycle"
    config = DatabaseConfig(url=database_url, schema=schema)
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    try:
        await migrations.upgrade_async(config, revision="0007")
        handle = Database(config=config)
        try:
            async with handle.sessions() as session:
                for name in ("roomy", "skogen"):
                    await session.execute(
                        text(
                            "INSERT INTO entity (id, type, name, public_key, node_hash, "
                            "sealed_seed, advert_config, enabled, created_at) VALUES "
                            "(:id, 'companion', :name, :pub, 7, :sealed, '{}', true, :now)"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "name": name,
                            "pub": os.urandom(32),
                            "sealed": bytes([SEAL_VERSION]) + os.urandom(72),
                            "now": NOW,
                        },
                    )
                await session.commit()
        finally:
            await handle.dispose()

        capsys.readouterr()
        await migrations.upgrade_async(config)
        reported = capsys.readouterr().out

        assert "2 stored identities hold a sealed seed" in reported
        assert "keys import --private-key" in reported
        assert "roomy" not in reported and "skogen" not in reported, (
            "the report must carry a count and no identity name"
        )

        connection = await _connect(database_url)
        try:
            columns = {
                row[0]
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE "
                    "table_schema = $1 AND table_name = 'entity'",
                    schema,
                )
            }
        finally:
            await connection.close()
        assert "sealed_private_key" in columns and "sealed_seed" not in columns

        await migrations.downgrade_async(config, revision="0007")
        connection = await _connect(database_url)
        try:
            reverted = {
                row[0]
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE "
                    "table_schema = $1 AND table_name = 'entity'",
                    schema,
                )
            }
            remaining = await connection.fetchval(
                f'SELECT count(*) FROM "{schema}".entity'
            )
        finally:
            await connection.close()
        assert "sealed_seed" in reverted, "the downgrade must reverse the rename"
        assert remaining == 2, "no row may be deleted by either direction"
    finally:
        await _drop_schema(database_url, schema)


@pytest.mark.database
async def test_an_empty_store_is_migrated_silently(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing to strand, nothing to say."""
    schema = f"{SCHEMA_PREFIX}private_key_empty"
    config = DatabaseConfig(url=database_url, schema=schema)
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    try:
        await migrations.upgrade_async(config, revision="0007")
        capsys.readouterr()
        await migrations.upgrade_async(config)
        assert "sealed seed" not in capsys.readouterr().out
    finally:
        await _drop_schema(database_url, schema)
