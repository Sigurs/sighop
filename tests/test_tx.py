"""The transmit scheduler (milestone 3, `tx-scheduler`, design D4-D9, D17).

The centrepiece is `test_the_ceiling_holds_across_a_simulated_day`: the 10%
duty cycle is a legal limit on EU 868, so its test has to be cheap enough to
run on every commit, which is what the injected clock buys.

The other property worth naming is the gate. Every test in the "receive-only"
section asserts the same thing from a different angle — with transmit disabled,
no packet reaches the sender — because that is what makes an overnight run
against a live mesh a safe thing to do.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt

import pytest

from sighop.net.bus import PriorityClass, Submission, TxResult
from sighop.net.tx import (
    DEFAULT_CEILING_FRACTION,
    WINDOW_SECONDS,
    AirtimeBudget,
    DropReason,
    TxScheduler,
)
from sighop.radio.modem import (
    EU868_NARROW,
    TransmitBusy,
    TransmitDone,
    TransmitFailed,
    TransmitResult,
    TransmitTimedOut,
)

START = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.UTC)
PACKET = b"\x04\x01" + b"\xab" * 62  # 64 bytes: ~738 ms at the default preset


class ManualClock:
    """Time only moves when a test says so — a simulated day in milliseconds."""

    def __init__(self, start: dt.datetime = START) -> None:
        self._now = start
        self.slept = 0.0

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        """Record the wait and yield — but do *not* move time.

        Advancing here would make any loop that sleeps a time machine: a status
        loop asking for 3600 s would fast-forward an hour per iteration and
        expire every deadline in the system. Tests move time explicitly.
        """
        self.slept += seconds
        await asyncio.sleep(0)


class RecordingSender:
    """Stands in for the modem, answering as instructed and counting packets."""

    def __init__(self, *results: TransmitResult) -> None:
        self.results = list(results)
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []

    async def send_packet(self, packet: bytes, *, timeout: float) -> TransmitResult:
        self.sent.append(packet)
        self.timeouts.append(timeout)
        if self.results:
            return self.results.pop(0)
        return TransmitDone(success=True)


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", event, fields))

    def of(self, name: str) -> list[dict[str, object]]:
        return [fields for _level, event, fields in self.events if event == name]


def submission(
    priority: PriorityClass = PriorityClass.MESSAGE,
    *,
    entity: str = "stub-1",
    packet: bytes = PACKET,
    deadline_in: float = 60.0,
    clock: ManualClock | None = None,
    origin: str = "test",
    packet_id: str | None = None,
) -> Submission:
    now = clock.now() if clock else START
    return Submission(
        packet=packet,
        priority=priority,
        entity_id=entity,
        entity_name=entity,
        entity_type="companion",
        deadline=now + dt.timedelta(seconds=deadline_in),
        origin=origin,
        packet_id=packet_id,
    )


def scheduler(
    clock: ManualClock,
    *,
    sender: RecordingSender | None = None,
    logger: RecordingLogger | None = None,
    transmit_enabled: bool = False,
    budget: AirtimeBudget | None = None,
    **kwargs: object,
) -> TxScheduler:
    return TxScheduler(
        sender=sender,
        radio=EU868_NARROW,
        clock=clock,
        budget=budget,
        transmit_enabled=transmit_enabled,
        logger=logger or RecordingLogger(),
        **kwargs,  # type: ignore[arg-type]
    )


async def drain(sched: TxScheduler, *, turns: int = 40, stop: bool = True) -> None:
    """Run the loop for a while.

    `stop=False` leaves whatever could not be sent still queued — which is the
    difference between "the budget stalled it" and "shutdown dropped it", and
    those are two different outcomes to a caller awaiting a handle.
    """
    task = asyncio.create_task(sched.run())
    for _ in range(turns):
        await asyncio.sleep(0)
    if stop:
        await sched.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# --- Receive-only gate -----------------------------------------------------


async def test_transmit_is_disabled_by_default() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    sched = scheduler(clock, sender=sender)

    handle = sched.submit(submission(clock=clock))
    await drain(sched)

    outcome = await handle
    assert outcome.result is TxResult.SUPPRESSED
    assert sender.sent == []


async def test_no_packet_reaches_the_sender_under_load_in_any_class() -> None:
    """Task 5.12: the milestone's safety property, asserted rather than argued."""
    clock = ManualClock()
    sender = RecordingSender()
    sched = scheduler(clock, sender=sender)

    handles = [
        sched.submit(submission(priority, entity=f"stub-{index}", clock=clock))
        for index in range(4)
        for priority in PriorityClass
    ]
    await drain(sched, turns=200)

    assert sender.sent == []
    outcomes = [await handle for handle in handles]
    assert {o.result for o in outcomes} == {TxResult.SUPPRESSED}


async def test_a_suppressed_packet_is_charged_and_logged_like_a_sent_one() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    sched = scheduler(clock, logger=logger)

    handle = sched.submit(submission(clock=clock))
    await drain(sched)
    outcome = await handle

    assert outcome.result is TxResult.SUPPRESSED
    assert outcome.airtime_ms == pytest.approx(738.3, abs=1)
    assert sched.budget.used_ms(clock.now()) == pytest.approx(738.3, abs=1)

    (event,) = logger.of("packet_tx")
    assert event["tx_result"] == "suppressed"
    assert event["transmit_enabled"] is False
    # Every field a transmitted packet would carry is present, which is what
    # makes a gated run's log comparable with a transmitting one.
    assert {"entity_id", "entity_name", "priority_class", "queue_wait_ms", "airtime_ms"} <= set(
        event
    )


async def test_enabling_transmit_reaches_the_sender() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    sched = scheduler(clock, sender=sender, transmit_enabled=True)

    handle = sched.submit(submission(clock=clock))
    await drain(sched)

    assert (await handle).result is TxResult.TRANSMITTED
    assert sender.sent == [PACKET]


# --- Priority --------------------------------------------------------------


async def test_a_higher_class_overtakes_queued_adverts() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    sched = scheduler(clock, sender=sender, transmit_enabled=True)

    advert = b"\x01" + b"\x00" * 40
    ack = b"\x02" + b"\x00" * 8
    sched.submit(submission(PriorityClass.ADVERT, packet=advert, clock=clock))
    sched.submit(submission(PriorityClass.ACK, packet=ack, clock=clock))
    await drain(sched)

    assert sender.sent[0] == ack


async def test_selection_rotates_between_entities_within_a_class() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    sched = scheduler(clock, sender=sender, transmit_enabled=True)

    for index in range(2):
        sched.submit(
            submission(packet=bytes([0xA0 + index]) + b"\x00" * 20, entity="a", clock=clock)
        )
    sched.submit(submission(packet=b"\xb0" + b"\x00" * 20, entity="b", clock=clock))
    await drain(sched)

    # a, b, a — not a's whole queue first.
    assert [packet[0] for packet in sender.sent] == [0xA0, 0xB0, 0xA1]


async def test_an_in_flight_packet_is_not_cancelled_by_a_higher_class() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    sched = scheduler(clock, sender=sender, transmit_enabled=True)

    advert = b"\x01" + b"\x00" * 40
    sched.submit(submission(PriorityClass.ADVERT, packet=advert, clock=clock))
    task = asyncio.create_task(sched.run())
    await asyncio.sleep(0)
    sched.submit(submission(PriorityClass.ACK, packet=b"\x02" * 9, clock=clock))
    for _ in range(20):
        await asyncio.sleep(0)
    await sched.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sender.sent[0] == advert


# --- The budget ------------------------------------------------------------


def test_the_ceiling_is_360_seconds_an_hour_by_default() -> None:
    budget = AirtimeBudget()
    assert budget.ceiling_fraction == DEFAULT_CEILING_FRACTION
    assert budget.ceiling_ms == 360_000
    assert budget.window_seconds == WINDOW_SECONDS


async def test_the_ceiling_holds_across_a_simulated_day() -> None:
    """No 3600-second interval may contain more than the ceiling. The sentence
    the regulation uses, asserted as the regulation states it."""
    clock = ManualClock()
    sender = RecordingSender()
    sched = scheduler(clock, sender=sender, transmit_enabled=True)
    charged: list[tuple[dt.datetime, float]] = []

    # 15 packets a minute of ~738 ms each is 11.1 s/minute — 665 s in an hour,
    # comfortably over the 360 s ceiling. Submitting *under* the ceiling would
    # make this test pass without the limiter ever engaging.
    task = asyncio.create_task(sched.run())
    for _minute in range(24 * 60):
        before = len(sender.sent)
        for _ in range(15):
            sched.submit(submission(clock=clock, deadline_in=120))
        for _ in range(60):
            await asyncio.sleep(0)
        charged.extend((clock.now(), 738.3) for _ in range(len(sender.sent) - before))
        clock.advance(60)
    await sched.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sender.sent, "nothing was transmitted; the test proves nothing"
    assert sched.stats.dropped > 0, (
        "nothing was ever refused; the offered load did not reach the ceiling "
        "and the limiter was never exercised"
    )

    # Every hour-long window that starts at a transmission, over the whole day,
    # swept linearly: a window can only ever start at a transmission, so this
    # covers every interval that could exceed the ceiling.
    window = dt.timedelta(seconds=WINDOW_SECONDS)
    tail = 0
    running = 0.0
    for head, (start, _ms) in enumerate(charged):
        running += charged[head][1]
        while charged[tail][0] <= start - window:
            running -= charged[tail][1]
            tail += 1
        assert running <= sched.budget.ceiling_ms, (
            f"the hour ending {start.isoformat()} carried {running:.0f} ms, "
            f"over the {sched.budget.ceiling_ms:.0f} ms ceiling"
        )


async def test_an_exhausted_budget_stalls_rather_than_dropping_immediately() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    budget = AirtimeBudget()
    budget.charge(budget.ceiling_ms, clock.now())
    sched = scheduler(clock, sender=sender, transmit_enabled=True, budget=budget)

    handle = sched.submit(submission(clock=clock, deadline_in=3600))
    await drain(sched, stop=False)

    assert sender.sent == []
    assert not handle.done  # still queued, waiting for the window to drain


async def test_the_reserve_stalls_classes_two_and_three_but_not_zero_and_one() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    budget = AirtimeBudget()
    budget.charge(budget.reserve_threshold_ms, clock.now())
    sched = scheduler(clock, sender=sender, transmit_enabled=True, budget=budget)

    ack = b"\x02" + b"\x00" * 8
    sched.submit(submission(PriorityClass.MESSAGE, clock=clock, deadline_in=3600))
    sched.submit(submission(PriorityClass.ADVERT, clock=clock, deadline_in=3600))
    sched.submit(submission(PriorityClass.ACK, packet=ack, clock=clock, deadline_in=3600))
    await drain(sched)

    assert sender.sent == [ack]


async def test_the_full_ceiling_stalls_every_class_including_acks() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    budget = AirtimeBudget()
    budget.charge(budget.ceiling_ms, clock.now())
    sched = scheduler(clock, sender=sender, transmit_enabled=True, budget=budget)

    sched.submit(submission(PriorityClass.ACK, packet=b"\x02" * 9, clock=clock, deadline_in=3600))
    await drain(sched)

    assert sender.sent == []


async def test_the_window_drains_and_traffic_resumes() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    budget = AirtimeBudget()
    budget.charge(budget.ceiling_ms, clock.now())
    sched = scheduler(clock, sender=sender, transmit_enabled=True, budget=budget)
    sched.submit(submission(clock=clock, deadline_in=7200))

    await drain(sched, turns=5, stop=False)
    assert sender.sent == []

    clock.advance(WINDOW_SECONDS + 1)
    await drain(sched, turns=10, stop=False)
    assert sender.sent == [PACKET]


async def test_a_raised_ceiling_warns_at_startup_and_in_the_status() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    budget = AirtimeBudget(ceiling_fraction=0.5)
    sched = scheduler(clock, logger=logger, budget=budget)

    assert logger.of("duty_cycle_ceiling_raised")
    assert sched.status().above_regulatory_default is True
    assert sched.status().as_json()["above_regulatory_default"] is True


# --- Charging --------------------------------------------------------------


async def test_a_failed_transmission_keeps_its_charge() -> None:
    clock = ManualClock()
    sender = RecordingSender(TransmitDone(success=False))
    sched = scheduler(clock, sender=sender, transmit_enabled=True)

    handle = sched.submit(submission(clock=clock))
    await drain(sched)

    assert (await handle).result is TxResult.FAILED
    assert sched.budget.used_ms(clock.now()) > 0


async def test_a_timeout_keeps_its_charge() -> None:
    clock = ManualClock()
    sender = RecordingSender(TransmitTimedOut())
    sched = scheduler(clock, sender=sender, transmit_enabled=True)

    handle = sched.submit(submission(clock=clock))
    await drain(sched)

    outcome = await handle
    assert outcome.result is TxResult.FAILED
    assert outcome.reason == "tx_done_timeout"
    assert sched.budget.used_ms(clock.now()) > 0


async def test_a_link_failure_resolves_the_handle_rather_than_hanging() -> None:
    clock = ManualClock()
    sender = RecordingSender(TransmitFailed(reason="transport reconnected"))
    sched = scheduler(clock, sender=sender, transmit_enabled=True)

    handle = sched.submit(submission(clock=clock))
    await drain(sched)

    outcome = await handle
    assert outcome.result is TxResult.FAILED
    assert outcome.reason == "transport reconnected"


async def test_a_dropped_packet_is_never_charged() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    handle = sched.submit(submission(clock=clock, deadline_in=10))

    clock.advance(30)
    await drain(sched)

    outcome = await handle
    assert outcome.result is TxResult.DROPPED
    assert sched.budget.used_ms(clock.now()) == 0


# --- Busy ------------------------------------------------------------------


async def test_a_busy_modem_requeues_at_the_head_and_retries() -> None:
    clock = ManualClock()
    sender = RecordingSender(TransmitBusy(), TransmitDone(success=True))
    sched = scheduler(clock, sender=sender, transmit_enabled=True)

    handle = sched.submit(submission(clock=clock, deadline_in=600))
    await drain(sched)

    outcome = await handle
    assert outcome.result is TxResult.TRANSMITTED
    assert outcome.attempts == 2
    assert sched.stats.busy_retries == 1
    # Refunded on busy: the modem stated it transmitted nothing, so exactly one
    # packet's airtime is charged rather than two.
    assert sched.budget.used_ms(clock.now()) == pytest.approx(738.3, abs=1)


async def test_a_busy_retry_waits_rather_than_busy_looping() -> None:
    clock = ManualClock()
    sender = RecordingSender(TransmitBusy(), TransmitDone(success=True))
    sched = scheduler(clock, sender=sender, transmit_enabled=True)

    sched.submit(submission(clock=clock, deadline_in=600))
    await drain(sched)

    assert clock.slept > 0


async def test_bounded_busy_retries_end_in_a_logged_drop() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    sender = RecordingSender(TransmitBusy(), TransmitBusy(), TransmitBusy())
    sched = scheduler(clock, sender=sender, logger=logger, transmit_enabled=True)

    handle = sched.submit(submission(clock=clock, deadline_in=600))
    await drain(sched)

    outcome = await handle
    assert outcome.result is TxResult.DROPPED
    assert outcome.reason == DropReason.BUSY_EXHAUSTED
    assert len(sender.sent) == 3


# --- Deadlines and caps ----------------------------------------------------


async def test_a_deadline_that_expires_while_queued_is_a_logged_drop() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    sched = scheduler(clock, logger=logger)

    handle = sched.submit(submission(clock=clock, deadline_in=5))
    clock.advance(10)
    await drain(sched)

    outcome = await handle
    assert outcome.result is TxResult.DROPPED
    assert outcome.reason == DropReason.DEADLINE
    assert outcome.queue_wait_ms == pytest.approx(10_000, abs=1)
    (event,) = [e for e in logger.of("packet_tx") if e["tx_result"] == "dropped"]
    assert event["reason"] == DropReason.DEADLINE


async def test_the_queue_cap_drops_the_oldest_member_of_the_class() -> None:
    clock = ManualClock()
    sched = scheduler(clock, queue_cap=2)

    first = sched.submit(submission(clock=clock, packet_id="first"))
    clock.advance(1)
    second = sched.submit(submission(clock=clock, packet_id="second"))
    clock.advance(1)
    sched.submit(submission(clock=clock, packet_id="third"))

    assert (await first).result is TxResult.DROPPED
    assert (await first).reason == DropReason.QUEUE_FULL
    assert not second.done


async def test_stopping_drains_the_queues_and_says_why() -> None:
    clock = ManualClock()
    sched = scheduler(clock)
    handles = [sched.submit(submission(clock=clock, deadline_in=600)) for _ in range(3)]

    await sched.stop()

    outcomes = [await handle for handle in handles]
    assert {o.result for o in outcomes} == {TxResult.DROPPED}
    assert {o.reason for o in outcomes} == {DropReason.SHUTDOWN}


# --- Radio readback --------------------------------------------------------


async def test_nothing_is_admitted_without_a_radio_readback() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    sched = TxScheduler(
        sender=sender,
        radio=None,
        clock=clock,
        transmit_enabled=True,
        logger=RecordingLogger(),
    )

    handle = sched.submit(submission(clock=clock))
    await drain(sched)

    outcome = await handle
    assert outcome.result is TxResult.DROPPED
    assert outcome.reason == DropReason.NO_RADIO
    assert sender.sent == []


async def test_the_readback_can_arrive_after_construction() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    sched = TxScheduler(
        sender=sender,
        radio=None,
        clock=clock,
        transmit_enabled=True,
        logger=RecordingLogger(),
    )
    sched.set_radio(EU868_NARROW)

    handle = sched.submit(submission(clock=clock))
    await drain(sched)

    assert (await handle).result is TxResult.TRANSMITTED


# --- The wide event --------------------------------------------------------


async def test_the_originating_packet_id_threads_through_to_the_tx_event() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    sched = scheduler(clock, logger=logger, transmit_enabled=True, sender=RecordingSender())

    sched.submit(submission(clock=clock, packet_id="reception-abc"))
    await drain(sched)

    (event,) = logger.of("packet_tx")
    assert event["packet_id"] == "reception-abc"


async def test_the_tx_event_carries_the_design_field_set() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    sched = scheduler(clock, logger=logger, transmit_enabled=True, sender=RecordingSender())

    sched.submit(submission(PriorityClass.ADVERT, clock=clock, origin="advert"))
    await drain(sched)

    (event,) = logger.of("packet_tx")
    assert {
        "packet_id",
        "entity_id",
        "entity_name",
        "entity_type",
        "priority_class",
        "queue_wait_ms",
        "airtime_ms",
        "budget_remaining_pct",
        "attempt",
        "tx_result",
        "acked",
    } <= set(event)
    assert event["priority_class"] == 3


async def test_the_send_timeout_exceeds_the_firmwares_own() -> None:
    """The firmware gives itself 1.5x airtime after a CSMA phase of its own."""
    clock = ManualClock()
    sender = RecordingSender()
    sched = scheduler(clock, sender=sender, transmit_enabled=True)

    sched.submit(submission(clock=clock))
    await drain(sched)

    (timeout,) = sender.timeouts
    assert timeout > 1.5 * 0.7383
    assert timeout >= 6.0
