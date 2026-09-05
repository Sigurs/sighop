"""Turning receptions and transmissions into feed rows, and pruning the feed.

The packet log is **a feed, not an audit trail** (§6). Nothing consults it for
dedup or protocol behaviour, nothing depends on a row being present, and every
function here is called *after* the decision it describes has already been made.
That is what lets the whole path be best-effort: a row is built off the reception
path, offered to a bounded queue that drops its oldest when full, and written in
batches by a task nobody waits for.

The one row that carries weight is the undecodable one. §4.1's rule — "a
malformed frame we silently drop is invisible forever" — is why `raw` and
`reason` exist, and why a frame that failed to decode is the one case where the
bytes themselves are stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable

from sighop.db.engine import Succeeded
from sighop.db.repositories import PacketLogRepository, PacketLogRow
from sighop.logging import Logger, get_logger
from sighop.net.bus import Submission, TxOutcome
from sighop.net.rx import RxRecord, outcome_fields

DEFAULT_PRUNE_INTERVAL_SECONDS = 600.0
"""Ten minutes. The cap is a row count and the measured rate is ~191
receptions/hour, so this is far more often than it needs to be — which is the
point: pruning on a schedule beats pruning on a threshold nobody watches."""

Sleep = Callable[[float], Awaitable[None]]


def rx_row(record: RxRecord, *, airtime_ms: float | None = None) -> PacketLogRow:
    """One reception as a feed row, correlated by `packet_id` with its wide event."""
    fields = outcome_fields(record)
    outcome = str(fields.pop("outcome"))
    reason = fields.get("failure_reason")
    detail = fields.get("failure_detail")
    if reason is not None and detail:
        reason = f"{reason}: {detail}"
    return PacketLogRow(
        packet_id=record.packet_id,
        direction="rx",
        at=record.received_at,
        outcome=outcome,
        route_type=record.route_type.name if record.route_type is not None else None,
        payload_type=record.payload_type.name if record.payload_type is not None else None,
        path_bytes=record.path,
        hop_count=record.hop_count,
        size_bytes=record.size_bytes,
        snr_db=record.snr_db,
        rssi_dbm=record.rssi_dbm,
        airtime_ms=airtime_ms,
        reason=None if reason is None else str(reason),
        # Only for what could not be decoded. Keeping every frame's bytes would
        # make the feed a capture file, which is a different artefact with a
        # different lifetime (§12).
        raw=record.raw if record.failed else None,
    )


def tx_row(submission: Submission, outcome: TxOutcome, *, at: dt.datetime) -> PacketLogRow:
    """One resolved transmission as a feed row — including one that never flew.

    A suppressed packet is recorded: the gate being closed is a decision an
    operator needs to see, and a feed that showed only what reached the air
    would make a gated run look like a quiet mesh.
    """
    return PacketLogRow(
        packet_id=outcome.packet_id,
        direction="tx",
        at=at,
        outcome=str(outcome.result),
        entity_id=_entity_uuid(submission.entity_id),
        priority_class=int(submission.priority),
        airtime_ms=outcome.airtime_ms,
        size_bytes=len(submission.packet),
        reason=outcome.reason or None,
    )


def _entity_uuid(entity_id: str) -> uuid.UUID | None:
    """The `entity` row this came from, when the identity is a stored one.

    An advert stub's id is `stub-1`, not a UUID, and an ephemeral identity has no
    row to point at. None rather than a fabricated id: the column means "this
    entity", and inventing one would make the feed claim a provenance it lacks.
    """
    try:
        return uuid.UUID(entity_id)
    except (ValueError, AttributeError):
        return None


class PacketLogPruner:
    """Keeps the feed inside its row cap, on a schedule and once at startup.

    Pruning once at startup matters: a process that ran for a week and was
    restarted meets a table already over the cap, and waiting for the first
    interval to elapse would leave it there. A pass that fails is reported and
    the next one is attempted at the normal interval — the task does not stop,
    because a pruner that gave up is how a disk fills.
    """

    def __init__(
        self,
        repository: PacketLogRepository,
        *,
        interval: float = DEFAULT_PRUNE_INTERVAL_SECONDS,
        sleep: Sleep = asyncio.sleep,
        logger: Logger | None = None,
    ) -> None:
        self._repository = repository
        self.interval = interval
        self._sleep = sleep
        self._log = logger or get_logger(component="packet-log")
        self._task: asyncio.Task[None] | None = None
        self.passes = 0
        self.failures = 0
        self.deleted = 0

    async def prune_once(self) -> int:
        outcome = await self._repository.prune()
        self.passes += 1
        if isinstance(outcome, Succeeded):
            self.deleted += outcome.value
            if outcome.value:
                self._log.info(
                    "packet_log_pruned",
                    outcome="success",
                    deleted=outcome.value,
                    max_rows=self._repository.max_rows,
                    deleted_total=self.deleted,
                )
            return outcome.value
        self.failures += 1
        self._log.error(
            "packet_log_prune_failed",
            outcome="error",
            error=str(outcome.error),
            failures=self.failures,
            detail="the next pass is attempted at the normal interval",
        )
        return 0

    async def run(self) -> None:
        """Prune now, then on the interval, forever."""
        await self.prune_once()
        while True:
            await self._sleep(self.interval)
            await self.prune_once()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="packet-log-pruner")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
