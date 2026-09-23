"""The engine, the session factory, and the containment around both.

Three properties this module owns, and each is a spec requirement rather than a
convenience:

* **A database fault is contained at the repository boundary** (design D8).
  `Database.run` returns an outcome; it does not raise into the RX path, the
  scheduler or the bus. A failure increments a counter, emits an `error` wide
  event naming the operation, and sets a `degraded` flag the status line reports
  — distinctly from "persistence off", because "off" is a choice and "degraded"
  is a fault.
* **Every operation is bounded in time** (design D16). The database is a
  NodePort on another host, and a host that is *down blackholes packets rather
  than refusing them*, so a connect does not fail fast; asyncpg's default is
  60 s. The connect and statement bounds go to the driver, the pool bound to the
  pool, and `run` wraps the lot in a ceiling so no caller can wait past it even
  against a sink that answers nothing at all.
* **Recovery is probed for, not waited for** (design D15). A flag that cleared
  only when some write happened to succeed would keep reporting `degraded` for
  hours on a node whose adverts are 24 h apart — a stuck needle. While degraded,
  a bounded-interval probe with capped backoff is what clears the flag and fires
  the recovery hook the contact backfill hangs off.

Failure modes are distinguished rather than lumped together, because the
remedies differ: an unreachable host, rejected credentials, the role's
connection limit (observed during the probe for this design) and a schema
version mismatch each map to their own reported cause.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sighop.config import DatabaseConfig
from sighop.db import migrations
from sighop.logging import Logger, get_logger

DEFAULT_PROBE_INTERVAL_SECONDS = 30.0
"""How soon a degraded runtime looks again. Short enough that an operator
watching the status line sees the fault clear, long enough that a database that
is down for an hour is not hammered by one process."""

MAX_PROBE_INTERVAL_SECONDS = 300.0
PROBE_BACKOFF_FACTOR = 2.0

Sleep = Callable[[float], Awaitable[None]]


# --- Failures ---------------------------------------------------------------


class DatabaseError(RuntimeError):
    """A database operation that did not succeed. Never carries the password."""

    def __init__(self, message: str, *, cause: str = "database_error") -> None:
        super().__init__(message)
        self.cause = cause


class DatabaseUnavailableError(DatabaseError):
    """The server could not be reached, refused the connection, or never answered."""

    def __init__(self, message: str) -> None:
        super().__init__(message, cause="unreachable")


class CredentialsRejectedError(DatabaseError):
    """The server rejected the role or its password. Names the role, not the password."""

    def __init__(self, message: str) -> None:
        super().__init__(message, cause="credentials_rejected")


class ConnectionLimitError(DatabaseError):
    """The role's connection limit is reached — a different fault from an
    unreachable server, with a different remedy, so it is reported separately.
    Observed as `FATAL: too many connections for role` while probing for this
    design, which is why the distinction is in the spec."""

    def __init__(self, message: str) -> None:
        super().__init__(message, cause="connection_limit")


class SchemaVersionError(DatabaseError):
    """The database is not at the revision this code expects (design D5)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, cause="schema_version")


_AUTH_SQLSTATES = frozenset({"28P01", "28000", "3D000"})
_TOO_MANY_CONNECTIONS_SQLSTATE = "53300"


def classify(exc: BaseException, *, config: DatabaseConfig, operation: str) -> DatabaseError:
    """One driver exception to one reported cause.

    The remedies differ — start the server, fix the credentials, wait for a
    connection to free up, run the migrations — so conflating them would send an
    operator to the wrong one.
    """
    if isinstance(exc, DatabaseError):
        return exc
    original: BaseException = exc
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        original = exc.orig

    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    where = f"{config.host}:{config.port}/{config.database}"

    if sqlstate == _TOO_MANY_CONNECTIONS_SQLSTATE:
        return ConnectionLimitError(
            f"{operation}: the server refused a connection because role "
            f"{config.username!r} has reached its connection limit at {where}; "
            "another instance, an Alembic run or a psql session is holding them"
        )
    if sqlstate in _AUTH_SQLSTATES:
        return CredentialsRejectedError(
            f"{operation}: the server at {where} rejected role {config.username!r} "
            f"({_describe(original)})"
        )
    if isinstance(original, TimeoutError | asyncio.TimeoutError):
        return DatabaseUnavailableError(
            f"{operation}: {where} did not answer within the configured bound "
            f"({config.connect_timeout:g}s connect, {config.statement_timeout:g}s "
            "statement); a host that is down blackholes rather than refuses"
        )
    if isinstance(original, OSError):
        return DatabaseUnavailableError(
            f"{operation}: could not reach {where} ({_describe(original)})"
        )
    if isinstance(original, SQLAlchemyError | Exception):
        return DatabaseError(
            f"{operation}: {where} reported {_describe(original)}",
            cause="database_error",
        )
    raise AssertionError(f"unclassifiable {exc!r}")  # pragma: no cover


def _describe(exc: BaseException) -> str:
    """A one-line description that cannot carry a password: the class and its
    message, both of which the driver builds from the server's own reply."""
    message = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}" if str(exc).strip() else message


# --- Outcomes ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Succeeded[T]:
    value: T

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Failed:
    operation: str
    error: DatabaseError

    @property
    def ok(self) -> bool:
        return False

    @property
    def cause(self) -> str:
        return self.error.cause


type Outcome[T] = Succeeded[T] | Failed


# --- Counters ---------------------------------------------------------------


@dataclass(slots=True)
class PersistenceStats:
    """What the status line reports about durability.

    Every counter reads zero rather than being omitted when nothing has gone
    wrong: a field that disappears when it is zero is a field a reader cannot
    tell from a field nobody wrote (packet-log spec).
    """

    operations: int = 0
    failures: int = 0
    packet_log_written: int = 0
    packet_log_discarded: int = 0
    routes_written: int = 0
    routes_discarded: int = 0
    contacts_written: int = 0
    direct_messages_written: int = 0
    direct_messages_discarded: int = 0
    """A conversation entry that never reached the database. Counted apart from
    the packet log's because it is not the same kind of loss: a dropped packet
    log row is a gap in a sample, and a dropped message is a gap in what somebody
    said (milestone 8 design D8)."""

    channel_messages_written: int = 0
    channel_messages_discarded: int = 0
    recoveries: int = 0
    probes: int = 0

    def as_json(self) -> dict[str, object]:
        return {
            "db_operations": self.operations,
            "db_failures": self.failures,
            "packet_log_written": self.packet_log_written,
            "packet_log_discarded": self.packet_log_discarded,
            "routes_written": self.routes_written,
            "routes_discarded": self.routes_discarded,
            "contacts_written": self.contacts_written,
            "direct_messages_written": self.direct_messages_written,
            "direct_messages_discarded": self.direct_messages_discarded,
            "channel_messages_written": self.channel_messages_written,
            "channel_messages_discarded": self.channel_messages_discarded,
            "db_recoveries": self.recoveries,
            "db_probes": self.probes,
        }


# --- The database -----------------------------------------------------------


@dataclass(slots=True)
class Database:
    """One engine, its sessions, and the containment around every use of them."""

    config: DatabaseConfig
    logger: Logger | None = None
    sleep: Sleep = asyncio.sleep
    probe_interval: float = DEFAULT_PROBE_INTERVAL_SECONDS
    max_probe_interval: float = MAX_PROBE_INTERVAL_SECONDS

    stats: PersistenceStats = field(default_factory=PersistenceStats)
    degraded: bool = False
    last_error: str = ""
    applied_revision: str | None = None

    _engine: AsyncEngine | None = field(default=None, init=False)
    _sessions: async_sessionmaker[AsyncSession] | None = field(default=None, init=False)
    _recovery_hooks: list[Callable[[], Awaitable[None]]] = field(default_factory=list, init=False)
    _probe_task: asyncio.Task[None] | None = field(default=None, init=False)
    _current_probe_interval: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="db")
        self._current_probe_interval = self.probe_interval

    # --- Lifecycle ---------------------------------------------------------

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self.config.url,
                pool_size=self.config.pool_size,
                max_overflow=self.config.max_overflow,
                pool_timeout=self.config.pool_timeout,
                pool_pre_ping=self.config.pool_pre_ping,
                connect_args=self.config.connect_args(),
            )
            self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        return self._engine

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        self.engine  # noqa: B018 - builds the factory on first use
        assert self._sessions is not None
        return self._sessions

    @property
    def operation_timeout(self) -> float:
        """The ceiling no operation may exceed, whatever it is waiting on.

        The driver's own bounds cover a real server that stopped answering; this
        covers everything else, including a sink that never answers at all.
        """
        return self.config.connect_timeout + self.config.statement_timeout

    async def dispose(self) -> None:
        """Close every pooled connection. Idempotent, and safe to call twice."""
        await self.stop_probe()
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessions = None

    # --- Startup -----------------------------------------------------------

    async def open(self) -> None:
        """Connect and check the schema version, or fail startup saying why.

        A configured database that cannot be reached is a startup failure, never
        a silent fall back to in-memory operation: falling back would present a
        running node whose durability an operator had already assumed.
        """
        assert self.logger is not None
        try:
            async with asyncio.timeout(self.operation_timeout):
                async with self.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        except Exception as exc:
            raise classify(exc, config=self.config, operation="connect") from exc
        await self.check_schema_version()
        self.degraded = False
        self.logger.info(
            "database_opened",
            outcome="success",
            schema_version=self.applied_revision,
            **self.config.as_json(),
        )

    async def read_applied_revision(self) -> str | None:
        """The revision the database says it is at, or None for no migrations."""

        def _read(connection: object) -> str | None:
            from alembic.runtime.migration import MigrationContext

            return MigrationContext.configure(connection).get_current_revision()  # type: ignore[arg-type]

        try:
            async with asyncio.timeout(self.operation_timeout):
                async with self.engine.connect() as connection:
                    return await connection.run_sync(_read)
        except Exception as exc:
            raise classify(exc, config=self.config, operation="read_schema_version") from exc

    async def check_schema_version(self) -> None:
        """Refuse a database that is not at the revision this code expects.

        Behind and ahead are separate failures. Behind has a fix — apply the
        outstanding migrations — and ahead does not: nothing in this checkout
        describes the schema, and operating against it would corrupt silently.
        """
        expected = migrations.expected_revision()
        applied = await self.read_applied_revision()
        self.applied_revision = applied
        if applied == expected:
            return
        if applied is None:
            raise SchemaVersionError(
                f"the database at {self.config.host}:{self.config.port}/"
                f"{self.config.database} has no schema version recorded (no migrations "
                f"applied); this code expects revision {expected}. To apply them, "
                f"{migrations.RESTART_TO_MIGRATE}"
            )
        if not migrations.knows_revision(applied):
            raise SchemaVersionError(
                f"the database is at revision {applied}, which this code does not know; "
                f"it expects {expected}. Refusing to operate against an unknown schema "
                "rather than guessing what it holds — run a build that knows it"
            )
        raise SchemaVersionError(
            f"the database is at revision {applied} and this code expects {expected}; "
            f"to apply the outstanding migrations, {migrations.RESTART_TO_MIGRATE}"
        )

    # --- Bounded, contained operations -------------------------------------

    async def run[T](
        self, operation: str, work: Callable[[AsyncSession], Awaitable[T]]
    ) -> Outcome[T]:
        """Run one unit of database work. Returns an outcome; never raises.

        This is the boundary design D8 describes. Everything downstream of it —
        the RX path, the scheduler, the bus — sees a value or a `Failed`, and a
        `Failed` is information rather than an exception to handle.
        """
        assert self.logger is not None
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.operation_timeout):
                async with self.sessions() as session:
                    result = await work(session)
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = classify(exc, config=self.config, operation=operation)
            self._record_failure(operation, error, time.monotonic() - started)
            return Failed(operation=operation, error=error)
        self._record_success()
        return Succeeded(value=result)

    def _record_failure(self, operation: str, error: DatabaseError, elapsed: float) -> None:
        assert self.logger is not None
        self.stats.operations += 1
        self.stats.failures += 1
        self.last_error = str(error)
        was_degraded = self.degraded
        self.degraded = True
        self.logger.error(
            "database_operation_failed",
            outcome="error",
            duration_ms=round(elapsed * 1000, 3),
            operation=operation,
            cause=error.cause,
            error=str(error),
            newly_degraded=not was_degraded,
            **self.config.as_json(),
        )

    def _record_success(self) -> None:
        self.stats.operations += 1
        if self.degraded:
            # A write that lands is as good a signal as a probe, and clearing
            # here keeps the flag from outliving the fault by up to one interval.
            self._clear_degraded(source="write")

    # --- Degraded state and its probe (design D15) -------------------------

    def on_recovery(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Run this when the database comes back. The contact backfill hangs here."""
        self._recovery_hooks.append(hook)

    def _clear_degraded(self, *, source: str) -> None:
        assert self.logger is not None
        self.degraded = False
        self._current_probe_interval = self.probe_interval
        self.stats.recoveries += 1
        self.logger.info("database_recovered", outcome="success", detected_by=source)

    async def probe(self) -> bool:
        """One lightweight connectivity check. Writes nothing, ever.

        A probe that wrote would make recovery detection depend on there being
        something to write, which is precisely the condition design D15 says
        cannot be relied on: adverts are hours apart.
        """
        self.stats.probes += 1
        try:
            async with asyncio.timeout(self.operation_timeout):
                async with self.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = classify(exc, config=self.config, operation="probe")
            self.last_error = str(error)
            self._current_probe_interval = min(
                self._current_probe_interval * PROBE_BACKOFF_FACTOR,
                self.max_probe_interval,
            )
            return False
        return True

    @property
    def current_probe_interval(self) -> float:
        return self._current_probe_interval

    async def probe_once(self) -> bool:
        """Probe, and on success clear the flag and run the recovery hooks."""
        if not self.degraded:
            return True
        if not await self.probe():
            return False
        self._clear_degraded(source="probe")
        await self._run_recovery_hooks()
        return True

    async def _run_recovery_hooks(self) -> None:
        assert self.logger is not None
        for hook in list(self._recovery_hooks):
            try:
                await hook()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A hook that fails must not undo the recovery or stop the next
                # hook: the flag reflects the database, not the backfill.
                self.logger.error("database_recovery_hook_failed", outcome="error", error=repr(exc))

    async def probe_loop(self) -> None:
        """Wake at a bounded interval and, while degraded, look for the database."""
        while True:
            await self.sleep(self._current_probe_interval if self.degraded else self.probe_interval)
            if self.degraded:
                await self.probe_once()

    def start_probe(self) -> None:
        if self._probe_task is None:
            self._probe_task = asyncio.create_task(self.probe_loop(), name="db-probe")

    async def stop_probe(self) -> None:
        task, self._probe_task = self._probe_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # --- Reporting ---------------------------------------------------------

    @property
    def state(self) -> str:
        """`on` or `degraded`. `off` is the absence of a database entirely, and
        is the runtime's word rather than this object's — "off" is a choice and
        "degraded" is a fault, and the status line must not blur them."""
        return "degraded" if self.degraded else "on"

    def as_json(self) -> dict[str, object]:
        return {
            "persistence": self.state,
            "schema_version": self.applied_revision,
            **self.stats.as_json(),
            **self.config.as_json(),
        }
