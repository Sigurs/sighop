"""The request surface and retention (milestone 6, `room-server`, `room-history`).

What these are mostly about is §4.1's rule reaching the wire: **what the board
reported is recorded, never inferred**. A room server answering `GET_STATUS` has
eighteen fields to fill and a KISS modem cannot supply all of them, so the ones
it cannot are reported as the wire format expresses absence rather than invented
— and the test names which those are, so a later "improvement" that fills one in
has to delete an assertion on purpose.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
from typing import Any
from unittest.mock import patch

from sighop.net.acks import AckRegistry
from sighop.net.paths import PathStore
from sighop.net.room import (
    POST_SYNC_DELAY_SECS,
    RefusalReason,
    RequestAnswered,
    RequestRefused,
    RetentionPruned,
    RoomRetentionPruner,
    RoomServer,
)
from sighop.protocol.crypto import (
    SharedSecretCache,
    ack_checksum,
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
    SERVER_STATS_OFFSETS,
    TELEM_CHANNEL_SELF,
    DirectEnvelope,
    LppType,
    Permission,
    RequestBody,
    RequestType,
    ServerStats,
    build_direct_envelope,
    build_request_body,
    parse_ack,
    parse_direct_envelope,
    parse_status_body,
    parse_telemetry_frame,
    temperature_entry,
    voltage_entry,
)
from sighop.protocol.result import DecodeFailure
from sighop.radio.modem import EU868_NARROW
from tests.roomfixtures import MemoryStorage, member_record, room_record
from tests.test_dm import Entity, RecordingSubmit, TickingClock, _packet_for, zero_hop_route_to
from tests.test_tx import RecordingLogger

START = dt.datetime(2026, 9, 5, 21, 0, tzinfo=dt.UTC)
NOW = int(START.timestamp())

KISS_CANNOT_PROVIDE = ("noise_floor",)
"""9.3: the fields a KISS modem has no equivalent for.

`noise_floor` is a value the radio driver samples on the board; a KISS modem
reports no such thing and there is nothing to derive it from. It is emitted as
zero, which is what this field's absence looks like on the wire, and it is named
here so that filling it in later is a deliberate act rather than a plausible
tidy-up. Everything else in the struct is either a runtime counter sighop
genuinely keeps, a value the §4.1 probe read back from the board, or this room's
own posted and pushed counts.
"""


def request_packet(
    *,
    member: Entity,
    server: Entity,
    request_type: RequestType | int,
    arguments: bytes = b"",
    timestamp: int = NOW,
    route_type: RouteType = RouteType.DIRECT,
) -> tuple[bytes, bytes]:
    plaintext = build_request_body(
        RequestBody(timestamp=timestamp, request_type=request_type, arguments=arguments)
    )
    secret = SharedSecretCache().get(member.identity, server.identity.public_key)
    mac, ciphertext = encrypt_then_mac(secret, plaintext)
    envelope = DirectEnvelope(
        payload_type=PayloadType.REQ,
        dest_hash=server.node_hash,
        src_hash=member.node_hash,
        mac=mac,
        ciphertext=ciphertext,
    )
    packet = encode_packet(
        Packet(
            header=PacketHeader(route_type, PayloadType.REQ, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=build_direct_envelope(envelope),
        )
    )
    return packet, plaintext


def server_for(
    member: Entity,
    server_entity: Entity,
    *,
    storage: MemoryStorage | None = None,
    submit: RecordingSubmit | None = None,
    events: list | None = None,
    permission: Permission = Permission.READ_WRITE,
    routed: bool = True,
    telemetry: list | None = None,
    runtime_stats=None,
    sync_since: int = 0,
    radio=EU868_NARROW,
    radio_ready: asyncio.Event | None = None,
    logger: RecordingLogger | None = None,
    **room_kwargs: Any,
) -> RoomServer:
    room = room_record(**room_kwargs)
    paths = PathStore()
    if routed:
        zero_hop_route_to(paths, member.identity.public_key, at=START)
    store = storage or MemoryStorage()
    row = member_record(
        room, member.identity.public_key, permission=permission, sync_since=sync_since
    )
    store.rows_seed(row)
    return RoomServer(
        entity=server_entity,
        room=room,
        storage=store,
        paths=paths,
        submit=submit or RecordingSubmit(),
        acks=AckRegistry(logger=RecordingLogger()),
        members=[row],
        clock=TickingClock(START),
        radio=radio,
        radio_ready=radio_ready,
        telemetry=telemetry or [],
        runtime_stats=runtime_stats,
        on_event=events.append if events is not None else None,
        logger=logger or RecordingLogger(),
    )


def decrypt_response(server: RoomServer, member: Entity, packet: bytes) -> bytes:
    decoded = decode_packet(packet)
    envelope = parse_direct_envelope(decoded.payload_type, decoded.payload)
    assert isinstance(envelope, DirectEnvelope)
    secret = SharedSecretCache().get(member.identity, server.entity.identity.public_key)
    _candidate, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
    assert plaintext is not None
    return plaintext


# --- 9.1 Keep-alive ---------------------------------------------------------


async def test_a_keep_alive_is_acknowledged_with_the_unsynced_count() -> None:
    """9.1, `MyMesh.cpp:556-576`."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = server_for(alice, lounge, storage=storage, submit=submit, events=events)
    author = Entity("author").identity.public_key
    for index in range(3):
        await storage.messages.store(
            room_id=server.room.id,
            author_public_key=author,
            text=b"x",
            now=NOW - 100 + index,
            posted_at=START,
        )

    packet, plaintext = request_packet(
        member=alice, server=lounge, request_type=RequestType.KEEP_ALIVE
    )
    await server.handle(_packet_for(packet))

    assert len(submit.submissions) == 1
    decoded = decode_packet(submit.submissions[0].packet)
    assert decoded.payload_type is PayloadType.ACK
    payload = decoded.payload

    # Asserted against the raw payload rather than through `parse_ack`, and the
    # reason is a real divergence worth recording: a keep-alive answer is
    # **five** bytes — the 4-byte checksum plus the appended unsynced count
    # (`MyMesh.cpp:574`) — while `parse_ack` accepts the 4- and 6-byte forms the
    # chat protocol uses. Nothing in this milestone receives one, because being
    # a *client* of someone else's room server is milestone 8's; when that
    # arrives, `parse_ack` has to learn the 5-byte shape.
    assert len(payload) == 5
    # Over the first nine bytes, zero-filled when the sender supplied no
    # position — which is what the firmware hashes either way (`:562`).
    assert payload[:4] == ack_checksum((plaintext + b"\x00" * 9)[:9], alice.identity.public_key)
    assert payload[4] == 3, "the unsynced count is appended to the ACK"
    assert isinstance(parse_ack(payload), DecodeFailure), (
        "if this starts parsing, the 5-byte form was added and the note above is stale"
    )

    answered = next(event for event in events if isinstance(event, RequestAnswered))
    assert answered.request_type is RequestType.KEEP_ALIVE


async def test_a_keep_alive_carrying_a_position_adopts_it() -> None:
    """9.1 / 8.7: a client that reset its history resynchronises from there."""
    alice, lounge = Entity("alice"), Entity("lounge")
    server = server_for(alice, lounge, sync_since=5_000)

    packet, _ = request_packet(
        member=alice,
        server=lounge,
        request_type=RequestType.KEEP_ALIVE,
        arguments=(1_234).to_bytes(4, "little"),
    )
    await server.handle(_packet_for(packet))

    assert server.members[alice.identity.public_key].sync_since == 1_234


async def test_a_zero_position_in_a_keep_alive_is_not_adopted() -> None:
    """`:560`: "this may be 0, if part of decrypted PADDING"."""
    alice, lounge = Entity("alice"), Entity("lounge")
    server = server_for(alice, lounge, sync_since=5_000)

    packet, _ = request_packet(
        member=alice,
        server=lounge,
        request_type=RequestType.KEEP_ALIVE,
        arguments=b"\x00\x00\x00\x00",
    )
    await server.handle(_packet_for(packet))

    assert server.members[alice.identity.public_key].sync_since == 5_000


async def test_a_keep_alive_with_no_known_route_is_answered_with_silence() -> None:
    """9.1, `:570`: a keep-alive answer has no meaning without a route."""
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    events: list = []
    server = server_for(alice, lounge, submit=submit, events=events, routed=False)

    packet, _ = request_packet(member=alice, server=lounge, request_type=RequestType.KEEP_ALIVE)
    await server.handle(_packet_for(packet))

    assert submit.submissions == [], "a keep-alive was flooded"
    refused = next(event for event in events if isinstance(event, RequestRefused))
    assert refused.reason is RefusalReason.NO_ROUTE


async def test_a_keep_alive_records_activity_and_resumes_a_backed_off_member() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    server = server_for(alice, lounge)
    state = server._state[alice.identity.public_key]
    state.failures = 3
    state.backed_off = True

    packet, _ = request_packet(member=alice, server=lounge, request_type=RequestType.KEEP_ALIVE)
    await server.handle(_packet_for(packet))

    assert server.members_behind() == 0


# --- 9.2 / 9.3 Status -------------------------------------------------------


def runtime_stats() -> ServerStats:
    """What the runtime would hand in: shared-radio counters and a probe readback."""
    return ServerStats(
        batt_milli_volts=4050,
        curr_tx_queue_len=2,
        last_rssi=-42,
        n_packets_recv=1003,
        n_packets_sent=17,
        total_air_time_secs=54,
        total_up_time_secs=3600,
        n_sent_flood=3,
        n_sent_direct=14,
        n_recv_flood=500,
        n_recv_direct=503,
        err_events=0,
        last_snr=32,
        n_direct_dups=7,
        n_flood_dups=11,
    )


async def test_a_status_answer_carries_the_runtime_counters_and_the_rooms_own() -> None:
    """9.2, design D13: the radio is shared, the post counters are not."""
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    server = server_for(alice, lounge, submit=submit, runtime_stats=runtime_stats)
    server.posts_stored = 9
    server.pushes_sent = 4

    packet, _ = request_packet(member=alice, server=lounge, request_type=RequestType.GET_STATUS)
    await server.handle(_packet_for(packet))

    body = parse_status_body(decrypt_response(server, alice, submit.submissions[0].packet))
    assert body.tag == NOW, "the request's timestamp is echoed back as a tag"
    stats = body.stats
    # Shared-radio counters, describing the whole runtime.
    assert stats.n_packets_recv == 1003
    assert stats.n_recv_flood == 500 and stats.n_recv_direct == 503
    assert stats.n_direct_dups == 7 and stats.n_flood_dups == 11
    assert stats.total_up_time_secs == 3600
    # This room's own.
    assert stats.n_posted == 9
    assert stats.n_post_push == 4


async def test_nothing_is_invented_for_what_a_kiss_modem_cannot_measure() -> None:
    """9.3: absence is expressed as the wire format expresses it, and named."""
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    server = server_for(alice, lounge, submit=submit, runtime_stats=runtime_stats)

    packet, _ = request_packet(member=alice, server=lounge, request_type=RequestType.GET_STATUS)
    await server.handle(_packet_for(packet))

    body = parse_status_body(decrypt_response(server, alice, submit.submissions[0].packet))
    for field in KISS_CANNOT_PROVIDE:
        assert getattr(body.stats, field) == 0, (
            f"{field} has no equivalent on a KISS modem and must not be invented"
        )
        assert field in SERVER_STATS_OFFSETS, "the field must still occupy its offset"
    # And the battery *is* filled, because the probe genuinely read it back.
    assert body.stats.batt_milli_volts == 4050


async def test_a_status_answer_with_no_runtime_counters_reports_zeroes_not_guesses() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    server = server_for(alice, lounge, submit=submit)

    packet, _ = request_packet(member=alice, server=lounge, request_type=RequestType.GET_STATUS)
    await server.handle(_packet_for(packet))

    body = parse_status_body(decrypt_response(server, alice, submit.submissions[0].packet))
    assert body.stats.n_packets_recv == 0


# --- 9.4 Telemetry ----------------------------------------------------------


async def test_a_telemetry_answer_carries_what_the_board_reported() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    server = server_for(
        alice,
        lounge,
        submit=submit,
        telemetry=[voltage_entry(4.05), temperature_entry(21.4)],
    )

    packet, _ = request_packet(
        member=alice, server=lounge, request_type=RequestType.GET_TELEMETRY_DATA
    )
    await server.handle(_packet_for(packet))

    body = decrypt_response(server, alice, submit.submissions[0].packet)
    assert body[:4] == NOW.to_bytes(4, "little")
    entries = parse_telemetry_frame(body[4:].rstrip(b"\x00"))
    assert [entry.lpp_type for entry in entries] == [
        LppType.VOLTAGE,
        LppType.TEMPERATURE,
    ]
    assert entries[0].channel == TELEM_CHANNEL_SELF


async def test_a_board_that_answered_nothing_produces_an_empty_frame() -> None:
    """9.4, §4.1: a value the board did not give is absent, never defaulted."""
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    server = server_for(alice, lounge, submit=submit, telemetry=[])

    packet, _ = request_packet(
        member=alice, server=lounge, request_type=RequestType.GET_TELEMETRY_DATA
    )
    await server.handle(_packet_for(packet))

    body = decrypt_response(server, alice, submit.submissions[0].packet)
    assert body[:4] == NOW.to_bytes(4, "little")
    assert body[4:].rstrip(b"\x00") == b"", "a placeholder reading was invented"


async def test_a_guest_receives_the_same_base_frame() -> None:
    """9.4, design D14: the request's mask gates external sensors, and we have none."""
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    server = server_for(
        alice,
        lounge,
        submit=submit,
        permission=Permission.GUEST,
        telemetry=[voltage_entry(4.05)],
    )

    packet, _ = request_packet(
        member=alice,
        server=lounge,
        request_type=RequestType.GET_TELEMETRY_DATA,
        # The inverse permission mask a client sends; there is nothing to gate.
        arguments=bytes([0xFF, 0, 0, 0]),
    )
    await server.handle(_packet_for(packet))

    body = decrypt_response(server, alice, submit.submissions[0].packet)
    entries = parse_telemetry_frame(body[4:].rstrip(b"\x00"))
    assert entries[0].value == 405


# --- 9.5 Unimplemented and unauthorised -------------------------------------


async def test_an_unimplemented_request_type_transmits_nothing_and_is_reported() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    events: list = []
    server = server_for(alice, lounge, submit=submit, events=events)

    packet, _ = request_packet(
        member=alice, server=lounge, request_type=RequestType.GET_ACCESS_LIST
    )
    await server.handle(_packet_for(packet))

    assert submit.submissions == []
    refused = next(event for event in events if isinstance(event, RequestRefused))
    assert refused.reason is RefusalReason.UNSUPPORTED_REQUEST
    assert refused.request_type is RequestType.GET_ACCESS_LIST
    assert refused.member == alice.identity.public_key


async def test_a_request_type_nobody_has_named_is_still_reported_by_its_number() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    events: list = []
    server = server_for(alice, lounge, submit=submit, events=events)

    packet, _ = request_packet(member=alice, server=lounge, request_type=0x7E)
    await server.handle(_packet_for(packet))

    assert submit.submissions == []
    refused = next(event for event in events if isinstance(event, RequestRefused))
    assert refused.request_type == 0x7E


async def test_a_request_from_someone_who_is_not_a_member_is_not_answered() -> None:
    alice, lounge, stranger = Entity("alice"), Entity("lounge"), Entity("stranger")
    submit = RecordingSubmit()
    events: list = []
    server = server_for(alice, lounge, submit=submit, events=events)

    packet, _ = request_packet(member=stranger, server=lounge, request_type=RequestType.GET_STATUS)
    await server.handle(_packet_for(packet))

    assert submit.submissions == []
    refused = next(event for event in events if isinstance(event, RequestRefused))
    assert refused.reason is RefusalReason.NOT_A_MEMBER


# --- Composed before the radio readback -------------------------------------


async def test_a_reply_composed_before_the_readback_waits_for_it() -> None:
    """The room half of the acknowledgement bug: a client that logs in first.

    A reply refused here is indistinguishable, from the client's side, from the
    silence this specification reserves for an unauthorised request.
    """
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    logger = RecordingLogger()
    ready = asyncio.Event()
    server = server_for(alice, lounge, submit=submit, radio=None, radio_ready=ready, logger=logger)

    packet, _ = request_packet(member=alice, server=lounge, request_type=RequestType.KEEP_ALIVE)
    answering = asyncio.create_task(server.handle(_packet_for(packet)))
    for _ in range(10):
        await asyncio.sleep(0)
    assert submit.submissions == [], "the reply was priced before the board answered"

    server.radio = EU868_NARROW
    ready.set()
    await asyncio.wait_for(answering, 5)

    assert len(submit.submissions) == 1, "the reply was lost to a readback that had not arrived"
    assert logger.of("room_reply_not_sent") == []


async def test_a_push_composed_before_the_readback_waits_for_it() -> None:
    """A push dropped at startup loses the delivery and tells the member nothing."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    logger = RecordingLogger()
    ready = asyncio.Event()
    server = server_for(
        alice,
        lounge,
        storage=storage,
        submit=submit,
        radio=None,
        radio_ready=ready,
        logger=logger,
    )
    await storage.messages.store(
        room_id=server.room.id,
        author_public_key=Entity("author").identity.public_key,
        text=b"a post",
        now=NOW - POST_SYNC_DELAY_SECS - 1,
        posted_at=START,
    )

    pushing = asyncio.create_task(server.push_once())
    for _ in range(10):
        await asyncio.sleep(0)
    assert submit.submissions == [], "the push was priced before the board answered"

    server.radio = EU868_NARROW
    ready.set()
    await asyncio.wait_for(pushing, 5)

    assert len(submit.submissions) == 1, "the push was lost to a readback that had not arrived"
    assert logger.of("room_push_not_sent") == []
    assert server.pushes_sent == 1
    # The delivery state a push that waited must reach is the one it would have
    # reached without waiting: an outstanding delivery for that post, awaiting
    # the member's acknowledgement.
    outstanding = server._state[alice.identity.public_key].delivery
    assert outstanding is not None and outstanding.member == alice.identity.public_key


async def test_a_reply_refuses_when_no_readback_ever_comes() -> None:
    """4.2: the refusal §4.1 asks for survives, for a board that answers nothing."""
    alice, lounge = Entity("alice"), Entity("lounge")
    submit = RecordingSubmit()
    logger = RecordingLogger()
    events: list = []
    server = server_for(
        alice,
        lounge,
        submit=submit,
        events=events,
        radio=None,
        radio_ready=asyncio.Event(),
        logger=logger,
    )

    packet, _ = request_packet(member=alice, server=lounge, request_type=RequestType.KEEP_ALIVE)
    with patch("sighop.net.readback.RADIO_READBACK_WAIT_SECONDS", 0.02):
        await asyncio.wait_for(server.handle(_packet_for(packet)), 2)

    assert submit.submissions == []
    refusal = logger.of("room_reply_not_sent")[0]
    assert "waiting" in str(refusal["reason"]), "the refusal does not name the expired wait"
    # 4.3: the counter keeps counting what it counted — the timeout is a
    # `NoRadioReadback`, so it lands in the same bucket the absent board does.
    assert server.throttle.refusals[str(RefusalReason.NO_RADIO_READBACK)] == 1


async def test_a_push_refuses_when_no_readback_ever_comes() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    logger = RecordingLogger()
    server = server_for(
        alice,
        lounge,
        storage=storage,
        submit=submit,
        radio=None,
        radio_ready=asyncio.Event(),
        logger=logger,
    )
    await storage.messages.store(
        room_id=server.room.id,
        author_public_key=Entity("author").identity.public_key,
        text=b"a post",
        now=NOW - POST_SYNC_DELAY_SECS - 1,
        posted_at=START,
    )

    with patch("sighop.net.readback.RADIO_READBACK_WAIT_SECONDS", 0.02):
        await asyncio.wait_for(server.push_once(), 2)

    assert submit.submissions == []
    assert "waiting" in str(logger.of("room_push_not_sent")[0]["reason"])
    # Design D6's rule, unchanged by the wait: nothing that depends on delivery
    # advances for a push that was never submitted.
    assert server._state[alice.identity.public_key].delivery is None


def test_the_post_ack_window_degrades_rather_than_waits() -> None:
    """3.5: a different decision from the four sites, and a correct one.

    This estimates *someone else's* acknowledgement window rather than pricing a
    transmission of ours, so §4.1 does not apply to it: there is nothing here to
    refuse, and waiting would hold a post's acknowledgement behind a board whose
    parameters the estimate does not even need.
    """
    alice, lounge = Entity("alice"), Entity("lounge")
    ready = asyncio.Event()
    server = server_for(alice, lounge, radio=None, radio_ready=ready)
    packet, _ = request_packet(member=alice, server=lounge, request_type=RequestType.KEEP_ALIVE)

    assert not inspect.iscoroutinefunction(server._post_ack_window), (
        "the window estimate became a coroutine; it can now wait, and D6 says it must not"
    )
    window = server._post_ack_window(_packet_for(packet))

    assert window > 0, "no window was estimated for a board that had not answered"
    assert not ready.is_set(), "the estimate touched the readiness signal"


# --- 10.1 / 10.2 Retention --------------------------------------------------


async def _seed(server: RoomServer, storage: MemoryStorage, count: int, *, days_old: int = 0):
    author = Entity("author").identity.public_key
    for index in range(count):
        await storage.messages.store(
            room_id=server.room.id,
            author_public_key=author,
            text=b"x",
            now=NOW - 1000 + index,
            posted_at=START - dt.timedelta(days=days_old),
        )


async def test_a_room_with_no_policy_deletes_nothing() -> None:
    """10.1, design D15: unlimited is the state a room ships in."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    server = server_for(alice, lounge, storage=storage)
    await _seed(server, storage, 10, days_old=400)

    assert await server.prune_once() == (0, 0)
    assert len(storage.messages.posts) == 10
    assert server.room.retention == "unlimited"


async def test_an_age_bound_removes_what_is_older_than_it() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    server = server_for(alice, lounge, storage=storage, retention_days=7)
    await _seed(server, storage, 3, days_old=30)
    await _seed(server, storage, 2, days_old=0)

    deleted, _unsynced = await server.prune_once()

    assert deleted == 3
    assert len(storage.messages.posts) == 2


async def test_a_count_bound_keeps_the_newest() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    server = server_for(alice, lounge, storage=storage, retention_messages=2)
    await _seed(server, storage, 5)

    deleted, _unsynced = await server.prune_once()

    assert deleted == 3
    stamps = sorted(post.post_timestamp for post in storage.messages.posts)
    assert len(stamps) == 2
    assert stamps == sorted(stamps)[-2:]


async def test_both_bounds_together_apply_both() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    server = server_for(alice, lounge, storage=storage, retention_days=7, retention_messages=3)
    await _seed(server, storage, 2, days_old=30)
    await _seed(server, storage, 5, days_old=0)

    deleted, _unsynced = await server.prune_once()

    assert deleted == 4, "two by age and two more by count"
    assert len(storage.messages.posts) == 3


async def test_retention_that_outran_a_members_cursor_is_counted_and_reported() -> None:
    """10.2, design D15: retention wins over sync, and the operator sees it."""
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    events: list = []
    server = server_for(
        alice, lounge, storage=storage, events=events, retention_messages=1, sync_since=0
    )
    await _seed(server, storage, 4)

    deleted, unsynced = await server.prune_once()

    assert deleted == 3
    assert unsynced == 3, "the member had received none of them"
    pruned = next(event for event in events if isinstance(event, RetentionPruned))
    assert pruned.deleted_unsynced == 3
    assert server.as_json()["messages_pruned_unsynced"] == 3


async def test_the_pruner_runs_over_every_room_and_reports_totals() -> None:
    alice, lounge = Entity("alice"), Entity("lounge")
    storage = MemoryStorage()
    server = server_for(alice, lounge, storage=storage, retention_messages=1)
    await _seed(server, storage, 3)

    pruner = RoomRetentionPruner(rooms=[server], logger=RecordingLogger())
    deleted = await pruner.prune_once()

    assert deleted == 2
    assert pruner.as_json() == {
        "retention_passes": 1,
        "retention_deleted": 2,
        "retention_deleted_unsynced": 2,
    }
