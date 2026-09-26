"""The packet log (section 7, design D2/D3).

Two claims are worth testing and one is worth testing hardest. The feed records
receptions and transmissions and prunes itself; that is ordinary. The claim that
matters is the negative one — **nothing depends on a row being present** — and
the way to check it is to run the pipeline twice, once with the log and once
without, and assert the two runs decided exactly the same things.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from sighop.db.engine import Database, Failed, Succeeded
from sighop.db.packetlog import PacketLogPruner, rx_row, tx_row
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    MAX_RECENT_PACKETS,
    PacketLogRepository,
    PacketLogRow,
)
from sighop.db.writer import WriteBehind
from sighop.net.bus import (
    IngressPipeline,
    NetworkBus,
    PriorityClass,
    Submission,
    TxOutcome,
    TxResult,
)
from sighop.net.dedup import DedupCache
from sighop.net.paths import PathStore
from sighop.net.rx import ModemUnparsed, RxRecord, decode_event
from sighop.radio.modem import UnparsedEvent
from sighop.radio.replay import CaptureReplay
from tests.protocol.corpus import CORPUS_DIR, CORPUS_FILES

NOW = dt.datetime(2026, 9, 5, 20, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def corpus_records() -> list[RxRecord]:
    records: list[RxRecord] = []
    for name in CORPUS_FILES:
        replay = CaptureReplay.open(CORPUS_DIR / name)
        records.extend(decode_event(event) for event in replay.read())
    return records


def _submission(entity_id: str = "stub-1") -> Submission:
    return Submission(
        packet=b"\x00\x01\x02\x03",
        priority=PriorityClass.ADVERT,
        entity_id=entity_id,
        deadline=NOW + dt.timedelta(minutes=5),
        entity_name="skogen",
        origin="advert",
    )


# --- 7.1 What a row carries -------------------------------------------------


async def test_a_batch_of_rx_and_tx_rows_is_written_and_read_back(
    database: Database, corpus_records: list[RxRecord]
) -> None:
    repository = PacketLogRepository(database=database)
    reception = next(record for record in corpus_records if record.packet is not None)

    rows = [
        rx_row(reception, airtime_ms=123.5),
        tx_row(
            _submission(),
            TxOutcome(
                result=TxResult.TRANSMITTED,
                packet_id="tx-1",
                airtime_ms=640.0,
                queue_wait_ms=12.0,
                attempts=1,
            ),
            at=NOW,
        ),
    ]
    assert isinstance(await repository.write_many(rows), Succeeded)

    async with database.sessions() as session:
        stored = (
            await session.execute(
                text(
                    "SELECT packet_id, direction, route_type, payload_type, hop_count, "
                    "size_bytes, snr_db, rssi_dbm, airtime_ms, priority_class, outcome "
                    "FROM packet_log ORDER BY direction"
                )
            )
        ).all()
    assert len(stored) == 2
    rx, tx = stored
    assert rx.direction == "rx"
    assert rx.packet_id == reception.packet_id
    assert rx.route_type == reception.route_type.name
    assert rx.payload_type == reception.payload_type.name
    assert rx.hop_count == reception.hop_count
    assert rx.size_bytes == reception.size_bytes
    assert rx.airtime_ms == 123.5
    assert tx.direction == "tx"
    assert tx.packet_id == "tx-1"
    assert tx.priority_class == int(PriorityClass.ADVERT)
    assert tx.outcome == "transmitted"


def test_a_transmission_that_never_flew_is_still_recorded() -> None:
    """A gated run resolves everything as suppressed; silence is not the same
    thing as a quiet mesh."""
    row = tx_row(
        _submission(),
        TxOutcome(
            result=TxResult.SUPPRESSED,
            packet_id="tx-2",
            airtime_ms=640.0,
            queue_wait_ms=1.0,
            attempts=1,
            reason="transmit disabled",
        ),
        at=NOW,
    )
    assert row.outcome == "suppressed"
    assert row.reason == "transmit disabled"
    assert row.raw is None


def test_a_stored_entity_id_reaches_the_row_and_a_stub_id_does_not() -> None:
    """The column means "this entity"; a stub has no row to point at."""
    entity = uuid.uuid4()
    outcome = TxOutcome(
        result=TxResult.TRANSMITTED,
        packet_id="tx-3",
        airtime_ms=1.0,
        queue_wait_ms=0.0,
        attempts=1,
    )
    assert tx_row(_submission(str(entity)), outcome, at=NOW).entity_id == entity
    assert tx_row(_submission("stub-1"), outcome, at=NOW).entity_id is None


# --- 7.2 An undecodable frame reaches the database --------------------------


async def test_a_malformed_frame_is_recorded_with_its_bytes_and_a_reason(
    database: Database,
) -> None:
    """§4.1: a frame we silently drop is invisible forever."""
    record = decode_event(
        UnparsedEvent(raw=b"\xde\xad\xbe\xef", reason="kiss framing error", received_at=NOW)
    )
    assert isinstance(record.outcome, ModemUnparsed)

    repository = PacketLogRepository(database=database)
    assert isinstance(await repository.write_many([rx_row(record)]), Succeeded)

    async with database.sessions() as session:
        row = (await session.execute(text("SELECT outcome, reason, raw FROM packet_log"))).one()
    assert row.outcome == "modem_unparsed"
    assert row.reason is not None and "kiss framing error" in row.reason
    assert bytes(row.raw) == b"\xde\xad\xbe\xef"


def test_a_frame_that_decoded_stores_no_raw_bytes(corpus_records: list[RxRecord]) -> None:
    """A feed that kept every frame's bytes would be a capture file, which is a
    different artefact with a different lifetime (§12)."""
    good = next(record for record in corpus_records if not record.failed)
    assert rx_row(good).raw is None


# --- 7.3 Logging never delays or fails a packet -----------------------------


async def test_a_slow_sink_leaves_the_reception_rate_alone_and_moves_the_counter() -> None:
    gate = asyncio.Event()

    async def slow(batch: list[PacketLogRow]) -> bool:
        await gate.wait()
        return True

    writer: WriteBehind[PacketLogRow] = WriteBehind("packet_log", slow, capacity=16, batch_size=8)
    writer.start()
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        for index in range(500):
            writer.offer(
                PacketLogRow(packet_id=f"p{index}", direction="rx", at=NOW, outcome="parsed")
            )
        elapsed = loop.time() - started

        assert elapsed < 0.5, "offering rows waited on the database"
        assert writer.pending <= writer.capacity
        assert writer.discarded > 0, "a full buffer discarded without counting"
    finally:
        gate.set()
        await writer.stop()


async def test_a_failing_write_degrades_rather_than_blocks(database: Database) -> None:
    persistence = Persistence(database=database)

    class _FailingRepository:
        max_rows = 10

        async def write_many(self, rows: list[PacketLogRow]) -> Failed:
            from sighop.db.engine import DatabaseUnavailableError

            return Failed(operation="write_packet_log", error=DatabaseUnavailableError("down"))

    persistence.packet_log = _FailingRepository()  # type: ignore[assignment]
    landed = await persistence._flush_packet_log(
        [PacketLogRow(packet_id="p1", direction="rx", at=NOW, outcome="parsed")]
    )
    assert landed is False
    assert persistence.database.stats.packet_log_discarded == 1


# --- 7.4 Pruning ------------------------------------------------------------


async def test_a_table_already_over_the_cap_is_pruned_at_startup(
    database: Database,
) -> None:
    repository = PacketLogRepository(database=database, max_rows=10)
    await repository.write_many(
        [
            PacketLogRow(
                packet_id=f"p{index}",
                direction="rx",
                at=NOW + dt.timedelta(seconds=index),
                outcome="parsed",
            )
            for index in range(25)
        ]
    )
    pruner = PacketLogPruner(repository, interval=3600)

    # The first pass happens immediately, without waiting for the interval: a
    # process restarted after a week meets a table already over the cap.
    deleted = await pruner.prune_once()

    assert deleted == 15
    count = await repository.count()
    assert isinstance(count, Succeeded)
    assert count.value == 10

    remaining = await _packet_ids(database)
    assert remaining == [f"p{index}" for index in range(15, 25)], "the newest were pruned"


async def test_pruning_repeats_on_the_interval(database: Database) -> None:
    repository = PacketLogRepository(database=database, max_rows=5)
    passes = asyncio.Event()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 2:
            passes.set()
            await asyncio.Event().wait()

    pruner = PacketLogPruner(repository, interval=42.0, sleep=fake_sleep)
    pruner.start()
    try:
        await asyncio.wait_for(passes.wait(), timeout=5)
    finally:
        await pruner.stop()
    assert slept[:2] == [42.0, 42.0]
    assert pruner.passes >= 2


async def test_a_failed_pass_is_reported_and_the_next_one_still_runs(
    database: Database,
) -> None:
    class _FlakyRepository(PacketLogRepository):
        """Fails its first pass and succeeds after. `PacketLogRepository` is a
        slotted dataclass, so a subclass is the seam rather than a patched
        attribute."""

        calls = 0

        async def prune(self):
            self.calls += 1
            if self.calls == 1:
                from sighop.db.engine import DatabaseUnavailableError

                return Failed(operation="prune_packet_log", error=DatabaseUnavailableError("down"))
            return await super().prune()

    repository = _FlakyRepository(database=database, max_rows=1)
    pruner = PacketLogPruner(repository, interval=3600)

    assert await pruner.prune_once() == 0
    assert pruner.failures == 1
    assert await pruner.prune_once() == 0  # succeeded; nothing to delete
    assert pruner.failures == 1
    assert pruner.passes == 2


async def _packet_ids(database: Database) -> list[str]:
    async with database.sessions() as session:
        return list(
            (await session.execute(text("SELECT packet_id FROM packet_log ORDER BY at"))).scalars()
        )


# --- 7.5 The log is a feed, not a dependency --------------------------------


def test_a_replay_decides_identically_with_and_without_the_packet_log(
    corpus_records: list[RxRecord],
) -> None:
    """Nothing consults the log for dedup or protocol behaviour, so two runs
    over the same frames must agree on every decision they made."""

    def run(record_rows: list[PacketLogRow] | None) -> dict[str, object]:
        bus = NetworkBus()
        pipeline = IngressPipeline(bus=bus, dedup=DedupCache(), paths=PathStore())
        delivered: list[bool] = []
        for record in corpus_records:
            delivered.append(pipeline.ingest(record))
            if record_rows is not None:
                record_rows.append(rx_row(record))
        return {
            "delivered": delivered,
            "duplicates": pipeline.duplicates,
            "paths": pipeline.paths.as_json(),
            "dedup": pipeline.dedup.stats.as_json(),
        }

    rows: list[PacketLogRow] = []
    with_log = run(rows)
    without_log = run(None)

    assert rows, "the feed recorded nothing at all"
    assert with_log == without_log


def test_duplicate_detection_never_reads_the_packet_log() -> None:
    """A static statement about the source: `net/dedup.py` cannot consult a
    table it has no way to reach."""
    import ast
    from pathlib import Path

    import sighop.net.dedup as dedup_module

    source = Path(dedup_module.__file__).read_text()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not any(name.startswith("sighop.db") for name in names)
    assert not any(name.split(".")[0] in {"sqlalchemy", "asyncpg"} for name in names)


# --- 5.1 / 5.2 The log becomes readable (milestone 8, design D14) -----------


def _row(index: int, *, at: dt.datetime, **overrides: object) -> PacketLogRow:
    fields: dict[str, object] = {
        "packet_id": f"pkt{index:03d}",
        "direction": "rx",
        "at": at,
        "outcome": "dispatched",
    }
    fields.update(overrides)
    return PacketLogRow(**fields)  # type: ignore[arg-type]


async def test_the_recent_read_is_newest_first_and_capped(database: Database) -> None:
    """5.1: what the feed paints before the browser has heard anything live."""
    repository = PacketLogRepository(database=database)
    written = await repository.write_many(
        [_row(index, at=NOW + dt.timedelta(seconds=index)) for index in range(10)]
    )
    assert isinstance(written, Succeeded)

    recent = await repository.recent(limit=3)
    assert isinstance(recent, Succeeded)
    assert [row.packet_id for row in recent.value] == ["pkt009", "pkt008", "pkt007"]


async def test_the_cap_is_not_the_callers_to_raise(database: Database) -> None:
    """5.1: a bound on a statement's cost that the caller could lift is not one."""
    repository = PacketLogRepository(database=database)
    assert isinstance(await repository.write_many([_row(0, at=NOW)]), Succeeded)

    huge = await repository.recent(limit=10_000_000)
    assert isinstance(huge, Succeeded)
    assert len(huge.value) == 1, "the read still answered, bounded"

    # The cap is applied to the statement, not to what came back by luck: a
    # request for more than the maximum is served as the maximum.
    assert MAX_RECENT_PACKETS < 10_000_000


async def test_an_unparsed_frame_comes_back_with_its_bytes_and_its_reason(
    database: Database,
) -> None:
    """5.1, §4.1: a frame nobody could decode must not disappear on the way back.

    It was written with its raw bytes and the reason precisely so it would stay
    visible; a read that dropped either would be the same silent loss one step
    further along.
    """
    repository = PacketLogRepository(database=database)
    raw = bytes.fromhex("deadbeef00")
    assert isinstance(
        await repository.write_many(
            [
                _row(
                    1,
                    at=NOW,
                    outcome="undecodable",
                    reason="truncated: 5 bytes",
                    raw=raw,
                )
            ]
        ),
        Succeeded,
    )

    recent = await repository.recent()
    assert isinstance(recent, Succeeded)
    stored = recent.value[0]
    assert stored.raw == raw
    assert stored.reason == "truncated: 5 bytes"
    assert stored.outcome == "undecodable"


async def test_a_degraded_database_answers_the_read_as_unavailable() -> None:
    """5.2: refused within the bound, and nothing queued for later.

    The read is a `Database.run` like every other operation, which is what makes
    this true rather than a promise: the operation timeout and the degraded flag
    are the engine's, and this inherits both.
    """
    from tests.test_db_engine import _database_over

    async def refused() -> object:
        raise ConnectionRefusedError(111, "refused")

    handle = _database_over(refused)
    repository = PacketLogRepository(database=handle)

    outcome = await repository.recent(limit=10)

    assert isinstance(outcome, Failed)
    assert outcome.operation == "read_recent_packets"
    assert handle.degraded is True
    assert handle.stats.failures == 1


def test_nothing_on_the_packet_path_reads_the_recent_packets() -> None:
    """5.2: the log stays a feed — asserted against the source, not by habit.

    A static scan over the modules that decide what happens to a packet. The
    read exists for a display; a decision that consulted it would make the log
    a dependency, which is the one thing design D3 said it must never become.
    """
    import ast
    from pathlib import Path

    import sighop.net.bus as bus_module
    import sighop.net.dedup as dedup_module
    import sighop.net.dm as dm_module
    import sighop.net.rx as rx_module
    import sighop.net.tx as tx_module

    offenders: dict[str, list[str]] = {}
    for module in (rx_module, dedup_module, bus_module, tx_module, dm_module):
        assert module.__file__ is not None
        source = Path(module.__file__).read_text()
        found = [
            node.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Attribute) and node.attr == "recent"
        ]
        if found:
            offenders[module.__name__] = found
    assert not offenders, (
        f"{offenders} read the packet log; nothing on the reception, dedup, "
        "dispatch or transmit path may consult a feed"
    )
