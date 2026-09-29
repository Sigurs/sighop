"""The packet archive's policy: what a frame becomes, and which months exist.

The archive is the permanent counterpart of the packet log (packet-archive D1):
every reception and every resolved transmission, with its wire bytes, kept until
an operator's retention bound says otherwise. Rows are built here from the same
two hooks the feed uses and written behind the reception path on a lane of their
own (D2); the SQL, partition DDL included, is `PacketArchiveRepository`'s.

`ArchiveMaintainer` keeps the monthly partitions ahead of the clock and, when a
bound is set, drops whole months that have fallen out of it (D4, D5). There is
no DEFAULT partition, so a month nobody created is a counted gap rather than
rows stuck where the month's own partition can never be attached.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from sighop.db.engine import Failed
from sighop.db.repositories import (
    ArchiveRow,
    PacketArchiveRepository,
    archive_partition_month,
    month_start,
    next_month,
)
from sighop.logging import Logger, get_logger
from sighop.net.bus import Submission, TxOutcome
from sighop.net.rx import ModemUnparsed, RxRecord
from sighop.radio.capture import (
    archive_meta_record,
    rx_frame_record,
    tx_frame_record,
    unparsed_record,
)

DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 6 * 3600.0
"""Six hours: partitions are made a month ahead, so this only has to be often
enough that a failed pass is retried long before the month turns."""

Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], dt.datetime]


def rx_archive_row(record: RxRecord) -> ArchiveRow:
    """One reception as an archive row — decodable, undecodable or unframed.

    A framed frame that failed to decode is `rx`, not something else: its
    bytes are the frame, and a later decoder may read what this one could not.
    Only bytes the modem could not frame at all are `unparsed`.
    """
    unparsed = isinstance(record.outcome, ModemUnparsed)
    return ArchiveRow(
        at=record.received_at,
        kind="unparsed" if unparsed else "rx",
        packet_id=record.packet_id,
        raw=record.raw,
        snr_db=record.snr_db,
        rssi_dbm=record.rssi_dbm,
        reason=record.outcome.reason if isinstance(record.outcome, ModemUnparsed) else None,
    )


def tx_archive_row(submission: Submission, outcome: TxOutcome, *, at: dt.datetime) -> ArchiveRow:
    """One resolved transmission — including one that never reached the air."""
    return ArchiveRow(
        at=at,
        kind="tx",
        packet_id=outcome.packet_id,
        raw=submission.packet,
        reason=outcome.reason or None,
        tx_result=str(outcome.result),
        airtime_ms=outcome.airtime_ms,
        entity_id=_entity_uuid(submission.entity_id),
        priority_class=int(submission.priority),
    )


def _entity_uuid(entity_id: str) -> uuid.UUID | None:
    """A stored identity's row id, or None for a stub or an ephemeral identity."""
    try:
        return uuid.UUID(entity_id)
    except (ValueError, AttributeError):
        return None


async def export_lines(
    repository: PacketArchiveRepository, since: dt.datetime, until: dt.datetime
) -> AsyncIterator[str]:
    """A range of the archive as capture JSONL, one line per yielded string.

    The header first, then every record in archive order through the capture
    writer's own record builders, so an export is one more dialect of the one
    format `radio/replay.py` reads (packet-archive D7). A transmission that never
    reached the air is left out: the file records what happened on the air.

    A read that fails partway raises `ArchiveReadError` after the last whole line.
    What came before is still valid JSONL.
    """
    yield json.dumps(archive_meta_record(since, until)) + "\n"
    async for record in repository.stream(since, until):
        row = record.row
        if row.kind == "rx":
            line = rx_frame_record(row.raw, at=row.at, snr_db=row.snr_db, rssi_dbm=row.rssi_dbm)
        elif row.kind == "unparsed":
            line = unparsed_record(row.raw, at=row.at, reason=row.reason or "")
        elif row.tx_result == "transmitted":
            line = tx_frame_record(
                row.raw, at=row.at, packet_id=row.packet_id, airtime_ms=row.airtime_ms
            )
        else:
            continue
        yield json.dumps(line) + "\n"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ArchiveMaintainer:
    """Creates this month's and next month's partitions; drops expired months.

    Runs once when started — awaited, so the current month exists before the
    archive writer starts — then on the interval, and whenever the database
    comes back. A failing pass is reported and retried at the normal interval;
    the task never stops, for the same reason the packet log's pruner does not.
    """

    def __init__(
        self,
        repository: PacketArchiveRepository,
        *,
        interval: float = DEFAULT_MAINTENANCE_INTERVAL_SECONDS,
        sleep: Sleep = asyncio.sleep,
        now: Now = _utc_now,
        logger: Logger | None = None,
    ) -> None:
        self._repository = repository
        self.interval = interval
        self._sleep = sleep
        self._now = now
        self._log = logger or get_logger(component="packet-archive")
        self._task: asyncio.Task[None] | None = None
        self.passes = 0
        self.failures = 0
        self.created = 0
        self.dropped = 0

    async def ensure(self, now: dt.datetime | None = None) -> bool:
        """Make sure the current and next months exist. False if either failed."""
        current = month_start(now or self._now())
        ok = True
        for start in (current, next_month(current)):
            outcome = await self._repository.create_partition(start)
            if isinstance(outcome, Failed):
                ok = False
                self._log.error(
                    "archive_partition_create_failed",
                    outcome="error",
                    month=start.strftime("%Y-%m"),
                    error=str(outcome.error),
                    detail="records for that month are discarded and counted until it exists",
                )
            elif outcome.value:
                self.created += 1
                self._log.info(
                    "archive_partition_created", outcome="success", month=start.strftime("%Y-%m")
                )
        return ok

    async def prune(self, now: dt.datetime | None = None) -> bool:
        """Drop every month wholly older than the retention bound. False on failure."""
        retention = await self._repository.get_retention()
        if isinstance(retention, Failed):
            self._log.error(
                "archive_retention_read_failed", outcome="error", error=str(retention.error)
            )
            return False
        if retention.value is None:
            return True
        cutoff = (now or self._now()) - dt.timedelta(days=retention.value)
        listed = await self._repository.partitions()
        if isinstance(listed, Failed):
            self._log.error(
                "archive_partitions_read_failed", outcome="error", error=str(listed.error)
            )
            return False
        ok = True
        for partition in listed.value:
            start = archive_partition_month(partition.name)
            if start is None:
                self._log.info(
                    "archive_partition_unrecognised",
                    outcome="skipped",
                    partition=partition.name,
                    detail="not named by the maintainer; left alone",
                )
                continue
            if next_month(start) > cutoff:
                continue
            dropped = await self._repository.drop_partition(partition.name)
            if isinstance(dropped, Failed):
                ok = False
                self._log.error(
                    "archive_partition_drop_failed",
                    outcome="error",
                    month=start.strftime("%Y-%m"),
                    error=str(dropped.error),
                )
                continue
            self.dropped += 1
            self._log.info(
                "archive_partition_dropped",
                outcome="success",
                month=start.strftime("%Y-%m"),
                rows=partition.rows,
                retention_days=retention.value,
            )
        return ok

    async def run_once(self) -> bool:
        now = self._now()
        created = await self.ensure(now)
        pruned = await self.prune(now)
        self.passes += 1
        if not (created and pruned):
            self.failures += 1
        return created and pruned

    async def run(self) -> None:
        """On the interval, forever. The first pass is `start`'s, not this loop's."""
        while True:
            await self._sleep(self.interval)
            await self.run_once()

    async def start(self) -> None:
        """One pass now, awaited; then the loop in the background."""
        await self.run_once()
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="packet-archive-maintainer")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
