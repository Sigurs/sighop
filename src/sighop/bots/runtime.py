"""The host: one bounded queue and one worker per bot, and the limits.

Design D1 and D12 between them decide the whole shape of this module.

**A bot is a consumer, not a second bus subscriber (D1).** `net/dm.py` already
decrypts every inbound `TXT_MSG` for every local entity that is not a room
server's, and acknowledges it. A bot subscribing to the bus in parallel would
decrypt the same packet a second time and, if it also acknowledged, put two
acknowledgements on the air for one message — the exact failure milestone 6's
design D10 was written for. So the host consumes `MessageReceived` reports, and
advert observations reach it through a listener on the contact store rather than
through a second subscription (D2): the store already knows whether a contact
was created, and asking it afterwards would be a race whose wrong branch greets
a peer twice or not at all.

**Driver work never runs on the reception path (D12).** Each bot owns a bounded
queue and one worker task. The listener and the message consumer *offer* — they
never await a driver and never raise into the caller — so a driver that sleeps
delays nothing but itself. Overflow drops the oldest pending dispatch and counts
it, matching `net/bus.py` rather than inventing a second policy. Per bot rather
than per runtime, so one slow driver cannot starve another.

**The limits are here and not in the driver.** The token bucket, the
observe/active decision and the never-flood rule are spent and applied by the
context this module builds. A driver that forgets to check them cannot get past
them, because the only way to the radio is through `BotContext.send`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field

from sighop.bots.base import (
    DEFAULT_BURST,
    DEFAULT_RATE_PER_HOUR,
    AdvertEvent,
    Bot,
    BotActed,
    BotContext,
    BotCounters,
    BotDispatchDropped,
    BotEvent,
    BotFailed,
    BotMode,
    BotRuntimeEvent,
    BotSendOutcome,
    BotSendResult,
    BotStateHandle,
    BotStorage,
    BotSuppressed,
    BotWouldAct,
    DirectMessageEvent,
    SuppressionReason,
)
from sighop.db.repositories import BotRecord
from sighop.logging import Logger, get_logger
from sighop.net.contacts import Contact, ContactObservation
from sighop.net.dm import LocalEntity, MessageReceived, SendOutcome, SendResult
from sighop.net.rx import RxRecord
from sighop.net.tx import Clock, SystemClock

DEFAULT_QUEUE_CAPACITY = 64
"""How many dispatches one bot may have pending.

Bounded, and small: the queue exists so a driver's work happens off the
reception path, not so a slow driver can accumulate an hour of adverts and then
answer all of them at once. Overflow is a reported event, not a silent stall."""

DEFAULT_BOT_SHUTDOWN_BUDGET = 2.0
"""What every bot together may spend finishing the dispatch it is running.

Two seconds out of compose's 20 s grace, beside the web server's 5 s and the
writers' 5 s (design D6). Long enough for a `bot_state` write bounded at one
`operation_timeout` to have been issued and answered, short enough that a driver
that is slow for its own reasons cannot hold the shutdown open."""


@dataclass(slots=True)
class TokenBucket:
    """The per-bot rate limit (design D11).

    One bucket, not two. `net/room.py`'s throttle needed a per-source bucket
    because one peer can replay a login endlessly; a bot's per-contact gate is
    absolute — once ever — so the only bucket that can be exhausted is the
    global one, which is exactly the "burst of adverts after an outage" §7 warns
    about.

    Not persisted, deliberately: a restart is already rate-limited by the
    driver's own once-ever record, and a bucket restored from a row would be a
    second, weaker copy of that guarantee.
    """

    rate_per_hour: float = DEFAULT_RATE_PER_HOUR
    burst: int = DEFAULT_BURST
    tokens: float = field(default=0.0, init=False)
    _last: dt.datetime | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.burst)

    def _refill(self, now: dt.datetime) -> None:
        if self._last is None:
            self._last = now
            return
        elapsed = (now - self._last).total_seconds()
        if elapsed <= 0:
            return
        self._last = now
        self.tokens = min(float(self.burst), self.tokens + elapsed * (self.rate_per_hour / 3600.0))

    def check(self, now: dt.datetime) -> bool:
        """Whether an action could be taken. Consumes nothing (see `BotContext`)."""
        self._refill(now)
        return self.tokens >= 1.0

    def spend(self, now: dt.datetime) -> bool:
        """Take one token, or report that there was none."""
        self._refill(now)
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True

    def as_json(self) -> dict[str, object]:
        return {
            "rate_per_hour": self.rate_per_hour,
            "burst": self.burst,
            "tokens_available": round(self.tokens, 2),
        }


def limits_from_config(config: dict) -> TokenBucket:
    """The bucket a stored bot's configuration describes.

    The two keys are reserved (`base.RESERVED_CONFIG_KEYS`) and a missing one is
    the documented default rather than an error: a bot created before a limit
    existed still has to run.
    """
    try:
        rate = float(config.get("rate_per_hour", DEFAULT_RATE_PER_HOUR))
    except (TypeError, ValueError):
        rate = DEFAULT_RATE_PER_HOUR
    try:
        burst = int(config.get("burst", DEFAULT_BURST))
    except (TypeError, ValueError):
        burst = DEFAULT_BURST
    return TokenBucket(rate_per_hour=max(rate, 0.0), burst=max(burst, 0))


@dataclass(slots=True)
class BotWorker:
    """One bot: its driver, its queue, its limit, its mode and its counters.

    Every collaborator is a callable rather than an object, which is what keeps
    the seam honest: the worker holds what it needs to reach the radio, the
    context it hands the driver holds only what a driver may do with it.
    """

    record: BotRecord
    driver: Bot
    storage: BotStorage
    send_message: Callable[[Contact, str, float], Awaitable[SendOutcome]]
    route_known: Callable[[Contact], bool]
    lookup: Callable[[bytes], Contact | None]
    announce_advert: Callable[[bool], Awaitable[bool]] | None = None
    """Emits one advert for this bot's identity and waits for it to reach the
    air. `True` asks for a flood, `False` for a zero-hop one. `None` when the
    runtime gave this bot no way to advert — a test double, usually."""

    entity: LocalEntity | None = None
    clock: Clock = field(default_factory=SystemClock)
    capacity: int = DEFAULT_QUEUE_CAPACITY
    on_event: Callable[[BotRuntimeEvent], None] | None = None
    logger: Logger | None = None

    counters: BotCounters = field(default_factory=BotCounters, init=False)
    bucket: TokenBucket = field(init=False)
    state: BotStateHandle = field(init=False)
    _queue: asyncio.Queue[BotEvent] = field(init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _idle: asyncio.Event = field(init=False)
    _stopping: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="bot", bot=self.name)
        self.bucket = limits_from_config(self.record.config)
        self.state = BotStateHandle(bot_id=self.record.id, store=self.storage.bot_state)
        self._queue = asyncio.Queue(maxsize=self.capacity)
        self._idle = asyncio.Event()
        self._idle.set()

    # --- Identity and reporting --------------------------------------------

    @property
    def name(self) -> str:
        """A bot is its identity: it has no name of its own (design D3)."""
        return self.record.entity_name or str(self.record.id)[:8]

    @property
    def driver_name(self) -> str:
        return self.record.driver

    @property
    def mode(self) -> BotMode:
        try:
            return BotMode(self.record.mode)
        except ValueError:  # pragma: no cover - the CLI and schema both bound it
            return BotMode.OBSERVE

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def limits(self) -> dict[str, object]:
        """Everything bounding this bot, the driver's own bounds included."""
        return {**self.bucket.as_json(), **self.driver.limits()}

    def as_json(self) -> dict[str, object]:
        return {
            **self.record.as_json(),
            **self.counters.as_json(),
            **self.bucket.as_json(),
            **{f"driver_{key}": value for key, value in self.driver.counters().items()},
        }

    # --- The context a driver gets -----------------------------------------

    def context(self) -> BotContext:
        return BotContext(
            bot_name=self.name,
            mode=self.mode,
            state=self.state,
            _send=self._send,
            _can_send=self._can_send,
            _lookup=self.lookup,
            _suppress=self.suppress,
            _degraded=lambda: self.storage.degraded,
            _announce=self._announce,
            _now=self.clock.now,
        )

    async def _announce(self, hops: int) -> bool:
        """Introduce this bot's identity at a distance, and wait for the air.

        The mechanism belongs here rather than to the driver: `hops == 0` is a
        zero-hop advert that stops at direct neighbours, and anything further
        needs a flood that every repeater in the mesh repeats. Awaited, because
        an advert is class 3 and a message is class 2 — issuing the send without
        waiting would let it overtake the advert that makes it readable.

        Observe mode transmits nothing here either. An advert is a transmission,
        and a dry run that put one on the air would not be a dry run.
        """
        assert self.logger is not None
        if self.mode is BotMode.OBSERVE or self.announce_advert is None:
            return False
        flood = hops > 0
        self.counters.announces += 1
        self.logger.info(
            "bot_announced",
            bot=self.name,
            driver=self.driver_name,
            advert="flood" if flood else "zero_hop",
            reach_hops=hops,
            detail=("a direct message is only readable by a node holding our public key"),
        )
        return await self.announce_advert(flood)

    def suppress(
        self,
        reason: SuppressionReason,
        contact: Contact | None = None,
        detail: str = "",
    ) -> None:
        assert self.logger is not None
        self.counters.suppress(reason)
        self.logger.info(
            "bot_suppressed",
            bot=self.name,
            driver=self.driver_name,
            suppression_reason=str(reason),
            public_key=None if contact is None else contact.public_key.hex(),
            detail=detail,
        )
        self._emit(
            BotSuppressed(
                bot_name=self.name,
                driver=self.driver_name,
                reason=reason,
                contact=contact,
                detail=detail,
            )
        )

    def _can_send(self, contact: Contact) -> SuppressionReason | None:
        """The non-consuming half of the limit (see `BotContext.can_send`).

        Route first: a contact with no known route would never have cost a
        token, and reporting `rate_limited` for one would send an operator
        looking at the wrong bound.
        """
        if not self.route_known(contact):
            return SuppressionReason.NO_ROUTE
        if not self.bucket.check(self.clock.now()):
            return SuppressionReason.RATE_LIMITED
        return None

    async def _send(
        self, contact: Contact, text: str, ack_grace_seconds: float = 0.0
    ) -> BotSendOutcome:
        """Every outbound driver action, in the one place the limits are.

        Order is the specification's: the limit is spent **before** anything is
        composed or queued, and the mode decides whether anything is queued at
        all. Observe mode spends a token too — an observed run's counters are
        meant to be what an active run would have done, and a bucket that only
        drained when transmitting would make the dry run optimistic about
        exactly the burst §7 warns about.
        """
        assert self.logger is not None
        now = self.clock.now()
        if not self.route_known(contact):
            self.suppress(SuppressionReason.NO_ROUTE, contact)
            return BotSendOutcome(
                result=BotSendResult.REFUSED,
                contact=contact,
                text=text,
                reason=SuppressionReason.NO_ROUTE,
            )
        if not self.bucket.spend(now):
            self.suppress(
                SuppressionReason.RATE_LIMITED,
                contact,
                detail=f"{self.bucket.rate_per_hour}/h burst {self.bucket.burst}",
            )
            return BotSendOutcome(
                result=BotSendResult.REFUSED,
                contact=contact,
                text=text,
                reason=SuppressionReason.RATE_LIMITED,
            )

        if self.mode is BotMode.OBSERVE:
            # The full decision path has run and this is where it stops. Nothing
            # is composed, nothing is queued, and the scheduler never hears of
            # it (design D4).
            self.counters.observations += 1
            self.logger.info(
                "bot_would_send",
                bot=self.name,
                driver=self.driver_name,
                public_key=contact.public_key.hex(),
                text_bytes=len(text.encode("utf-8")),
                detail="observe mode: nothing was transmitted",
            )
            self._emit(
                BotWouldAct(
                    bot_name=self.name,
                    driver=self.driver_name,
                    contact=contact,
                    text=text,
                )
            )
            return BotSendOutcome(result=BotSendResult.OBSERVED, contact=contact, text=text)

        outcome = await self.send_message(contact, text, ack_grace_seconds)
        self.counters.actions += 1
        result = {
            SendResult.ACKNOWLEDGED: BotSendResult.ACKNOWLEDGED,
            SendResult.UNACKNOWLEDGED: BotSendResult.UNACKNOWLEDGED,
            SendResult.DROPPED: BotSendResult.REFUSED,
        }[outcome.result]
        self._emit(
            BotActed(
                bot_name=self.name,
                driver=self.driver_name,
                contact=contact,
                text=text,
                result=result,
                attempts=outcome.attempts,
                route=outcome.route.label,
            )
        )
        return BotSendOutcome(
            result=result,
            contact=contact,
            text=text,
            attempts=outcome.attempts,
            route=outcome.route.label,
            reason=None if result is not BotSendResult.REFUSED else SuppressionReason.NO_ROUTE,
        )

    def _emit(self, event: BotRuntimeEvent) -> None:
        if self.on_event is not None:
            self.on_event(event)

    # --- Dispatch (design D12) ---------------------------------------------

    def offer(self, event: BotEvent) -> None:
        """Hand this bot an event. Never awaits, never raises.

        Called from the contact store's listener and from the direct messenger's
        report consumer, both of which are on the reception path. Anything that
        could block or throw here would be the bug this design exists to
        prevent.
        """
        assert self.logger is not None
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.counters.dropped += 1
            self.logger.error(
                "bot_dispatch_dropped",
                outcome="error",
                bot=self.name,
                driver=self.driver_name,
                dropped=self.counters.dropped,
                detail="the oldest pending dispatch was discarded; the queue is full",
            )
            self._emit(
                BotDispatchDropped(
                    bot_name=self.name,
                    driver=self.driver_name,
                    dropped=self.counters.dropped,
                )
            )
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(event)

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self.run(), name=f"bot-{self.name}")

    async def stop(self, deadline: float | None = None) -> None:
        """Let the dispatch in progress finish, then stop (design D6).

        A dispatch can be holding the `bot_state` write that guards an outbound
        action, so cancelling where it stands costs that write. `deadline` is an
        absolute event-loop instant shared with the other workers; without one
        the budget is `DEFAULT_BOT_SHUTDOWN_BUDGET` from now. A dispatch still
        running at the deadline is cancelled and reported, naming the bot, so a
        badly behaved driver is identifiable rather than merely slow.

        Safe at its position in the shutdown order because the scheduler has
        already stopped and resolved every pending send (`runtime.py`), so a
        dispatch waiting on a send gets an answer rather than hanging.
        """
        assert self.logger is not None
        self._stopping = True
        task, self._task = self._task, None
        if task is None:
            return
        if deadline is None:
            timeout = DEFAULT_BOT_SHUTDOWN_BUDGET
        else:
            timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        cut_short = False
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except TimeoutError:
            cut_short = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if cut_short:
            self.logger.error(
                "bot_dispatch_cut_short",
                outcome="error",
                bot=self.name,
                driver=self.driver_name,
                detail="the dispatch in progress did not finish within the shutdown budget",
            )

    async def run(self) -> None:
        while True:
            event = await self._queue.get()
            if self._stopping:
                # Queued dispatches are not started once the stop has begun.
                return
            self._idle.clear()
            try:
                await self.dispatch(event)
            finally:
                self._idle.set()

    async def dispatch(self, event: BotEvent) -> None:
        """Run one handler, containing whatever it does.

        A driver that raises costs its bot a counter and a reported failure, and
        costs the runtime, the radio and every other bot nothing. `CancelledError`
        is re-raised rather than counted: a shutdown is not a driver fault, and
        swallowing it here is how a task survives its own cancellation.
        """
        assert self.logger is not None
        context = self.context()
        try:
            match event:
                case AdvertEvent():
                    await self.driver.on_advert(context, event)
                case DirectMessageEvent():
                    await self.driver.on_direct_message(context, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a driver is in-process and may do anything
            self.counters.failures += 1
            kind = type(event).__name__
            self.logger.error(
                "bot_driver_failed",
                outcome="error",
                bot=self.name,
                driver=self.driver_name,
                bot_event=kind,
                error=f"{type(exc).__name__}: {exc}",
                failures=self.counters.failures,
            )
            self._emit(
                BotFailed(
                    bot_name=self.name,
                    driver=self.driver_name,
                    event=kind,
                    error=f"{type(exc).__name__}: {exc}",
                    failures=self.counters.failures,
                )
            )


@dataclass(slots=True)
class BotHost:
    """Every bot this run serves, and the two paths events reach them by.

    Both entry points are synchronous and total: they offer to each bot's queue
    and return. Nothing here awaits a driver, so nothing a driver does can
    delay a reception, a decode or an acknowledgement.
    """

    workers: list[BotWorker] = field(default_factory=list)
    logger: Logger | None = None

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="bots")

    def __len__(self) -> int:
        return len(self.workers)

    def __iter__(self) -> Iterator[BotWorker]:
        return iter(self.workers)

    def add(self, worker: BotWorker) -> BotWorker:
        self.workers.append(worker)
        return worker

    def remove(self, worker: BotWorker) -> bool:
        """Drop a worker from the fan-out, reporting whether it was here.

        Stopping is the caller's, not this method's: `BotWorker.stop` is a
        coroutine that waits for the dispatch in flight, and both entry points
        here are synchronous and total so that nothing a driver does can delay
        a reception. Removing a worker that was never stopped leaves its task
        running with nothing to feed it.
        """
        if worker not in self.workers:
            return False
        self.workers.remove(worker)
        return True

    def on_observation(self, observation: ContactObservation, record: RxRecord) -> None:
        """The contact store's listener (design D2).

        Every bot sees every advert observation: an advert is public and a bot's
        entity has no claim on it. What is *not* fanned out is a direct message,
        which reaches only the bot bound to the entity it was addressed to.
        """
        if not self.workers:
            return
        event = AdvertEvent(
            contact=observation.contact,
            created=observation.created,
            hop_count=record.hop_count,
            snr_db=record.snr_db,
            packet_id=record.packet_id,
            received_at=record.received_at,
            name_changed=observation.name_changed,
        )
        for worker in self.workers:
            worker.offer(event)

    def on_message(self, message: MessageReceived) -> None:
        """The direct messenger's report consumer (design D1, D15).

        Matched on the receiving entity itself rather than on its display name:
        two entities may share a name, and a bot answering for the wrong
        identity would be replying as somebody else.
        """
        for worker in self.workers:
            if worker.entity is None or message.entity is None:
                continue
            if worker.entity.entity_id != message.entity.entity_id:
                continue
            worker.offer(
                DirectMessageEvent(
                    contact=message.contact,
                    text=message.body.text,
                    timestamp=message.body.timestamp,
                    packet_id=message.packet_id,
                )
            )

    def start(self) -> None:
        for worker in self.workers:
            worker.start()

    async def stop(self) -> None:
        """Drain the workers concurrently under one deadline, as the lanes do.

        One budget for all the bots rather than one each: the stop's cost is
        bounded by the slowest driver, not by how many bots are configured.
        """
        if not self.workers:
            return
        deadline = asyncio.get_running_loop().time() + DEFAULT_BOT_SHUTDOWN_BUDGET
        await asyncio.gather(*(worker.stop(deadline=deadline) for worker in self.workers))

    def as_json(self) -> dict[str, object]:
        return {"bots": len(self.workers)}
