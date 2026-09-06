"""The offline exercise (milestone 6, section 13).

Two different things are checked here, and they fail for different reasons.

**The loopback** drives one sighop entity as a client against a sighop room
server through the bus: login, post, push, acknowledge, cursor advance. It
proves the pieces compose — the same caveat every loopback in this suite
carries, since both ends run the same code and a shared misreading of the
firmware would pass. Section 14's live run against a stock MeshCore client is
what confirms the wire format.

**The corpus checks** are the opposite kind: they assert that nothing in this
milestone changed what a replay produces. The reception path is a pure decode
(milestone 2 design D6) and a room server is a bus subscriber, so wiring one in
must leave every delivered, duplicate, contact and path count identical. If it
does not, the room server has reached into the decode stage.
"""

from __future__ import annotations

import base64
import datetime as dt

import pytest

from sighop.config import generate_secret_key
from sighop.db.engine import Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import EntityRepository
from sighop.net.acks import AckDispatcher, AckRegistry
from sighop.net.bus import IngressPipeline, NetworkBus
from sighop.net.contacts import ContactStore
from sighop.net.dedup import DedupCache
from sighop.net.paths import PathStore
from sighop.net.room import (
    POST_SYNC_DELAY_SECS,
    DeliveryAcknowledged,
    LoginAdmitted,
    PostStored,
    RoomServer,
)
from sighop.net.rx import decode_event
from sighop.passwords import hash_password
from sighop.protocol.crypto import SharedSecretCache, ack_checksum, mac_then_decrypt
from sighop.protocol.identity import generate_identity
from sighop.protocol.packet import PayloadType
from sighop.protocol.packet import decode as decode_packet
from sighop.protocol.payloads import (
    Acknowledgement,
    AnonRequestEnvelope,
    DirectEnvelope,
    NodeType,
    Permission,
    build_text_message_body,
    parse_direct_envelope,
    parse_payload,
    parse_room_login_response_body,
    parse_text_message_body,
)
from sighop.protocol.result import DecodeFailure
from sighop.radio.modem import EU868_NARROW
from sighop.radio.replay import CaptureReplay
from tests.protocol.corpus import CAPTURE_FILES, CAPTURES_DIR
from tests.roomfixtures import ADMIN_PASSWORD, MemoryStorage, room_record
from tests.test_dm import Entity, RecordingSubmit, TickingClock, _packet_for
from tests.test_room_login import login_packet
from tests.test_room_posts import post_packet
from tests.test_tx import RecordingLogger

START = dt.datetime(2026, 9, 5, 21, 0, tzinfo=dt.UTC)
NOW = int(START.timestamp())


# --- 13.1 The loopback exercise ---------------------------------------------


def _exercise(clock: TickingClock, storage: MemoryStorage):
    """A room server and a client entity, with nothing between them but the bus."""
    lounge, client = Entity("lounge"), Entity("client")
    submit = RecordingSubmit()
    events: list = []
    server = RoomServer(
        entity=lounge,
        room=room_record(name="lounge"),
        storage=storage,
        paths=PathStore(),
        submit=submit,
        acks=AckRegistry(logger=RecordingLogger()),
        clock=clock,
        radio=EU868_NARROW,
        on_event=events.append,
        logger=RecordingLogger(),
    )
    return server, lounge, client, submit, events


def _decrypt(server: RoomServer, client: Entity, packet: bytes) -> tuple[PayloadType, bytes]:
    decoded = decode_packet(packet)
    assert not isinstance(decoded, DecodeFailure)
    envelope = parse_direct_envelope(decoded.payload_type, decoded.payload)
    assert isinstance(envelope, DirectEnvelope)
    secret = SharedSecretCache().get(client.identity, server.entity.identity.public_key)
    _candidate, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
    assert plaintext is not None
    return envelope.payload_type, plaintext


async def test_a_client_logs_in_posts_and_syncs_through_the_bus() -> None:
    """13.1: login, post, push, acknowledge, cursor advance — no database."""
    clock = TickingClock(START)
    storage = MemoryStorage()
    server, lounge, client, submit, events = _exercise(clock, storage)

    # 1. Login.
    await server.handle(
        _packet_for(
            login_packet(client=client, server=lounge, password=ADMIN_PASSWORD, timestamp=NOW),
            at=START,
        )
    )
    admitted = next(event for event in events if isinstance(event, LoginAdmitted))
    assert admitted.permission is Permission.ADMIN
    payload_type, reply = _decrypt(server, client, submit.submissions[0].packet)
    assert payload_type is PayloadType.RESPONSE
    response = parse_room_login_response_body(reply)
    assert not isinstance(response, DecodeFailure)
    assert response.permissions == int(Permission.ADMIN)

    # 2. The client posts, and is acknowledged only after the row lands.
    packet, _plaintext = post_packet(
        author=client, server=lounge, text=b"first post", timestamp=NOW + 1
    )
    await server.handle(_packet_for(packet, at=START + dt.timedelta(seconds=1)))
    stored = next(event for event in events if isinstance(event, PostStored))
    assert stored.text.raw == b"first post"
    assert len(storage.messages.posts) == 1
    assert stored.acknowledged is True

    # 3. A second member is behind, so the first member's post is pushed to it.
    other = Entity("other")
    await server.handle(
        _packet_for(
            login_packet(client=other, server=lounge, password=ADMIN_PASSWORD, timestamp=NOW + 2),
            at=START + dt.timedelta(seconds=2),
        )
    )
    before = len(submit.submissions)
    clock.advance(POST_SYNC_DELAY_SECS + 1)
    pushed = False
    for _ in range(4):
        if await server.push_once():
            pushed = True
            break
    assert pushed, "the post never became eligible for delivery"

    push = submit.submissions[-1]
    assert len(submit.submissions) > before
    _type, body = _decrypt(server, other, push.packet)
    parsed = parse_text_message_body(body)
    assert not isinstance(parsed, DecodeFailure)
    assert parsed.text.raw == b"first post"
    assert parsed.sender_key_prefix == client.identity.public_key[:4]

    # 4. The member acknowledges, and only then does its cursor advance.
    assert server.members[other.identity.public_key].sync_since == 0
    checksum = ack_checksum(build_text_message_body(parsed), other.identity.public_key)
    server.acks.deliver(
        Acknowledgement(checksum=checksum), packet_id="ack", received_at=clock.now()
    )
    acknowledged = next(event for event in events if isinstance(event, DeliveryAcknowledged))
    assert server.members[other.identity.public_key].sync_since == (
        storage.messages.posts[0].post_timestamp
    )
    assert acknowledged.post_timestamp == storage.messages.posts[0].post_timestamp

    # 5. Nothing is left outstanding, and the author never got its own post.
    assert server.deliveries_outstanding == 0
    assert await server.push_once() is False


@pytest.mark.database
async def test_the_same_exercise_runs_against_the_real_tables(database) -> None:
    """13.1: the loopback again, with Postgres behind it rather than a fixture."""
    secret = base64.b64decode(generate_secret_key())
    persistence = Persistence(database=database)
    entities = EntityRepository(database=database)
    identity = generate_identity()
    stored = await entities.store(
        name="rs-1", identity=identity, secret=secret, node_type=NodeType.ROOM_SERVER
    )
    assert isinstance(stored, Succeeded)
    created = await persistence.rooms.create(
        entity_id=stored.value.id,
        name="lounge",
        admin_password_hash=hash_password(ADMIN_PASSWORD),
    )
    assert isinstance(created, Succeeded)

    # The same test helper every other room test uses, carrying the identity
    # the store actually sealed — so this exercises the stored key, not a fresh
    # one that happens to be shaped like it.
    lounge = Entity("rs-1", identity)
    client = Entity("client")
    clock = TickingClock(START)
    submit = RecordingSubmit()
    events: list = []
    server = RoomServer(
        entity=lounge,
        room=created.value,
        storage=persistence,
        paths=PathStore(),
        submit=submit,
        acks=AckRegistry(logger=RecordingLogger()),
        clock=clock,
        radio=EU868_NARROW,
        on_event=events.append,
        logger=RecordingLogger(),
    )

    await server.handle(
        _packet_for(
            login_packet(client=client, server=lounge, password=ADMIN_PASSWORD, timestamp=NOW),
            at=START,
        )
    )
    assert any(isinstance(event, LoginAdmitted) for event in events)

    packet, _ = post_packet(author=client, server=lounge, text=b"durable", timestamp=NOW + 1)
    await server.handle(_packet_for(packet, at=START + dt.timedelta(seconds=1)))

    history = await persistence.messages.history(created.value.id)
    assert isinstance(history, Succeeded)
    assert [post.text for post in history.value] == [b"durable"]

    members = await persistence.members.load_for_room(created.value.id)
    assert isinstance(members, Succeeded)
    assert [member.public_key for member in members.value] == [client.identity.public_key]


# --- 13.2 A restart mid-exercise --------------------------------------------


async def test_a_restarted_server_resumes_delivery_from_each_stored_position() -> None:
    """13.2: the server is stopped and rebuilt between a post and its delivery."""
    clock = TickingClock(START)
    storage = MemoryStorage()
    server, lounge, client, _submit, _events = _exercise(clock, storage)

    await server.handle(
        _packet_for(
            login_packet(client=client, server=lounge, password=ADMIN_PASSWORD, timestamp=NOW),
            at=START,
        )
    )
    author = Entity("author").identity.public_key
    await storage.messages.store(
        room_id=server.room.id,
        author_public_key=author,
        text=b"posted while it was away",
        now=NOW,
        posted_at=START,
    )
    # The process ends here: nothing was pushed, and the member's cursor is
    # wherever its login put it.
    assert server.members[client.identity.public_key].sync_since == 0

    submit = RecordingSubmit()
    restarted = RoomServer(
        entity=lounge,
        room=server.room,
        storage=storage,
        paths=PathStore(),
        submit=submit,
        acks=AckRegistry(logger=RecordingLogger()),
        members=storage.members.of(server.room.id),
        clock=clock,
        radio=EU868_NARROW,
        logger=RecordingLogger(),
    )
    clock.advance(POST_SYNC_DELAY_SECS + 1)
    assert await restarted.push_once() is True

    _type, body = _decrypt(restarted, client, submit.submissions[0].packet)
    parsed = parse_text_message_body(body)
    assert not isinstance(parsed, DecodeFailure)
    assert parsed.text.raw == b"posted while it was away", (
        "the restart lost the member's position and delivered the wrong post"
    )


# --- 13.3 / 13.4 / 13.5 The corpus is unchanged -----------------------------


def _replayed() -> list:
    records: list = []
    for name in CAPTURE_FILES:
        replay = CaptureReplay.open(CAPTURES_DIR / name)
        records.extend(decode_event(event) for event in replay.read())
    return records


@pytest.fixture(scope="module")
def corpus_records():
    return _replayed()


def test_every_anon_req_frame_still_parses_as_an_anonymous_request(
    corpus_records,
) -> None:
    """13.4: the corpus's `ANON_REQ` frames are unchanged by the login codec.

    42 were third-party anonymous requests already in the corpus; milestone 6
    added 15 more that are genuine logins to `[redacted]`, and both kinds must
    still parse as the same envelope shape.
    """
    anon = 0
    for record in corpus_records:
        if record.packet is None or record.packet.payload_type is not PayloadType.ANON_REQ:
            continue
        envelope = parse_payload(PayloadType.ANON_REQ, record.packet.payload)
        assert isinstance(envelope, AnonRequestEnvelope), (
            f"{record.packet_id} no longer parses as an anonymous request"
        )
        anon += 1
    assert anon == 57, "the corpus's anonymous-request count changed"


async def test_no_corpus_frame_is_mistaken_for_a_login_to_one_of_our_entities(
    corpus_records,
) -> None:
    """13.4: zero login attempts during a full replay.

    None of the corpus's 57 anonymous requests is addressed to the identity
    this synthetic room server holds. 15 of them are the milestone 6
    exercise's real logins, but to `[redacted]`'s key, not this one — so a
    room server wired into the replay must not so much as try to decrypt a
    request addressed to somebody else's key, and certainly must not answer.
    """
    clock = TickingClock(START)
    storage = MemoryStorage()
    server, _lounge, _client, submit, events = _exercise(clock, storage)

    for record in corpus_records:
        await server.handle(record)

    assert events == [], "a corpus frame produced a room-server event"
    assert submit.submissions == [], "a corpus frame made the room server transmit"
    assert server.logins_admitted == 0
    assert storage.members.rows == {}


async def _pipeline_counts(*, with_room: bool) -> dict[str, int]:
    """Replay the corpus through the live pipeline, optionally with a room server."""
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
    if with_room:
        clock = TickingClock(START)
        storage = MemoryStorage()
        acks = AckRegistry(logger=RecordingLogger())
        server, _lounge, _client, _submit, _events = _exercise(clock, storage)
        server.acks = acks
        server.subscribe(bus)
        AckDispatcher(registry=acks).subscribe(bus)

    for record in _replayed():
        pipeline.ingest(record)
    # Every subscriber drains before anything is counted: a contact learned by a
    # subscriber that had not yet run would make the two sides differ for a
    # reason that has nothing to do with rooms.
    await bus.aclose()

    return {
        "delivered": pipeline.delivered,
        "duplicates": pipeline.dedup.stats.duplicates,
        "considered": pipeline.dedup.stats.considered,
        "contacts": len(contacts),
        "paths": pipeline.paths.destination_count,
    }


async def test_the_reception_path_is_identical_with_and_without_a_room_server() -> None:
    """13.5, design D6: a room server is a subscriber and touches no decode."""
    without = await _pipeline_counts(with_room=False)
    with_room = await _pipeline_counts(with_room=True)

    assert with_room == without, "wiring a room server changed what the reception path produced"
    assert without["delivered"] > 0, "the comparison would be vacuous"


def test_the_decode_records_are_unchanged_by_this_milestone(corpus_records) -> None:
    """13.3: the corpus decodes identically — the existing corpus tests are the
    authority, and this asserts the same evidence is still reachable."""
    from tests.protocol.corpus import EXPECTED_RECEIVED_COUNT

    assert len(corpus_records) == EXPECTED_RECEIVED_COUNT
    assert all(record.packet_id for record in corpus_records)
