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

The database is not optional and no test skips for want of one (`database`
spec). The environment must name one — `SIGHOP_TEST_DATABASE_URL` or
`DATABASE_URL` — and a run that names neither fails at once, naming both, rather
than reporting a reduced suite as a passing one.

A container engine is deliberately not a route to a database here. The
development container carries no Docker socket, so a suite that started its own
server would be a suite that only ran on the host, and the per-run schema below
already gives every isolation property a throwaway server would: the migration
chain is applied into it, it is dropped with `CASCADE` when the run ends, and
its random name keeps two concurrent runs — or two developers on one shared
database — out of each other's tables.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from sighop.config import DatabaseConfig
from sighop.db import migrations
from sighop.db.engine import Database
from sighop.db.persistence import Persistence

TEST_DATABASE_URL_VARIABLE = "SIGHOP_TEST_DATABASE_URL"
"""Preferred over `DATABASE_URL` so a developer can point the suite somewhere
other than the database their runtime is using, without unsetting either."""

SCHEMA_PREFIX = "sighop_test_"

NO_DATABASE = (
    "the test suite requires PostgreSQL and no database is configured.\n"
    f"Set {TEST_DATABASE_URL_VARIABLE} or DATABASE_URL — for example\n"
    "`uv run --env-file .env.dev pytest`.\n"
    "The run creates a schema of its own in that database and drops it afterwards, "
    "so it leaves nothing behind."
)


def configured_url() -> str | None:
    return os.environ.get(TEST_DATABASE_URL_VARIABLE) or os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def database_url() -> str:
    """The database this run works in, or a failure that names how to give it one.

    `pytest.fail` rather than `pytest.skip`: a suite that cannot reach a database
    has not passed, and reporting it as skipped is how a green run comes to mean
    less than it appears to.
    """
    url = configured_url()
    if url is None:
        pytest.fail(NO_DATABASE, pytrace=False)
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


@pytest.fixture
def fresh_persistence(database_config: DatabaseConfig) -> Persistence:
    """A `Persistence` over the run's schema, emptied, and not yet used.

    Every runtime and every page has a database behind it (`database` spec), so
    a test that builds either carries a real one rather than `None`. It is
    emptied here, as the `database` fixture empties before each test, so a test
    cannot see another test's rows.

    Unpooled, because of where it is first used. A synchronous page test's
    client runs the application on its own thread and its own event loop, and an
    asyncpg connection belongs to the loop that opened it: a pooled one would
    outlive that loop, could not be closed from anywhere, and the role allows
    30. With no pool, every session closes its connection inside the loop that
    opened it, and nothing is left to dispose of.
    """
    asyncio.run(_emptied(database_config))
    return Persistence(database=UnpooledDatabase(config=database_config))


_default_persistence: Persistence | None = None


@pytest.fixture
def default_persistence(fresh_persistence: Persistence) -> Iterator[Persistence]:
    """Make `fresh_persistence` what helpers use when a test passes none.

    `stub_state()` and the runtime-building helpers take a `persistence`
    argument; a test that has rows to put in place first passes its own. The
    many that have none would otherwise need a fixture threaded through every
    helper between them and the constructor, so a module opts in once, at the
    top, where a reader sees it:

        pytestmark = pytest.mark.usefixtures("default_persistence")
    """
    global _default_persistence
    _default_persistence = fresh_persistence
    try:
        yield fresh_persistence
    finally:
        _default_persistence = None


def the_default_persistence() -> Persistence:
    """The persistence `default_persistence` provided, or a failure saying how to get one."""
    if _default_persistence is None:
        raise RuntimeError(
            "no Persistence for this test: pass persistence=..., or declare "
            'pytestmark = pytest.mark.usefixtures("default_persistence") in this module'
        )
    return _default_persistence


class UnpooledDatabase(Database):
    """`Database`, with a connection per session instead of a pool (see above)."""

    __slots__ = ()

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self.config.url, poolclass=NullPool, connect_args=self.config.connect_args()
            )
            self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        return self._engine


async def _emptied(config: DatabaseConfig) -> None:
    handle = Database(config=config)
    try:
        await _truncate(handle)
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
        # `room` cascades to `room_member` and `message`, `bot` cascades to
        # `bot_state`, and `entity` cascades to both `room` and `bot`; naming
        # all of them anyway keeps the statement readable as the list of what a
        # test may leave behind.
        await session.execute(
            text(
                "TRUNCATE entity, contact, path, packet_log, room, room_member, "
                "message, bot, bot_state, direct_message, web_user, webhook, channel, "
                "channel_message, repeater_collection, repeater_target, repeater_poll, "
                "repeater_neighbour, route_preference RESTART IDENTITY CASCADE"
            )
        )
        # Back to what an upgrade leaves: migration `0007` seeds the Public channel.
        await session.execute(
            text(
                "INSERT INTO channel (name, kind, channel_hash, created_at) "
                "VALUES ('Public', 'public', 17, now())"
            )
        )
        await session.commit()


async def strand_a_row(database: Database, name: str, secret: bytes) -> None:
    """Rewrite one identity row's key material as a pre-0008 build sealed it.

    What migration 0008 leaves behind for a row it could not carry forward
    (design D10): a public key still held, and a seed nothing can open.
    """
    import os

    from nacl.secret import SecretBox
    from sqlalchemy import text

    from sighop.db.sealing import SEAL_VERSION

    sealed = bytes([SEAL_VERSION]) + bytes(SecretBox(secret).encrypt(os.urandom(32)))
    async with database.sessions() as session:
        await session.execute(
            text("UPDATE entity SET sealed_private_key = :sealed WHERE name = :name"),
            {"sealed": sealed, "name": name},
        )
        await session.commit()
