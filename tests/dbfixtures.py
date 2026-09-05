"""The database test harness (design D11).

The measured role's `rolcreatedb` is **false**, so the usual per-run test
database is not available. It does have `CREATE` on the database, so each
session creates a schema `sighop_test_<random>`, points every connection's
search path at it, runs the *real* migration chain into it — with Alembic's
version table in the same schema — and drops it with `CASCADE` at the end.

That is arguably better than a throwaway database: it is faster, it exercises
the migrations the deployment runs, and it cannot collide with another
developer's run. Its one hazard is a crashed run leaving a schema behind, which
is why they share a prefix and stale ones are dropped at session start.

Every database test is gated behind the `database` marker and skips when no test
database is configured, so the existing suite gains no database dependency.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio

from sighop.config import DatabaseConfig
from sighop.db import migrations
from sighop.db.engine import Database

TEST_DATABASE_URL_VARIABLE = "SIGHOP_TEST_DATABASE_URL"
"""Preferred over `DATABASE_URL` so a developer can point the suite somewhere
other than the database their runtime is using, without unsetting either."""

SCHEMA_PREFIX = "sighop_test_"

SKIP_REASON = (
    f"no test database configured: set {TEST_DATABASE_URL_VARIABLE} or DATABASE_URL "
    "(for example `uv run --env-file .env.dev pytest -m database`)"
)


def configured_url() -> str | None:
    return os.environ.get(TEST_DATABASE_URL_VARIABLE) or os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def database_url() -> str:
    url = configured_url()
    if url is None:
        pytest.skip(SKIP_REASON)
    return url


@pytest.fixture(scope="session")
def database_config(database_url: str, test_schema: str) -> DatabaseConfig:
    """The suite's own configuration: the dev database, its throwaway schema.

    `pool_size` is deliberately small. The role's connection limit is 30 and the
    runtime's own budget is 10 (design D6); a suite that opened the production
    default would be the thing that made `too many connections for role` real.
    """
    return DatabaseConfig(
        url=database_url,
        schema=test_schema,
        pool_size=2,
        max_overflow=2,
        pool_timeout=5.0,
    )


@pytest.fixture(scope="session")
def test_schema(database_url: str) -> Iterator[str]:
    """A throwaway schema with the real migration chain applied into it."""
    schema = f"{SCHEMA_PREFIX}{secrets.token_hex(4)}"
    asyncio.run(_drop_stale_schemas(database_url))
    asyncio.run(_create_schema(database_url, schema))
    try:
        migrations.upgrade(DatabaseConfig(url=database_url, schema=schema))
        yield schema
    finally:
        asyncio.run(_drop_schema(database_url, schema))


@pytest_asyncio.fixture
async def database(database_config: DatabaseConfig) -> AsyncIterator[Database]:
    """An opened `Database` on the throwaway schema, emptied before each test."""
    handle = Database(config=database_config)
    await handle.open()
    await _truncate(handle)
    try:
        yield handle
    finally:
        await handle.dispose()


# --- Raw connections, deliberately not through the engine -------------------
#
# Creating and dropping the schema cannot go through a `Database`, whose whole
# point is that its connections are already pointed at a schema that may not
# exist yet.


async def _connect(url: str) -> asyncpg.Connection:
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    return await asyncpg.connect(
        host=parsed.host,
        port=parsed.port,
        user=parsed.username,
        password=parsed.password,
        database=parsed.database,
        timeout=5,
    )


async def _create_schema(url: str, schema: str) -> None:
    connection = await _connect(url)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await connection.close()


async def _drop_schema(url: str, schema: str) -> None:
    connection = await _connect(url)
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await connection.close()


async def _drop_stale_schemas(url: str, *, keep: frozenset[str] = frozenset()) -> list[str]:
    """Remove schemas a crashed run left behind, so they cannot accumulate.

    Run before this session creates its own, so `keep` is empty in the normal
    path. It exists because the sweep is by prefix and cannot tell a crashed
    run's leftovers from a session that is still using one — two suites running
    against this database at the same moment would otherwise take each other's
    schema out. One consumer today (design D11's own note), and `keep` is what a
    caller inside a live session passes.
    """
    connection = await _connect(url)
    try:
        rows = await connection.fetch(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE $1",
            f"{SCHEMA_PREFIX}%",
        )
        dropped = [row[0] for row in rows if row[0] not in keep]
        for name in dropped:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        return dropped
    finally:
        await connection.close()


async def _truncate(handle: Database) -> None:
    from sqlalchemy import text

    async with handle.sessions() as session:
        await session.execute(text("TRUNCATE entity, contact, path, packet_log RESTART IDENTITY"))
        await session.commit()
