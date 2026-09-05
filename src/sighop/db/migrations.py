"""Alembic as the only schema authority (design D5).

`alembic upgrade head` is a deliberate act — `sighop db upgrade` — and never a
side effect of starting the node. `sighop run` compares the database's applied
revision against the revision compiled into the code and refuses to start when
they differ, naming both and the command that reconciles them.

Auto-migrating on startup is what turns a rollback into an outage: an old binary
restarted after a failed deploy meets a schema it does not know, and a new binary
racing another instance applies DDL twice. Refusing is one line of operational
friction and removes the whole class.

**Why the bridge.** asyncpg has no synchronous mode and Alembic's migration
runner is synchronous by construction, so `alembic/env.py` drives the migration
through `connection.run_sync` (design D1). Alembic's own commands manage an event
loop, so every entry point here that a coroutine might call goes through a
thread: starting a second loop inside a running one is the failure that pattern
is otherwise guaranteed to produce.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

import sighop
from alembic import command
from sighop.config import DatabaseConfig

ALEMBIC_DIR_VARIABLE = "SIGHOP_ALEMBIC_DIR"
"""Where the migration chain lives, when it is not beside the source tree. The
container that packages sighop is milestone 9's; until then the repository
checkout is the deployment and this is the escape hatch."""

UPGRADE_COMMAND = "sighop db upgrade"
"""Named in every version-mismatch message. One command, quoted identically
everywhere, because an error that describes a fix vaguely is a fix nobody applies."""


class MigrationsNotFoundError(RuntimeError):
    """The migration chain could not be located. Names where it was looked for."""


def migrations_dir() -> Path:
    """The `alembic/` directory, as an explicit override or beside the source."""
    override = os.environ.get(ALEMBIC_DIR_VARIABLE)
    candidates = []
    if override:
        candidates.append(Path(override))
    # src/sighop/__init__.py → src/sighop → src → the checkout root.
    candidates.append(Path(sighop.__file__).resolve().parents[2] / "alembic")
    candidates.append(Path.cwd() / "alembic")
    for candidate in candidates:
        if (candidate / "env.py").is_file():
            return candidate
    looked = ", ".join(str(candidate) for candidate in candidates)
    raise MigrationsNotFoundError(
        f"no alembic/env.py found (looked in {looked}); set {ALEMBIC_DIR_VARIABLE} "
        "to the migration directory"
    )


def alembic_config(database: DatabaseConfig | None = None) -> AlembicConfig:
    """An Alembic configuration taking its URL from `config.py`, not `alembic.ini`.

    The configuration's `schema` travels with it: the role cannot `CREATEDB`, so
    a test session isolates in a throwaway *schema* and the version table goes
    into it beside the tables it versions (design D11).
    """
    directory = migrations_dir()
    config = AlembicConfig(str(directory.parent / "alembic.ini"))
    config.set_main_option("script_location", str(directory))
    config.attributes["sighop_database"] = database
    config.attributes["sighop_schema"] = None if database is None else database.schema
    return config


def expected_revision() -> str:
    """The head of the migration chain that ships with this code."""
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:  # pragma: no cover - the chain is committed with the code
        raise MigrationsNotFoundError("the migration chain has no head revision")
    return head


def knows_revision(revision: str) -> bool:
    """Whether this code's migration chain contains a revision at all.

    A database ahead of the code answers False here, and that is a different
    failure from being behind: nothing in this checkout describes the schema.
    """
    try:
        ScriptDirectory.from_config(alembic_config()).get_revision(revision)
    except Exception:
        return False
    return True


def upgrade(database: DatabaseConfig, *, revision: str = "head") -> None:
    """Apply outstanding migrations. Synchronous — see the module docstring."""
    command.upgrade(alembic_config(database), revision)


def downgrade(database: DatabaseConfig, *, revision: str = "base") -> None:
    """Unwind migrations. Tested, because an untested downgrade does not exist."""
    command.downgrade(alembic_config(database), revision)


async def upgrade_async(database: DatabaseConfig, *, revision: str = "head") -> None:
    """`upgrade` from inside a running loop, in a thread that owns its own."""
    await asyncio.to_thread(upgrade, database, revision=revision)


async def downgrade_async(database: DatabaseConfig, *, revision: str = "base") -> None:
    await asyncio.to_thread(downgrade, database, revision=revision)
