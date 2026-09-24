"""History sync: the push loop and the cursor (milestone 6, `room-history`).

Two properties carry this group, and both are about what *does not* happen:

* a member's cursor advances **only** on an acknowledgement, so a member
  returning after a week gets what it missed rather than a gap, and
* deliveries alternate between members, so §7's *"a member returning after a
  week must not monopolize the channel"* is a property of taking members in
  turn rather than of the priority class.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import replace

import pytest

from sighop.db.repositories import RoomRecord
from sighop.net.acks import AckRegistry, AckUnowned
from sighop.net.bus import PriorityClass, TxOutcome, TxResult
from sighop.net.dm import Route
from sighop.net.paths import LearnedPath, PathKey, PathStore
from sighop.net.room import (
    MAX_PUSH_FAILURES,
    POST_SYNC_DELAY_SECS,
    DeliveryAcknowledged,
    DeliverySent,
    MemberBackedOff,
    RoomServer,
    _push_body,
    _push_timeout_ms,
)
from sighop.protocol.crypto import SharedSecretCache, ack_checksum, mac_then_decrypt
from sighop.protocol.packet import PayloadType, RouteType
from sighop.protocol.packet import decode as decode_packet
from sighop.protocol.payloads import (
    Acknowledgement,
    DirectEnvelope,
    Permission,
    TextType,
    build_text_message_body,
    parse_direct_envelope,
    parse_text_message_body,
)
from sighop.protocol.result import DecodeFailure
from sighop.radio.modem import EU868_NARROW
from tests.roomfixtures import MemoryStorage, member_record, room_record
from tests.test_dm import Entity, RecordingSubmit, TickingClock, zero_hop_route_to
from tests.test_tx import RecordingLogger

START = dt.datetime(2026, 9, 5, 21, 0, tzinfo=dt.UTC)
NOW = int(START.timestamp())


def server_with_members(
    *members: Entity,
    server_entity: Entity,
    storage: MemoryStorage | None = None,
    submit: RecordingSubmit | HeldSubmit | None = None,
    events: list | None = None,
    acks: AckRegistry | None = None,
    clock: TickingClock | None = None,
    routes: bool = True,
    sync_since: int = 0,
    room: RoomRecord | None = None,
    last_heard=None,
) -> RoomServer:
    room = room or room_record()
    paths = PathStore()
    if routes:
        for member in members:
            zero_hop_route_to(paths, member.identity.public_key, at=START)
    store = storage or MemoryStorage()
    rows = [
        member_record(
            room,
            member.identity.public_key,
            permission=Permission.READ_WRITE,
            sync_since=sync_since,
        )
        for member in members
    ]
    # Seeded in storage as well as in memory: these members logged in during an
    # earlier run, which is the state every sync test starts from.
    for row in rows:
        store.rows_seed(row)
    return RoomServer(
        entity=server_entity,
        room=room,
        storage=store,
        paths=paths,
        submit=submit or RecordingSubmit(),
        acks=acks or AckRegistry(logger=RecordingLogger()),
        members=rows,
        clock=clock or TickingClock(START),
        radio=EU868_NARROW,
        on_event=events.append if events is not None else None,
        logger=RecordingLogger(),
        last_heard=last_heard,
    )


async def seed_post(
    server: RoomServer,
    storage: MemoryStorage,
    *,
    author: bytes,
    text: bytes = b"a post",
    at: int | None = None,
) -> int:
    """Store a post directly, the way `sighop room post` or an earlier run would."""
    outcome = await storage.messages.store(
        room_id=server.room.id,
        author_public_key=author,
        text=text,
        now=at if at is not None else NOW - POST_SYNC_DELAY_SECS - 1,
        posted_at=START,
    )
    return outcome.value.post_timestamp


def decrypt_push(server: RoomServer, member: Entity, packet: bytes):
    """Decrypt a push the way the receiving client does.

    The returned plaintext is the **unpadded** body, rebuilt from the parsed
    one. AES-128-ECB pads to a block boundary and the cipher cannot tell padding
    from a plaintext that ends in `0x00`, so the client strips it in the parser
    and computes its acknowledgement over what it parsed
    (`BaseChatMesh.cpp:243`). Comparing against the padded bytes here would test
    a checksum nobody computes.
    """
    decoded = decode_packet(packet)
    envelope = parse_direct_envelope(decoded.payload_type, decoded.payload)
    assert isinstance(envelope, DirectEnvelope)
    secret = SharedSecretCache().get(member.identity, server.entity.identity.public_key)
    _candidate, padded = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
    assert padded is not None
    body = parse_text_message_body(padded)
    assert not isinstance(body, DecodeFailure)
    return envelope, build_text_message_body(body)


# --- 8.1 The round-robin ----------------------------------------------------


async def test_deliveries_alternate_between_two_members_who_are_behind() -> None:
    """8.1, §7: one member far behind must not exclude the others."""
    alice, bob, lounge = Entity("alice"), Entity("bob"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = server_with_members(
        alice, bob, server_entity=lounge, storage=storage, submit=submit, events=events
    )
    # Three posts by a fourth party, so neither member is an author.
    author = Entity("author").identity.public_key
    for index in range(3):
        await seed_post(server, storage, author=author, at=NOW - POST_SYNC_DELAY_SECS - 10 + index)

    sent: list[bytes] = []
    for _ in range(4):
        await server.push_once()
        delivered = [event for event in events if isinstance(event, DeliverySent)]
        if delivered:
            sent = [event.member for event in delivered]

    # Alice, Bob, then Alice again — turns, not a drain.
    assert sent[0] == alice.identity.public_key
    assert sent[1] == bob.identity.public_key
    assert len(sent) >= 2


async def test_an_author_is_never_sent_its_own_post() -> None:
    """8.1, `MyMesh.cpp:1023`."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    events: list = []
    server = server_with_members(alice, server_entity=lounge, storage=storage, events=events)
    await seed_post(server, storage, author=alice.identity.public_key)

    pushed = await server.push_once()

    assert pushed is False
    assert [event for event in events if isinstance(event, DeliverySent)] == []


async def test_a_new_post_is_held_before_it_becomes_eligible() -> None:
    """8.1: `POST_SYNC_DELAY_SECS`, so the author's own client is not raced."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    clock = TickingClock(START)
    server = server_with_members(alice, server_entity=lounge, storage=storage, clock=clock)
    author = Entity("author").identity.public_key
    await seed_post(server, storage, author=author, at=NOW)

    assert await server.push_once() is False, "a post was pushed before its hold expired"

    clock.advance(POST_SYNC_DELAY_SECS + 1)
    assert await server.push_once() is True


async def test_only_one_delivery_is_outstanding_per_member() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    server = server_with_members(alice, server_entity=lounge, storage=storage, submit=submit)
    author = Entity("author").identity.public_key
    for index in range(3):
        await seed_post(server, storage, author=author, at=NOW - POST_SYNC_DELAY_SECS - 10 + index)

    await server.push_once()
    await server.push_once()

    assert len(submit.submissions) == 1, "a second push while one was outstanding"


# --- 8.2 The push body ------------------------------------------------------


async def test_a_push_body_is_timestamp_flags_author_prefix_and_text() -> None:
    """8.2, `MyMesh.cpp:69-108`, checked against the bytes."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    server = server_with_members(alice, server_entity=lounge, storage=storage, submit=submit)
    author = Entity("author").identity.public_key
    stamp = await seed_post(server, storage, author=author, text=b"hello")

    await server.push_once()

    envelope, plaintext = decrypt_push(server, alice, submit.submissions[0].packet)
    assert envelope.payload_type is PayloadType.TXT_MSG
    assert plaintext[:4] == stamp.to_bytes(4, "little")
    flags = plaintext[4]
    assert flags >> 2 == int(TextType.SIGNED_PLAIN)
    assert 0 <= (flags & 0x03) <= 3, "the attempt is two bits"
    assert plaintext[5:9] == author[:4], "the author's 4-byte key prefix"
    assert plaintext[9:] == b"hello"

    body = parse_text_message_body(plaintext)
    assert body.sender_key_prefix == author[:4]
    assert body.text.raw == b"hello"


def test_two_pushes_of_one_post_differ_so_a_repeater_cannot_deduplicate_the_retry() -> None:
    """8.2: the attempt is drawn at random precisely for this."""
    from sighop.db.repositories import PostRecord

    post = PostRecord(
        id=1,
        room_id=room_record().id,
        author_public_key=b"\x01" * 32,
        post_timestamp=NOW,
        sender_timestamp=None,
        text=b"same text",
        posted_at=START,
    )
    seen = {build_text_message_body(_push_body(post)) for _ in range(64)}
    assert len(seen) > 1, "every push of one post produced identical bytes"


# --- 8.3 The expected acknowledgement ---------------------------------------


async def test_the_expectation_matches_what_the_reference_construction_produces() -> None:
    """8.3: computed over exactly the transmitted plaintext, with the member's key."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    events: list = []
    server = server_with_members(
        alice,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        acks=acks,
        events=events,
    )
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    await server.push_once()

    _envelope, plaintext = decrypt_push(server, alice, submit.submissions[0].packet)
    # What the receiving client computes (`BaseChatMesh.cpp:243`).
    expected = ack_checksum(plaintext, alice.identity.public_key)
    assert acks.owner_of(expected) == server.subscriber_name

    sent = next(event for event in events if isinstance(event, DeliverySent))
    assert sent.expected_ack == expected


# --- 8.4 The cursor ---------------------------------------------------------


async def test_an_acknowledged_delivery_advances_and_persists_the_cursor() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    events: list = []
    server = server_with_members(
        alice,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        acks=acks,
        events=events,
    )
    stamp = await seed_post(server, storage, author=Entity("author").identity.public_key)

    await server.push_once()
    _envelope, plaintext = decrypt_push(server, alice, submit.submissions[0].packet)
    checksum = ack_checksum(plaintext, alice.identity.public_key)
    acks.deliver(Acknowledgement(checksum=checksum), packet_id="pkt", received_at=START)

    assert server.members[alice.identity.public_key].sync_since == stamp
    acknowledged = next(event for event in events if isinstance(event, DeliveryAcknowledged))
    assert acknowledged.post_timestamp == stamp

    # Persisted, so a restart neither resends it nor skips what comes next.
    import asyncio

    await asyncio.sleep(0)
    assert storage.members.of(server.room.id)[0].sync_since == stamp


async def test_an_unacknowledged_delivery_leaves_the_cursor_where_it_was() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    server = server_with_members(alice, server_entity=lounge, storage=storage)
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    await server.push_once()

    assert server.members[alice.identity.public_key].sync_since == 0


async def test_a_member_that_was_behind_resumes_from_its_stored_position() -> None:
    """8.4, 13.2: a restart between a post and its delivery."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    first = server_with_members(alice, server_entity=lounge, storage=storage)
    author = Entity("author").identity.public_key
    first_stamp = await seed_post(first, storage, author=author, at=NOW - 100)
    second_stamp = await seed_post(first, storage, author=author, at=NOW - 90)

    # The first post is delivered and acknowledged; then the process ends.
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    first.submit = submit
    first.acks = acks
    await first.push_once()
    _envelope, plaintext = decrypt_push(first, alice, submit.submissions[0].packet)
    acks.deliver(
        Acknowledgement(checksum=ack_checksum(plaintext, alice.identity.public_key)),
        packet_id="pkt",
        received_at=START,
    )
    import asyncio

    await asyncio.sleep(0)

    restarted_submit = RecordingSubmit()
    restarted = RoomServer(
        entity=lounge,
        room=first.room,
        storage=storage,
        paths=first.paths,
        submit=restarted_submit,
        acks=AckRegistry(logger=RecordingLogger()),
        members=storage.members.of(first.room.id),
        clock=TickingClock(START),
        radio=EU868_NARROW,
        logger=RecordingLogger(),
    )
    assert restarted.members[alice.identity.public_key].sync_since == first_stamp

    await restarted.push_once()
    _envelope, plaintext = decrypt_push(restarted, alice, restarted_submit.submissions[0].packet)
    assert plaintext[:4] == second_stamp.to_bytes(4, "little"), (
        "delivery resumed from the wrong place after a restart"
    )


# --- 8.5 Priority, deadline and the gate ------------------------------------


async def test_a_delivery_is_submitted_at_the_reply_class_with_the_peers_own_window() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    server = server_with_members(alice, server_entity=lounge, storage=storage, submit=submit)
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    await server.push_once()

    submission = submit.submissions[0]
    assert submission.priority is PriorityClass.REPLY
    assert submission.entity_type == "room_server"
    # A zero-hop direct route: base + factor x (hops + 1).
    route = server._known_route_to(server.members[alice.identity.public_key])
    assert route is not None
    expected_ms = _push_timeout_ms(route)
    assert submission.deadline == START + dt.timedelta(milliseconds=expected_ms)


async def test_a_suppressed_delivery_advances_nothing() -> None:
    """8.5: gated runs schedule and report exactly as any other traffic."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit(TxResult.SUPPRESSED)
    acks = AckRegistry(logger=RecordingLogger())
    events: list = []
    server = server_with_members(
        alice,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        acks=acks,
        events=events,
    )
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    sent = await server.push_once()

    assert sent is False
    delivery = next(event for event in events if isinstance(event, DeliverySent))
    assert delivery.transmitted is False
    assert server.members[alice.identity.public_key].sync_since == 0
    assert acks.outstanding() == 0, "a suppressed delivery left an expectation behind"


async def test_a_member_with_no_known_route_is_delivered_to_by_flood() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, routes=False
    )
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    await server.push_once()

    decoded = decode_packet(submit.submissions[0].packet)
    assert decoded.route_type is RouteType.FLOOD


# --- 8.6 Backing off --------------------------------------------------------


async def test_a_member_that_never_answers_is_backed_off_and_then_resumes() -> None:
    """8.6, `MyMesh.cpp:1012`: three failures, then silence until it is heard."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    clock = TickingClock(START)
    events: list = []
    server = server_with_members(
        alice,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        clock=clock,
        events=events,
    )
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    for _ in range(MAX_PUSH_FAILURES):
        await server.push_once()
        clock.advance(60)  # past the delivery's own window

    await server.push_once()
    assert len(submit.submissions) == MAX_PUSH_FAILURES, "delivery did not stop"
    backed_off = next(event for event in events if isinstance(event, MemberBackedOff))
    assert backed_off.failures == MAX_PUSH_FAILURES
    assert server.members_behind() == 1
    assert server.as_json()["members_backed_off"] == 1

    # Heard from again: delivery resumes, from the unchanged position.
    server._note_activity(server.members[alice.identity.public_key])
    assert server.members_behind() == 0
    await server.push_once()
    assert len(submit.submissions) == MAX_PUSH_FAILURES + 1
    assert server.members[alice.identity.public_key].sync_since == 0


# --- 8.7 A member naming its own position -----------------------------------


async def test_a_forced_position_causes_redelivery_from_that_point() -> None:
    """8.7: a client that reset its history resynchronises."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    server = server_with_members(alice, server_entity=lounge, storage=storage, submit=submit)
    author = Entity("author").identity.public_key
    first = await seed_post(server, storage, author=author, at=NOW - 100)
    await seed_post(server, storage, author=author, at=NOW - 90)

    # Already synced past both.
    server._set_cursor(server.members[alice.identity.public_key], first + 100)
    assert await server.push_once() is False

    # The client says it is back at the beginning.
    server._set_cursor(server.members[alice.identity.public_key], first - 1)
    assert await server.push_once() is True
    _envelope, plaintext = decrypt_push(server, alice, submit.submissions[0].packet)
    assert plaintext[:4] == first.to_bytes(4, "little")


# --- The window starts at the end of transmission ---------------------------


class _HeldHandle:
    def __init__(self, future: asyncio.Future[TxOutcome]) -> None:
        self.future = future

    def __await__(self):
        return self.future.__await__()


class HeldSubmit:
    """Stands in for the scheduler, but a handle resolves only when told to —
    the push is queued or on air until `transmit` is called."""

    def __init__(self) -> None:
        self.submissions: list = []
        self.futures: list[asyncio.Future[TxOutcome]] = []

    def __call__(self, submission):
        self.submissions.append(submission)
        future: asyncio.Future[TxOutcome] = asyncio.get_running_loop().create_future()
        self.futures.append(future)
        return _HeldHandle(future)

    def transmit(self, result: TxResult = TxResult.TRANSMITTED) -> None:
        self.futures[-1].set_result(
            TxOutcome(
                result=result,
                packet_id=f"pkt{len(self.submissions)}",
                airtime_ms=1.0,
                queue_wait_ms=0.0,
                attempts=1,
            )
        )


def fixed_attempts(monkeypatch: pytest.MonkeyPatch, *values: int) -> None:
    """Make the push's random `attempt` a known sequence, so two attempts of
    one post are known to expect different acknowledgements."""
    queue = list(values)
    monkeypatch.setattr(
        "sighop.net.room.secrets.randbelow", lambda _n: queue.pop(0) if queue else 0
    )


def checksum_of(server: RoomServer, member: Entity, packet: bytes) -> bytes:
    _envelope, plaintext = decrypt_push(server, member, packet)
    return ack_checksum(plaintext, member.identity.public_key)


def ack(acks: AckRegistry, checksum: bytes, *, at: dt.datetime = START):
    return acks.deliver(Acknowledgement(checksum=checksum), packet_id="ackpkt", received_at=at)


async def test_a_push_held_in_the_queue_past_its_window_is_not_expired() -> None:
    """A push that waits behind other traffic still gets its full window after
    transmission, and an acknowledgement within it advances the cursor."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = HeldSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    clock = TickingClock(START)
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, acks=acks, clock=clock
    )
    stamp = await seed_post(server, storage, author=Entity("author").identity.public_key)
    key = alice.identity.public_key

    pushing = asyncio.create_task(server.push_once())
    await asyncio.sleep(0)
    assert len(submit.submissions) == 1
    window = _push_timeout_ms(server._route_to(server.members[key]))

    # Queued for longer than the whole window.
    clock.advance(window / 1000 + 30)
    await server.push_once()
    assert server._state[key].delivery is not None, "a queued push timed out"
    assert server._state[key].failures == 0

    submit.transmit()
    assert await pushing is True
    transmitted_at = clock.now()
    delivery = server._state[key].delivery
    assert delivery is not None
    assert delivery.transmitted_at == transmitted_at
    assert delivery.deadline == transmitted_at + dt.timedelta(milliseconds=window)

    # Answered just inside the window counted from transmission.
    clock.advance(window / 1000 - 1)
    await server.push_once()
    assert server._state[key].delivery is not None
    ack(acks, checksum_of(server, alice, submit.submissions[0].packet), at=clock.now())

    assert server.members[key].sync_since == stamp
    assert len(submit.submissions) == 1, "the push was retried"


async def test_a_push_still_awaiting_its_handle_never_counts_as_a_failure() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = HeldSubmit()
    clock = TickingClock(START)
    events: list = []
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, clock=clock, events=events
    )
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    pushing = asyncio.create_task(server.push_once())
    await asyncio.sleep(0)
    for _ in range(MAX_PUSH_FAILURES + 2):
        clock.advance(600)
        await server.push_once()

    state = server._state[alice.identity.public_key]
    assert state.failures == 0
    assert state.backed_off is False
    assert [event for event in events if isinstance(event, MemberBackedOff)] == []
    assert len(submit.submissions) == 1
    submit.transmit()
    await pushing


async def test_a_dropped_push_starts_no_window_and_counts_no_failure() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit(TxResult.DROPPED)
    acks = AckRegistry(logger=RecordingLogger())
    clock = TickingClock(START)
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, acks=acks, clock=clock
    )
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    assert await server.push_once() is False
    state = server._state[alice.identity.public_key]
    assert state.delivery is None
    assert state.prior == {}
    assert acks.outstanding() == 0

    clock.advance(60)
    await server.push_once()
    assert state.failures == 0
    assert len(submit.submissions) == 2, "the dropped post was not tried again"


# --- A late acknowledgement of an earlier attempt still confirms the post ---


async def test_an_expired_attempt_stays_registered() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    clock = TickingClock(START)
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, acks=acks, clock=clock
    )
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    await server.push_once()
    first = checksum_of(server, alice, submit.submissions[0].packet)
    clock.advance(60)
    server._expire_deliveries()

    assert server._state[alice.identity.public_key].failures == 1
    assert acks.owner_of(first) == server.subscriber_name
    assert acks.outstanding() == 1


async def test_the_first_attempts_ack_arriving_during_the_retry_confirms_the_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_attempts(monkeypatch, 0, 1)
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    clock = TickingClock(START)
    events: list = []
    server = server_with_members(
        alice,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        acks=acks,
        clock=clock,
        events=events,
    )
    stamp = await seed_post(server, storage, author=Entity("author").identity.public_key)
    key = alice.identity.public_key

    await server.push_once()
    first = checksum_of(server, alice, submit.submissions[0].packet)
    clock.advance(60)
    await server.push_once()  # expires attempt 1, submits the retry
    assert len(submit.submissions) == 2
    retry = checksum_of(server, alice, submit.submissions[1].packet)
    assert retry != first

    matched = ack(acks, first, at=clock.now())

    assert not isinstance(matched, AckUnowned)
    assert acks.logger.of("ack_unmatched") == []
    state = server._state[key]
    assert server.members[key].sync_since == stamp
    assert state.delivery is None, "the retry is still outstanding"
    assert state.failures == 0
    assert acks.owner_of(retry) is None
    assert acks.owner_of(first) is None
    acknowledged = next(event for event in events if isinstance(event, DeliveryAcknowledged))
    assert acknowledged.late is True
    await asyncio.sleep(0)
    assert storage.members.of(server.room.id)[0].sync_since == stamp


async def test_after_confirmation_another_attempts_ack_moves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_attempts(monkeypatch, 0, 1, 2)
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    clock = TickingClock(START)
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, acks=acks, clock=clock
    )
    author = Entity("author").identity.public_key
    stamp = await seed_post(server, storage, author=author, at=NOW - 100)
    await seed_post(server, storage, author=author, at=NOW - 90)
    key = alice.identity.public_key

    checksums = []
    for _ in range(3):
        await server.push_once()
        checksums.append(checksum_of(server, alice, submit.submissions[-1].packet))
        clock.advance(60)
    # Attempts 1 and 2 have expired; attempt 3 is live.

    ack(acks, checksums[0])
    assert server.members[key].sync_since == stamp

    again = ack(acks, checksums[1])
    assert isinstance(again, AckUnowned)
    assert server.members[key].sync_since == stamp
    assert server.pushes_acknowledged == 1


async def test_a_forced_cursor_change_releases_earlier_attempts() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    clock = TickingClock(START)
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, acks=acks, clock=clock
    )
    stamp = await seed_post(server, storage, author=Entity("author").identity.public_key)
    key = alice.identity.public_key

    await server.push_once()
    first = checksum_of(server, alice, submit.submissions[0].packet)
    clock.advance(60)
    server._expire_deliveries()
    assert acks.owner_of(first) == server.subscriber_name

    server._set_cursor(server.members[key], stamp + 10)

    assert acks.owner_of(first) is None
    assert server._state[key].prior == {}


async def test_composing_a_different_post_releases_earlier_attempts() -> None:
    """Retention removed the pending post, so the next push is another one."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    clock = TickingClock(START)
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, acks=acks, clock=clock
    )
    author = Entity("author").identity.public_key
    await seed_post(server, storage, author=author, at=NOW - 100)
    second = await seed_post(server, storage, author=author, at=NOW - 90)

    await server.push_once()
    first = checksum_of(server, alice, submit.submissions[0].packet)
    clock.advance(60)
    await storage.messages.prune(server.room.id, retention_days=None, retention_messages=1)
    await server.push_once()

    _envelope, plaintext = decrypt_push(server, alice, submit.submissions[1].packet)
    assert plaintext[:4] == second.to_bytes(4, "little")
    assert acks.owner_of(first) is None


async def test_revoking_a_member_releases_its_attempts() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    clock = TickingClock(START)
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, acks=acks, clock=clock
    )
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    await server.push_once()
    clock.advance(60)
    await server.push_once()  # one expired attempt, one live
    assert acks.outstanding() >= 1

    await server.revoke(alice.identity.public_key)

    assert acks.outstanding() == 0


async def test_a_backed_off_member_resumes_when_an_earlier_ack_arrives_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_attempts(monkeypatch, 0, 1, 2)
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    clock = TickingClock(START)
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, acks=acks, clock=clock
    )
    author = Entity("author").identity.public_key
    stamp = await seed_post(server, storage, author=author, at=NOW - 100)
    await seed_post(server, storage, author=author, at=NOW - 90)
    key = alice.identity.public_key

    for _ in range(MAX_PUSH_FAILURES):
        await server.push_once()
        clock.advance(60)
    await server.push_once()
    state = server._state[key]
    assert state.backed_off is True

    ack(acks, checksum_of(server, alice, submit.submissions[1].packet))

    assert state.failures == 0
    assert state.backed_off is False
    assert server.members[key].sync_since == stamp
    assert await server.push_once() is True, "delivery did not resume"
    assert len(submit.submissions) == MAX_PUSH_FAILURES + 1


async def test_delivery_reports_carry_the_window_and_the_time_after_transmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_attempts(monkeypatch, 0, 1, 2)
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    acks = AckRegistry(logger=RecordingLogger())
    clock = TickingClock(START)
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, acks=acks, clock=clock
    )
    author = Entity("author").identity.public_key
    await seed_post(server, storage, author=author, at=NOW - 100)
    await seed_post(server, storage, author=author, at=NOW - 90)
    logger = server._log
    assert isinstance(logger, RecordingLogger)

    await server.push_once()
    window = _push_timeout_ms(server._route_to(server.members[alice.identity.public_key]))
    assert logger.of("room_push_sent")[0]["ack_window_ms"] == window

    # On time: 2.5 s after transmission, matching the live attempt.
    ack(
        acks,
        checksum_of(server, alice, submit.submissions[0].packet),
        at=clock.now() + dt.timedelta(seconds=2.5),
    )
    on_time = logger.of("room_delivery_acknowledged")[0]
    assert on_time["ack_after_tx_ms"] == 2500.0
    assert on_time["late"] is False

    # Late: the next post's first attempt expires, a retry goes out, and the
    # first attempt's acknowledgement arrives 70 s after it was transmitted.
    await server.push_once()
    first_at = clock.now()
    clock.advance(60)
    await server.push_once()
    ack(
        acks,
        checksum_of(server, alice, submit.submissions[1].packet),
        at=first_at + dt.timedelta(seconds=70),
    )
    late = logger.of("room_delivery_acknowledged")[1]
    assert late["ack_after_tx_ms"] == 70_000.0
    assert late["late"] is True


# --- The room's own window --------------------------------------------------


def multi_hop_route_to(paths: PathStore, public_key: bytes, hops: int) -> None:
    paths._insert(
        PathKey.for_public_key(public_key),
        LearnedPath(
            path=bytes(range(1, hops + 1)),
            hash_size=1,
            hop_count=hops,
            snr_db=8.0,
            confirmed_at=START,
            packet_id="seed",
        ),
    )


def test_a_room_window_replaces_the_firmware_formula_for_every_route() -> None:
    for route in (Route(flood=True), Route(flood=False), Route(flood=False, hop_count=5)):
        assert _push_timeout_ms(route, 30) == 30_000.0
    assert _push_timeout_ms(Route(flood=True)) == 12_000.0
    assert _push_timeout_ms(Route(flood=False, hop_count=2)) == 4_000.0 + 2_000.0 * 3


@pytest.mark.parametrize("routing", ["flood", "direct"])
async def test_a_room_with_a_30_second_window_waits_30_seconds_after_transmission(
    routing: str,
) -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    clock = TickingClock(START)
    server = server_with_members(
        alice,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        clock=clock,
        routes=False,
        room=room_record(push_ack_window_seconds=30),
    )
    if routing == "direct":
        multi_hop_route_to(server.paths, alice.identity.public_key, 4)
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    await server.push_once()

    decoded = decode_packet(submit.submissions[0].packet)
    expected_type = RouteType.FLOOD if routing == "flood" else RouteType.DIRECT
    assert decoded.route_type is expected_type
    delivery = server._state[alice.identity.public_key].delivery
    assert delivery is not None
    assert delivery.deadline == clock.now() + dt.timedelta(seconds=30)


# --- Only members heard recently --------------------------------------------


async def test_a_member_not_heard_within_the_limit_is_skipped_and_resumes_when_heard() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    # The member was last active on 2026-09-05; ten days later it is stale.
    clock = TickingClock(START + dt.timedelta(days=10))
    server = server_with_members(
        alice,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        clock=clock,
        room=room_record(push_recent_days=7),
    )
    await seed_post(server, storage, author=Entity("author").identity.public_key)
    key = alice.identity.public_key

    for _ in range(3):
        assert await server.push_once() is False
    assert submit.submissions == []
    assert server._state[key].failures == 0
    assert server.members[key].sync_since == 0
    assert server.members_not_recent() == 1
    assert server.as_json()["members_not_recent"] == 1
    assert server.as_json()["push_recent_days"] == 7

    server._note_activity(server.members[key])
    assert server.members_not_recent() == 0
    assert await server.push_once() is True


async def test_a_member_heard_only_by_advert_is_eligible() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    clock = TickingClock(START + dt.timedelta(days=10))
    adverts: dict[bytes, dt.datetime] = {}
    server = server_with_members(
        alice,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        clock=clock,
        room=room_record(push_recent_days=7),
        last_heard=adverts.get,
    )
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    assert await server.push_once() is False
    adverts[alice.identity.public_key] = clock.now() - dt.timedelta(days=1)
    assert await server.push_once() is True


async def test_with_no_limit_every_member_is_pushed_to_however_long_ago_it_was_heard() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    clock = TickingClock(START + dt.timedelta(days=400))
    server = server_with_members(alice, server_entity=lounge, storage=storage, clock=clock)
    await seed_post(server, storage, author=Entity("author").identity.public_key)

    assert await server.push_once() is True
    assert server.members_not_recent() == 0
    assert server.as_json()["push_ack_window_seconds"] is None


# --- Settings applied to a running room --------------------------------------


async def test_a_new_window_applies_to_the_next_push_only() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    clock = TickingClock(START)
    server = server_with_members(
        alice, server_entity=lounge, storage=storage, submit=submit, clock=clock
    )
    author = Entity("author").identity.public_key
    stamp = await seed_post(server, storage, author=author, at=NOW - 100)
    await seed_post(server, storage, author=author, at=NOW - 90)
    key = alice.identity.public_key

    await server.push_once()
    outstanding = server._state[key].delivery
    assert outstanding is not None
    deadline = outstanding.deadline

    changed = server.apply_record(replace(server.room, push_ack_window_seconds=120))

    assert changed == ["push_ack_window_seconds"]
    assert server.room.push_ack_window_seconds == 120
    assert server._state[key].delivery is outstanding
    assert outstanding.deadline == deadline, "the outstanding push's window moved"
    logger = server._log
    assert isinstance(logger, RecordingLogger)
    assert logger.of("room_settings_applied")[0]["changed"] == ["push_ack_window_seconds"]

    server._clear_delivery(key, advance=True)
    server._set_cursor(server.members[key], stamp)
    await server.push_once()
    following = server._state[key].delivery
    assert following is not None
    assert following.deadline == clock.now() + dt.timedelta(seconds=120)

    assert server.apply_record(server.room) == [], "an unchanged record reported a change"
