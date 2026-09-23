"""Engine, session lifecycle, and the containment around both (section 3).

The tests that need a real server are marked `database` and skip without one.
The rest — classification, the degraded flag, the bounded operation, the
write-behind worker — are unit tests over fakes, because the behaviour they
check is what happens when the database *misbehaves*, and a real server is the
one thing that will not misbehave on demand.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

import asyncpg
import pytest
from sqlalchemy import delete, func, select, text

from sighop.config import DatabaseConfig
from sighop.db import migrations
from sighop.db.engine import (
    ConnectionLimitError,
    CredentialsRejectedError,
    Database,
    DatabaseUnavailableError,
    Failed,
    SchemaVersionError,
    Succeeded,
    classify,
)
from sighop.db.models import PacketLog
from sighop.db.persistence import Persistence
from sighop.db.times import NaiveDatetimeError, ensure_utc
from sighop.db.writer import WriteBehind
from tests.dbfixtures import _connect, _create_schema, _drop_schema

URL = "postgresql+asyncpg://role:secret@db.example:5432/sighop"
CONFIG = DatabaseConfig(url=URL)


class RecordingLogger:
    """Captures events at the two levels the wide-event contract allows."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", event, fields))

    def names(self) -> list[str]:
        return [name for _level, name, _fields in self.events]


# --- 3.1 Engine and session lifecycle ---------------------------------------


async def test_opening_and_disposing_leaves_no_connection_open(
    database_config: DatabaseConfig, database_url: str
) -> None:
    async def open_connections() -> int:
        connection = await _connect(database_url)
        try:
            return await connection.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'sighop'"
            )
        finally:
            await connection.close()

    before = await open_connections()
    handle = Database(config=database_config)
    await handle.open()
    async with handle.sessions() as session:
        await session.execute(text("SELECT 1"))
    assert await open_connections() > before
    await handle.dispose()
    assert await open_connections() == before


# --- 3.2 Time zones (design D7) ---------------------------------------------


def test_a_naive_datetime_is_refused_at_the_storage_boundary() -> None:
    with pytest.raises(NaiveDatetimeError) as excinfo:
        ensure_utc(dt.datetime(2026, 9, 5, 12, 0, 0), field="last_heard")
    assert "last_heard" in str(excinfo.value)


def test_an_aware_datetime_is_normalised_to_utc() -> None:
    helsinki = dt.timezone(dt.timedelta(hours=3))
    value = dt.datetime(2026, 9, 5, 15, 0, 0, tzinfo=helsinki)
    normalised = ensure_utc(value, field="at")
    assert normalised.tzinfo is dt.UTC
    assert normalised == value


async def test_a_timestamp_round_trips_as_the_same_instant(database: Database) -> None:
    """The server's own TimeZone is Europe/Helsinki, not UTC (design D7)."""
    async with database.sessions() as session:
        server_zone = (await session.execute(text("SHOW TimeZone"))).scalar_one()
    assert server_zone != "UTC", "this test is only meaningful on a non-UTC server"

    written = dt.datetime(2026, 9, 5, 9, 1, 12, 664000, tzinfo=dt.UTC)
    async with database.sessions() as session:
        session.add(
            PacketLog(
                packet_id="round-trip",
                direction="rx",
                at=written,
                outcome="parsed",
            )
        )
        await session.commit()
    async with database.sessions() as session:
        read = (
            await session.execute(select(PacketLog.at).where(PacketLog.packet_id == "round-trip"))
        ).scalar_one()
    assert read.tzinfo is not None
    assert read == written
    assert read.astimezone(dt.UTC) == written


# --- 3.3 Failure modes are distinguished ------------------------------------


def test_an_unreachable_host_is_its_own_cause() -> None:
    error = classify(ConnectionRefusedError(111, "refused"), config=CONFIG, operation="connect")
    assert isinstance(error, DatabaseUnavailableError)
    assert error.cause == "unreachable"
    assert "db.example:5432/sighop" in str(error)


def test_a_blackholed_host_is_reported_as_unreachable_within_the_bound() -> None:
    error = classify(TimeoutError(), config=CONFIG, operation="connect")
    assert isinstance(error, DatabaseUnavailableError)
    assert "blackholes rather than refuses" in str(error)


def test_rejected_credentials_are_their_own_cause_and_omit_the_password() -> None:
    error = classify(
        asyncpg.exceptions.InvalidPasswordError("password authentication failed"),
        config=CONFIG,
        operation="connect",
    )
    assert isinstance(error, CredentialsRejectedError)
    assert error.cause == "credentials_rejected"
    assert "role" in str(error)
    assert "secret" not in str(error)


def test_the_role_connection_limit_is_reported_separately_from_unreachable() -> None:
    """Observed as `FATAL: too many connections for role` while probing for the
    design; the remedy differs from an unreachable server, so the message must."""
    error = classify(
        asyncpg.exceptions.TooManyConnectionsError("too many connections for role"),
        config=CONFIG,
        operation="connect",
    )
    assert isinstance(error, ConnectionLimitError)
    assert error.cause == "connection_limit"
    assert "connection limit" in str(error)


def test_a_schema_version_mismatch_keeps_its_own_cause() -> None:
    original = SchemaVersionError("behind")
    assert classify(original, config=CONFIG, operation="open") is original
    assert original.cause == "schema_version"


def test_no_classified_error_ever_carries_the_password() -> None:
    for exc in (
        ConnectionRefusedError(111, "refused"),
        TimeoutError(),
        asyncpg.exceptions.InvalidPasswordError("nope"),
        asyncpg.exceptions.TooManyConnectionsError("nope"),
        RuntimeError("some driver noise"),
    ):
        assert "secret" not in str(classify(exc, config=CONFIG, operation="op"))


# --- 3.4 The schema version check (design D5) -------------------------------


async def test_a_matching_schema_version_starts(database_config: DatabaseConfig) -> None:
    handle = Database(config=database_config)
    try:
        await handle.open()
        assert handle.applied_revision == migrations.expected_revision()
        assert handle.degraded is False
    finally:
        await handle.dispose()


async def test_a_database_behind_the_code_fails_naming_both_and_the_command(
    database_url: str,
) -> None:
    schema = "sighop_test_behind"
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    handle = Database(config=DatabaseConfig(url=database_url, schema=schema))
    try:
        with pytest.raises(SchemaVersionError) as excinfo:
            await handle.open()
        message = str(excinfo.value)
        assert "(no migrations applied)" in message
        assert migrations.expected_revision() in message
        assert migrations.RESTART_TO_MIGRATE in message
    finally:
        await handle.dispose()
        await _drop_schema(database_url, schema)


async def test_a_database_one_revision_behind_names_both_revisions_and_the_command(
    database_url: str,
) -> None:
    """1.6: a half-migrated deployment fails at startup, not at the first login.

    The "no migrations applied" case above is the empty database. This is the
    one a milestone-5 deployment actually meets after a milestone-6 binary is
    rolled out: the schema is real, it is simply one revision short.
    """
    schema = "sighop_test_one_behind"
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    config = DatabaseConfig(url=database_url, schema=schema)
    await migrations.upgrade_async(config, revision="0001")

    handle = Database(config=config)
    try:
        with pytest.raises(SchemaVersionError) as excinfo:
            await handle.open()
        message = str(excinfo.value)
        assert "0001" in message, "the message must name where the database is"
        assert migrations.expected_revision() in message, "and where the code expects it to be"
        assert migrations.RESTART_TO_MIGRATE in message, "and the command that reconciles them"
    finally:
        await handle.dispose()
        await _drop_schema(database_url, schema)


async def test_a_database_ahead_of_the_code_fails_naming_both(
    database_url: str,
) -> None:
    schema = "sighop_test_ahead"
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    config = DatabaseConfig(url=database_url, schema=schema)
    await migrations.upgrade_async(config)
    connection = await _connect(database_url)
    try:
        await connection.execute(f'UPDATE "{schema}".alembic_version SET version_num = $1', "9999")
    finally:
        await connection.close()

    handle = Database(config=config)
    try:
        with pytest.raises(SchemaVersionError) as excinfo:
            await handle.open()
        message = str(excinfo.value)
        assert "9999" in message
        assert migrations.expected_revision() in message
        assert "does not know" in message
    finally:
        await handle.dispose()
        await _drop_schema(database_url, schema)


# --- 3.5 / 3.7 Containment and bounds ---------------------------------------


async def _act(session: Any) -> object:
    """Run whatever the fake session was told to do.

    A named function rather than a lambda so its parameter can be `Any`: the
    real `run` is typed for an `AsyncSession`, and the fake is deliberately not
    one.
    """
    return await session.act()


class _FakeSession:
    """A session whose behaviour a test chooses: succeed, raise, or hang."""

    def __init__(self, behaviour: Callable[[], Awaitable[object]]) -> None:
        self._behaviour = behaviour

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def act(self) -> object:
        return await self._behaviour()


class _ProbeableDatabase(Database):
    """A `Database` whose connectivity probe a test can answer for.

    `Database` is a slotted dataclass, so a method cannot be monkeypatched onto
    an instance; a subclass is the seam, and it is a better one — the real
    `probe` is what the live-server tests exercise.
    """

    reachable: bool = False
    probe_attempts: int = 0

    async def probe(self) -> bool:
        self.probe_attempts += 1
        self.stats.probes += 1
        if self.reachable:
            return True
        self._current_probe_interval = min(
            self._current_probe_interval * 2, self.max_probe_interval
        )
        return False


def _database_over(
    behaviour: Callable[[], Awaitable[object]],
    *,
    cls: type[Database] = Database,
    **overrides: object,
) -> Database:
    """A `Database` whose sessions are a fake, so failure modes are on demand."""
    handle = cls(
        config=DatabaseConfig(url=URL, connect_timeout=0.05, statement_timeout=0.05),
        **overrides,  # type: ignore[arg-type]
    )
    # Both, so the `engine` property does not build a real one over the top.
    handle._engine = object()  # type: ignore[assignment]
    handle._sessions = lambda: _FakeSession(behaviour)  # type: ignore[assignment]
    return handle


async def test_a_failing_operation_returns_an_outcome_and_sets_degraded() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    class Recorder:
        def error(self, event: str, **fields: object) -> None:
            events.append((event, fields))

        def info(self, event: str, **fields: object) -> None:
            events.append((event, fields))

    async def boom() -> object:
        raise ConnectionRefusedError(111, "refused")

    handle = _database_over(boom, logger=Recorder())
    outcome = await handle.run("write_contact", _act)

    assert isinstance(outcome, Failed)
    assert outcome.operation == "write_contact"
    assert outcome.cause == "unreachable"
    assert handle.degraded is True
    assert handle.stats.failures == 1
    name, fields = events[0]
    assert name == "database_operation_failed"
    assert fields["operation"] == "write_contact"
    assert fields["outcome"] == "error"
    assert "secret" not in repr(fields)


async def test_a_later_success_clears_the_degraded_flag() -> None:
    state = {"fail": True}

    async def sometimes() -> object:
        if state["fail"]:
            raise ConnectionRefusedError(111, "refused")
        return "ok"

    handle = _database_over(sometimes)
    assert isinstance(await handle.run("write", _act), Failed)
    assert handle.degraded is True

    state["fail"] = False
    outcome = await handle.run("write", _act)
    assert isinstance(outcome, Succeeded)
    assert outcome.value == "ok"
    assert handle.degraded is False


async def test_an_operation_against_a_sink_that_never_answers_returns_within_the_bound() -> None:
    async def never() -> object:
        await asyncio.Event().wait()
        return None

    handle = _database_over(never)
    started = asyncio.get_running_loop().time()
    outcome = await handle.run("write_path", _act)
    elapsed = asyncio.get_running_loop().time() - started

    assert isinstance(outcome, Failed)
    assert isinstance(outcome.error, DatabaseUnavailableError)
    # 0.05 s connect + 0.05 s statement; generous slack for a loaded machine, but
    # nowhere near asyncpg's own 60 s default, which is the point.
    assert elapsed < 1.0
    assert handle.degraded is True


# --- 3.8 The degraded probe (design D15) ------------------------------------


async def test_the_probe_clears_the_flag_and_fires_recovery_without_any_write() -> None:
    writes = {"count": 0}

    async def work() -> object:
        writes["count"] += 1
        raise ConnectionRefusedError(111, "refused")

    handle = _database_over(work, cls=_ProbeableDatabase)
    recovered: list[str] = []

    async def backfill() -> None:
        recovered.append("flushed")

    handle.on_recovery(backfill)

    assert isinstance(await handle.run("write", _act), Failed)
    assert handle.degraded is True
    writes_after_failure = writes["count"]

    assert await handle.probe_once() is False
    assert handle.degraded is True

    handle.reachable = True  # type: ignore[attr-defined]
    assert await handle.probe_once() is True
    assert handle.degraded is False
    assert recovered == ["flushed"]
    # The probe is what detected the return: no write was attempted for it.
    assert writes["count"] == writes_after_failure


async def test_the_probe_interval_backs_off_while_the_database_stays_down() -> None:
    handle = _database_over(_never_used, cls=_ProbeableDatabase, probe_interval=1.0)
    handle.max_probe_interval = 8.0
    handle.degraded = True

    intervals = [handle.current_probe_interval]
    for _ in range(5):
        await handle.probe()
        intervals.append(handle.current_probe_interval)

    assert intervals == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


async def _never_used() -> object:  # pragma: no cover - the fake session is unused
    raise AssertionError("no operation should reach the sink in this test")


async def test_a_recovery_hook_that_fails_does_not_undo_the_recovery() -> None:
    handle = _database_over(_never_used, cls=_ProbeableDatabase)
    handle.degraded = True
    handle.reachable = True  # type: ignore[attr-defined]

    async def broken() -> None:
        raise RuntimeError("the backfill blew up")

    handle.on_recovery(broken)
    assert await handle.probe_once() is True
    assert handle.degraded is False


# --- 3.6 The write-behind worker --------------------------------------------


async def test_a_slow_sink_leaves_the_producer_unblocked_and_the_backlog_bounded() -> None:
    accepted: list[int] = []
    gate = asyncio.Event()

    async def slow(batch: list[int]) -> bool:
        await gate.wait()
        accepted.extend(batch)
        return True

    writer = WriteBehind("packet_log", slow, capacity=8, batch_size=4)
    writer.start()
    try:
        for value in range(100):
            assert writer.offer(value) is True  # never awaits, never refuses
        assert writer.pending <= writer.capacity
        assert writer.overflowed > 0
        gate.set()
        await asyncio.wait_for(writer.wait_idle(), timeout=2)
        assert accepted
        assert writer.written + writer.discarded == 100
    finally:
        gate.set()
        await writer.stop()


async def test_a_refusing_buffer_hands_the_item_back_instead_of_dropping_it() -> None:
    """The contact writer's shape (design D16): async does not become lossy."""

    async def never_drains(batch: list[int]) -> bool:  # pragma: no cover - not started
        return True

    writer = WriteBehind("contact", never_drains, capacity=3, drop_oldest=False)
    assert [writer.offer(value) for value in range(3)] == [True, True, True]
    assert writer.offer(99) is False
    assert writer.overflowed == 0, "a refusal is not a drop"
    assert writer.pending == 3


async def test_a_failed_write_is_counted_as_a_discard() -> None:
    async def refuse(batch: list[int]) -> bool:
        return False

    writer = WriteBehind("path", refuse, capacity=8, batch_size=2)
    for value in range(4):
        writer.offer(value)
    await writer.flush_pending()
    assert writer.written == 0
    assert writer.failed == 4
    assert writer.discarded == 4


async def test_a_sink_that_raises_does_not_take_the_writer_down() -> None:
    async def explode(batch: list[int]) -> bool:
        raise RuntimeError("driver noise")

    writer = WriteBehind("path", explode, capacity=8, batch_size=2)
    writer.offer(1)
    await writer.flush_pending()
    assert writer.failed == 1
    assert writer.pending == 0


async def test_shutdown_flushes_what_is_buffered() -> None:
    written: list[int] = []

    async def sink(batch: list[int]) -> bool:
        written.extend(batch)
        return True

    writer = WriteBehind("packet_log", sink, capacity=64, batch_size=8)
    for value in range(20):
        writer.offer(value)
    await writer.stop()
    assert written == list(range(20))
    assert writer.written == 20


async def test_a_stop_does_not_lose_the_batch_it_was_writing() -> None:
    """The batch already taken off the buffer is written, not cancelled away."""
    written: list[int] = []
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def sink(batch: list[int]) -> bool:
        entered.set()
        await gate.wait()
        written.extend(batch)
        return True

    writer = WriteBehind("dm", sink, capacity=64, batch_size=32)
    writer.start()
    for value in range(33):
        assert writer.offer(value) is True
    await asyncio.wait_for(entered.wait(), timeout=2)

    stopping = asyncio.ensure_future(writer.stop())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(stopping, timeout=2)

    assert written == list(range(33))
    assert writer.written == 33
    assert writer.discarded == 0


async def test_a_cancelled_flush_returns_its_batch() -> None:
    """Any cancellation, not just a stop: the rows are back in the buffer."""
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def sink(batch: list[int]) -> bool:
        entered.set()
        await gate.wait()
        return True

    writer = WriteBehind("dm", sink, capacity=64, batch_size=4)
    writer.start()
    for value in range(4):
        writer.offer(value)
    await asyncio.wait_for(entered.wait(), timeout=2)

    task = writer._task
    assert task is not None
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert writer.written == 0
    assert writer.failed == 0
    assert writer.pending == 4, "the in-flight batch is back in the buffer"


async def test_a_stop_against_a_sink_that_never_answers_counts_what_it_lost() -> None:
    """The budget expires, the rows are counted as failed, the loss is said once."""

    async def never_answers(batch: list[int]) -> bool:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    logger = RecordingLogger()
    writer = WriteBehind("dm", never_answers, capacity=64, batch_size=4, logger=logger)
    writer.start()
    for value in range(6):
        writer.offer(value)

    loop = asyncio.get_running_loop()
    await asyncio.wait_for(writer.stop(deadline=loop.time() + 0.1), timeout=2)

    assert writer.written == 0
    assert writer.failed == 6
    assert writer.discarded == 6
    assert writer.pending == 0
    incomplete = [
        fields
        for level, name, fields in logger.events
        if name == "write_behind_shutdown_incomplete" and level == "error"
    ]
    assert len(incomplete) == 1
    assert incomplete[0]["rows"] == 6
    assert incomplete[0]["writer"] == "dm"


async def test_a_clean_stop_reports_no_loss() -> None:
    async def sink(batch: list[int]) -> bool:
        return True

    logger = RecordingLogger()
    writer = WriteBehind("dm", sink, capacity=64, batch_size=4, logger=logger)
    writer.start()
    for value in range(6):
        writer.offer(value)

    loop = asyncio.get_running_loop()
    await asyncio.wait_for(writer.stop(deadline=loop.time() + 2), timeout=5)

    assert writer.written == 6
    assert writer.discarded == 0
    assert logger.names() == []


async def test_the_lanes_drain_under_one_budget_rather_than_one_each() -> None:
    """Design D3: five writers, one absolute deadline, drained concurrently."""
    budget = 0.2
    persistence = Persistence(
        database=Database(config=DatabaseConfig(url=URL, shutdown_budget=budget))
    )
    lanes = (
        persistence.contact_writer,
        persistence.path_writer,
        persistence.packet_log_writer,
        persistence.dm_writer,
        persistence.channel_writer,
    )

    async def never_answers(batch: list[Any]) -> bool:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    for lane in lanes:
        lane._flush = never_answers
        lane.start()
        assert lane.offer(cast(Any, object())) is True

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(persistence.stop(), timeout=5)
    elapsed = loop.time() - started

    assert elapsed >= budget
    assert elapsed < budget * len(lanes), "the budget is shared, not spent per lane"
    for lane in lanes:
        assert lane.failed == 1, lane.name
        assert lane.pending == 0


async def test_a_stopped_writer_runs_again_when_started() -> None:
    written: list[int] = []

    async def sink(batch: list[int]) -> bool:
        written.extend(batch)
        return True

    writer = WriteBehind("dm", sink, capacity=64, batch_size=4)
    writer.start()
    writer.offer(1)
    await writer.stop()
    writer.start()
    writer.offer(2)
    await asyncio.wait_for(writer.wait_idle(), timeout=2)
    await writer.stop()
    assert written == [1, 2]


# --- Reporting --------------------------------------------------------------


async def test_counters_read_zero_rather_than_being_omitted() -> None:
    handle = Database(config=CONFIG)
    fields = handle.as_json()
    for key in (
        "packet_log_discarded",
        "routes_discarded",
        "db_failures",
        "db_operations",
    ):
        assert fields[key] == 0
    assert fields["persistence"] == "on"
    assert "secret" not in repr(fields)


async def test_row_counting_helpers_are_available_for_the_repositories(
    database: Database,
) -> None:
    """A smoke test that the model layer talks to the throwaway schema at all."""
    async with database.sessions() as session:
        await session.execute(delete(PacketLog))
        session.add(
            PacketLog(
                packet_id=uuid.uuid4().hex[:16],
                direction="rx",
                at=dt.datetime.now(dt.UTC),
                outcome="parsed",
            )
        )
        await session.commit()
        assert (
            await session.execute(select(func.count()).select_from(PacketLog))
        ).scalar_one() == 1
