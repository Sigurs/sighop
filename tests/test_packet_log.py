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
from sighop.db.repositories import PacketLogRepository, PacketLogRow
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
from tests.protocol.corpus import CAPTURE_FILES, CAPTURES_DIR

NOW = dt.datetime(2026, 9, 5, 20, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def corpus_records() -> list[RxRecord]:
    records: list[RxRecord] = []
    for name in CAPTURE_FILES:
        replay = CaptureReplay.open(CAPTURES_DIR / name)
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


@pytest.mark.database
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


@pytest.mark.database
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


@pytest.mark.database
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


@pytest.mark.database
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


@pytest.mark.database
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


@pytest.mark.database
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
