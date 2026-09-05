"""The virtual network bus (DESIGN.md §4.4).

An in-process asyncio pub/sub with two halves:

* **RX is fan-out.** Every subscriber sees every non-duplicate reception, with
  no filtering done on its behalf — a destination hash that matches several
  entities reaches all of them, and each attempts its own MAC verification.
  That is how MeshCore itself disambiguates the 1-byte hash (§3).
* **TX is submission.** A caller hands over a packet with its priority class and
  a deadline, and gets back a handle to await. The scheduler behind the handle
  is the only thing that talks to the modem.

The fan-out never awaits a subscriber (design D14). Each gets its own bounded
queue, and a full queue drops for that subscriber alone, loudly. The radio keeps
receiving whether or not a room server is busy writing to a database; letting a
slow subscriber back-pressure the decode stage would turn one busy entity into
lost frames for everyone — lost inside the modem's buffer, where nothing can log
them.

Deliberately in-process for v1. If entities ever become separate processes this
interface is the seam, but nothing pays for that flexibility yet.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Protocol

from sighop.logging import Logger, get_logger
from sighop.net.airtime import time_on_air_ms
from sighop.net.dedup import DedupCache, Duplicate
from sighop.net.paths import PathStore
from sighop.net.rx import RxRecord, emit_packet_rx
from sighop.radio.modem import RadioParams

DEFAULT_QUEUE_SIZE = 256


class PriorityClass(IntEnum):
    """DESIGN.md §4.3's four classes, lowest number served first."""

    ACK = 0
    """The sender is retrying until it hears one; delay multiplies traffic."""

    REPLY = 1
    """Direct replies to a live request — a human is waiting."""

    MESSAGE = 2
    """Originated traffic: room broadcasts, bot DMs."""

    ADVERT = 3
    """Purely periodic, and always yields."""


class TxResult(StrEnum):
    """How a submission ended. `SUPPRESSED` is neither success nor failure."""

    TRANSMITTED = "transmitted"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    """The receive-only gate was closed. Scheduled, charged, counted, not sent."""

    DROPPED = "dropped"
    """Never reached the modem: deadline expired, admission refused, shutdown."""


@dataclass(frozen=True, slots=True)
class Submission:
    """A packet offered to the scheduler."""

    packet: bytes
    priority: PriorityClass
    entity_id: str
    deadline: dt.datetime
    entity_name: str = ""
    entity_type: str = ""
    packet_id: str | None = None
    """The reception that caused this, where there was one — DESIGN.md §9's join
    key, threaded from ingress through to the reply it produced."""

    origin: str = ""
    """Free-text description for logs: "advert", "ack", and so on."""


@dataclass(frozen=True, slots=True)
class TxOutcome:
    """What became of a submission."""

    result: TxResult
    packet_id: str
    airtime_ms: float
    queue_wait_ms: float
    attempts: int
    reason: str = ""

    @property
    def sent(self) -> bool:
        return self.result is TxResult.TRANSMITTED

    def as_json(self) -> dict[str, object]:
        return {
            "tx_result": str(self.result),
            "packet_id": self.packet_id,
            "airtime_ms": round(self.airtime_ms, 3),
            "queue_wait_ms": round(self.queue_wait_ms, 3),
            "attempt": self.attempts,
            "reason": self.reason,
        }


class TxHandle:
    """An awaitable receipt for one submission.

    It always resolves — with `DROPPED` or `FAILED` where a transmission never
    happened — so a caller that awaits one cannot hang on a packet the scheduler
    quietly abandoned.
    """

    def __init__(self, submission: Submission) -> None:
        self.submission = submission
        self._future: asyncio.Future[TxOutcome] = asyncio.get_running_loop().create_future()

    def resolve(self, outcome: TxOutcome) -> None:
        if not self._future.done():
            self._future.set_result(outcome)

    @property
    def done(self) -> bool:
        return self._future.done()

    def __await__(self):  # type: ignore[no-untyped-def]
        return self._future.__await__()

    async def wait(self) -> TxOutcome:
        return await self._future


class TxSink(Protocol):
    """The slice of the scheduler the bus needs. Implemented by `net/tx.py`."""

    def submit(self, submission: Submission) -> TxHandle: ...


@dataclass(slots=True)
class SubscriberStats:
    name: str
    delivered: int = 0
    dropped: int = 0
    errors: int = 0
    depth: int = 0
    max_depth: int = 0
    capacity: int = 0

    def as_json(self) -> dict[str, object]:
        return {
            "subscriber": self.name,
            "delivered": self.delivered,
            "dropped": self.dropped,
            "errors": self.errors,
            "depth": self.depth,
            "max_depth": self.max_depth,
            "capacity": self.capacity,
        }


Handler = Callable[[RxRecord], Awaitable[None]]


class Subscription:
    """One subscriber's queue. Iterate `stream()`, or pass a handler."""

    def __init__(self, name: str, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.name = name
        self.queue: asyncio.Queue[RxRecord] = asyncio.Queue(maxsize=queue_size)
        self.stats = SubscriberStats(name=name, capacity=queue_size)
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    def offer(self, record: RxRecord) -> bool:
        """Non-blocking. False when the queue is full — the caller logs it."""
        if self._closed:
            return False
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.stats.dropped += 1
            return False
        self.stats.delivered += 1
        self.stats.depth = self.queue.qsize()
        self.stats.max_depth = max(self.stats.max_depth, self.stats.depth)
        return True

    async def stream(self) -> AsyncIterator[RxRecord]:
        """Receptions in order, until the subscription is closed."""
        while True:
            record = await self.queue.get()
            self.stats.depth = self.queue.qsize()
            yield record

    def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            self._task = None


class NetworkBus:
    """Fan-out for receptions, submission for transmissions."""

    def __init__(
        self,
        *,
        tx_sink: TxSink | None = None,
        logger: Logger | None = None,
        default_queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._subscriptions: list[Subscription] = []
        self._tx_sink = tx_sink
        self._log = logger or get_logger(component="bus")
        self._default_queue_size = default_queue_size
        self.published = 0

    # --- RX ----------------------------------------------------------------

    def subscribe(
        self,
        name: str,
        *,
        queue_size: int | None = None,
        handler: Handler | None = None,
    ) -> Subscription:
        """Attach a subscriber. It sees receptions published from now on."""
        subscription = Subscription(name, queue_size=queue_size or self._default_queue_size)
        self._subscriptions.append(subscription)
        if handler is not None:
            subscription._task = asyncio.create_task(
                self._pump(subscription, handler), name=f"bus-subscriber-{name}"
            )
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        subscription.close()
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    async def _pump(self, subscription: Subscription, handler: Handler) -> None:
        """Run a handler over a subscription, containing its failures.

        A subscriber that raises stays attached: the exception is its problem,
        and detaching it would silently stop delivering to an entity that is
        merely buggy in one code path.
        """
        async for record in subscription.stream():
            try:
                await handler(record)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                subscription.stats.errors += 1
                self._log.error(
                    "bus_subscriber_error",
                    subscriber=subscription.name,
                    packet_id=record.packet_id,
                    error=repr(exc),
                )

    def publish(self, record: RxRecord) -> int:
        """Offer a reception to every subscriber. Never awaits, never raises.

        Returns the number of subscribers that took it.
        """
        self.published += 1
        delivered = 0
        for subscription in self._subscriptions:
            if subscription.offer(record):
                delivered += 1
            else:
                self._log.error(
                    "bus_queue_overflow",
                    subscriber=subscription.name,
                    packet_id=record.packet_id,
                    depth=subscription.queue.qsize(),
                    capacity=subscription.stats.capacity,
                    dropped_total=subscription.stats.dropped,
                )
        return delivered

    @property
    def subscriber_stats(self) -> tuple[SubscriberStats, ...]:
        return tuple(subscription.stats for subscription in self._subscriptions)

    # --- TX ----------------------------------------------------------------

    def submit(self, submission: Submission) -> TxHandle:
        """Hand a packet to the scheduler and get a receipt to await."""
        if self._tx_sink is None:
            raise RuntimeError("no transmit scheduler is attached to this bus")
        return self._tx_sink.submit(submission)

    def attach_tx_sink(self, sink: TxSink) -> None:
        self._tx_sink = sink

    async def aclose(self) -> None:
        for subscription in list(self._subscriptions):
            self.unsubscribe(subscription)


@dataclass(slots=True)
class IngressPipeline:
    """Decode stage → dedup → path learning → fan-out (design D12).

    The stateful parts live here rather than inside `net/rx.py`, which stays a
    pure function of one frame. That is what keeps a replayed capture showing
    every copy of every packet — including the duplicates this drops — and so
    keeps the corpus usable for measuring the cache that drops them.
    """

    bus: NetworkBus
    dedup: DedupCache = field(default_factory=DedupCache)
    paths: PathStore = field(default_factory=PathStore)
    logger: Logger | None = None
    radio: RadioParams | None = None
    """The board's readback, when known: it is what lets the RX event carry the
    `airtime_ms` DESIGN.md §9 asks for. Absent rather than guessed when it is
    not known — the same rule the budget follows."""

    duplicates: int = 0
    delivered: int = 0

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="rx")

    def ingest(self, record: RxRecord) -> bool:
        """Process one decoded reception. True when it reached the bus."""
        assert self.logger is not None
        airtime: dict[str, object] = {}
        if self.radio is not None:
            airtime["airtime_ms"] = round(time_on_air_ms(record.size_bytes, self.radio), 3)

        verdict = self.dedup.observe(record)
        if isinstance(verdict, Duplicate):
            self.duplicates += 1
            emit_packet_rx(
                record,
                logger=self.logger,
                extra={"dup": True, **airtime, **verdict.as_json()},
            )
            return False

        learned = self.paths.observe(record)
        extra: dict[str, object] = {"dup": False, **airtime}
        if learned is not None:
            key, path = learned
            extra["learned_path"] = path.path.hex()
            extra["learned_path_hops"] = path.hop_count
            extra["learned_path_ambiguous"] = key.ambiguous

        emit_packet_rx(record, logger=self.logger, extra=extra)
        self.bus.publish(record)
        self.delivered += 1
        return True
