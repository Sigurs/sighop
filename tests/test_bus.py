"""The virtual network bus (milestone 3, `net-bus`, design D12/D14).

The scenarios that matter here are the failure ones: a subscriber that stops
consuming, and a subscriber that raises. Both must be survivable, because the
radio keeps receiving either way and the alternative is losing frames inside the
modem's buffer where nothing can log them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import replace
from typing import cast

import pytest

from sighop.net.bus import (
    IngressPipeline,
    NetworkBus,
    PriorityClass,
    Submission,
    TxHandle,
    TxOutcome,
    TxResult,
)
from sighop.net.dedup import DedupCache
from sighop.net.rx import RxRecord, decode_event
from sighop.radio.modem import EU868_NARROW
from sighop.radio.replay import CaptureReplay
from tests.protocol.corpus import CAPTURE_FILES, CAPTURES_DIR


@pytest.fixture(scope="module")
def corpus_records() -> list[RxRecord]:
    records: list[RxRecord] = []
    for name in CAPTURE_FILES:
        replay = CaptureReplay.open(CAPTURES_DIR / name)
        records.extend(decode_event(event) for event in replay.read())
    return records


@pytest.fixture
def record(corpus_records: list[RxRecord]) -> RxRecord:
    return next(r for r in corpus_records if not r.failed and r.packet is not None)


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", event, fields))

    def names(self) -> list[str]:
        return [name for _level, name, _fields in self.events]


def _copy(record: RxRecord, packet_id: str, *, after: float = 0.0) -> RxRecord:
    return replace(
        record,
        packet_id=packet_id,
        received_at=record.received_at + dt.timedelta(seconds=after),
    )


# --- Fan-out ---------------------------------------------------------------


async def test_every_subscriber_receives_every_reception(record) -> None:
    bus = NetworkBus(logger=RecordingLogger())
    subs = [bus.subscribe(f"entity-{index}") for index in range(3)]

    delivered = bus.publish(record)

    assert delivered == 3
    for subscription in subs:
        assert subscription.queue.get_nowait() is record


async def test_the_bus_filters_nothing_on_a_subscribers_behalf(record) -> None:
    """A destination hash matching several entities must reach all of them (§3)."""
    bus = NetworkBus(logger=RecordingLogger())
    first = bus.subscribe("room-server")
    second = bus.subscribe("companion")

    bus.publish(record)

    assert first.queue.qsize() == 1
    assert second.queue.qsize() == 1


async def test_a_subscriber_attached_later_sees_only_later_receptions(record) -> None:
    bus = NetworkBus(logger=RecordingLogger())
    early = bus.subscribe("early")
    bus.publish(record)

    late = bus.subscribe("late")
    bus.publish(_copy(record, "second", after=1))

    assert early.queue.qsize() == 2
    assert late.queue.qsize() == 1


async def test_an_unsubscribed_subscriber_stops_receiving(record) -> None:
    bus = NetworkBus(logger=RecordingLogger())
    subscription = bus.subscribe("leaving")
    bus.unsubscribe(subscription)

    assert bus.publish(record) == 0


# --- Isolation -------------------------------------------------------------


async def test_a_full_queue_drops_for_that_subscriber_only(record) -> None:
    logger = RecordingLogger()
    bus = NetworkBus(logger=logger)
    stalled = bus.subscribe("stalled", queue_size=1)
    healthy = bus.subscribe("healthy", queue_size=8)

    for index in range(4):
        bus.publish(_copy(record, f"packet-{index}", after=index))

    assert stalled.stats.delivered == 1
    assert stalled.stats.dropped == 3
    assert healthy.stats.delivered == 4
    assert healthy.stats.dropped == 0


async def test_an_overflow_names_the_subscriber_in_a_wide_event(record) -> None:
    logger = RecordingLogger()
    bus = NetworkBus(logger=logger)
    bus.subscribe("stalled", queue_size=1)

    bus.publish(record)
    bus.publish(_copy(record, "overflowing", after=1))

    overflow = [e for e in logger.events if e[1] == "bus_queue_overflow"]
    assert len(overflow) == 1
    level, _name, fields = overflow[0]
    assert level == "error"
    assert fields["subscriber"] == "stalled"
    assert fields["capacity"] == 1
    assert fields["packet_id"] == "overflowing"


async def test_publishing_never_awaits_a_subscriber(record) -> None:
    """Publish is synchronous by construction: a stalled entity cannot block it."""
    bus = NetworkBus(logger=RecordingLogger())
    bus.subscribe("stalled", queue_size=1)

    # No await anywhere in this call: if publish ever became a coroutine, or
    # awaited a queue, this test stops compiling rather than merely hanging.
    assert isinstance(bus.publish(record), int)
    assert isinstance(bus.publish(_copy(record, "x", after=1)), int)


async def test_a_raising_subscriber_stays_attached_and_others_are_unaffected(record) -> None:
    logger = RecordingLogger()
    bus = NetworkBus(logger=logger)
    seen_by_broken: list[str] = []
    seen_by_healthy: list[str] = []

    async def broken(rec: RxRecord) -> None:
        seen_by_broken.append(rec.packet_id)
        raise RuntimeError("entity is having a bad day")

    async def healthy(rec: RxRecord) -> None:
        seen_by_healthy.append(rec.packet_id)

    bus.subscribe("broken", handler=broken)
    bus.subscribe("healthy", handler=healthy)

    bus.publish(_copy(record, "first"))
    bus.publish(_copy(record, "second", after=1))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert seen_by_broken == ["first", "second"]
    assert seen_by_healthy == ["first", "second"]
    errors = [e for e in logger.events if e[1] == "bus_subscriber_error"]
    assert len(errors) == 2
    assert errors[0][2]["subscriber"] == "broken"
    await bus.aclose()


# --- Submission ------------------------------------------------------------


def _submission(**overrides: object) -> Submission:
    base = {
        "packet": b"\x00" * 32,
        "priority": PriorityClass.MESSAGE,
        "entity_id": "stub-1",
        "deadline": dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30),
    }
    base.update(overrides)
    return Submission(**base)  # type: ignore[arg-type]


class ResolvingSink:
    """A scheduler stand-in that resolves each submission as instructed."""

    def __init__(self, result: TxResult, reason: str = "") -> None:
        self.result = result
        self.reason = reason
        self.submissions: list[Submission] = []

    def submit(self, submission: Submission) -> TxHandle:
        self.submissions.append(submission)
        handle = TxHandle(submission)
        handle.resolve(
            TxOutcome(
                result=self.result,
                packet_id=submission.packet_id or "minted",
                airtime_ms=738.0,
                queue_wait_ms=0.0,
                attempts=1,
                reason=self.reason,
            )
        )
        return handle


@pytest.mark.parametrize(
    "result",
    [TxResult.TRANSMITTED, TxResult.FAILED, TxResult.SUPPRESSED, TxResult.DROPPED],
)
async def test_a_handle_resolves_for_every_outcome(result: TxResult) -> None:
    sink = ResolvingSink(result)
    bus = NetworkBus(tx_sink=sink, logger=RecordingLogger())

    outcome = await bus.submit(_submission())

    assert outcome.result is result
    assert outcome.sent is (result is TxResult.TRANSMITTED)


async def test_suppressed_is_neither_success_nor_failure() -> None:
    sink = ResolvingSink(TxResult.SUPPRESSED, reason="transmit disabled")
    bus = NetworkBus(tx_sink=sink, logger=RecordingLogger())

    outcome = await bus.submit(_submission(priority=PriorityClass.ADVERT))

    assert outcome.result is TxResult.SUPPRESSED
    assert not outcome.sent
    assert outcome.as_json()["tx_result"] == "suppressed"


async def test_submitting_without_a_scheduler_is_an_error() -> None:
    bus = NetworkBus(logger=RecordingLogger())

    with pytest.raises(RuntimeError, match="no transmit scheduler"):
        bus.submit(_submission())


# --- The ingress pipeline --------------------------------------------------


async def test_a_duplicate_is_logged_and_not_fanned_out(record) -> None:
    logger = RecordingLogger()
    bus = NetworkBus(logger=RecordingLogger())
    subscription = bus.subscribe("entity")
    pipeline = IngressPipeline(bus=bus, logger=logger)

    assert pipeline.ingest(record) is True
    assert pipeline.ingest(_copy(record, "repeat", after=2)) is False

    assert subscription.queue.qsize() == 1
    assert pipeline.duplicates == 1
    dup_events = [f for _l, name, f in logger.events if name == "packet_rx" and f.get("dup")]
    assert len(dup_events) == 1
    assert dup_events[0]["dup_of_packet_id"] == record.packet_id


async def test_every_reception_still_produces_exactly_one_wide_event(record) -> None:
    logger = RecordingLogger()
    bus = NetworkBus(logger=RecordingLogger())
    pipeline = IngressPipeline(bus=bus, logger=logger)

    pipeline.ingest(record)
    pipeline.ingest(_copy(record, "repeat", after=1))

    assert logger.names() == ["packet_rx", "packet_rx"]


async def test_the_pipeline_learns_paths_as_it_publishes(corpus_records) -> None:
    bus = NetworkBus(logger=RecordingLogger())
    pipeline = IngressPipeline(bus=bus, logger=RecordingLogger())

    for rec in corpus_records:
        pipeline.ingest(rec)

    assert pipeline.paths.destination_count > 0
    assert pipeline.delivered + pipeline.duplicates == len(corpus_records)


async def test_the_rx_event_carries_airtime_when_the_radio_is_known(record) -> None:
    logger = RecordingLogger()
    bus = NetworkBus(logger=RecordingLogger())
    pipeline = IngressPipeline(bus=bus, logger=logger, radio=EU868_NARROW)

    pipeline.ingest(record)

    fields = logger.events[0][2]
    assert cast(float, fields["airtime_ms"]) > 0


async def test_the_rx_event_omits_airtime_when_the_radio_is_unknown(record) -> None:
    logger = RecordingLogger()
    bus = NetworkBus(logger=RecordingLogger())
    pipeline = IngressPipeline(bus=bus, logger=logger)

    pipeline.ingest(record)

    assert "airtime_ms" not in logger.events[0][2]


async def test_undecodable_receptions_are_published_every_time(corpus_records) -> None:
    """They are never deduped, so both copies must reach the bus (design D10)."""
    from sighop.radio.modem import UnparsedEvent

    bus = NetworkBus(logger=RecordingLogger())
    subscription = bus.subscribe("entity")
    pipeline = IngressPipeline(bus=bus, dedup=DedupCache(), logger=RecordingLogger())

    event = UnparsedEvent(raw=b"\x80\x01", reason="unrecognized command byte")
    pipeline.ingest(decode_event(event))
    pipeline.ingest(decode_event(event))

    assert subscription.queue.qsize() == 2
