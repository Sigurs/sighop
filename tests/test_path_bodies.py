"""Explicit path bodies (milestone 6, `path-learning`, design D12).

The route is the visible half of this and the cheaper half. The half that costs
something when it is missing is the acknowledgement the firmware bundles *inside*
a path return (`MyMesh.cpp:601-620`): without decrypting the body, a push that
was answered looks unanswered, is retried three times, and leaves the member's
cursor where it was — which is indistinguishable from a client that is not
receiving at all.

Loopback, with the same caveat every loopback in this suite carries: it proves
the pieces compose, not that the wire format is right. The corpus and the live
exercise are what confirm the format.
"""

from __future__ import annotations

import datetime as dt

from sighop.net.acks import AckMatch, AckRegistry
from sighop.net.contacts import ContactStore
from sighop.net.pathbodies import PathBodyLearned, PathBodyReader, PathBodyUndecryptable
from sighop.net.paths import LearnedPath, PathKey, PathSource, PathStore
from sighop.protocol.crypto import SharedSecretCache, encrypt_then_mac
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
    ReturnedPathBody,
    build_ack,
    build_direct_envelope,
    build_returned_path_body,
)
from tests.test_dm import Entity, _packet_for
from tests.test_tx import RecordingLogger

START = dt.datetime(2026, 9, 5, 21, 0, tzinfo=dt.UTC)


def path_packet(
    *,
    sender: Entity,
    recipient: Entity,
    body: ReturnedPathBody,
    secret: bytes | None = None,
    route_type: RouteType = RouteType.FLOOD,
) -> bytes:
    secret = secret or SharedSecretCache().get(sender.identity, recipient.identity.public_key)
    mac, ciphertext = encrypt_then_mac(secret, build_returned_path_body(body))
    envelope = DirectEnvelope(
        payload_type=PayloadType.PATH,
        dest_hash=recipient.node_hash,
        src_hash=sender.node_hash,
        mac=mac,
        ciphertext=ciphertext,
    )
    return encode_packet(
        Packet(
            header=PacketHeader(route_type, PayloadType.PATH, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=build_direct_envelope(envelope),
        )
    )


def reader(
    *entities: Entity,
    paths: PathStore | None = None,
    contacts: ContactStore | None = None,
    acks: AckRegistry | None = None,
    events: list | None = None,
) -> PathBodyReader:
    return PathBodyReader(
        paths=paths or PathStore(),
        contacts=contacts or ContactStore(logger=RecordingLogger()),
        entities=entities,
        acks=acks,
        on_event=events.append if events is not None else None,
        logger=RecordingLogger(),
    )


# --- 5.1 Decryption uses the same candidate trial ---------------------------


async def test_a_path_body_addressed_to_us_is_decrypted_and_its_route_recorded() -> None:
    us, them = Entity("us"), Entity("them")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(them.identity.public_key)
    paths = PathStore()
    events: list = []
    subscriber = reader(us, paths=paths, contacts=contacts, events=events)

    body = ReturnedPathBody(hop_count=2, hash_size=1, path=b"\xab\xcd")
    await subscriber.handle(_packet_for(path_packet(sender=them, recipient=us, body=body)))

    learned = subscriber.paths.lookup_public_key(them.identity.public_key)
    assert learned is not None
    # Not reversed: the peer stated the route *to* it, which is the route out.
    assert learned.path == b"\xab\xcd"
    assert learned.hop_count == 2
    assert learned.source is PathSource.PATH_BODY
    assert learned.claimed is True

    event = next(e for e in events if isinstance(e, PathBodyLearned))
    assert event.contact.public_key == them.identity.public_key
    assert event.candidates_tried == 1


async def test_a_path_body_no_candidate_key_opens_is_reported_with_the_count() -> None:
    """5.1: never silently dropped — the candidate count is the whole report."""
    us, them, stranger = Entity("us"), Entity("them"), Entity("stranger")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(them.identity.public_key)
    paths = PathStore()
    events: list = []
    subscriber = reader(us, paths=paths, contacts=contacts, events=events)

    # Encrypted to a key nobody here holds.
    wrong = SharedSecretCache().get(stranger.identity, us.identity.public_key)
    packet = path_packet(
        sender=stranger,
        recipient=us,
        body=ReturnedPathBody(hop_count=1, hash_size=1, path=b"\x01"),
        secret=wrong,
    )
    await subscriber.handle(_packet_for(packet))

    failure = next(e for e in events if isinstance(e, PathBodyUndecryptable))
    assert failure.candidates_tried == 1
    assert subscriber.undecryptable == 1
    assert paths.destination_count == 0


async def test_a_path_body_for_a_hash_no_entity_carries_is_left_alone() -> None:
    us, them = Entity("us"), Entity("them")
    other = Entity("other")
    while other.node_hash == us.node_hash:  # pragma: no cover - vanishingly rare
        other = Entity("other")
    events: list = []
    subscriber = reader(us, events=events)

    packet = path_packet(
        sender=them,
        recipient=other,
        body=ReturnedPathBody(hop_count=0, hash_size=1, path=b""),
    )
    await subscriber.handle(_packet_for(packet))

    assert events == []
    assert subscriber.undecryptable == 0


async def test_the_decode_stage_is_unchanged_by_this_subscriber() -> None:
    """5.1: nothing here runs inside `net/rx.py` (design D6)."""
    from sighop.net.bus import NetworkBus

    bus = NetworkBus(logger=RecordingLogger())
    subscription = reader(Entity("us")).subscribe(bus)

    assert subscription.name == "path-bodies"
    assert [stats.name for stats in bus.subscriber_stats] == ["path-bodies"]


# --- 5.2 A claimed route is a candidate beside a reverse-learned one --------


async def test_a_path_body_route_and_a_reverse_learned_route_coexist() -> None:
    """5.2: two candidates, and the newer one wins — the existing rule, unchanged."""
    us, them = Entity("us"), Entity("them")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(them.identity.public_key)
    paths = PathStore()
    key = PathKey.for_public_key(them.identity.public_key)

    # Already known the ordinary way, and confirmed first.
    paths._insert(
        key,
        LearnedPath(
            path=b"\x11",
            hash_size=1,
            hop_count=1,
            snr_db=6.0,
            confirmed_at=START,
            packet_id="reverse",
        ),
    )

    subscriber = reader(us, paths=paths, contacts=contacts)
    packet = path_packet(
        sender=them,
        recipient=us,
        body=ReturnedPathBody(hop_count=1, hash_size=1, path=b"\x22"),
    )
    await subscriber.handle(_packet_for(packet, at=START + dt.timedelta(seconds=30)))

    candidates = paths.candidates(key)
    assert [candidate.path for candidate in candidates] == [b"\x11", b"\x22"]
    assert [candidate.source for candidate in candidates] == [
        PathSource.REVERSE,
        PathSource.PATH_BODY,
    ]
    # Most-recently-confirmed-wins, exactly as for two reverse-learned candidates.
    selected = paths.lookup(key)
    assert selected is not None and selected.path == b"\x22"


async def test_a_claimed_route_is_subject_to_the_ordinary_candidate_limits() -> None:
    """5.2: nothing about a path body exempts it from the bounds."""
    us, them = Entity("us"), Entity("them")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(them.identity.public_key)
    paths = PathStore(max_candidates=2)
    subscriber = reader(us, paths=paths, contacts=contacts)

    for index in range(4):
        packet = path_packet(
            sender=them,
            recipient=us,
            body=ReturnedPathBody(hop_count=1, hash_size=1, path=bytes([index])),
        )
        await subscriber.handle(_packet_for(packet, at=START + dt.timedelta(seconds=index)))

    key = PathKey.for_public_key(them.identity.public_key)
    assert len(paths.candidates(key)) == 2


def test_a_route_read_back_from_storage_makes_the_weaker_claim() -> None:
    """5.2: `source` is not persisted, and a restored route says the less."""
    restored = LearnedPath(
        path=b"\x33",
        hash_size=1,
        hop_count=1,
        snr_db=None,
        confirmed_at=START,
        packet_id="restored",
    )
    assert restored.source is PathSource.REVERSE
    assert restored.claimed is False
    assert restored.as_json()["source"] == "reverse"


# --- 5.3 The payload bundled inside ----------------------------------------


async def test_a_bundled_acknowledgement_resolves_the_delivery_waiting_on_it() -> None:
    """5.3, design D12: the reason this milestone decrypts path bodies at all."""
    us, them = Entity("us"), Entity("them")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(them.identity.public_key)
    acks = AckRegistry(logger=RecordingLogger())
    matched: list[AckMatch] = []
    checksum = b"\xde\xad\xbe\xef"
    acks.register(checksum, owner="room:lounge", on_match=matched.append)

    subscriber = reader(us, contacts=contacts, acks=acks)
    body = ReturnedPathBody(
        hop_count=1,
        hash_size=1,
        path=b"\x44",
        extra_type=PayloadType.ACK,
        extra_raw=build_ack(Acknowledgement(checksum=checksum)),
    )
    await subscriber.handle(_packet_for(path_packet(sender=them, recipient=us, body=body)))

    assert [match.checksum for match in matched] == [checksum]
    assert matched[0].owner == "room:lounge"
    assert matched[0].bundled is True, "a bundled ACK is reported as bundled"
    assert acks.unowned == 0, "a delivery that was answered was reported unmatched"
    assert subscriber.bundled_acks == 1


async def test_a_bundled_acknowledgement_nobody_awaits_is_reported_not_dropped() -> None:
    us, them = Entity("us"), Entity("them")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(them.identity.public_key)
    acks = AckRegistry(logger=RecordingLogger())
    events: list = []
    subscriber = reader(us, contacts=contacts, acks=acks, events=events)

    body = ReturnedPathBody(
        hop_count=0,
        hash_size=1,
        path=b"",
        extra_type=PayloadType.ACK,
        extra_raw=build_ack(Acknowledgement(checksum=b"\x00\x11\x22\x33")),
    )
    await subscriber.handle(_packet_for(path_packet(sender=them, recipient=us, body=body)))

    assert acks.unowned == 1
    event = next(e for e in events if isinstance(e, PathBodyLearned))
    assert event.bundled_type is PayloadType.ACK
    assert event.bundled_matched is False
    # The route is still learned: the bundle failing to match says nothing about
    # the route the same body declared.
    assert subscriber.learned == 1


async def test_a_bundled_type_we_do_not_act_on_is_reported_and_not_interpreted() -> None:
    us, them = Entity("us"), Entity("them")
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(them.identity.public_key)
    acks = AckRegistry(logger=RecordingLogger())
    events: list = []
    subscriber = reader(us, contacts=contacts, acks=acks, events=events)

    body = ReturnedPathBody(
        hop_count=0,
        hash_size=1,
        path=b"",
        extra_type=PayloadType.RESPONSE,
        extra_raw=b"\x01\x02\x03\x04",
    )
    await subscriber.handle(_packet_for(path_packet(sender=them, recipient=us, body=body)))

    event = next(e for e in events if isinstance(e, PathBodyLearned))
    assert event.bundled_type is PayloadType.RESPONSE
    assert event.bundled_matched is False
    assert subscriber.bundled_acks == 0
