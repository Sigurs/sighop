"""The transmit scheduler (DESIGN.md §4.3).

Everything downstream of "the modem accepts one packet at a time" lives here:
priority classes, the airtime budget, deadlines, and the receive-only gate.

Three properties are worth stating plainly, because a bug in any of them is a
compliance problem rather than a bad user experience:

* **The gate is closed by default.** With it closed a packet is scheduled,
  charged, counted and logged exactly as when transmitting, and dropped at the
  final hand-off with `tx_result: suppressed` (design D6). A gated run's log is
  therefore directly comparable with a transmitting one — which is the only way
  "verify the budget accounting against what would have been sent" means
  anything.
* **The budget is a sliding window, not a token bucket** (design D4). The
  regulation says no more than 360 s of transmission in any hour; a window of
  `(charged_at, airtime_ms)` over the last 3600 s lets the test assert exactly
  that sentence, which a bucket only approximates.
* **Airtime is charged at hand-off and not refunded on failure** (design D5).
  A failed transmission still occupied the channel. The single exception is an
  explicit `TxBusy`: that is the one outcome where the modem states it did not
  transmit, and busy retries are bounded, so refunding it is both accurate and
  safe. A timeout keeps its charge, because a timeout means we do not know.

Nothing here computes radio maths; `net/airtime.py` does, from the board's
readback, and refuses when there is no readback to compute from.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

from sighop.logging import get_logger
from sighop.net.airtime import require_params, time_on_air_ms
from sighop.net.bus import PriorityClass, Submission, TxHandle, TxOutcome, TxResult
from sighop.net.rx import new_packet_id
from sighop.radio.modem import (
    RadioParams,
    TransmitBusy,
    TransmitDone,
    TransmitFailed,
    TransmitResult,
    TransmitTimedOut,
)

WINDOW_SECONDS = 3600.0
"""The regulation's observation window: one hour."""

DEFAULT_CEILING_FRACTION = 0.10
"""EU 868's 869.4-869.65 MHz sub-band permits 500 mW e.r.p. conditional on a
10% duty cycle. Enforced here regardless of any modem-side setting — the stock
firmware ships a 50% default, which does not satisfy it."""

DEFAULT_RESERVE_FRACTION = 0.90
"""Classes 2 and 3 stall here; 0 and 1 continue to the full ceiling (design D7)."""

DEFAULT_QUEUE_CAP = 64
"""Per class. A deeper queue is not more throughput, it is more staleness."""

DEFAULT_BUSY_ATTEMPTS = 3
DEFAULT_BUSY_DELAY_SECONDS = 0.25

TX_TIMEOUT_AIRTIME_FACTOR = 2.0
"""Above the firmware's own 1.5x (`KISS_TX_TIMEOUT_FACTOR`), so we never abandon
a modem that is still going to answer."""

TX_TIMEOUT_FIXED_SECONDS = 6.0
"""The CSMA phase before transmission even starts: `txdelay` is 500 ms by
default and p-persistent slot waits (`slottime` 100 ms, persistence 63/255) can
add several more seconds on a busy channel."""

IDLE_TICK_SECONDS = 1.0
"""How long the loop sleeps when it has work it cannot yet send, so a deadline
that expires while the budget is exhausted is still noticed promptly."""


class Clock(Protocol):
    """Time, injected so a simulated hour runs in milliseconds (design D17)."""

    def now(self) -> dt.datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Wall clock and real sleeps."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class PacketSender(Protocol):
    """The slice of `Modem` the scheduler uses."""

    async def send_packet(self, packet: bytes, *, timeout: float) -> TransmitResult: ...


class DropReason:
    """Stable strings, because these end up in logs people grep."""

    DEADLINE = "deadline_expired"
    QUEUE_FULL = "queue_cap_reached"
    BUSY_EXHAUSTED = "busy_retries_exhausted"
    SHUTDOWN = "scheduler_stopped"
    NO_RADIO = "no_radio_readback"
    OVERSIZED = "airtime_exceeds_ceiling"


@dataclass(slots=True)
class _Queued:
    submission: Submission
    handle: TxHandle
    packet_id: str
    queued_at: dt.datetime
    attempts: int = 0


class AirtimeBudget:
    """A sliding window of charged transmissions over the last hour.

    `used_ms(now)` is what the regulation cares about: the total charged in the
    preceding 3600 seconds. Nothing decays gradually and nothing refills on a
    timer — entries simply age out of the window.
    """

    def __init__(
        self,
        *,
        ceiling_fraction: float = DEFAULT_CEILING_FRACTION,
        reserve_fraction: float = DEFAULT_RESERVE_FRACTION,
        window_seconds: float = WINDOW_SECONDS,
    ) -> None:
        if not 0 < ceiling_fraction <= 1:
            raise ValueError(f"ceiling fraction {ceiling_fraction} outside (0, 1]")
        if not 0 < reserve_fraction <= 1:
            raise ValueError(f"reserve fraction {reserve_fraction} outside (0, 1]")
        self.ceiling_fraction = ceiling_fraction
        self.reserve_fraction = reserve_fraction
        self.window_seconds = window_seconds
        self._charges: deque[tuple[dt.datetime, float]] = deque()
        # Kept as a running total rather than summed per query: admission is
        # checked on every loop iteration, and an hour of traffic is hundreds of
        # entries. The window is the record; this is its sum.
        self._total_ms = 0.0

    @property
    def ceiling_ms(self) -> float:
        return self.window_seconds * 1000.0 * self.ceiling_fraction

    @property
    def reserve_threshold_ms(self) -> float:
        return self.ceiling_ms * self.reserve_fraction

    @property
    def above_regulatory_default(self) -> bool:
        """True when configured above 10%, which the UI must keep saying."""
        return self.ceiling_fraction > DEFAULT_CEILING_FRACTION

    def _expire(self, now: dt.datetime) -> None:
        cutoff = now - dt.timedelta(seconds=self.window_seconds)
        while self._charges and self._charges[0][0] <= cutoff:
            _at, airtime = self._charges.popleft()
            self._total_ms -= airtime

    def used_ms(self, now: dt.datetime) -> float:
        self._expire(now)
        return self._total_ms

    def limit_for(self, priority: PriorityClass) -> float:
        """Classes 2 and 3 stop at the reserve; 0 and 1 go to the ceiling."""
        if priority <= PriorityClass.REPLY:
            return self.ceiling_ms
        return self.reserve_threshold_ms

    def can_admit(self, airtime_ms: float, priority: PriorityClass, now: dt.datetime) -> bool:
        return self.used_ms(now) + airtime_ms <= self.limit_for(priority)

    def exceeds_ceiling(self, airtime_ms: float) -> bool:
        """A packet no empty hour could carry. Dropped rather than queued forever."""
        return airtime_ms > self.ceiling_ms

    def charge(self, airtime_ms: float, now: dt.datetime) -> None:
        self._expire(now)
        self._charges.append((now, airtime_ms))
        self._total_ms += airtime_ms

    def refund(self, airtime_ms: float) -> None:
        """Only for an explicit busy rejection — see this module's docstring."""
        for index in range(len(self._charges) - 1, -1, -1):
            if self._charges[index][1] == airtime_ms:
                del self._charges[index]
                self._total_ms -= airtime_ms
                return

    def remaining_pct(self, now: dt.datetime) -> float:
        used = self.used_ms(now)
        return max(0.0, 100.0 * (1.0 - used / self.ceiling_ms))

    def as_json(self, now: dt.datetime) -> dict[str, object]:
        used = self.used_ms(now)
        return {
            "duty_cycle_used_ms": round(used, 1),
            "duty_cycle_ceiling_ms": round(self.ceiling_ms, 1),
            "duty_cycle_pct": round(100.0 * used / self.ceiling_ms, 2),
            "budget_remaining_pct": round(self.remaining_pct(now), 2),
            "ceiling_fraction": self.ceiling_fraction,
            "reserve_fraction": self.reserve_fraction,
            "above_regulatory_default": self.above_regulatory_default,
        }


class _ClassQueue:
    """One priority class: per-entity queues served round-robin (design §4.3)."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self._entities: OrderedDict[str, deque[_Queued]] = OrderedDict()

    def __len__(self) -> int:
        return sum(len(queue) for queue in self._entities.values())

    def push(self, item: _Queued) -> _Queued | None:
        """Append, evicting the oldest member of the class when at cap."""
        evicted: _Queued | None = None
        if len(self) >= self.cap:
            evicted = self._pop_oldest()
        self._entities.setdefault(item.submission.entity_id, deque()).append(item)
        return evicted

    def push_front(self, item: _Queued) -> None:
        """Requeue at the head of its entity, and serve that entity next."""
        queue = self._entities.setdefault(item.submission.entity_id, deque())
        queue.appendleft(item)
        self._entities.move_to_end(item.submission.entity_id, last=False)

    def pop(self) -> _Queued | None:
        for entity_id in list(self._entities):
            queue = self._entities[entity_id]
            if not queue:
                del self._entities[entity_id]
                continue
            item = queue.popleft()
            if queue:
                # Rotation: this entity goes to the back so the next selection
                # from this class serves someone else first.
                self._entities.move_to_end(entity_id)
            else:
                del self._entities[entity_id]
            return item
        return None

    def _pop_oldest(self) -> _Queued | None:
        oldest: _Queued | None = None
        owner: str | None = None
        for entity_id, queue in self._entities.items():
            if queue and (oldest is None or queue[0].queued_at < oldest.queued_at):
                oldest, owner = queue[0], entity_id
        if oldest is None or owner is None:
            return None
        self._entities[owner].popleft()
        if not self._entities[owner]:
            del self._entities[owner]
        return oldest

    def expired(self, now: dt.datetime) -> list[_Queued]:
        """Remove and return everything past its deadline."""
        expired: list[_Queued] = []
        for entity_id in list(self._entities):
            queue = self._entities[entity_id]
            keep = deque(item for item in queue if item.submission.deadline > now)
            expired.extend(item for item in queue if item.submission.deadline <= now)
            if keep:
                self._entities[entity_id] = keep
            else:
                del self._entities[entity_id]
        return expired

    def drain(self) -> list[_Queued]:
        items = [item for queue in self._entities.values() for item in queue]
        self._entities.clear()
        return items

    def next_deadline(self) -> dt.datetime | None:
        deadlines = [
            item.submission.deadline for queue in self._entities.values() for item in queue
        ]
        return min(deadlines) if deadlines else None


@dataclass(slots=True)
class SchedulerStats:
    transmitted: int = 0
    failed: int = 0
    suppressed: int = 0
    dropped: int = 0
    busy_retries: int = 0
    submitted: int = 0
    charged_ms: float = 0.0

    def as_json(self) -> dict[str, object]:
        return {
            "submitted": self.submitted,
            "transmitted": self.transmitted,
            "failed": self.failed,
            "suppressed": self.suppressed,
            "dropped": self.dropped,
            "busy_retries": self.busy_retries,
            "charged_ms": round(self.charged_ms, 1),
        }


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    """What the periodic status line reports."""

    transmit_enabled: bool
    duty_cycle_pct: float
    duty_cycle_used_ms: float
    duty_cycle_ceiling_ms: float
    reserve_reached: bool
    above_regulatory_default: bool
    queue_depths: dict[int, int]
    stats: SchedulerStats

    def as_json(self) -> dict[str, object]:
        return {
            "transmit_enabled": self.transmit_enabled,
            "duty_cycle_pct": round(self.duty_cycle_pct, 2),
            "duty_cycle_used_ms": round(self.duty_cycle_used_ms, 1),
            "duty_cycle_ceiling_ms": round(self.duty_cycle_ceiling_ms, 1),
            "reserve_reached": self.reserve_reached,
            "above_regulatory_default": self.above_regulatory_default,
            "queue_depths": {str(k): v for k, v in self.queue_depths.items()},
            **self.stats.as_json(),
        }


class TxScheduler:
    """One packet in flight, four classes, a rolling-hour ceiling, and a gate."""

    def __init__(
        self,
        *,
        sender: PacketSender | None = None,
        radio: RadioParams | None = None,
        clock: Clock | None = None,
        budget: AirtimeBudget | None = None,
        transmit_enabled: bool = False,
        queue_cap: int = DEFAULT_QUEUE_CAP,
        busy_attempts: int = DEFAULT_BUSY_ATTEMPTS,
        busy_delay: float = DEFAULT_BUSY_DELAY_SECONDS,
        on_transmitted: Callable[[bytes, TxOutcome], None] | None = None,
        on_resolved: Callable[[Submission, TxOutcome], None] | None = None,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self.on_transmitted = on_transmitted
        """Called with the bytes of every packet that actually reached the air.
        The capture writer uses it, so a transmitting run records both
        directions; a suppressed packet never fires it, because nothing was
        transmitted."""

        self.on_resolved = on_resolved
        """Called once for *every* submission that resolves — transmitted,
        suppressed, dropped or failed — beside the §9 *Packet TX* wide event.

        Milestone 5's packet log uses it: the feed records what the scheduler
        decided, and a packet the gate suppressed is exactly the kind of thing
        an operator needs to see there. Never awaits; a hook that raises is
        contained, because a logging sink must not be able to fail a packet."""

        self._sender = sender
        self._radio = radio
        self._clock = clock or SystemClock()
        self.budget = budget or AirtimeBudget()
        self.transmit_enabled = transmit_enabled
        self._busy_attempts = busy_attempts
        self._busy_delay = busy_delay
        self._log = logger or get_logger(component="tx")
        self._queues = {priority: _ClassQueue(queue_cap) for priority in PriorityClass}
        self.stats = SchedulerStats()
        self._wake = asyncio.Event()
        self._running = False
        self._stopped = asyncio.Event()

        if self.budget.above_regulatory_default:
            # A standing warning, at startup and in every status line: the
            # ceiling above 10% is a deliberate act, and it stays visible.
            self._log.error(
                "duty_cycle_ceiling_raised",
                ceiling_fraction=self.budget.ceiling_fraction,
                detail=(
                    "configured above the EU 868 10% limit; sighop will permit "
                    "more airtime than the sub-band allows"
                ),
            )

    # --- Configuration -----------------------------------------------------

    def set_radio(self, radio: RadioParams | None) -> None:
        """Adopt the board's readback. Called after every probe and reconnect."""
        self._radio = radio

    def attach_sender(self, sender: PacketSender | None) -> None:
        self._sender = sender

    def enable_transmit(self, enabled: bool) -> None:
        self.transmit_enabled = enabled

    # --- Submission --------------------------------------------------------

    def submit(self, submission: Submission) -> TxHandle:
        """Queue a packet. Never blocks, never raises for a full queue."""
        handle = TxHandle(submission)
        now = self._clock.now()
        item = _Queued(
            submission=submission,
            handle=handle,
            packet_id=submission.packet_id or new_packet_id(),
            queued_at=now,
        )
        self.stats.submitted += 1
        evicted = self._queues[submission.priority].push(item)
        if evicted is not None:
            self._drop(evicted, DropReason.QUEUE_FULL, now)
        self._wake.set()
        return handle

    # --- The loop ----------------------------------------------------------

    async def run(self) -> None:
        """Serve queues until stopped. Exactly one packet in flight throughout."""
        self._running = True
        self._stopped.clear()
        try:
            while self._running:
                now = self._clock.now()
                self._expire(now)
                item = self._select(now)
                if item is None:
                    await self._idle()
                    continue
                await self._dispatch(item)
        finally:
            self._running = False
            self._stopped.set()

    async def stop(self) -> None:
        """Stop the loop and drop what is queued, saying so for each packet."""
        self._running = False
        self._wake.set()
        now = self._clock.now()
        for queue in self._queues.values():
            for item in queue.drain():
                self._drop(item, DropReason.SHUTDOWN, now)

    async def _idle(self) -> None:
        """Wait for a submission, or for a deadline or the budget to move."""
        self._wake.clear()
        if self._has_work():
            # Work exists but cannot go now: the budget is exhausted or a busy
            # retry is pending. Tick, so an expiring deadline is still noticed.
            with_timeout = IDLE_TICK_SECONDS
        else:
            with_timeout = None
        if with_timeout is None:
            await self._wake.wait()
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), with_timeout)

    def _has_work(self) -> bool:
        return any(len(queue) for queue in self._queues.values())

    def _expire(self, now: dt.datetime) -> None:
        for queue in self._queues.values():
            for item in queue.expired(now):
                self._drop(item, DropReason.DEADLINE, now)

    def _select(self, now: dt.datetime) -> _Queued | None:
        """The highest-priority packet the budget currently allows."""
        for priority in sorted(self._queues):
            queue = self._queues[priority]
            if not len(queue):
                continue
            item = queue.pop()
            assert item is not None
            airtime = self._airtime_ms(item)
            if airtime is None:
                self._drop(item, DropReason.NO_RADIO, now)
                continue
            if self.budget.exceeds_ceiling(airtime):
                self._drop(item, DropReason.OVERSIZED, now, airtime_ms=airtime)
                continue
            if not self.budget.can_admit(airtime, priority, now):
                # Not this packet's fault and not a drop: put it back and let
                # the window drain. Its deadline is what eventually decides.
                # Lower classes are still tried — a class stalled by the reserve
                # must not also block a smaller packet that would fit, and the
                # two classes below the reserve share its limit anyway.
                queue.push_front(item)
                continue
            return item
        return None

    def _airtime_ms(self, item: _Queued) -> float | None:
        try:
            radio = require_params(self._radio)
        except Exception:
            return None
        return time_on_air_ms(len(item.submission.packet), radio)

    async def _dispatch(self, item: _Queued) -> None:
        now = self._clock.now()
        airtime = self._airtime_ms(item)
        if airtime is None:
            self._drop(item, DropReason.NO_RADIO, now)
            return

        item.attempts += 1
        # Charged at hand-off, including a suppressed one: a gated run must
        # account exactly as a transmitting one would (design D5/D6).
        self.budget.charge(airtime, now)
        self.stats.charged_ms += airtime

        if not self.transmit_enabled or self._sender is None:
            self.stats.suppressed += 1
            self._resolve(item, TxResult.SUPPRESSED, airtime, now, reason="transmit disabled")
            return

        timeout = TX_TIMEOUT_AIRTIME_FACTOR * (airtime / 1000.0) + TX_TIMEOUT_FIXED_SECONDS
        result = await self._sender.send_packet(item.submission.packet, timeout=timeout)
        finished = self._clock.now()

        match result:
            case TransmitDone(success=True):
                self.stats.transmitted += 1
                self._resolve(item, TxResult.TRANSMITTED, airtime, finished)
                if self.on_transmitted is not None:
                    self.on_transmitted(
                        item.submission.packet,
                        TxOutcome(
                            result=TxResult.TRANSMITTED,
                            packet_id=item.packet_id,
                            airtime_ms=airtime,
                            queue_wait_ms=(finished - item.queued_at).total_seconds() * 1000.0,
                            attempts=item.attempts,
                        ),
                    )
            case TransmitDone(success=False):
                self.stats.failed += 1
                self._resolve(
                    item, TxResult.FAILED, airtime, finished, reason="modem reported failure"
                )
            case TransmitBusy():
                # The one outcome where the modem states nothing was sent.
                self.budget.refund(airtime)
                self.stats.charged_ms -= airtime
                self.stats.busy_retries += 1
                if item.attempts >= self._busy_attempts:
                    self._drop(item, DropReason.BUSY_EXHAUSTED, finished, airtime_ms=airtime)
                    return
                self._queues[item.submission.priority].push_front(item)
                self._log.info(
                    "packet_tx_busy",
                    packet_id=item.packet_id,
                    attempt=item.attempts,
                    entity_id=item.submission.entity_id,
                )
                await self._clock.sleep(self._busy_delay)
                self._wake.set()
            case TransmitTimedOut():
                self.stats.failed += 1
                self._resolve(item, TxResult.FAILED, airtime, finished, reason="tx_done_timeout")
            case TransmitFailed(reason=reason):
                self.stats.failed += 1
                self._resolve(item, TxResult.FAILED, airtime, finished, reason=reason)

    # --- Outcomes ----------------------------------------------------------

    def _resolve(
        self,
        item: _Queued,
        result: TxResult,
        airtime_ms: float,
        now: dt.datetime,
        *,
        reason: str = "",
    ) -> None:
        outcome = TxOutcome(
            result=result,
            packet_id=item.packet_id,
            airtime_ms=airtime_ms,
            queue_wait_ms=(now - item.queued_at).total_seconds() * 1000.0,
            attempts=item.attempts,
            reason=reason,
        )
        item.handle.resolve(outcome)
        self._emit(item, outcome, now)

    def _drop(
        self,
        item: _Queued,
        reason: str,
        now: dt.datetime,
        *,
        airtime_ms: float = 0.0,
    ) -> None:
        self.stats.dropped += 1
        outcome = TxOutcome(
            result=TxResult.DROPPED,
            packet_id=item.packet_id,
            airtime_ms=airtime_ms,
            queue_wait_ms=(now - item.queued_at).total_seconds() * 1000.0,
            attempts=item.attempts,
            reason=reason,
        )
        item.handle.resolve(outcome)
        self._emit(item, outcome, now)

    def _emit(self, item: _Queued, outcome: TxOutcome, now: dt.datetime) -> None:
        """The DESIGN.md §9 *Packet TX* wide event, one per attempt."""
        if self.on_resolved is not None:
            try:
                self.on_resolved(item.submission, outcome)
            except Exception as exc:
                # A feed must never be able to fail a packet (packet-log spec).
                self._log.error(
                    "tx_resolution_hook_failed",
                    packet_id=outcome.packet_id,
                    error=repr(exc),
                )
        failed = outcome.result in (TxResult.FAILED, TxResult.DROPPED)
        emit = self._log.error if failed else self._log.info
        emit(
            "packet_tx",
            entity_id=item.submission.entity_id,
            entity_name=item.submission.entity_name,
            entity_type=item.submission.entity_type,
            priority_class=int(item.submission.priority),
            origin=item.submission.origin,
            size_bytes=len(item.submission.packet),
            acked=False,
            transmit_enabled=self.transmit_enabled,
            budget_remaining_pct=round(self.budget.remaining_pct(now), 2),
            **outcome.as_json(),
        )

    # --- Reporting ---------------------------------------------------------

    def status(self) -> SchedulerStatus:
        now = self._clock.now()
        used = self.budget.used_ms(now)
        return SchedulerStatus(
            transmit_enabled=self.transmit_enabled,
            duty_cycle_pct=100.0 * used / self.budget.ceiling_ms,
            duty_cycle_used_ms=used,
            duty_cycle_ceiling_ms=self.budget.ceiling_ms,
            reserve_reached=used >= self.budget.reserve_threshold_ms,
            above_regulatory_default=self.budget.above_regulatory_default,
            queue_depths={int(p): len(q) for p, q in self._queues.items()},
            stats=self.stats,
        )
