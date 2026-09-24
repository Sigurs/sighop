"""Posts and durable history (milestone 6, `room-history`).

The rule these are built around is design D6's, and it is the one that separates
sighop from the firmware it copies: **a post is acknowledged only once it is
stored**. The firmware acknowledges after putting the post in a RAM ring, which
cannot fail; ours can, and an acknowledgement is a promise the client will not
retry. So every failure path here asserts on the scheduler having received
nothing, not on a flag somewhere.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from sighop.net.acks import AckRegistry
from sighop.net.bus import PriorityClass
from sighop.net.paths import PathStore
from sighop.net.room import (
    MAX_POST_TEXT_LEN,
    MAX_PUSHABLE_TEXT_LEN,
    POST_CLOCK_SKEW_TOLERANCE_S,
    STORED_POST_TEXT_LEN,
    PostRefused,
    PostStored,
    RefusalReason,
    RoomServer,
)
from sighop.protocol.crypto import (
    SharedSecretCache,
    ack_checksum_for,
    encrypt_then_mac,
    mac_then_decrypt,
)
from sighop.protocol.packet import (
    PAYLOAD_VERSION_1,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
)
from sighop.protocol.packet import decode as decode_packet
from sighop.protocol.packet import encode as encode_packet
from sighop.protocol.payloads import (
    DirectEnvelope,
    Permission,
    ReturnedPathBody,
    TextType,
    build_direct_envelope,
    build_text_message_body,
    parse_ack,
    parse_direct_envelope,
    parse_returned_path_body,
)
from sighop.radio.modem import EU868_NARROW
from tests.roomfixtures import MemoryStorage, member_record, room_record
from tests.test_dm import (
    Entity,
    RecordingSubmit,
    TickingClock,
    _packet_for,
    compose_body,
    zero_hop_route_to,
)
from tests.test_room_login import login_packet
from tests.test_tx import RecordingLogger

START = dt.datetime(2026, 9, 5, 21, 0, tzinfo=dt.UTC)
NOW = int(START.timestamp())


def post_packet(
    *,
    author: Entity,
    server: Entity,
    text: bytes = b"hello room",
    timestamp: int = NOW,
    attempt: int = 0,
    txt_type: TextType = TextType.PLAIN,
    route_type: RouteType = RouteType.DIRECT,
    path: bytes = b"",
    hash_size: int = 1,
) -> tuple[bytes, bytes]:
    """A member's post, and the plaintext its acknowledgement is computed over."""
    body = compose_body(timestamp=timestamp, attempt=attempt, text=text, txt_type=txt_type)
    plaintext = build_text_message_body(body)
    secret = SharedSecretCache().get(author.identity, server.identity.public_key)
    mac, ciphertext = encrypt_then_mac(secret, plaintext)
    envelope = DirectEnvelope(
        payload_type=PayloadType.TXT_MSG,
        dest_hash=server.node_hash,
        src_hash=author.node_hash,
        mac=mac,
        ciphertext=ciphertext,
    )
    packet = encode_packet(
        Packet(
            header=PacketHeader(route_type, PayloadType.TXT_MSG, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=len(path) // hash_size,
            hash_size=hash_size,
            path=path,
            payload=build_direct_envelope(envelope),
        )
    )
    return packet, plaintext


def room_with(
    *,
    author: Entity,
    server_entity: Entity,
    permission: Permission = Permission.READ_WRITE,
    storage: MemoryStorage | None = None,
    submit: RecordingSubmit | None = None,
    events: list | None = None,
    paths: PathStore | None = None,
    clock: TickingClock | None = None,
    last_timestamp: int = 0,
    logger: RecordingLogger | None = None,
    **room_kwargs: Any,
) -> RoomServer:
    room = room_record(**room_kwargs)
    routes = paths or PathStore()
    zero_hop_route_to(routes, author.identity.public_key, at=START)
    return RoomServer(
        entity=server_entity,
        room=room,
        storage=storage or MemoryStorage(),
        paths=routes,
        submit=submit or RecordingSubmit(),
        acks=AckRegistry(logger=RecordingLogger()),
        members=[
            member_record(
                room,
                author.identity.public_key,
                permission=permission,
                last_timestamp=last_timestamp,
            )
        ],
        clock=clock or TickingClock(START),
        radio=EU868_NARROW,
        on_event=events.append if events is not None else None,
        logger=logger or RecordingLogger(),
    )


# --- 7.1 Who may post -------------------------------------------------------


async def test_a_member_with_posting_permission_has_its_post_stored_and_acknowledged() -> None:
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = room_with(
        author=author, server_entity=lounge, storage=storage, submit=submit, events=events
    )

    packet, _plaintext = post_packet(author=author, server=lounge)
    await server.handle(_packet_for(packet))

    stored = next(event for event in events if isinstance(event, PostStored))
    assert stored.text.raw == b"hello room"
    assert storage.messages.posts[0].author_public_key == author.identity.public_key
    assert len(submit.submissions) == 1
    assert submit.submissions[0].priority is PriorityClass.ACK


@pytest.mark.parametrize("permission", [Permission.READ_WRITE, Permission.ADMIN])
async def test_read_write_and_admin_members_may_post(permission: Permission) -> None:
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    server = room_with(author=author, server_entity=lounge, permission=permission, storage=storage)

    packet, _ = post_packet(author=author, server=lounge)
    await server.handle(_packet_for(packet))

    assert len(storage.messages.posts) == 1


async def test_a_read_only_member_is_refused_with_no_acknowledgement() -> None:
    """7.1, `MyMesh.cpp:479`: refused silently from a `PERM_ACL_GUEST` member."""
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = room_with(
        author=author,
        server_entity=lounge,
        permission=Permission.GUEST,
        storage=storage,
        submit=submit,
        events=events,
    )

    packet, _ = post_packet(author=author, server=lounge)
    await server.handle(_packet_for(packet))

    assert storage.messages.posts == []
    assert submit.submissions == [], "a read-only member's post was acknowledged"
    refused = next(event for event in events if isinstance(event, PostRefused))
    assert refused.reason is RefusalReason.NO_PERMISSION
    assert "not post" in refused.detail


async def test_a_text_message_no_member_key_opens_is_reported_not_acknowledged() -> None:
    stranger, lounge = Entity("stranger"), Entity("lounge")
    member = Entity("member")
    submit = RecordingSubmit()
    events: list = []
    server = room_with(author=member, server_entity=lounge, submit=submit, events=events)

    packet, _ = post_packet(author=stranger, server=lounge)
    await server.handle(_packet_for(packet))

    assert submit.submissions == []
    refused = next(event for event in events if isinstance(event, PostRefused))
    assert refused.reason is RefusalReason.NOT_A_MEMBER


async def test_a_cli_data_message_is_not_answered() -> None:
    """Remote administration over the air is explicitly out of scope."""
    author, lounge = Entity("author"), Entity("lounge")
    submit = RecordingSubmit()
    events: list = []
    server = room_with(author=author, server_entity=lounge, submit=submit, events=events)

    packet, _ = post_packet(author=author, server=lounge, txt_type=TextType.CLI_DATA)
    await server.handle(_packet_for(packet))

    assert submit.submissions == []
    refused = next(event for event in events if isinstance(event, PostRefused))
    assert refused.reason is RefusalReason.UNSUPPORTED_TEXT_TYPE


# --- 7.2 The ordering value (design D3) -------------------------------------


async def test_two_posts_in_one_second_get_distinct_increasing_stamps() -> None:
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    server = room_with(author=author, server_entity=lounge, storage=storage)

    for index in range(2):
        packet, _ = post_packet(author=author, server=lounge, timestamp=NOW + index)
        await server.handle(_packet_for(packet))

    stamps = [post.post_timestamp for post in storage.messages.posts]
    assert stamps == [NOW, NOW + 1]
    assert len(set(stamps)) == 2


async def test_a_clock_that_steps_backwards_still_produces_an_increasing_order() -> None:
    """7.2: a backwards clock stalls the stamping, never reorders the history."""
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    clock = TickingClock(START)
    server = room_with(author=author, server_entity=lounge, storage=storage, clock=clock)

    packet, _ = post_packet(author=author, server=lounge, timestamp=NOW)
    await server.handle(_packet_for(packet))

    clock._now = START - dt.timedelta(hours=1)
    packet, _ = post_packet(author=author, server=lounge, timestamp=NOW + 1)
    await server.handle(_packet_for(packet))

    stamps = [post.post_timestamp for post in storage.messages.posts]
    assert stamps == sorted(stamps)
    assert stamps[1] == stamps[0] + 1


async def test_ordering_continues_after_a_restart() -> None:
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    first = room_with(author=author, server_entity=lounge, storage=storage)
    packet, _ = post_packet(author=author, server=lounge, timestamp=NOW)
    await first.handle(_packet_for(packet))
    before = storage.messages.posts[-1].post_timestamp

    restarted = RoomServer(
        entity=lounge,
        room=first.room,
        storage=storage,
        paths=first.paths,
        submit=RecordingSubmit(),
        acks=AckRegistry(logger=RecordingLogger()),
        members=list(first.members.values()),
        clock=TickingClock(START),
        radio=EU868_NARROW,
        logger=RecordingLogger(),
    )
    packet, _ = post_packet(author=author, server=lounge, timestamp=NOW + 5)
    await restarted.handle(_packet_for(packet))

    assert storage.messages.posts[-1].post_timestamp > before


# --- 7.3 What is stored -----------------------------------------------------


async def test_text_that_is_not_valid_utf8_comes_back_byte_for_byte() -> None:
    """7.3, design D4: a room server re-transmits a post and must reproduce it."""
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    events: list = []
    server = room_with(author=author, server_entity=lounge, storage=storage, events=events)

    raw = b"caf\xe9 \xff\xfe not utf-8"
    packet, _ = post_packet(author=author, server=lounge, text=raw)
    await server.handle(_packet_for(packet))

    assert storage.messages.posts[0].text == raw
    stored = next(event for event in events if isinstance(event, PostStored))
    # Rendered for display, and *marked* as a rendering rather than presented as
    # the author's text.
    assert stored.text.raw == raw
    assert stored.text.is_valid_utf8 is False
    assert "�" in stored.text.text


async def test_a_post_records_its_author_and_the_senders_own_timestamp() -> None:
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    server = room_with(author=author, server_entity=lounge, storage=storage)

    packet, _ = post_packet(author=author, server=lounge, timestamp=NOW + 7)
    await server.handle(_packet_for(packet))

    post = storage.messages.posts[0]
    assert post.author_public_key == author.identity.public_key
    assert post.sender_timestamp == NOW + 7
    assert post.post_timestamp >= NOW


# --- 7.4 The length limit (design D4) ---------------------------------------


@pytest.mark.parametrize("length", [STORED_POST_TEXT_LEN, STORED_POST_TEXT_LEN + 1, 160])
async def test_an_over_long_post_is_shortened_and_still_acknowledged(length: int) -> None:
    """`MyMesh.cpp:57`, `:484-488`: no length check, `strncpy`, then always ack.

    Refusing produced silence the client read as a lost packet, and it retried
    until it gave up — observed in the milestone 6 live exercise.

    160 rather than `MAX_PUSHABLE_TEXT_LEN` is the top of the range because
    `compose_body` refuses past the firmware's `MAX_TEXT_LEN`, so 161..167 is
    reachable by nothing that speaks this protocol — including this test.
    """
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = room_with(
        author=author, server_entity=lounge, storage=storage, submit=submit, events=events
    )

    packet, _ = post_packet(author=author, server=lounge, text=b"x" * length)
    await server.handle(_packet_for(packet))

    assert len(storage.messages.posts) == 1
    assert storage.messages.posts[0].text == b"x" * min(length, STORED_POST_TEXT_LEN)
    assert len(submit.submissions) == 1, "the post was not acknowledged"

    stored = next(event for event in events if isinstance(event, PostStored))
    expected = None if length <= STORED_POST_TEXT_LEN else length
    assert stored.truncated_from == expected, "dropping the author's words must be visible"


async def test_the_acknowledgement_covers_the_text_as_sent_not_as_kept() -> None:
    """The client computes its expectation over what it sent (`:461-462`).

    An acknowledgement over the shortened text would not match, so the client
    would retry a post the room already has.
    """
    author, lounge = Entity("author"), Entity("lounge")
    submit = RecordingSubmit()
    server = room_with(author=author, server_entity=lounge, submit=submit)

    text = b"x" * (STORED_POST_TEXT_LEN + 4)
    packet, _ = post_packet(author=author, server=lounge, text=text)
    await server.handle(_packet_for(packet))

    ack = parse_ack(decode_packet(submit.submissions[0].packet).payload)
    sent = compose_body(timestamp=NOW, attempt=0, text=text)
    assert ack.checksum == ack_checksum_for(sent, author.identity.public_key)
    kept = compose_body(timestamp=NOW, attempt=0, text=text[:STORED_POST_TEXT_LEN])
    assert ack.checksum != ack_checksum_for(kept, author.identity.public_key)


def test_the_store_keeps_exactly_what_a_stock_client_can_send() -> None:
    """156 is the client's own frame budget, not a number we chose.

    `queueMessage` bounds the frame the radio hands the app against
    `MAX_FRAME_SIZE` (176), and a signed post to a v3 app spends 20 bytes of
    prefix before the text. That is why a stock composer stops at 156, and why
    storing 156 means nothing a stock client can compose is ever shortened.
    """
    max_frame_size = 176  # BaseSerialInterface.h:5
    v3_prefix = 4 + 6 + 1 + 1 + 4 + 4  # companion_radio/MyMesh.cpp:435-452
    assert max_frame_size - v3_prefix == STORED_POST_TEXT_LEN
    assert STORED_POST_TEXT_LEN == 156


def test_everything_stored_can_be_pushed() -> None:
    """The store limit is useless if it exceeds what a packet can carry."""
    from sighop.protocol.packet import MAX_PACKET_PAYLOAD

    push_prefix = 4 + 1 + 4  # timestamp, flags, author key prefix (`:72-85`)
    envelope = 1 + 1 + 2  # dest hash, src hash, MAC
    blocks = (MAX_PACKET_PAYLOAD - envelope) // 16
    assert blocks * 16 - push_prefix == MAX_PUSHABLE_TEXT_LEN
    assert MAX_PUSHABLE_TEXT_LEN == 167
    assert STORED_POST_TEXT_LEN < MAX_PUSHABLE_TEXT_LEN, "a stored post must push"


def test_the_firmwares_own_constant_is_a_convention_we_do_not_follow() -> None:
    """`160 - 9`, where 160 is ten cipher blocks picked for chat messages.

    Recorded so the number is not mistaken for a protocol limit and quietly
    reintroduced. The cost of not following it: a room served by stock firmware
    truncates at 150, so history is not byte-identical across the two.
    """
    from sighop.net.dm import MAX_TEXT_LEN

    assert MAX_POST_TEXT_LEN == MAX_TEXT_LEN - 9
    assert MAX_POST_TEXT_LEN == 151
    assert STORED_POST_TEXT_LEN > MAX_POST_TEXT_LEN


# --- 7.5 Stored before acknowledged (design D6) -----------------------------


async def test_a_post_that_cannot_be_stored_in_time_is_not_acknowledged() -> None:
    """7.5: an acknowledgement is a promise, and this is where it is not made."""
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    storage.messages.never_answers = True
    submit = RecordingSubmit()
    events: list = []
    server = room_with(
        author=author, server_entity=lounge, storage=storage, submit=submit, events=events
    )

    packet, _ = post_packet(author=author, server=lounge)
    # The reception path is not delayed for the sender's whole window: the
    # handler awaits a task with a deadline, and the deadline is what expires.
    await asyncio.wait_for(server.handle(_packet_for(packet)), timeout=10)

    assert submit.submissions == [], "a post that never landed was acknowledged"
    refused = next(event for event in events if isinstance(event, PostRefused))
    assert refused.reason is RefusalReason.STORAGE_FAILED


async def test_a_room_refuses_posts_while_its_storage_is_degraded() -> None:
    """7.7, design D6: a room is exactly as available as its history."""
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage(degraded=True)
    submit = RecordingSubmit()
    events: list = []
    server = room_with(
        author=author, server_entity=lounge, storage=storage, submit=submit, events=events
    )

    assert server.accepting_posts is False
    assert server.as_json()["accepting_posts"] is False

    packet, _ = post_packet(author=author, server=lounge)
    await server.handle(_packet_for(packet))

    assert submit.submissions == []
    refused = next(event for event in events if isinstance(event, PostRefused))
    assert refused.reason is RefusalReason.STORAGE_DEGRADED

    # And it clears with the database's own flag, rather than needing a restart.
    storage.degraded = False
    assert server.accepting_posts is True
    packet, _ = post_packet(author=author, server=lounge, timestamp=NOW + 1)
    await server.handle(_packet_for(packet))
    assert len(storage.messages.posts) == 1


async def test_the_acknowledgement_is_the_one_the_sender_computes() -> None:
    """The whole exchange is worthless if these two disagree."""
    author, lounge = Entity("author"), Entity("lounge")
    submit = RecordingSubmit()
    server = room_with(author=author, server_entity=lounge, submit=submit)

    packet, _plaintext = post_packet(author=author, server=lounge)
    await server.handle(_packet_for(packet))

    sent = decode_packet(submit.submissions[0].packet)
    ack = parse_ack(sent.payload)
    body = compose_body(timestamp=NOW, attempt=0, text=b"hello room")
    assert ack.checksum == ack_checksum_for(body, author.identity.public_key)


# --- 7.6 Retries ------------------------------------------------------------


async def test_a_retried_post_is_acknowledged_again_and_stored_once() -> None:
    """7.6, `MyMesh.cpp:449`: "prevent replay attacks, but send Acks for retries"."""
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    server = room_with(author=author, server_entity=lounge, storage=storage, submit=submit)

    for attempt in (0, 1):
        packet, _ = post_packet(author=author, server=lounge, timestamp=NOW, attempt=attempt)
        await server.handle(_packet_for(packet))

    assert len(storage.messages.posts) == 1, "a retry created a second row"
    assert len(submit.submissions) == 2, "a retry went unacknowledged"


async def test_a_post_below_the_recorded_timestamp_is_refused_as_a_replay() -> None:
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = room_with(
        author=author,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        events=events,
        last_timestamp=NOW + POST_CLOCK_SKEW_TOLERANCE_S + 1,
    )

    packet, _ = post_packet(author=author, server=lounge, timestamp=NOW)
    await server.handle(_packet_for(packet))

    assert storage.messages.posts == []
    assert submit.submissions == []
    refused = next(event for event in events if isinstance(event, PostRefused))
    assert refused.reason is RefusalReason.REPLAY
    assert "301 s below" in refused.detail, "the refusal must state the gap"


# --- Clock skew between the phone and the radio (post-replay-tolerance) -----


async def test_a_post_stamped_by_a_clock_behind_the_login_is_stored_and_acknowledged() -> None:
    """`issue-room-post-2`: the radio stamped the login 15 s ahead of the phone."""
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    server = room_with(
        author=author,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        last_timestamp=NOW + 15,
    )

    packet, _ = post_packet(author=author, server=lounge, timestamp=NOW)
    await server.handle(_packet_for(packet))

    assert len(storage.messages.posts) == 1
    assert len(submit.submissions) == 1
    member = server.members[author.identity.public_key]
    assert member.last_timestamp == NOW + 15, "an accepted post lowered the guard"


async def test_a_post_exactly_at_the_tolerance_is_accepted() -> None:
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    server = room_with(
        author=author,
        server_entity=lounge,
        storage=storage,
        last_timestamp=NOW + POST_CLOCK_SKEW_TOLERANCE_S,
    )

    packet, _ = post_packet(author=author, server=lounge, timestamp=NOW)
    await server.handle(_packet_for(packet))

    assert len(storage.messages.posts) == 1


async def test_a_retry_of_a_post_below_the_recorded_timestamp_is_reported_as_one() -> None:
    """D3: `retry` is "already stored", which equality with the guard no longer says."""
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = room_with(
        author=author,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        events=events,
        last_timestamp=NOW + 15,
    )

    for attempt in (0, 1):
        packet, _ = post_packet(author=author, server=lounge, timestamp=NOW, attempt=attempt)
        await server.handle(_packet_for(packet))

    assert len(storage.messages.posts) == 1, "a retry created a second row"
    assert len(submit.submissions) == 2, "a retry went unacknowledged"
    stored = [event for event in events if isinstance(event, PostStored)]
    assert [event.retry for event in stored] == [False, True]


async def test_a_post_after_a_blank_password_relogin_from_a_fast_radio_is_stored() -> None:
    """`issue-room-post-2` end to end: the re-login is stamped by the radio, 15 s
    ahead, and the post written after it by the phone, 9 s older than the login."""
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = room_with(
        author=author,
        server_entity=lounge,
        storage=storage,
        submit=submit,
        events=events,
        last_timestamp=NOW,
    )

    login = login_packet(client=author, server=lounge, password="", timestamp=NOW + 15)
    await server.handle(_packet_for(login))
    packet, _ = post_packet(author=author, server=lounge, timestamp=NOW + 6)
    await server.handle(_packet_for(packet))

    assert not any(isinstance(event, PostRefused) for event in events)
    assert len(storage.messages.posts) == 1
    stored = next(event for event in events if isinstance(event, PostStored))
    assert stored.acknowledged


# --- How the acknowledgement is routed (flooded-post-path-return-ack) ------


def _path_return(author: Entity, lounge: Entity, packet: bytes) -> ReturnedPathBody:
    """Open a submitted reply as the author would, and insist it is a path return."""
    decoded = decode_packet(packet)
    envelope = parse_direct_envelope(decoded.payload_type, decoded.payload)
    assert isinstance(envelope, DirectEnvelope)
    assert envelope.payload_type is PayloadType.PATH
    secret = SharedSecretCache().get(author.identity, lounge.identity.public_key)
    _candidate, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
    assert plaintext is not None
    body = parse_returned_path_body(plaintext)
    assert isinstance(body, ReturnedPathBody)
    return body


async def test_a_flooded_post_is_acknowledged_by_a_path_return_despite_a_known_route() -> None:
    """D1: `room_with` gives the author a zero-hop route, and it is not used."""
    author, lounge = Entity("author"), Entity("lounge")
    submit = RecordingSubmit()
    server = room_with(author=author, server_entity=lounge, submit=submit)

    packet, _ = post_packet(
        author=author, server=lounge, route_type=RouteType.FLOOD, path=b"\xab\xcd"
    )
    await server.handle(_packet_for(packet))

    assert len(submit.submissions) == 1
    assert submit.submissions[0].priority is PriorityClass.ACK
    assert submit.submissions[0].origin == "room_post_ack"
    sent = decode_packet(submit.submissions[0].packet)
    assert sent.route_type is RouteType.FLOOD
    body = _path_return(author, lounge, submit.submissions[0].packet)
    assert (body.hop_count, body.hash_size, body.path) == (2, 1, b"\xab\xcd")
    assert body.extra_type is PayloadType.ACK
    assert body.extra_ack is not None
    expected = compose_body(timestamp=NOW, attempt=0, text=b"hello room")
    assert body.extra_ack.checksum == ack_checksum_for(expected, author.identity.public_key)


async def test_a_zero_hop_flooded_post_gets_a_path_return_and_no_direct_ack() -> None:
    author, lounge = Entity("author"), Entity("lounge")
    submit = RecordingSubmit()
    server = room_with(author=author, server_entity=lounge, submit=submit)

    packet, _ = post_packet(author=author, server=lounge, route_type=RouteType.FLOOD, hash_size=2)
    await server.handle(_packet_for(packet))

    assert len(submit.submissions) == 1
    sent = decode_packet(submit.submissions[0].packet)
    assert sent.route_type is RouteType.FLOOD, "a zero-hop DIRECT ACK went out"
    assert sent.payload_type is PayloadType.PATH
    body = _path_return(author, lounge, submit.submissions[0].packet)
    assert (body.hop_count, body.hash_size, body.path) == (0, 2, b"")
    assert body.extra_type is PayloadType.ACK


async def test_a_retried_flooded_post_gets_its_own_path_return_and_is_stored_once() -> None:
    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    server = room_with(author=author, server_entity=lounge, storage=storage, submit=submit)

    for attempt in (0, 1):
        packet, _ = post_packet(
            author=author, server=lounge, attempt=attempt, route_type=RouteType.FLOOD
        )
        await server.handle(_packet_for(packet))

    assert len(storage.messages.posts) == 1, "a retry created a second row"
    assert len(submit.submissions) == 2, "a retry went unacknowledged"
    retried = _path_return(author, lounge, submit.submissions[1].packet)
    expected = compose_body(timestamp=NOW, attempt=1, text=b"hello room")
    assert retried.extra_ack is not None
    assert retried.extra_ack.checksum == ack_checksum_for(expected, author.identity.public_key)


async def test_a_direct_post_is_acknowledged_with_a_bare_ack_on_the_known_route() -> None:
    author, lounge = Entity("author"), Entity("lounge")
    submit = RecordingSubmit()
    server = room_with(author=author, server_entity=lounge, submit=submit)

    packet, _ = post_packet(author=author, server=lounge)
    await server.handle(_packet_for(packet))

    sent = decode_packet(submit.submissions[0].packet)
    assert (sent.route_type, sent.payload_type) == (RouteType.DIRECT, PayloadType.ACK)


async def test_a_direct_post_with_no_known_route_gets_a_bare_flooded_ack() -> None:
    author, lounge = Entity("author"), Entity("lounge")
    submit = RecordingSubmit()
    server = room_with(author=author, server_entity=lounge, submit=submit)
    server.paths = PathStore()

    packet, _ = post_packet(author=author, server=lounge)
    await server.handle(_packet_for(packet))

    sent = decode_packet(submit.submissions[0].packet)
    assert (sent.route_type, sent.payload_type) == (RouteType.FLOOD, PayloadType.ACK)
    expected = compose_body(timestamp=NOW, attempt=0, text=b"hello room")
    assert parse_ack(sent.payload).checksum == ack_checksum_for(
        expected, author.identity.public_key
    )


@pytest.mark.parametrize(
    ("route_type", "ack_route"),
    [(RouteType.FLOOD, "PATH_RETURN"), (RouteType.DIRECT, "DIRECT h0")],
)
async def test_the_stored_post_names_the_route_its_acknowledgement_took(
    route_type: RouteType, ack_route: str
) -> None:
    author, lounge = Entity("author"), Entity("lounge")
    logger = RecordingLogger()
    events: list = []
    server = room_with(author=author, server_entity=lounge, logger=logger, events=events)

    packet, _ = post_packet(author=author, server=lounge, route_type=route_type)
    await server.handle(_packet_for(packet))

    [logged] = logger.of("room_post_stored")
    assert logged["ack_route"] == ack_route
    stored = next(event for event in events if isinstance(event, PostStored))
    assert stored.ack_route == ack_route


# --- 4.4 Exactly one acknowledgement, with both subscribers wired ------------


async def test_only_one_acknowledgement_reaches_the_scheduler_for_a_room_post() -> None:
    """4.4, design D10: the duplicate this rule exists to prevent."""
    from sighop.net.contacts import ContactStore
    from sighop.net.dm import DirectMessenger

    author, lounge = Entity("author"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    paths = PathStore()
    zero_hop_route_to(paths, author.identity.public_key, at=START)
    contacts = ContactStore(logger=RecordingLogger())
    contacts.add_public_key(author.identity.public_key)
    acks = AckRegistry(logger=RecordingLogger())

    room = room_record()
    server = RoomServer(
        entity=lounge,
        room=room,
        storage=storage,
        paths=paths,
        submit=submit,
        acks=acks,
        members=[member_record(room, author.identity.public_key)],
        clock=TickingClock(START),
        radio=EU868_NARROW,
        logger=RecordingLogger(),
    )
    messenger = DirectMessenger(
        contacts=contacts,
        paths=paths,
        submit=submit,
        entities=[lounge],
        clock=TickingClock(START),
        radio=EU868_NARROW,
        acks=acks,
        logger=RecordingLogger(),
    )
    messenger.claim_for_room(lounge.entity_id)

    packet, _ = post_packet(author=author, server=lounge)
    record = _packet_for(packet)
    # Both subscribers see every reception, exactly as the bus delivers it.
    await asyncio.gather(server.handle(record), messenger.handle(record))

    assert len(submit.submissions) == 1, "two acknowledgements for one post"
    assert len(storage.messages.posts) == 1
    assert messenger.received == 0
