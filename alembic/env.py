"""Alembic's environment, async because sighop carries one driver (design D1).

asyncpg has no synchronous mode, so the migration runner — synchronous by
construction — cannot drive it directly. This bridges through
`connection.run_sync(do_run_migrations)`, which is the documented pattern and
the whole cost of the decision: no second driver, no second URL, no split
configuration.

The URL is not read from `alembic.ini`. It comes from `sighop.config`, which is
the one place that validates the driver and the query parameters and redacts the
password — and, when the test harness set one, from the throwaway schema it
passes in (design D11).
"""

from __future__ import annotations

import asyncio

from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from sighop.config import Config, ConfigError, DatabaseConfig
from sighop.db.models import Base

target_metadata = Base.metadata

_config = context.config
_schema: str | None = _config.attributes.get("sighop_schema")


def _database() -> DatabaseConfig:
    """The database to migrate: the caller's, else the environment's."""
    database = _config.attributes.get("sighop_database")
    if database is not None:
        return database
    database = Config.from_environment().database
    if database is None:
        raise ConfigError("no DATABASE_URL is configured, so there is no database to migrate")
    return database


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # The version table lives beside the tables it versions, so a throwaway
        # schema carries its own migration state and dropping it takes both.
        version_table_schema=_schema,
        include_schemas=_schema is not None,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    """Render the migration as SQL, for a review that never opens a connection."""
    database = _database()
    context.configure(
        url=database.url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=_schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        if _schema is not None:
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{_schema}"')
        context.run_migrations()


async def run_migrations_online() -> None:
    database = _database()
    engine = async_engine_from_config(
        {
            "sqlalchemy.url": database.url,
            # The same bounds every other operation has: a migration against a
            # host that blackholes must fail within them rather than wait for
            # asyncpg's 60 s default (design D16). The search path travels here
            # too, so an isolating test session's DDL lands in its own schema.
            "sqlalchemy.connect_args": database.connect_args(),
        },
        prefix="sqlalchemy.",
        # A migration wants one connection and no pool behind it; the pool's
        # budget (design D6) belongs to the runtime, which may be running.
        poolclass=pool.NullPool,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
