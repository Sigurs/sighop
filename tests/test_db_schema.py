"""Schema, migrations and the harness that runs them (design D5, D11).

The migration chain is exercised by the same command the deployment runs — there
is no `create_all()` anywhere, in application code or here, because a schema
built two ways is a schema whose migrations are never tested until they meet
real data.
"""

from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy import text

import sighop
from sighop.config import DatabaseConfig
from sighop.db import migrations
from sighop.db.engine import Database
from tests.dbfixtures import SCHEMA_PREFIX, _connect, _create_schema, _drop_schema

TABLES = ("entity", "contact", "path", "packet_log")
ROOM_TABLES = ("room", "room_member", "message")
BOT_TABLES = ("bot", "bot_state")
DM_TABLES = ("direct_message",)
WEB_TABLES = ("web_user",)
WEBHOOK_TABLES = ("webhook",)


# --- 2.5 Migrations are the only schema authority ---------------------------


def test_no_application_code_calls_create_all() -> None:
    """Schema comes only from migrations (design D5).

    A static scan rather than a runtime check, in the same spirit as the
    single-`Data`-send assertion: `create_all()` on a path nobody exercised in
    testing is exactly the one that would run in production.
    """
    package = Path(sighop.__file__).parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Attribute) and node.attr in {"create_all", "drop_all"}) or (
                isinstance(node, ast.Name) and node.id in {"create_all", "drop_all"}
            ):
                offenders.append(f"{path.relative_to(package)}:{node.lineno}")
    assert not offenders, (
        f"{offenders} build schema from application code; migrations are the only "
        "authority on schema (design D5)"
    )


def test_the_migration_chain_has_one_head_the_code_expects() -> None:
    assert migrations.expected_revision() == "0009"
    assert migrations.knows_revision("0001")
    assert migrations.knows_revision("0002")
    assert migrations.knows_revision("0003")
    assert migrations.knows_revision("0004")
    assert migrations.knows_revision("0005")
    assert migrations.knows_revision("0006")
    assert migrations.knows_revision("0007")
    assert migrations.knows_revision("0008")
    assert migrations.knows_revision("0009")
    assert not migrations.knows_revision("beef")


def test_the_initial_migration_names_the_tables_it_deliberately_omits() -> None:
    """2.3: absence reads as intent, naming the milestone that owns each."""
    source = (migrations.migrations_dir() / "versions" / "0001_initial_schema.py").read_text()
    for table, milestone in (
        ("room", "milestone 6"),
        ("room_member", "milestone 6"),
        ("message", "milestone 6"),
        ("bot_state", "milestone 7"),
    ):
        assert f"``{table}``" in source, f"{table} is not named as deliberately absent"
        assert milestone in source


def test_the_room_migration_records_what_now_exists_and_what_still_does_not() -> None:
    """1.5: absence still reads as intent once three of the four arrive."""
    source = (migrations.migrations_dir() / "versions" / "0002_room_server.py").read_text()
    for table in ROOM_TABLES:
        assert f"``{table}``" in source, f"{table} is not named as now existing"
    assert "``bot_state``" in source, "bot_state's absence is no longer stated as intent"
    assert "milestone 7" in source


# --- 2.2 / 2.4 The migration against a real server --------------------------


async def test_the_tables_exist_with_timestamptz_and_a_non_unique_node_hash(
    database: Database, test_schema: str, database_url: str
) -> None:
    async with database.sessions() as session:
        present = set(
            (
                await session.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = :schema"),
                    {"schema": test_schema},
                )
            ).scalars()
        )
        assert set(TABLES) <= present
        assert set(ROOM_TABLES) <= present
        assert set(BOT_TABLES) <= present
        assert set(DM_TABLES) <= present
        assert set(WEB_TABLES) <= present
        assert set(WEBHOOK_TABLES) <= present

        # Every timestamp column carries a time zone (design D7): the dev server's
        # own TimeZone is Europe/Helsinki, so a naive column would record local
        # wall-clock time and the same code elsewhere would record another instant.
        naive = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND data_type = "
                    "'timestamp without time zone'"
                ),
                {"schema": test_schema},
            )
        ).all()
        assert naive == []

        # node_hash is indexed and NOT unique on any of the three tables that
        # carry one: a byte collides at 1 in 256 and the design is built on
        # candidate sets (§3). Two members of one room may share one.
        for table in ("entity", "contact", "room_member"):
            indexes = (
                (
                    await session.execute(
                        text(
                            "SELECT indexdef FROM pg_indexes WHERE schemaname = :schema "
                            "AND tablename = :table AND indexdef LIKE '%node_hash%'"
                        ),
                        {"schema": test_schema, "table": table},
                    )
                )
                .scalars()
                .all()
            )
            assert indexes, f"{table}.node_hash is not indexed"
            assert not any("UNIQUE" in definition for definition in indexes)


async def test_upgrade_downgrade_upgrade_leaves_the_schema_at_head(
    database_url: str,
) -> None:
    """2.4: an untested `downgrade()` is a downgrade that does not exist."""
    schema = f"{SCHEMA_PREFIX}cycle"
    config = DatabaseConfig(url=database_url, schema=schema)
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    try:
        every = set(TABLES) | set(ROOM_TABLES) | set(BOT_TABLES) | set(DM_TABLES) | set(WEB_TABLES)

        await migrations.upgrade_async(config)
        assert await _tables_in(database_url, schema) >= every

        await migrations.downgrade_async(config)
        left = await _tables_in(database_url, schema)
        assert not (left & every), f"downgrade left {sorted(left & every)}"

        await migrations.upgrade_async(config)
        assert await _tables_in(database_url, schema) >= every

        handle = Database(config=config)
        try:
            assert await handle.read_applied_revision() == migrations.expected_revision()
        finally:
            await handle.dispose()
    finally:
        await _drop_schema(database_url, schema)


async def _tables_in(url: str, schema: str) -> set[str]:
    connection = await _connect(url)
    try:
        rows = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = $1", schema
        )
        return {row[0] for row in rows}
    finally:
        await connection.close()


# --- 9.1 / 9.2 / 9.4 The harness itself -------------------------------------


async def test_the_session_schema_is_named_for_the_prefix_and_carries_the_chain(
    test_schema: str, database_url: str
) -> None:
    assert test_schema.startswith(SCHEMA_PREFIX)
    assert await _tables_in(database_url, test_schema) >= set(TABLES)


async def test_a_stale_schema_is_dropped_at_session_start(
    database_url: str, test_schema: str
) -> None:
    """9.2: a crashed run must not accumulate schemas run after run."""
    from tests.dbfixtures import _drop_stale_schemas

    stale = f"{SCHEMA_PREFIX}stale_by_hand"
    await _create_schema(database_url, stale)
    try:
        assert stale in await _schemas(database_url)
        # `keep` is this live session's own schema: the real sweep runs before
        # one exists, and without it this test would drop the schema it is
        # running inside.
        dropped = await _drop_stale_schemas(database_url, keep=frozenset({test_schema}))
        assert stale in dropped
        assert test_schema not in dropped
        assert stale not in await _schemas(database_url)
    finally:
        await _drop_schema(database_url, stale)


async def _schemas(url: str) -> set[str]:
    connection = await _connect(url)
    try:
        rows = await connection.fetch("SELECT nspname FROM pg_namespace")
        return {row[0] for row in rows}
    finally:
        await connection.close()


def test_the_suite_stays_inside_the_roles_connection_budget(
    database_config: DatabaseConfig,
) -> None:
    """9.4: the role's limit is 30 and the runtime's own budget is 10 (design D6)."""
    from sighop.config import ROLE_CONNECTION_LIMIT

    assert database_config.max_connections <= 4
    assert database_config.max_connections * 2 < ROLE_CONNECTION_LIMIT


async def test_the_fixture_disposes_its_engine(database_config: DatabaseConfig) -> None:
    handle = Database(config=database_config)
    await handle.open()
    async with handle.sessions() as session:
        await session.execute(text("SELECT 1"))
    await handle.dispose()
    assert handle._engine is None
