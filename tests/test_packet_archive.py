"""The packet archive: rows, repository, partitions and retention (packet-archive 2.x)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from sighop.db.archive import ArchiveMaintainer, export_lines, rx_archive_row, tx_archive_row
from sighop.db.engine import Database, Failed, Outcome, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    ArchiveReadError,
    ArchiveRecord,
    ArchiveRow,
    ArchiveSettingsError,
    PacketArchiveRepository,
    archive_partition_name,
    month_start,
    next_month,
)
from sighop.net.bus import PriorityClass, Submission, TxOutcome, TxResult
from sighop.net.rx import ModemUnparsed, RxRecord, decode_event
from sighop.radio.modem import RxEvent, RxMeta, UnparsedEvent
from sighop.radio.replay import CaptureReplay
from tests.protocol.corpus import CORPUS_DIR, CORPUS_FILES

NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)
THIS_MONTH = month_start(NOW)


def _value[T](outcome: Outcome[T]) -> T:
    assert isinstance(outcome, Succeeded), outcome
    return outcome.value


@pytest.fixture(scope="module")
def frame() -> bytes:
    """One real, decodable frame from the corpus."""
    for event in CaptureReplay.open(CORPUS_DIR / CORPUS_FILES[0]).read():
        if isinstance(event, RxEvent) and not decode_event(event).failed:
            return event.packet
    raise AssertionError("the corpus has no decodable frame")


def _rx(raw: bytes, at: dt.datetime = NOW, *, snr: float = 6.25, rssi: int = -97) -> RxRecord:
    return decode_event(
        RxEvent(packet=raw, rx_meta=RxMeta(snr_db=snr, rssi_dbm=rssi)), received_at=at
    )


def _submission(entity_id: str = "stub-1") -> Submission:
    return Submission(
        packet=b"\x00\x01\x02\x03",
        priority=PriorityClass.ADVERT,
        entity_id=entity_id,
        deadline=NOW + dt.timedelta(minutes=5),
        origin="advert",
    )


def _outcome(result: TxResult, reason: str = "") -> TxOutcome:
    return TxOutcome(
        result=result,
        packet_id="tx-1",
        airtime_ms=640.0,
        queue_wait_ms=1.0,
        attempts=1,
        reason=reason,
    )


def _row(at: dt.datetime, n: int) -> ArchiveRow:
    return ArchiveRow(at=at, kind="rx", packet_id=f"p-{n}", raw=bytes([n % 256, 0xAB]))


async def _children(database: Database) -> list[str]:
    async with database.sessions() as session:
        rows = await session.execute(
            text(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = 'packet_archive'::regclass ORDER BY c.relname"
            )
        )
        return list(rows.scalars())


async def _create(repository: PacketArchiveRepository, *months: dt.datetime) -> None:
    for start in months:
        _value(await repository.create_partition(start))


# --- 2.2 What a frame becomes ------------------------------------------------


def test_a_decodable_reception_keeps_bytes_signal_and_packet_id(frame: bytes) -> None:
    record = _rx(frame)
    row = rx_archive_row(record)
    assert (row.kind, row.raw, row.snr_db, row.rssi_dbm) == ("rx", frame, 6.25, -97)
    assert row.packet_id == record.packet_id
    assert row.at == NOW


def test_an_undecodable_framed_reception_is_rx_with_exact_bytes() -> None:
    record = _rx(b"\xff")
    assert record.failed
    row = rx_archive_row(record)
    assert (row.kind, row.raw, row.reason) == ("rx", b"\xff", None)


def test_unframed_bytes_keep_the_modems_reason() -> None:
    record = decode_event(UnparsedEvent(raw=b"\xc0\x99", reason="bad escape"), received_at=NOW)
    assert isinstance(record.outcome, ModemUnparsed)
    row = rx_archive_row(record)
    assert (row.kind, row.raw, row.reason, row.snr_db) == (
        "unparsed",
        b"\xc0\x99",
        "bad escape",
        None,
    )


def test_a_suppressed_transmission_keeps_its_bytes_and_result() -> None:
    row = tx_archive_row(_submission(), _outcome(TxResult.SUPPRESSED, "transmit disabled"), at=NOW)
    assert (row.kind, row.raw, row.tx_result, row.reason) == (
        "tx",
        b"\x00\x01\x02\x03",
        "suppressed",
        "transmit disabled",
    )
    assert row.priority_class == int(PriorityClass.ADVERT)
    assert row.entity_id is None  # a stub's id is not a stored identity


def test_a_stored_identity_is_kept_as_its_uuid() -> None:
    entity = uuid.uuid4()
    row = tx_archive_row(_submission(str(entity)), _outcome(TxResult.TRANSMITTED), at=NOW)
    assert row.entity_id == entity


def test_an_archived_reception_decodes_again_exactly(frame: bytes) -> None:
    original = _rx(frame)
    event = ArchiveRecord(id=1, row=rx_archive_row(original)).as_modem_event()
    again = decode_event(event)
    assert again.raw == original.raw
    assert (again.snr_db, again.rssi_dbm, again.received_at) == (6.25, -97, NOW)
    assert again.packet == original.packet


def test_unframed_bytes_come_back_as_an_unparsed_event() -> None:
    row = ArchiveRow(at=NOW, kind="unparsed", packet_id="u", raw=b"\x01", reason="why")
    assert ArchiveRecord(id=1, row=row).as_modem_event() == UnparsedEvent(
        raw=b"\x01", reason="why", received_at=NOW
    )


def test_a_transmission_has_no_modem_event() -> None:
    row = ArchiveRow(at=NOW, kind="tx", packet_id="t", raw=b"\x01")
    with pytest.raises(ValueError, match="no modem event"):
        ArchiveRecord(id=1, row=row).as_modem_event()


# --- 2.1 Repository ----------------------------------------------------------


async def test_rows_spanning_two_months_read_back_oldest_first_and_identical(
    database: Database, frame: bytes
) -> None:
    repository = PacketArchiveRepository(database=database)
    following = next_month(THIS_MONTH)
    await _create(repository, following)
    late = following - dt.timedelta(seconds=1)
    rows = [
        ArchiveRow(at=following + dt.timedelta(seconds=5), kind="rx", packet_id="c", raw=frame),
        ArchiveRow(
            at=late,
            kind="rx",
            packet_id="a",
            raw=frame,
            snr_db=-3.5,
            rssi_dbm=-110,
        ),
        tx_archive_row(_submission(), _outcome(TxResult.DROPPED, "deadline"), at=late),
        ArchiveRow(at=following, kind="unparsed", packet_id="b", raw=b"\x00", reason="short"),
    ]
    assert _value(await repository.write_many(rows)) == 4

    read = [r async for r in repository.stream(THIS_MONTH, next_month(following))]
    assert [r.row.packet_id for r in read] == ["a", "tx-1", "b", "c"]
    assert read[0].row == rows[1]
    assert read[1].row == rows[2]
    assert read[2].row == rows[3]
    assert read[3].row == rows[0]


async def test_a_range_bounds_are_inclusive_then_exclusive(database: Database) -> None:
    repository = PacketArchiveRepository(database=database)
    start = THIS_MONTH + dt.timedelta(days=1)
    _value(await repository.write_many([_row(start, 1), _row(start + dt.timedelta(hours=1), 2)]))
    read = [r async for r in repository.stream(start, start + dt.timedelta(hours=1))]
    assert [r.row.packet_id for r in read] == ["p-1"]


async def test_a_range_larger_than_one_batch_streams_completely(database: Database) -> None:
    repository = PacketArchiveRepository(database=database)
    start = THIS_MONTH + dt.timedelta(days=1)
    # Seven share one instant, so the keyset has to fall through to the id.
    rows = [_row(start, n) for n in range(7)] + [
        _row(start + dt.timedelta(seconds=n), 6 + n) for n in range(1, 6)
    ]
    _value(await repository.write_many(rows))
    read = [r async for r in repository.stream(start, start + dt.timedelta(days=1), batch=3)]
    assert [r.row.packet_id for r in read] == [f"p-{n}" for n in range(12)]
    assert len({r.id for r in read}) == 12


def _down(operation: str) -> Failed:
    from sighop.db.engine import DatabaseUnavailableError

    return Failed(operation=operation, error=DatabaseUnavailableError("down"))


async def test_a_failing_batch_is_raised_rather_than_skipped(database: Database) -> None:
    class _SecondPageFails(PacketArchiveRepository):
        pages = 0

        async def _page(self, since, until, after, size):
            self.pages += 1
            if self.pages == 2:
                return _down("read_packet_archive")
            return await super()._page(since, until, after, size)

    repository = _SecondPageFails(database=database)
    start = THIS_MONTH + dt.timedelta(days=1)
    _value(
        await repository.write_many([_row(start + dt.timedelta(seconds=n), n) for n in range(4)])
    )
    seen: list[str] = []
    with pytest.raises(ArchiveReadError):
        async for record in repository.stream(start, start + dt.timedelta(days=1), batch=2):
            seen.append(record.row.packet_id)
    assert seen == ["p-0", "p-1"]


async def test_a_month_without_a_partition_fails_the_write(database: Database) -> None:
    repository = PacketArchiveRepository(database=database)
    far = dt.datetime(2001, 1, 5, tzinfo=dt.UTC)
    assert isinstance(await repository.write_many([_row(far, 1)]), Failed)


async def test_retention_round_trips_including_forever(database: Database) -> None:
    repository = PacketArchiveRepository(database=database)
    assert _value(await repository.get_retention()) is None
    _value(await repository.set_retention(365))
    assert _value(await repository.get_retention()) == 365
    _value(await repository.set_retention(None))
    assert _value(await repository.get_retention()) is None


@pytest.mark.parametrize("days", [0, 29, 3651])
async def test_retention_out_of_range_is_refused_and_nothing_stored(
    database: Database, days: int
) -> None:
    repository = PacketArchiveRepository(database=database)
    _value(await repository.set_retention(90))
    with pytest.raises(ArchiveSettingsError, match="30 to 3650"):
        await repository.set_retention(days)
    assert _value(await repository.get_retention()) == 90


async def test_the_summary_counts_rows_times_and_partitions(database: Database) -> None:
    repository = PacketArchiveRepository(database=database)
    empty = _value(await repository.summary())
    assert (empty.rows, empty.oldest, empty.newest, empty.partitions) == (0, None, None, 1)

    start = THIS_MONTH + dt.timedelta(days=2)
    end = start + dt.timedelta(minutes=3)
    _value(await repository.write_many([_row(start, 1), _row(end, 2), _row(start, 3)]))
    summary = _value(await repository.summary())
    assert (summary.rows, summary.oldest, summary.newest, summary.partitions) == (3, start, end, 1)


# --- 2.3 Partitions and retention -------------------------------------------


async def test_ensure_creates_this_month_and_next_and_is_idempotent(database: Database) -> None:
    repository = PacketArchiveRepository(database=database)
    maintainer = ArchiveMaintainer(repository, now=lambda: NOW)
    assert await maintainer.ensure()
    assert await maintainer.ensure()
    assert await _children(database) == [
        archive_partition_name(THIS_MONTH),
        archive_partition_name(next_month(THIS_MONTH)),
    ]
    assert maintainer.created == 1  # this month's came with the migration


async def test_a_record_after_the_month_turns_lands_in_the_next_child(database: Database) -> None:
    repository = PacketArchiveRepository(database=database)
    await ArchiveMaintainer(repository, now=lambda: NOW).ensure()
    turned = next_month(THIS_MONTH) + dt.timedelta(microseconds=1)
    _value(await repository.write_many([_row(turned, 1)]))
    child = archive_partition_name(next_month(THIS_MONTH))
    async with database.sessions() as session:
        assert (await session.execute(text(f'SELECT count(*) FROM "{child}"'))).scalar_one() == 1


async def test_forever_drops_nothing(database: Database) -> None:
    repository = PacketArchiveRepository(database=database)
    old = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    await _create(repository, old)
    maintainer = ArchiveMaintainer(repository, now=lambda: NOW)
    assert await maintainer.prune()
    assert archive_partition_name(old) in await _children(database)
    assert maintainer.dropped == 0


async def test_a_bound_drops_expired_months_and_keeps_a_straddling_one(
    database: Database,
) -> None:
    repository = PacketArchiveRepository(database=database)
    now = dt.datetime(2026, 9, 15, tzinfo=dt.UTC)
    # With 90 days the cutoff is 2026-06-17: May has ended before it, June straddles it.
    may, june = dt.datetime(2026, 5, 1, tzinfo=dt.UTC), dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    await _create(repository, may, june)
    _value(await repository.write_many([_row(may + dt.timedelta(days=3), 1)]))
    _value(await repository.set_retention(90))

    maintainer = ArchiveMaintainer(repository, now=lambda: now)
    assert await maintainer.prune()
    children = await _children(database)
    assert archive_partition_name(may) not in children
    assert archive_partition_name(june) in children
    assert archive_partition_name(THIS_MONTH) in children
    assert maintainer.dropped == 1


async def test_a_stranger_partition_is_left_alone(database: Database) -> None:
    repository = PacketArchiveRepository(database=database)
    async with database.sessions() as session:
        await session.execute(
            text(
                "CREATE TABLE packet_archive_old PARTITION OF packet_archive "
                "FOR VALUES FROM ('1999-01-01+00') TO ('1999-02-01+00')"
            )
        )
        await session.commit()
    _value(await repository.set_retention(30))
    assert await ArchiveMaintainer(repository, now=lambda: NOW).prune()
    assert "packet_archive_old" in await _children(database)


async def test_a_failing_pass_is_reported_and_the_loop_retries(database: Database) -> None:
    class _FlakyRepository(PacketArchiveRepository):
        calls = 0

        async def create_partition(self, start):
            self.calls += 1
            if self.calls <= 2:
                return _down("create_packet_archive_partition")
            return await super().create_partition(start)

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise _Stop

    repository = _FlakyRepository(database=database)
    maintainer = ArchiveMaintainer(repository, now=lambda: NOW, interval=7.0, sleep=sleep)
    assert not await maintainer.run_once()
    assert maintainer.failures == 1
    with pytest.raises(_Stop):
        await maintainer.run()
    assert sleeps == [7.0, 7.0]
    assert (maintainer.passes, maintainer.failures) == (2, 1)
    assert archive_partition_name(next_month(THIS_MONTH)) in await _children(database)


class _Stop(Exception):
    pass


# --- 3.1 / 3.2 The persistence lane -------------------------------------------


async def _started(persistence: Persistence) -> None:
    persistence.start()
    assert persistence._archive_start is not None
    await persistence._archive_start


async def test_a_reception_and_a_transmission_each_reach_the_archive(
    database: Database, frame: bytes
) -> None:
    persistence = Persistence(database=database)
    await _started(persistence)
    try:
        record = _rx(frame)
        persistence.record_rx(record)
        persistence.record_tx(_submission(), _outcome(TxResult.TRANSMITTED), at=NOW)
        await persistence.archive_writer.wait_idle()
        read = [
            r async for r in persistence.packet_archive.stream(THIS_MONTH, next_month(THIS_MONTH))
        ]
        assert [(r.row.kind, r.row.packet_id) for r in read] == [
            ("rx", record.packet_id),
            ("tx", "tx-1"),
        ]
        assert database.stats.archive_written == 2
        assert persistence.as_json()["archive_discarded"] == 0
    finally:
        await persistence.stop()


async def test_a_failed_archive_write_is_counted(database: Database) -> None:
    persistence = Persistence(database=database)

    class _Failing(PacketArchiveRepository):
        async def write_many(self, rows):
            return _down("write_packet_archive")

    persistence.packet_archive = _Failing(database=database)
    assert await persistence._flush_archive([_row(NOW, 1), _row(NOW, 2)]) is False
    assert database.stats.archive_discarded == 2


async def test_an_overflowing_archive_buffer_is_counted(database: Database) -> None:
    persistence = Persistence(database=database)
    persistence.archive_writer.capacity = 2
    for n in range(5):
        persistence.record_rx(_rx(bytes([n, 1])))
    assert persistence.archive_writer.overflowed == 3
    assert persistence.archive_writer.discarded == 3


async def test_a_replay_run_archives_nothing(database: Database, frame: bytes) -> None:
    persistence = Persistence(database=database, writes_enabled=False)
    persistence.start()
    try:
        persistence.record_rx(_rx(frame))
        persistence.record_tx(_submission(), _outcome(TxResult.TRANSMITTED), at=NOW)
        assert persistence.archive_writer.pending == 0
        assert persistence._archive_start is None
    finally:
        await persistence.stop()


async def test_the_current_month_exists_before_the_first_archive_write(
    database: Database, frame: bytes
) -> None:
    async with database.sessions() as session:
        await session.execute(text(f'DROP TABLE "{archive_partition_name(THIS_MONTH)}"'))
        await session.commit()
    persistence = Persistence(database=database)
    # Offered before start: it waits in the buffer until the partition exists.
    persistence.record_rx(_rx(frame))
    await _started(persistence)
    try:
        await persistence.archive_writer.wait_idle()
        assert database.stats.archive_written == 1
        assert database.stats.archive_discarded == 0
        assert archive_partition_name(THIS_MONTH) in await _children(database)
    finally:
        await persistence.stop()


# --- 4.2 Export ----------------------------------------------------------------


async def test_an_export_replays_every_reception_in_order(
    database: Database, frame: bytes, tmp_path: Path
) -> None:
    repository = PacketArchiveRepository(database=database)
    start = THIS_MONTH + dt.timedelta(days=1)
    receptions = [
        rx_archive_row(_rx(frame, start, snr=4.5, rssi=-88)),
        rx_archive_row(_rx(b"\xff", start + dt.timedelta(seconds=1))),
        rx_archive_row(
            decode_event(
                UnparsedEvent(raw=b"\xc0\x01", reason="bad escape"),
                received_at=start + dt.timedelta(seconds=2),
            )
        ),
    ]
    sent = tx_archive_row(
        _submission(), _outcome(TxResult.TRANSMITTED), at=start + dt.timedelta(seconds=3)
    )
    suppressed = tx_archive_row(
        _submission(), _outcome(TxResult.SUPPRESSED), at=start + dt.timedelta(seconds=4)
    )
    _value(await repository.write_many([*receptions, sent, suppressed]))

    until = start + dt.timedelta(days=1)
    path = tmp_path / "export.jsonl"
    path.write_text("".join([line async for line in export_lines(repository, start, until)]))

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    header = lines[0]
    assert (header["kind"], header["source"]) == ("capture_meta", "packet_archive")
    assert header["range"] == {"since": start.isoformat(), "until": until.isoformat()}
    assert set(header["sighop"]) == {"version", "commit_hash"}
    assert [line["kind"] for line in lines[1:]] == ["rx_frame", "rx_frame", "unparsed", "tx_frame"]

    replay = CaptureReplay.open(path)
    events = list(replay.read())
    assert replay.unreadable == []
    assert replay.transmitted_skipped == 1
    assert events == [ArchiveRecord(id=0, row=row).as_modem_event() for row in receptions]
