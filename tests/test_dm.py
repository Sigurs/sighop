"""Direct messages, both directions (milestone 4, `direct-messaging`, D4/D7-D10).

**A loopback proves self-consistency and nothing more.** Every exchange in this
module has sighop on both ends: the same composition code writes the plaintext
that the same decryption code reads, so a shared misreading of the firmware
would pass every test here. Section 8 of the change's tasks — a real MeshCore
peer encrypting to a key we hold — is what confirms the wire format, and the
known-answer test it produces is what keeps it confirmed.

What a loopback *does* prove is that the pieces compose: the destination-hash
fan-out finds the right entity, a wrong entity's MAC fails, the acknowledgement
a receiver computes is the one the sender expected, and a send that goes
unanswered resolves out loud rather than vanishing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from unittest.mock import patch

import pytest

from sighop.net.bus import NetworkBus, PriorityClass, TxOutcome, TxResult
from sighop.net.contacts import Contact, ContactStore
from sighop.net.dm import (
    DIRECT_SEND_PERHOP_EXTRA_MS,
    DIRECT_SEND_PERHOP_FACTOR,
    FLOOD_SEND_TIMEOUT_FACTOR,
    MAX_ATTEMPT,
    MAX_TEXT_LEN,
    SEND_TIMEOUT_BASE_MS,
    AckMatched,
    AckUnmatched,
    DirectMessenger,
    MessageReceived,
    MessageSent,
    MessageTooLongError,
    MessageUndecryptable,
    MessageUnparsable,
    NoRouteError,
    Route,
    SendResult,
    ack_timeout_ms,
    build_message_packet,
    choose_route,
    compose_body,
)
from sighop.net.airtime import NoRadioReadback
from sighop.net.paths import LearnedPath, PathKey, PathStore
from sighop.net.readback import RadioReadbackTimeout, wait_for_readback
from sighop.net.rx import decode_event
from sighop.protocol.crypto import SharedSecretCache, ack_checksum_for, encrypt_then_mac
from sighop.protocol.identity import LocalIdentity, generate_identity
from sighop.protocol.packet import (
    PAYLOAD_VERSION_1,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
)
from sighop.protocol.packet import encode as encode_packet
from sighop.protocol.payloads import (
    Acknowledgement,
    DirectEnvelope,
    TextType,
    build_ack,
    build_direct_envelope,
)
from sighop.radio.modem import EU868_NARROW, RxEvent, RxMeta
from tests.test_tx import RecordingLogger

START = dt.datetime(2026, 9, 5, 21, 0, tzinfo=dt.UTC)


class TickingClock:
    """A clock whose sleeps move time, so a four-attempt retry runs instantly.

    `tests/test_tx.py`'s `ManualClock` deliberately does *not* advance on sleep,
    because a status loop asking for an hour would otherwise fast-forward the
    whole system. Here the sleep *is* the thing under test — the acknowledgement
    window — so it advances, and no other loop is running to be surprised by it.
    """

    def __init__(self, start: dt.datetime = START) -> None:
        self._now = start
        self.slept = 0.0

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.advance(seconds)
        await asyncio.sleep(0)


class Entity:
    """A local entity, the shape `net/adverts.py`'s stub already has."""

    def __init__(self, name: str, identity: LocalIdentity | None = None) -> None:
        self.entity_id = name
        self.name = name
        self.identity = identity or generate_identity()

    @property
    def node_hash(self) -> int:
        return self.identity.node_hash


class RecordingSubmit:
    """Stands in for the scheduler: records submissions, answers as instructed."""

    def __init__(self, *outcomes: TxResult) -> None:
        self.submissions: list = []
        self.outcomes = list(outcomes)

    def __call__(self, submission):
        self.submissions.append(submission)
        result = self.outcomes.pop(0) if self.outcomes else TxResult.TRANSMITTED

        class _Handle:
            def __await__(_self):
                async def resolve() -> TxOutcome:
                    return TxOutcome(
                        result=result,
                        packet_id=f"pkt{len(self.submissions)}",
                        airtime_ms=1.0,
                        queue_wait_ms=0.0,
                        attempts=1,
                        reason="deadline_expired" if result is TxResult.DROPPED else "",
                    )

                return resolve().__await__()

        return _Handle()


def messenger(
    *entities: Entity,
    contacts: ContactStore | None = None,
    paths: PathStore | None = None,
    submit: RecordingSubmit | None = None,
    clock: TickingClock | None = None,
    events: list | None = None,
    allow_flood: bool = False,
    records=None,
    radio=EU868_NARROW,
    radio_ready: asyncio.Event | None = None,
    logger: RecordingLogger | None = None,
) -> DirectMessenger:
    return DirectMessenger(
        contacts=contacts or ContactStore(logger=RecordingLogger()),
        paths=paths or PathStore(),
        submit=submit or RecordingSubmit(),
        entities=entities,
        clock=clock or TickingClock(),
        radio=radio,
        radio_ready=radio_ready,
        allow_flood=allow_flood,
        on_event=events.append if events is not None else None,
        logger=logger or RecordingLogger(),
        records=records,
    )


def zero_hop_route_to(paths: PathStore, public_key: bytes, *, at: dt.datetime = START) -> None:
    paths._insert(  # the store learns this from a reception; here we state it
        PathKey.for_public_key(public_key),
        LearnedPath(
            path=b"",
            hash_size=1,
            hop_count=0,
            snr_db=8.0,
            confirmed_at=at,
            packet_id="seed",
        ),
    )


def contact_for(identity: LocalIdentity, name: str = "peer") -> Contact:
    from sighop.protocol.payloads import WireText

    return Contact(
        public_key=identity.public_key,
        name=WireText.from_bytes(name.encode()),
        advert_verified=True,
    )


# --- Composition (task 3.1, 3.2) -------------------------------------------


def test_the_composed_plaintext_matches_a_hand_built_vector() -> None:
    """`timestamp(4, LE) ‖ (attempt & 3) | (txt_type << 2) ‖ text`."""
    from sighop.protocol.payloads import build_text_message_body

    body = compose_body(timestamp=0x11223344, attempt=2, text=b"hi")

    assert build_text_message_body(body) == bytes.fromhex("44332211") + bytes([0x02]) + b"hi"


def test_the_flags_byte_carries_the_text_type_above_the_attempt() -> None:
    from sighop.protocol.payloads import build_text_message_body

    body = compose_body(
        timestamp=0, attempt=1, text=b"x", txt_type=TextType.CLI_DATA
    )

    assert build_text_message_body(body)[4] == (1 & 3) | (int(TextType.CLI_DATA) << 2)


def test_no_nul_terminator_is_transmitted_or_hashed() -> None:
    """`composeMsgPacket` copies one into its buffer and sends `5 + text_len`."""
    from sighop.protocol.payloads import build_text_message_body

    text = b"hello there"
    body = compose_body(timestamp=1, attempt=0, text=text)
    plaintext = build_text_message_body(body)

    assert len(plaintext) == 5 + len(text)
    assert not plaintext.endswith(b"\x00")
    assert body.ack_prefix == plaintext, "the ACK is hashed over other bytes than were sent"


def test_text_above_the_firmware_limit_is_refused_before_anything_is_queued() -> None:
    submit = RecordingSubmit()
    with pytest.raises(MessageTooLongError, match=str(MAX_TEXT_LEN)):
        compose_body(timestamp=0, attempt=0, text=b"x" * (MAX_TEXT_LEN + 1))

    assert submit.submissions == []


async def test_an_oversized_message_never_reaches_the_scheduler() -> None:
    entity = Entity("us")
    peer = Entity("them")
    submit = RecordingSubmit()
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)
    dm = messenger(entity, paths=paths, submit=submit)

    with pytest.raises(MessageTooLongError):
        await dm.send(entity, contact_for(peer.identity), "x" * (MAX_TEXT_LEN + 1))

    assert submit.submissions == []


# --- Routing (task 3.4) ----------------------------------------------------


def test_a_zero_hop_route_yields_an_empty_path_direct_packet() -> None:
    entity = Entity("us")
    peer = Entity("them")
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)

    route = choose_route(paths, contact_for(peer.identity))
    packet, _ = build_message_packet(
        sender=entity.identity,
        recipient_node_hash=peer.node_hash,
        secret=b"\x01" * 32,
        body=compose_body(timestamp=1, attempt=0, text=b"hi"),
        route=route,
    )

    from sighop.protocol.packet import decode

    decoded = decode(packet)
    assert route.flood is False
    assert decoded.route_type is RouteType.DIRECT
    assert decoded.path == b""
    assert decoded.hop_count == 0


def test_an_unknown_route_refuses_to_send_without_the_flood_flag() -> None:
    paths = PathStore()

    with pytest.raises(NoRouteError, match="no route is known"):
        choose_route(paths, contact_for(generate_identity()))


def test_an_unknown_route_floods_when_flooding_was_permitted() -> None:
    route = choose_route(
        PathStore(), contact_for(generate_identity()), allow_flood=True
    )

    assert route.flood is True
    assert route.label == "FLOOD"


def test_a_route_found_only_by_node_hash_is_marked_ambiguous() -> None:
    """An inbound direct message names a hash, so its ACK has nothing else."""
    peer = generate_identity()
    paths = PathStore()
    paths._insert(
        PathKey.for_node_hash(peer.node_hash),
        LearnedPath(b"", 1, 0, None, START, "seed"),
    )

    route = choose_route(paths, contact_for(peer))

    assert route.ambiguous is True
    assert route.label.endswith("?")


# --- The acknowledgement (tasks 3.5, 3.6) ----------------------------------


def test_the_expected_acknowledgement_is_the_firmware_construction() -> None:
    import hashlib

    from sighop.protocol.payloads import build_text_message_body

    entity = Entity("us")
    body = compose_body(timestamp=1_700_000_000, attempt=0, text=b"hej")
    plaintext = build_text_message_body(body)

    expected = ack_checksum_for(body, entity.identity.public_key)

    assert expected == hashlib.sha256(
        plaintext + entity.identity.public_key
    ).digest()[:4]
    assert len(expected) == 4


@pytest.mark.parametrize("hops", [0, 1, 3])
def test_the_direct_timeout_is_the_peers_formula(hops: int) -> None:
    route = Route(flood=False, path=b"\x01" * hops, hash_size=1, hop_count=hops)

    assert ack_timeout_ms(100.0, route) == pytest.approx(
        SEND_TIMEOUT_BASE_MS
        + (100.0 * DIRECT_SEND_PERHOP_FACTOR + DIRECT_SEND_PERHOP_EXTRA_MS) * (hops + 1)
    )


def test_the_flood_timeout_is_the_peers_formula() -> None:
    assert ack_timeout_ms(100.0, Route(flood=True)) == pytest.approx(
        SEND_TIMEOUT_BASE_MS + FLOOD_SEND_TIMEOUT_FACTOR * 100.0
    )


def test_a_zero_hop_direct_message_uses_the_single_hop_form() -> None:
    """The spec's worked example: `500 + (6t + 250)`."""
    assert ack_timeout_ms(100.0, Route(flood=False)) == pytest.approx(500 + (600 + 250))


# --- Sending (tasks 3.7 - 3.9) ---------------------------------------------


def _packet_for(record_bytes: bytes, *, at: dt.datetime = START):
    return decode_event(
        RxEvent(packet=record_bytes, rx_meta=RxMeta(snr_db=9.0, rssi_dbm=-40), received_at=at),
        received_at=at,
    )


def ack_record(checksum: bytes, *, tail: bytes = b"", at: dt.datetime = START):
    payload = build_ack(Acknowledgement(checksum=checksum, tail=tail))
    packet = encode_packet(
        Packet(
            header=PacketHeader(RouteType.DIRECT, PayloadType.ACK, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=payload,
        )
    )
    return _packet_for(packet, at=at)


async def test_a_message_is_submitted_at_class_two_with_its_ack_timeout_as_deadline() -> None:
    entity = Entity("us")
    peer = Entity("them")
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)
    submit = RecordingSubmit()
    clock = TickingClock()
    events: list = []
    dm = messenger(entity, paths=paths, submit=submit, clock=clock, events=events)

    task = asyncio.create_task(dm.send(entity, contact_for(peer.identity), "hej"))
    for _ in range(4):
        await asyncio.sleep(0)
    sent = next(event for event in events if isinstance(event, MessageSent))
    dm._handle_ack(ack_record(sent.expected_ack, at=clock.now()), Acknowledgement(sent.expected_ack))
    outcome = await asyncio.wait_for(task, 2)

    assert outcome.result is SendResult.ACKNOWLEDGED
    submission = submit.submissions[0]
    assert submission.priority is PriorityClass.MESSAGE
    expected_deadline = START + dt.timedelta(milliseconds=sent.ack_timeout_ms)
    assert submission.deadline == expected_deadline
    assert submission.origin == "direct_message"


async def test_four_unanswered_attempts_resolve_as_unacknowledged() -> None:
    entity = Entity("us")
    peer = Entity("them")
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)
    submit = RecordingSubmit()
    events: list = []
    dm = messenger(entity, paths=paths, submit=submit, events=events)

    outcome = await dm.send(entity, contact_for(peer.identity), "hej")

    assert outcome.result is SendResult.UNACKNOWLEDGED
    assert outcome.attempts == MAX_ATTEMPT + 1
    assert len(submit.submissions) == MAX_ATTEMPT + 1
    assert "no acknowledgement" in outcome.reason


async def test_a_retry_reuses_the_timestamp_and_changes_the_ciphertext() -> None:
    entity = Entity("us")
    peer = Entity("them")
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)
    submit = RecordingSubmit()
    dm = messenger(entity, paths=paths, submit=submit)

    await dm.send(entity, contact_for(peer.identity), "hej")

    from sighop.protocol.packet import decode
    from sighop.protocol.payloads import parse_payload

    envelopes = [
        parse_payload(PayloadType.TXT_MSG, decode(s.packet).payload)
        for s in submit.submissions
    ]
    ciphertexts = [envelope.ciphertext for envelope in envelopes]
    assert len(set(ciphertexts)) == len(ciphertexts), (
        "identical retransmissions are exactly what a repeater deduplicates"
    )
    # The timestamp is the same across attempts; only the flags byte moved.
    secret = dm.secrets.get(entity.identity, peer.identity.public_key)
    from sighop.protocol.crypto import decrypt
    from sighop.protocol.payloads import parse_text_message_body

    bodies = [parse_text_message_body(decrypt(secret, c)) for c in ciphertexts]
    assert len({body.timestamp for body in bodies}) == 1
    assert [body.attempt for body in bodies] == [0, 1, 2, 3]


async def test_a_late_acknowledgement_for_an_earlier_attempt_still_resolves() -> None:
    entity = Entity("us")
    peer = Entity("them")
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)
    clock = TickingClock()
    events: list = []
    dm = messenger(entity, paths=paths, clock=clock, events=events)

    task = asyncio.create_task(dm.send(entity, contact_for(peer.identity), "hej"))
    # Let the send reach its third attempt, then answer the first one.
    while len([e for e in events if isinstance(e, MessageSent)]) < 3:
        await asyncio.sleep(0)
    first = next(event for event in events if isinstance(event, MessageSent))
    assert first.attempt == 0
    dm._handle_ack(ack_record(first.expected_ack, at=clock.now()), Acknowledgement(first.expected_ack))
    outcome = await asyncio.wait_for(task, 2)

    assert outcome.result is SendResult.ACKNOWLEDGED
    matched = next(event for event in events if isinstance(event, AckMatched))
    assert matched.attempt == 0, "a late ACK matched the wrong attempt's expectation"


async def test_an_acknowledgement_arriving_inside_the_grace_window_still_counts() -> None:
    """A caller whose next move on silence is expensive can keep listening.

    Nothing extra is transmitted: the expectations of the attempts already sent
    stay registered, so an acknowledgement that came home over a longer path is
    matched instead of being counted `ack_unmatched`.
    """
    entity = Entity("us")
    peer = Entity("them")
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)
    clock = TickingClock()
    submit = RecordingSubmit()
    events: list = []
    dm = messenger(entity, paths=paths, clock=clock, submit=submit, events=events)

    task = asyncio.create_task(
        dm.send(entity, contact_for(peer.identity), "hej", ack_grace_ms=60_000)
    )
    # Every attempt is spent and the send is now inside the grace window.
    while len(submit.submissions) < MAX_ATTEMPT + 1:
        await asyncio.sleep(0)
    for _ in range(200):
        await asyncio.sleep(0)
    assert not task.done(), "the grace window had not been waited out"

    last = [event for event in events if isinstance(event, MessageSent)][-1]
    dm._handle_ack(
        ack_record(last.expected_ack, at=clock.now()), Acknowledgement(last.expected_ack)
    )
    outcome = await asyncio.wait_for(task, 2)

    assert outcome.result is SendResult.ACKNOWLEDGED
    assert len(submit.submissions) == MAX_ATTEMPT + 1, "grace listens, never transmits"


async def test_the_grace_window_ends_and_the_send_is_still_unacknowledged() -> None:
    entity = Entity("us")
    peer = Entity("them")
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)
    submit = RecordingSubmit()
    dm = messenger(entity, paths=paths, submit=submit)

    outcome = await dm.send(
        entity, contact_for(peer.identity), "hej", ack_grace_ms=1_000
    )

    assert outcome.result is SendResult.UNACKNOWLEDGED
    assert len(submit.submissions) == MAX_ATTEMPT + 1
    assert "grace" in outcome.reason


async def test_a_dropped_submission_resolves_as_dropped_and_says_why() -> None:
    entity = Entity("us")
    peer = Entity("them")
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)
    submit = RecordingSubmit(TxResult.DROPPED)
    dm = messenger(entity, paths=paths, submit=submit)

    outcome = await dm.send(entity, contact_for(peer.identity), "hej")

    assert outcome.result is SendResult.DROPPED
    assert outcome.reason == "deadline_expired"
    assert len(submit.submissions) == 1, "a dropped message was retried anyway"


# --- Acknowledgement matching (task 4.6) -----------------------------------


async def test_a_six_byte_acknowledgement_matches_on_its_first_four_bytes() -> None:
    entity = Entity("us")
    peer = Entity("them")
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)
    clock = TickingClock()
    events: list = []
    dm = messenger(entity, paths=paths, clock=clock, events=events)

    task = asyncio.create_task(dm.send(entity, contact_for(peer.identity), "hej"))
    for _ in range(4):
        await asyncio.sleep(0)
    sent = next(event for event in events if isinstance(event, MessageSent))
    record = ack_record(sent.expected_ack, tail=b"\x02\x91", at=clock.now())
    assert record.outcome.payload.tail == b"\x02\x91"
    dm._handle_ack(record, record.outcome.payload)
    outcome = await asyncio.wait_for(task, 2)

    assert outcome.result is SendResult.ACKNOWLEDGED
    matched = next(event for event in events if isinstance(event, AckMatched))
    assert matched.payload_bytes == 6


async def test_an_acknowledgement_matching_nothing_is_reported_and_discarded() -> None:
    events: list = []
    dm = messenger(Entity("us"), events=events)

    record = ack_record(b"\xde\xad\xbe\xef")
    await dm.handle(record)

    unmatched = next(event for event in events if isinstance(event, AckUnmatched))
    assert unmatched.checksum == b"\xde\xad\xbe\xef"
    assert unmatched.outstanding == 0


# --- Inbound (tasks 4.2 - 4.5) ---------------------------------------------


def message_packet(
    *,
    sender: Entity,
    recipient_node_hash: int,
    secret: bytes,
    text: bytes = b"hej fran andra sidan",
    timestamp: int = 1_700_000_000,
    attempt: int = 0,
) -> tuple[bytes, bytes]:
    body = compose_body(timestamp=timestamp, attempt=attempt, text=text)
    return build_message_packet(
        sender=sender.identity,
        recipient_node_hash=recipient_node_hash,
        secret=secret,
        body=body,
        route=Route(flood=False),
    )


async def test_a_loopback_message_decrypts_parses_and_is_acknowledged() -> None:
    """Two local entities, one fake sender. Self-consistency, not wire proof."""
    alice, bob = Entity("alice"), Entity("bob")
    contacts = ContactStore(logger=RecordingLogger())
    alice_contact = contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    submit = RecordingSubmit()
    events: list = []
    dm = messenger(bob, contacts=contacts, paths=paths, submit=submit, events=events)

    secret = SharedSecretCache().get(alice.identity, bob.identity.public_key)
    packet, plaintext = message_packet(
        sender=alice, recipient_node_hash=bob.node_hash, secret=secret
    )
    await dm.handle(_packet_for(packet))

    received = next(event for event in events if isinstance(event, MessageReceived))
    assert received.entity_name == "bob"
    assert received.contact is alice_contact
    assert received.body.text.text == "hej fran andra sidan"
    assert received.acknowledged is True

    ack_submission = submit.submissions[0]
    assert ack_submission.priority is PriorityClass.ACK
    assert ack_submission.origin == "ack"
    from sighop.protocol.packet import decode
    from sighop.protocol.payloads import parse_payload

    emitted = parse_payload(PayloadType.ACK, decode(ack_submission.packet).payload)
    import hashlib

    assert emitted.checksum == hashlib.sha256(
        plaintext + alice.identity.public_key
    ).digest()[:4]
    assert emitted.tail == b"", "we accept 6 bytes and emit 4"


async def test_the_receivers_acknowledgement_is_the_one_the_sender_expected() -> None:
    """The two halves of the ACK construction meet: `:431` against `:243`."""
    alice, bob = Entity("alice"), Entity("bob")
    secret = SharedSecretCache().get(alice.identity, bob.identity.public_key)
    body = compose_body(timestamp=1_700_000_000, attempt=1, text=b"knock")

    sender_expects = ack_checksum_for(body, alice.identity.public_key)

    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    submit = RecordingSubmit()
    dm = messenger(bob, contacts=contacts, paths=paths, submit=submit)
    packet, _ = build_message_packet(
        sender=alice.identity,
        recipient_node_hash=bob.node_hash,
        secret=secret,
        body=body,
        route=Route(flood=False),
    )
    await dm.handle(_packet_for(packet))

    from sighop.protocol.packet import decode
    from sighop.protocol.payloads import parse_payload

    emitted = parse_payload(PayloadType.ACK, decode(submit.submissions[0].packet).payload)
    assert emitted.checksum == sender_expects


async def test_a_destination_hash_collision_picks_the_entity_whose_mac_verifies() -> None:
    alice = Entity("alice")
    bob = Entity("bob")
    # A second local entity on the same node hash: the fan-out must try both.
    while True:
        twin_identity = generate_identity()
        if twin_identity.node_hash == bob.node_hash:
            break
    twin = Entity("bob-twin", twin_identity)

    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    events: list = []
    dm = messenger(twin, bob, contacts=contacts, paths=paths, events=events)

    secret = SharedSecretCache().get(alice.identity, bob.identity.public_key)
    packet, _ = message_packet(
        sender=alice, recipient_node_hash=bob.node_hash, secret=secret
    )
    await dm.handle(_packet_for(packet))

    received = next(event for event in events if isinstance(event, MessageReceived))
    assert received.entity_name == "bob", "the twin's key decrypted someone else's message"
    assert received.candidates_tried == 2, "the wrong entity was not tried first"


async def test_an_undecryptable_message_reports_its_candidate_count() -> None:
    bob = Entity("bob")
    contacts = ContactStore(logger=RecordingLogger())
    for _ in range(3):
        contacts.add_public_key(generate_identity().public_key)
    events: list = []
    dm = messenger(bob, contacts=contacts, events=events)

    envelope = DirectEnvelope(
        payload_type=PayloadType.TXT_MSG,
        dest_hash=bob.node_hash,
        src_hash=0x99,
        mac=b"\x00\x00",
        ciphertext=b"\xaa" * 16,
    )
    packet = encode_packet(
        Packet(
            header=PacketHeader(RouteType.DIRECT, PayloadType.TXT_MSG, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=build_direct_envelope(envelope),
        )
    )
    await dm.handle(_packet_for(packet))

    undecryptable = next(
        event for event in events if isinstance(event, MessageUndecryptable)
    )
    assert undecryptable.candidates_tried == 3
    assert undecryptable.dest_hash == bob.node_hash
    assert dm.undecryptable == 1


async def test_a_message_for_another_destination_hash_is_left_alone() -> None:
    bob = Entity("bob")
    events: list = []
    dm = messenger(bob, events=events)

    envelope = DirectEnvelope(
        payload_type=PayloadType.TXT_MSG,
        dest_hash=(bob.node_hash + 1) & 0xFF,
        src_hash=0x99,
        mac=b"\x00\x00",
        ciphertext=b"\xaa" * 16,
    )
    packet = encode_packet(
        Packet(
            header=PacketHeader(RouteType.DIRECT, PayloadType.TXT_MSG, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=build_direct_envelope(envelope),
        )
    )
    await dm.handle(_packet_for(packet))

    assert events == []


async def test_a_mac_match_whose_plaintext_does_not_parse_is_reported_not_acknowledged() -> None:
    """Forced with a real MAC over a plaintext that is not a valid body.

    Block padding means a decrypted buffer is never shorter than 16 bytes, so
    "too short" is unreachable; a `SIGNED_PLAIN` body without its 4-byte sender
    key prefix is the shape that genuinely will not parse.
    """
    alice, bob = Entity("alice"), Entity("bob")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    submit = RecordingSubmit()
    events: list = []
    dm = messenger(bob, contacts=contacts, paths=paths, submit=submit, events=events)

    secret = SharedSecretCache().get(alice.identity, bob.identity.public_key)
    signed_plain_without_prefix = (
        (1).to_bytes(4, "little") + bytes([int(TextType.SIGNED_PLAIN) << 2]) + b"ab"
    )
    mac, ciphertext = encrypt_then_mac(secret, signed_plain_without_prefix)
    envelope = DirectEnvelope(
        payload_type=PayloadType.TXT_MSG,
        dest_hash=bob.node_hash,
        src_hash=alice.node_hash,
        mac=mac,
        ciphertext=ciphertext,
    )
    packet = encode_packet(
        Packet(
            header=PacketHeader(RouteType.DIRECT, PayloadType.TXT_MSG, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=build_direct_envelope(envelope),
        )
    )
    await dm.handle(_packet_for(packet))

    unparsable = next(event for event in events if isinstance(event, MessageUnparsable))
    assert "truncated" in unparsable.reason.lower()
    assert submit.submissions == [], "an unparseable plaintext was acknowledged"


async def test_an_unknown_source_hash_falls_back_to_every_contact() -> None:
    alice, bob = Entity("alice"), Entity("bob")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    events: list = []
    dm = messenger(bob, contacts=contacts, paths=paths, events=events)

    secret = SharedSecretCache().get(alice.identity, bob.identity.public_key)
    body = compose_body(timestamp=1, attempt=0, text=b"anon")
    from sighop.protocol.payloads import build_text_message_body

    mac, ciphertext = encrypt_then_mac(secret, build_text_message_body(body))
    envelope = DirectEnvelope(
        payload_type=PayloadType.TXT_MSG,
        dest_hash=bob.node_hash,
        # A source hash no contact carries: a contact we have not heard from is
        # still a possible sender (design D7).
        src_hash=(alice.node_hash + 1) & 0xFF,
        mac=mac,
        ciphertext=ciphertext,
    )
    packet = encode_packet(
        Packet(
            header=PacketHeader(RouteType.DIRECT, PayloadType.TXT_MSG, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=build_direct_envelope(envelope),
        )
    )
    await dm.handle(_packet_for(packet))

    received = next(event for event in events if isinstance(event, MessageReceived))
    assert received.contact.public_key == alice.identity.public_key


# --- An entity a room server serves is not this messenger's (design D10) ----


def _colliding_pair(first: str, second: str) -> tuple[Entity, Entity]:
    """Two entities whose node hashes are equal — the 1-in-256 case (§3).

    Generated rather than contrived, because the property under test is that the
    *entity* is filtered and the destination hash is not; two entities that do
    not actually collide would let the test pass for the wrong reason.
    """
    by_hash: dict[int, Entity] = {}
    for index in range(4096):
        entity = Entity(f"{first}-{index}")
        existing = by_hash.get(entity.node_hash)
        if existing is not None:
            existing.entity_id = existing.name = first
            entity.entity_id = entity.name = second
            return existing, entity
        by_hash[entity.node_hash] = entity
    raise AssertionError("no node-hash collision in 4096 identities")


def _text_packet_to(*, sender: Entity, recipient: Entity, text: bytes) -> bytes:
    secret = SharedSecretCache().get(sender.identity, recipient.identity.public_key)
    packet, _ = message_packet(
        sender=sender,
        recipient_node_hash=recipient.node_hash,
        secret=secret,
        text=text,
    )
    return packet


async def test_a_message_to_a_room_server_entity_is_neither_decrypted_nor_acknowledged() -> None:
    """4.3, design D10: exactly one component decrypts a packet.

    Without the rule both subscribers decrypt a member's post and both
    acknowledge it — two acknowledgements on the air for one packet, and two
    contradictory log lines to explain them afterwards.
    """
    alice, lounge = Entity("alice"), Entity("lounge")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    submit = RecordingSubmit()
    events: list = []
    dm = messenger(lounge, contacts=contacts, paths=paths, submit=submit, events=events)
    dm.claim_for_room(lounge.entity_id)

    await dm.handle(_packet_for(_text_packet_to(sender=alice, recipient=lounge, text=b"hi")))

    assert not [event for event in events if isinstance(event, MessageReceived)]
    assert submit.submissions == [], "the direct messenger acknowledged a room's post"
    assert dm.received == 0
    # Not even reported as undecryptable: it was not this component's packet to
    # decrypt, which is a different statement from failing to decrypt it.
    assert not [event for event in events if isinstance(event, MessageUndecryptable)]


async def test_an_ordinary_entity_sharing_a_room_servers_node_hash_is_still_tried() -> None:
    """4.3: the rule filters entities, not destination hashes.

    One byte of identity collides at 1 in 256 (§3). Dropping the packet because
    a room server happens to share the hash would be dropping someone's message.
    """
    alice = Entity("alice")
    lounge, bob = _colliding_pair("lounge", "bob")
    assert lounge.node_hash == bob.node_hash

    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    submit = RecordingSubmit()
    events: list = []
    dm = messenger(lounge, bob, contacts=contacts, paths=paths, submit=submit, events=events)
    dm.claim_for_room(lounge.entity_id)

    packet = _text_packet_to(sender=alice, recipient=bob, text=b"for bob")
    await dm.handle(_packet_for(packet))

    received = next(event for event in events if isinstance(event, MessageReceived))
    assert received.entity_name == "bob"
    assert received.body.text.raw == b"for bob"
    assert len(submit.submissions) == 1, "exactly one acknowledgement"


# --- The decode stage stays stateless --------------------------------------


async def test_the_subscriber_attaches_to_the_bus_without_touching_the_decode_stage() -> None:
    bob = Entity("bob")
    bus = NetworkBus(logger=RecordingLogger())
    dm = messenger(bob)

    subscription = dm.subscribe(bus)

    assert subscription.name == "direct-messages"
    assert bus.subscriber_stats[0].name == "direct-messages"


# --- Composed before the radio readback (design D1-D5) ---------------------


def _inbound(alice: Entity, bob: Entity, *, text: bytes = b"hej fran andra sidan"):
    """One message from alice to bob, as a reception."""
    secret = SharedSecretCache().get(alice.identity, bob.identity.public_key)
    packet, plaintext = message_packet(
        sender=alice, recipient_node_hash=bob.node_hash, secret=secret, text=text
    )
    return _packet_for(packet), plaintext


def _messenger_awaiting_readback(alice: Entity, bob: Entity, **kwargs):
    """A messenger with no parameters yet and a signal to release them with."""
    contacts = kwargs.pop("contacts", None) or ContactStore(logger=RecordingLogger())
    contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    ready = asyncio.Event()
    dm = messenger(
        bob, contacts=contacts, paths=paths, radio=None, radio_ready=ready, **kwargs
    )
    return dm, ready


async def test_a_send_composed_before_the_readback_is_sent_not_dropped() -> None:
    """3.4: the outbound half. A `--send` racing its own startup."""
    entity, peer = Entity("us"), Entity("them")
    paths = PathStore()
    zero_hop_route_to(paths, peer.identity.public_key)
    submit = RecordingSubmit()
    clock = TickingClock()
    events: list = []
    ready = asyncio.Event()
    dm = messenger(
        entity,
        paths=paths,
        submit=submit,
        clock=clock,
        events=events,
        radio=None,
        radio_ready=ready,
    )

    task = asyncio.create_task(dm.send(entity, contact_for(peer.identity), "hej"))
    for _ in range(4):
        await asyncio.sleep(0)
    assert submit.submissions == [], "the message was priced before the board answered"

    dm.set_radio(EU868_NARROW)
    ready.set()
    for _ in range(4):
        await asyncio.sleep(0)
    sent = next(event for event in events if isinstance(event, MessageSent))
    dm._handle_ack(ack_record(sent.expected_ack, at=clock.now()), Acknowledgement(sent.expected_ack))
    outcome = await asyncio.wait_for(task, 2)

    assert outcome.result is SendResult.ACKNOWLEDGED, outcome.reason


async def test_an_acknowledgement_waits_and_is_then_submitted() -> None:
    alice, bob = Entity("alice"), Entity("bob")
    submit = RecordingSubmit()
    events: list = []
    dm, ready = _messenger_awaiting_readback(alice, bob, submit=submit, events=events)
    record, _ = _inbound(alice, bob)

    handling = asyncio.create_task(dm.handle(record))
    for _ in range(5):
        await asyncio.sleep(0)
    assert submit.submissions == [], "the acknowledgement was priced before the board answered"

    dm.set_radio(EU868_NARROW)
    ready.set()
    await asyncio.wait_for(handling, 2)

    assert submit.submissions[0].origin == "ack"
    received = next(event for event in events if isinstance(event, MessageReceived))
    assert received.acknowledged is True


async def test_a_readback_that_never_comes_still_refuses_inside_the_budget() -> None:
    """4.2: the refusal §4.1 asks for survives, for the board that answers nothing."""
    alice, bob = Entity("alice"), Entity("bob")
    submit = RecordingSubmit()
    logger = RecordingLogger()
    events: list = []
    dm, _ready = _messenger_awaiting_readback(
        alice, bob, submit=submit, events=events, logger=logger
    )
    record, _ = _inbound(alice, bob)

    with patch("sighop.net.readback.RADIO_READBACK_WAIT_SECONDS", 0.02):
        await asyncio.wait_for(dm.handle(record), 2)

    assert submit.submissions == []
    refusal = logger.of("ack_not_routed")[0]
    assert "waiting" in str(refusal["reason"]), "the refusal does not name the expired wait"
    received = next(event for event in events if isinstance(event, MessageReceived))
    assert received.acknowledged is False, "a refused acknowledgement was reported as sent"


async def test_the_two_refusals_name_their_own_cases() -> None:
    """4.1: a board that said nothing and a board that was too slow differ."""
    absent = NoRadioReadback(
        "no GetRadio readback available; refusing to compute airtime from configured values"
    )
    with pytest.raises(RadioReadbackTimeout) as raised:
        await wait_for_readback(asyncio.Event(), budget=0.01)

    assert str(raised.value) != str(absent)
    assert "waiting" in str(raised.value) and "waiting" not in str(absent)
    assert isinstance(raised.value, NoRadioReadback), "existing handlers would stop catching it"


async def test_a_wait_that_succeeds_is_not_counted_as_a_refusal() -> None:
    """4.3: the counters keep counting what they counted."""
    alice, bob = Entity("alice"), Entity("bob")
    logger = RecordingLogger()
    dm, ready = _messenger_awaiting_readback(alice, bob, logger=logger)
    record, _ = _inbound(alice, bob)

    handling = asyncio.create_task(dm.handle(record))
    for _ in range(5):
        await asyncio.sleep(0)
    dm.set_radio(EU868_NARROW)
    ready.set()
    await asyncio.wait_for(handling, 2)

    assert logger.of("ack_not_routed") == []
    assert dm.received == 1


async def test_the_report_follows_the_acknowledgements_settled_outcome() -> None:
    """5.2, design D5: `acknowledged` keeps meaning "reached the scheduler".

    The report is sequenced behind the acknowledgement, as it always has been,
    so the field is never emitted before its value is known — and a refused
    acknowledgement is never reported as a sent one.
    """
    alice, bob = Entity("alice"), Entity("bob")
    events: list = []
    dm, ready = _messenger_awaiting_readback(alice, bob, events=events)
    record, _ = _inbound(alice, bob)

    handling = asyncio.create_task(dm.handle(record))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not any(isinstance(event, MessageReceived) for event in events), (
        "the report was emitted before its acknowledgement settled"
    )

    dm.set_radio(EU868_NARROW)
    ready.set()
    await asyncio.wait_for(handling, 2)

    report = next(event for event in events if isinstance(event, MessageReceived))
    assert report.acknowledged is True


async def test_a_consumer_cannot_affect_an_acknowledgement_that_waited() -> None:
    """5.3: the ordering rule `direct-messaging` states, under a wait."""
    alice, bob = Entity("alice"), Entity("bob")
    submit = RecordingSubmit()

    def raising(event) -> None:
        if isinstance(event, MessageReceived):
            raise RuntimeError("a consumer that blows up")

    dm, ready = _messenger_awaiting_readback(alice, bob, submit=submit)
    dm._on_event = raising
    record, _ = _inbound(alice, bob)

    handling = asyncio.create_task(dm.handle(record))
    for _ in range(5):
        await asyncio.sleep(0)
    dm.set_radio(EU868_NARROW)
    ready.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(handling, 2)

    assert submit.submissions[0].origin == "ack", (
        "the acknowledgement did not survive a consumer that raised"
    )


async def test_the_wait_is_bounded_by_the_budget() -> None:
    """5.4: a report delayed by a wait is delayed by at most the budget."""
    alice, bob = Entity("alice"), Entity("bob")
    events: list = []
    dm, _ready = _messenger_awaiting_readback(alice, bob, events=events)
    record, _ = _inbound(alice, bob)

    started = time.monotonic()
    with patch("sighop.net.readback.RADIO_READBACK_WAIT_SECONDS", 0.05):
        await asyncio.wait_for(dm.handle(record), 2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "the report was held longer than the budget"
    assert any(isinstance(event, MessageReceived) for event in events)
