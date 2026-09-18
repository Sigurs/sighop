"""What the messenger offers to be recorded (`direct-messaging`, design D8).

The sink is on the contact and path stores' contract — never awaits, never
raises — and the tests that matter are the ones about what it may *not* change.
A message is a thing the protocol has already committed to: the acknowledgement
for one we received is computed on decrypt and owed inside a 4-5 second retry
window, and the retry schedule for one we sent is the peer's own arithmetic.
Neither may move because something wanted to write a row.

The concurrency tests are the other half of the milestone's change here: until
now the only caller was `sighop run --send`, a one-shot that owned the process.
A browser is not that, so two sends can be in flight at once — and within one
conversation they still have to reach the air in the order they were submitted.
"""

from __future__ import annotations

import asyncio

import pytest

from sighop.net.bus import IngressPipeline, NetworkBus, PriorityClass, TxResult
from sighop.net.contacts import ContactStore
from sighop.net.dedup import DedupCache
from sighop.net.dm import (
    INBOUND,
    OUTBOUND,
    DirectMessageRecord,
    RecordedOutcome,
    SendResult,
)
from sighop.net.paths import PathStore
from sighop.protocol.crypto import SharedSecretCache
from sighop.radio.modem import EU868_NARROW
from tests.test_dm import (
    Entity,
    RecordingSubmit,
    TickingClock,
    _packet_for,
    ack_record,
    contact_for,
    message_packet,
    messenger,
    zero_hop_route_to,
)
from tests.test_room_exercise import _replayed
from tests.test_tx import RecordingLogger


class Recorder:
    """A sink that takes everything and remembers it, in offer order."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.offered: list[DirectMessageRecord] = []

    def offer(self, record: DirectMessageRecord) -> bool:
        self.offered.append(record)
        return self.accept

    def by_ref(self, ref: str) -> list[DirectMessageRecord]:
        return [record for record in self.offered if record.ref == ref]


class RaisingSink:
    """A sink that is broken in the worst way a sink can be broken.

    It records how much the scheduler had been given at the moment it was
    offered anything, which is how the ordering rule — acknowledge first, record
    second — is asserted rather than assumed.
    """

    def __init__(self, submit: RecordingSubmit) -> None:
        self.submit = submit
        self.offers = 0
        self.submissions_at_first_offer: int | None = None

    def offer(self, record: DirectMessageRecord) -> bool:
        self.offers += 1
        if self.submissions_at_first_offer is None:
            self.submissions_at_first_offer = len(self.submit.submissions)
        raise RuntimeError("the database is on fire")


def _peer_setup(name: str = "peer"):
    """One local entity, one routed contact, and the pieces around them."""
    us, them = Entity("us"), Entity(name)
    contacts = ContactStore(logger=RecordingLogger())
    contacts.restore([contact_for(them.identity, name)])
    contact = contacts.contacts()[0]
    paths = PathStore()
    zero_hop_route_to(paths, them.identity.public_key)
    return us, them, contacts, contact, paths


# --- 4.1 A send is offered at submission and again when it resolves ----------


async def test_a_send_is_offered_at_submission_and_again_with_its_outcome() -> None:
    """4.1: two offers, one message — joined by `ref`, so a store can update."""
    us, _them, contacts, contact, paths = _peer_setup()
    submit = RecordingSubmit()
    records = Recorder()
    dm = messenger(us, contacts=contacts, paths=paths, submit=submit, records=records)

    outcome = await dm.send(us, contact, "hello")
    assert outcome.result is SendResult.UNACKNOWLEDGED

    offered = records.by_ref(outcome.message_id)
    assert len(offered) == 2, "a send is recorded at submission and at resolution"
    submitted, resolved = offered

    assert submitted.outcome is RecordedOutcome.IN_FLIGHT
    assert submitted.resolved is False
    assert submitted.direction == OUTBOUND
    assert submitted.entity_public_key == us.identity.public_key
    assert submitted.peer_public_key == contact.public_key
    assert submitted.text == b"hello"
    assert submitted.packet_ids == ()

    assert resolved.outcome is RecordedOutcome.UNACKNOWLEDGED
    assert resolved.ref == submitted.ref, "the two offers must be one message"
    assert resolved.attempts == outcome.attempts
    assert resolved.packet_ids == outcome.packet_ids
    assert resolved.route_flood is False
    assert resolved.route_path == b""


async def test_an_acknowledged_send_records_its_latency_and_attempt_count() -> None:
    """4.1: the outcome offered is the outcome reported, field for field."""
    us, _them, contacts, contact, paths = _peer_setup()
    clock = TickingClock()
    submit = RecordingSubmit()
    records = Recorder()
    dm = messenger(us, contacts=contacts, paths=paths, submit=submit, clock=clock, records=records)

    send = asyncio.create_task(dm.send(us, contact, "hello"))
    await asyncio.sleep(0)
    expected = next(iter(dm._pending_by_checksum))
    dm._handle_ack(ack_record(expected, at=clock.now()), _ack_payload(expected))
    outcome = await send

    assert outcome.result is SendResult.ACKNOWLEDGED
    resolved = records.by_ref(outcome.message_id)[-1]
    assert resolved.outcome is RecordedOutcome.ACKNOWLEDGED
    assert resolved.resolved is True
    assert resolved.attempts == outcome.attempts
    assert resolved.ack_latency_ms == pytest.approx(outcome.ack_latency_ms)


async def test_a_dropped_send_is_recorded_as_dropped() -> None:
    """4.1: a message that never reached the air is history too, and says so."""
    us, _them, contacts, contact, paths = _peer_setup()
    submit = RecordingSubmit(TxResult.DROPPED)
    records = Recorder()
    dm = messenger(us, contacts=contacts, paths=paths, submit=submit, records=records)

    outcome = await dm.send(us, contact, "hello")

    assert outcome.result is SendResult.DROPPED
    assert records.by_ref(outcome.message_id)[-1].outcome is RecordedOutcome.DROPPED


async def test_a_sink_that_raises_changes_neither_the_schedule_nor_the_outcome() -> None:
    """4.1: the same send, twice — once with a broken sink, once with none."""
    us, _them, contacts, contact, paths = _peer_setup()

    plain_submit = RecordingSubmit()
    plain = messenger(us, contacts=contacts, paths=paths, submit=plain_submit)
    plain_outcome = await plain.send(us, contact, "hello")

    broken_submit = RecordingSubmit()
    sink = RaisingSink(broken_submit)
    broken = messenger(us, contacts=contacts, paths=paths, submit=broken_submit, records=sink)
    broken_outcome = await broken.send(us, contact, "hello")

    assert sink.offers == 2, "the broken sink was offered both records"
    assert broken_outcome.result is plain_outcome.result
    assert broken_outcome.attempts == plain_outcome.attempts
    assert len(broken_submit.submissions) == len(plain_submit.submissions)
    # The retry schedule, attempt by attempt. Both runs start from the same
    # clock, so the deadlines are directly comparable — and the deadline is the
    # acknowledgement window, which is the thing a durable write must never move.
    assert [s.deadline for s in broken_submit.submissions] == [
        s.deadline for s in plain_submit.submissions
    ]
    assert broken.records_refused == 2, "a failed record is counted, not swallowed"


async def test_with_no_sink_every_behaviour_is_what_it_was() -> None:
    """4.1: the absence of a sink changes nothing, asserted against a wired one."""
    us, _them, contacts, contact, paths = _peer_setup()

    without_submit = RecordingSubmit()
    without = messenger(us, contacts=contacts, paths=paths, submit=without_submit)
    without_outcome = await without.send(us, contact, "hello")

    with_submit = RecordingSubmit()
    with_sink = messenger(
        us, contacts=contacts, paths=paths, submit=with_submit, records=Recorder()
    )
    with_outcome = await with_sink.send(us, contact, "hello")

    assert with_outcome.result is without_outcome.result
    assert with_outcome.attempts == without_outcome.attempts
    assert with_outcome.packet_ids == without_outcome.packet_ids
    assert [s.priority for s in with_submit.submissions] == [
        s.priority for s in without_submit.submissions
    ]
    assert [s.origin for s in with_submit.submissions] == [
        s.origin for s in without_submit.submissions
    ]
    assert without.as_json()["records_offered"] == 0


# --- 4.2 A reception is recorded after its acknowledgement -------------------


def _ack_payload(checksum: bytes):
    from sighop.protocol.payloads import Acknowledgement

    return Acknowledgement(checksum=checksum)


async def _receive(dm, *, sender: Entity, recipient: Entity, text: bytes = b"hi there"):
    secret = SharedSecretCache().get(sender.identity, recipient.identity.public_key)
    packet, _plaintext = message_packet(
        sender=sender, recipient_node_hash=recipient.node_hash, secret=secret, text=text
    )
    await dm.handle(_packet_for(packet))


async def test_a_received_message_is_recorded_under_the_identity_it_reached() -> None:
    """4.2: keyed by the local identity and the contact whose key decrypted it."""
    them, us = Entity("them"), Entity("us")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(them.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, them.identity.public_key)
    records = Recorder()
    dm = messenger(us, contacts=contacts, paths=paths, records=records)

    await _receive(dm, sender=them, recipient=us, text=b"hej \xff not utf-8")

    assert len(records.offered) == 1
    stored = records.offered[0]
    assert stored.direction == INBOUND
    assert stored.outcome is RecordedOutcome.RECEIVED
    assert stored.entity_public_key == us.identity.public_key
    assert stored.peer_public_key == them.identity.public_key
    assert stored.text == b"hej \xff not utf-8", "stored as the bytes that arrived"
    assert stored.rendered().text == "hej � not utf-8"
    assert stored.packet_ids == (stored.ref,)


async def test_the_acknowledgement_reaches_the_scheduler_before_the_sink_is_offered() -> None:
    """4.2, design D8: acknowledge, then record — asserted with a sink that raises.

    A sink that raises is the sharpest form of the question. If recording came
    first, the acknowledgement this peer is waiting for inside its retry window
    would never be submitted at all.
    """
    them, us = Entity("them"), Entity("us")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(them.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, them.identity.public_key)
    submit = RecordingSubmit()
    sink = RaisingSink(submit)
    events: list = []
    dm = messenger(us, contacts=contacts, paths=paths, submit=submit, records=sink, events=events)

    await _receive(dm, sender=them, recipient=us)

    assert sink.offers == 1
    assert sink.submissions_at_first_offer == 1, (
        "the acknowledgement had not been submitted when the record was offered"
    )
    assert submit.submissions[0].priority is PriorityClass.ACK
    from sighop.net.dm import MessageReceived

    received = next(event for event in events if isinstance(event, MessageReceived))
    assert received.acknowledged is True, "a broken sink must not unacknowledge a message"


# --- 4.3 Concurrent sends, ordered within a conversation --------------------


async def test_two_sends_to_different_peers_do_not_wait_on_each_other() -> None:
    """4.3: different conversations are unrelated and proceed together."""
    us = Entity("us")
    first, second = Entity("first"), Entity("second")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.restore([contact_for(first.identity, "first"), contact_for(second.identity, "second")])
    one = next(c for c in contacts.contacts() if c.public_key == first.identity.public_key)
    two = next(c for c in contacts.contacts() if c.public_key == second.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, first.identity.public_key)
    zero_hop_route_to(paths, second.identity.public_key)
    submit = RecordingSubmit()
    dm = messenger(us, contacts=contacts, paths=paths, submit=submit)

    a = asyncio.create_task(dm.send(us, one, "to first"))
    b = asyncio.create_task(dm.send(us, two, "to second"))
    await asyncio.sleep(0)

    assert len(submit.submissions) == 2, (
        "the second send waited on the first's acknowledgement window"
    )
    await asyncio.gather(a, b)


async def test_two_sends_to_one_peer_transmit_in_submission_order() -> None:
    """4.3: within one conversation, the first message reaches the air first."""
    us, _them, contacts, contact, paths = _peer_setup()
    submit = RecordingSubmit()
    dm = messenger(us, contacts=contacts, paths=paths, submit=submit)

    first = asyncio.create_task(dm.send(us, contact, "first"))
    await asyncio.sleep(0)
    second = asyncio.create_task(dm.send(us, contact, "second"))
    await asyncio.sleep(0)

    assert len(submit.submissions) == 1, "the second send overlapped the first"

    outcomes = await asyncio.gather(first, second)
    assert [outcome.result for outcome in outcomes] == [
        SendResult.UNACKNOWLEDGED,
        SendResult.UNACKNOWLEDGED,
    ]
    # Four attempts each, in order: the whole of the first send precedes the
    # whole of the second.
    assert len(submit.submissions) == 8
    assert submit.submissions[0].packet != submit.submissions[4].packet


async def test_a_queued_send_is_recorded_as_submitted_in_its_own_order() -> None:
    """4.3 + 4.1: a message waiting its turn is history already, and in order.

    Recording at submission rather than at first transmission is what makes a
    composed message visible while it waits — and the order it is recorded in
    is the order it was submitted in, not the order the radio got to it.
    """
    us, _them, contacts, contact, paths = _peer_setup()
    records = Recorder()
    dm = messenger(us, contacts=contacts, paths=paths, records=records)

    first = asyncio.create_task(dm.send(us, contact, "first"))
    await asyncio.sleep(0)
    second = asyncio.create_task(dm.send(us, contact, "second"))
    await asyncio.sleep(0)

    submitted = [record for record in records.offered if not record.resolved]
    assert [record.text for record in submitted] == [b"first", b"second"]

    await asyncio.gather(first, second)


async def test_each_acknowledgement_matches_its_own_message() -> None:
    """4.3: two conversations in flight, two expectations, no cross-talk."""
    us = Entity("us")
    first, second = Entity("first"), Entity("second")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.restore([contact_for(first.identity, "first"), contact_for(second.identity, "second")])
    one = next(c for c in contacts.contacts() if c.public_key == first.identity.public_key)
    two = next(c for c in contacts.contacts() if c.public_key == second.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, first.identity.public_key)
    zero_hop_route_to(paths, second.identity.public_key)
    clock = TickingClock()
    records = Recorder()
    dm = messenger(us, contacts=contacts, paths=paths, clock=clock, records=records)

    a = asyncio.create_task(dm.send(us, one, "to first"))
    b = asyncio.create_task(dm.send(us, two, "to second"))
    await asyncio.sleep(0)

    assert len(dm._pending_by_checksum) == 2, "both sends are outstanding at once"
    # Answer only the second conversation's expectation.
    pending_for_two = next(
        checksum
        for checksum, pending in dm._pending_by_checksum.items()
        if pending.contact.public_key == two.public_key
    )
    dm._handle_ack(ack_record(pending_for_two, at=clock.now()), _ack_payload(pending_for_two))

    first_outcome, second_outcome = await asyncio.gather(a, b)
    assert second_outcome.result is SendResult.ACKNOWLEDGED
    assert first_outcome.result is SendResult.UNACKNOWLEDGED

    resolved = {
        record.peer_public_key: record.outcome for record in records.offered if record.resolved
    }
    assert resolved[two.public_key] is RecordedOutcome.ACKNOWLEDGED
    assert resolved[one.public_key] is RecordedOutcome.UNACKNOWLEDGED


# --- 4.4 The corpus replays identically with the sink wired in --------------


async def _pipeline_counts(*, with_sink: bool) -> dict[str, int]:
    """Replay the corpus through the live pipeline, with and without recording.

    The assertion milestones 6 and 7 both used, applied to milestone 8's own
    addition: whether anything is being written down must be invisible to the
    reception path.
    """
    bus = NetworkBus(logger=RecordingLogger())
    pipeline = IngressPipeline(
        bus=bus,
        dedup=DedupCache(),
        paths=PathStore(),
        logger=RecordingLogger(),
        radio=EU868_NARROW,
    )
    contacts = ContactStore(logger=RecordingLogger())
    contacts.subscribe(bus)
    us = Entity("us")
    dm = messenger(
        us,
        contacts=contacts,
        paths=pipeline.paths,
        records=Recorder() if with_sink else None,
    )
    dm.subscribe(bus)

    for record in _replayed():
        pipeline.ingest(record)
    # Every subscriber drains before anything is counted, for the reason the
    # room exercise gives: a contact learned by a subscriber that had not yet
    # run would make the two sides differ for a reason unrelated to the sink.
    await bus.aclose()

    return {
        "delivered": pipeline.delivered,
        "duplicates": pipeline.dedup.stats.duplicates,
        "considered": pipeline.dedup.stats.considered,
        "contacts": len(contacts),
        "paths": pipeline.paths.destination_count,
    }


async def test_the_reception_path_is_identical_with_and_without_the_record_sink() -> None:
    """4.4: wiring somewhere to write conversations down changed no count."""
    without = await _pipeline_counts(with_sink=False)
    with_sink = await _pipeline_counts(with_sink=True)

    assert with_sink == without, "wiring the record sink changed the reception path"
    assert without["delivered"] > 0, "the comparison would be vacuous"
