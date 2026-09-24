"""Room login and the ACL (milestone 6, `room-acl`, `room-server`).

The property most of these are really about is the *negative* one §7 states and
design D8 builds on: **an unauthenticated stranger cannot make sighop
transmit**. It is easy to hold today and easy to lose the first time somebody
adds a helpful error reply, so every branch that must be silent asserts on the
scheduler having received nothing at all, not on a flag.

Loopback, with the usual caveat: this proves the pieces compose. The live
exercise in section 14 is what confirms a stock client agrees.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from sighop.net.acks import AckRegistry
from sighop.net.paths import PathStore
from sighop.net.room import (
    LoginAdmitted,
    LoginRefused,
    RefusalReason,
    ReplyThrottle,
    RoomServer,
)
from sighop.passwords import PasswordHasher
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
    AnonRequestEnvelope,
    ClientKind,
    DirectEnvelope,
    Permission,
    RoomLoginBody,
    WireText,
    build_anon_request,
    build_room_login_body,
    parse_direct_envelope,
    parse_returned_path_body,
    parse_room_login_response_body,
)
from sighop.radio.modem import EU868_NARROW
from tests.roomfixtures import (
    ADMIN_PASSWORD,
    GUEST_PASSWORD,
    MemoryStorage,
    member_record,
    room_record,
)
from tests.test_dm import Entity, RecordingSubmit, TickingClock, _packet_for, zero_hop_route_to
from tests.test_tx import RecordingLogger

START = dt.datetime(2026, 9, 5, 21, 0, tzinfo=dt.UTC)


def login_packet(
    *,
    client: Entity,
    server: Entity,
    password: str = ADMIN_PASSWORD,
    timestamp: int = 1_700_000_000,
    sync_timestamp: int = 0,
    route_type: RouteType = RouteType.DIRECT,
    path: bytes = b"",
    secret: bytes | None = None,
) -> bytes:
    """An `ANON_REQ` login, built the way a stock client builds one."""
    secret = secret or SharedSecretCache().get(client.identity, server.identity.public_key)
    plaintext = build_room_login_body(
        RoomLoginBody(
            timestamp=timestamp,
            sync_timestamp=sync_timestamp,
            password=WireText.from_bytes(password.encode()),
        )
    )
    mac, ciphertext = encrypt_then_mac(secret, plaintext)
    envelope = AnonRequestEnvelope(
        dest_hash=server.node_hash,
        sender_public_key=client.identity.public_key,
        mac=mac,
        ciphertext=ciphertext,
    )
    return encode_packet(
        Packet(
            header=PacketHeader(route_type, PayloadType.ANON_REQ, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=len(path),
            hash_size=1,
            path=path,
            payload=build_anon_request(envelope),
        )
    )


def server_for(
    entity: Entity,
    *,
    storage: MemoryStorage | None = None,
    paths: PathStore | None = None,
    submit: RecordingSubmit | None = None,
    events: list | None = None,
    clock: TickingClock | None = None,
    throttle: ReplyThrottle | None = None,
    members: list | None = None,
    **room_kwargs: Any,
) -> RoomServer:
    room = room_record(entity_id=None, **room_kwargs)
    return RoomServer(
        entity=entity,
        room=room,
        storage=storage or MemoryStorage(),
        paths=paths or PathStore(),
        submit=submit or RecordingSubmit(),
        acks=AckRegistry(logger=RecordingLogger()),
        members=members or [],
        hasher=PasswordHasher(),
        throttle=throttle,
        clock=clock or TickingClock(START),
        radio=EU868_NARROW,
        on_event=events.append if events is not None else None,
        logger=RecordingLogger(),
    )


# --- 6.1 A subscriber, and nothing in the decode stage ----------------------


async def test_the_room_server_attaches_to_the_bus_under_its_own_name() -> None:
    from sighop.net.bus import NetworkBus

    bus = NetworkBus(logger=RecordingLogger())
    server = server_for(Entity("lounge"), name="lounge")

    subscription = server.subscribe(bus)

    assert subscription.name == "room:lounge"
    assert [stats.name for stats in bus.subscriber_stats] == ["room:lounge"]


async def test_a_login_for_another_node_hash_is_ignored() -> None:
    lounge, other = Entity("lounge"), Entity("other")
    while other.node_hash == lounge.node_hash:  # pragma: no cover
        other = Entity("other")
    submit = RecordingSubmit()
    events: list = []
    server = server_for(lounge, submit=submit, events=events)

    await server.handle(_packet_for(login_packet(client=Entity("client"), server=other)))

    assert events == []
    assert submit.submissions == []


# --- 6.2 Reading the login ---------------------------------------------------


async def test_a_login_is_read_with_the_key_the_envelope_itself_carries() -> None:
    """6.2: an `ANON_REQ` is how a stranger introduces itself.

    Deriving the secret from a stored contact would make a first login
    impossible, which is the whole point of the payload type.
    """
    lounge, client = Entity("lounge"), Entity("client")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = server_for(lounge, storage=storage, submit=submit, events=events)

    await server.handle(
        _packet_for(
            login_packet(client=client, server=lounge, password=ADMIN_PASSWORD, sync_timestamp=42)
        )
    )

    admitted = next(event for event in events if isinstance(event, LoginAdmitted))
    assert admitted.public_key == client.identity.public_key
    assert admitted.permission is Permission.ADMIN
    assert admitted.new_member is True
    member = storage.members.of(server.room.id)[0]
    assert member.sync_since == 42, "the member named its own starting position"
    assert member.last_timestamp == 1_700_000_000


async def test_a_login_whose_mac_fails_creates_no_member_and_transmits_nothing() -> None:
    lounge, client, stranger = Entity("lounge"), Entity("client"), Entity("stranger")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = server_for(lounge, storage=storage, submit=submit, events=events)

    # Encrypted under a secret the envelope's own public key does not derive.
    wrong = SharedSecretCache().get(stranger.identity, lounge.identity.public_key)
    await server.handle(_packet_for(login_packet(client=client, server=lounge, secret=wrong)))

    assert storage.members.rows == {}
    assert submit.submissions == []
    refused = next(event for event in events if isinstance(event, LoginRefused))
    assert refused.reason is RefusalReason.MAC_FAILED


# --- 6.3 The firmware's order -----------------------------------------------


@pytest.mark.parametrize(
    ("password", "expected"),
    [
        (ADMIN_PASSWORD, Permission.ADMIN),
        (GUEST_PASSWORD, Permission.READ_WRITE),
    ],
)
async def test_each_password_earns_the_level_the_firmware_gives_it(
    password: str, expected: Permission
) -> None:
    lounge, client = Entity("lounge"), Entity("client")
    events: list = []
    server = server_for(lounge, events=events)

    await server.handle(_packet_for(login_packet(client=client, server=lounge, password=password)))

    admitted = next(event for event in events if isinstance(event, LoginAdmitted))
    assert admitted.permission is expected


async def test_a_wrong_password_with_read_only_allowed_is_admitted_as_a_spectator() -> None:
    """6.3: `PERM_ACL_GUEST`, which is zero and may not post (`MyMesh.cpp:350`)."""
    lounge, client = Entity("lounge"), Entity("client")
    submit = RecordingSubmit()
    events: list = []
    server = server_for(lounge, submit=submit, events=events, allow_read_only=True)

    await server.handle(_packet_for(login_packet(client=client, server=lounge, password="wrong")))

    admitted = next(event for event in events if isinstance(event, LoginAdmitted))
    assert admitted.permission is Permission.GUEST
    assert admitted.permission.may_post is False
    assert len(submit.submissions) == 1, "a spectator is still answered"


async def test_a_wrong_password_with_read_only_disallowed_transmits_nothing() -> None:
    """6.3, §7: the branch that makes "a stranger cannot make us transmit" true."""
    lounge, client = Entity("lounge"), Entity("client")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    server = server_for(lounge, storage=storage, submit=submit, events=events)

    await server.handle(_packet_for(login_packet(client=client, server=lounge, password="wrong")))

    assert submit.submissions == [], "a failed login produced a transmission"
    assert storage.members.rows == {}
    refused = next(event for event in events if isinstance(event, LoginRefused))
    assert refused.reason is RefusalReason.BAD_PASSWORD
    assert server.throttle.refusals["bad_password"] == 1


async def test_the_admin_password_is_checked_before_the_guest_one() -> None:
    lounge, client = Entity("lounge"), Entity("client")
    events: list = []
    server = server_for(lounge, events=events, admin_password="same", guest_password="same")

    await server.handle(_packet_for(login_packet(client=client, server=lounge, password="same")))

    admitted = next(event for event in events if isinstance(event, LoginAdmitted))
    assert admitted.permission is Permission.ADMIN


# --- 6.4 The member row -----------------------------------------------------


async def test_a_successful_login_writes_the_member_through() -> None:
    lounge, client = Entity("lounge"), Entity("client")
    storage = MemoryStorage()
    server = server_for(lounge, storage=storage)

    await server.handle(_packet_for(login_packet(client=client, server=lounge)))

    stored = storage.members.of(server.room.id)
    assert len(stored) == 1
    assert stored[0].public_key == client.identity.public_key
    assert stored[0].node_hash == client.identity.public_key[0]
    assert stored[0].permission is Permission.ADMIN
    # And in memory, which is what the per-packet lookup reads (design D5).
    assert client.identity.public_key in server.members


async def test_a_second_login_updates_rather_than_duplicates() -> None:
    lounge, client = Entity("lounge"), Entity("client")
    storage = MemoryStorage()
    server = server_for(lounge, storage=storage)

    await server.handle(
        _packet_for(login_packet(client=client, server=lounge, timestamp=1_700_000_000))
    )
    await server.handle(
        _packet_for(login_packet(client=client, server=lounge, timestamp=1_700_000_100))
    )

    assert len(storage.members.of(server.room.id)) == 1
    assert storage.members.of(server.room.id)[0].last_timestamp == 1_700_000_100
    assert server.logins_admitted == 2


# --- 6.5 The replay guard ---------------------------------------------------


async def test_a_replayed_login_is_refused_and_transmits_nothing() -> None:
    lounge, client = Entity("lounge"), Entity("client")
    submit = RecordingSubmit()
    events: list = []
    server = server_for(lounge, submit=submit, events=events)

    packet = login_packet(client=client, server=lounge, timestamp=1_700_000_000)
    await server.handle(_packet_for(packet))
    submitted_after_first = len(submit.submissions)
    await server.handle(_packet_for(packet))

    assert len(submit.submissions) == submitted_after_first, "a replay was answered"
    refused = next(event for event in events if isinstance(event, LoginRefused))
    assert refused.reason is RefusalReason.REPLAY
    assert "revok" in refused.detail, "the reason must name the way out"


async def test_the_replay_guard_survives_a_restart() -> None:
    """6.5, design D9: the point of a durable ACL.

    The firmware's copy is transient and clears on reboot, which reopens the
    replay window at every restart. Ours is restored from the row.
    """
    lounge, client = Entity("lounge"), Entity("client")
    storage = MemoryStorage()
    first = server_for(lounge, storage=storage)
    packet = login_packet(client=client, server=lounge, timestamp=1_700_000_000)
    await first.handle(_packet_for(packet))

    # A new process: the same rows, none of the memory.
    submit = RecordingSubmit()
    events: list = []
    restarted = RoomServer(
        entity=lounge,
        room=first.room,
        storage=storage,
        paths=PathStore(),
        submit=submit,
        acks=AckRegistry(logger=RecordingLogger()),
        members=storage.members.of(first.room.id),
        clock=TickingClock(START),
        radio=EU868_NARROW,
        on_event=events.append,
        logger=RecordingLogger(),
    )
    await restarted.handle(_packet_for(packet))

    assert submit.submissions == []
    refused = next(event for event in events if isinstance(event, LoginRefused))
    assert refused.reason is RefusalReason.REPLAY


async def test_an_existing_member_with_an_empty_password_is_answered() -> None:
    """6.5, design D9: `MyMesh.cpp:335-342` short-circuits before both checks.

    Matched on purpose. It is how a client re-establishes a lost path, a client
    that cannot re-establish one has silently left the room, and diverging would
    break interop with every stock client. The throttle is what keeps it from
    being free.
    """
    lounge, client = Entity("lounge"), Entity("client")
    storage = MemoryStorage()
    submit = RecordingSubmit()
    events: list = []
    room = room_record(name="lounge")
    existing = member_record(
        room,
        client.identity.public_key,
        permission=Permission.READ_WRITE,
        last_timestamp=1_700_000_500,
    )
    server = RoomServer(
        entity=lounge,
        room=room,
        storage=storage,
        paths=PathStore(),
        submit=submit,
        acks=AckRegistry(logger=RecordingLogger()),
        members=[existing],
        clock=TickingClock(START),
        radio=EU868_NARROW,
        on_event=events.append,
        logger=RecordingLogger(),
    )

    # A timestamp *below* the recorded one, which would be refused as a replay
    # for any non-empty password.
    await server.handle(
        _packet_for(login_packet(client=client, server=lounge, password="", timestamp=1))
    )

    admitted = next(event for event in events if isinstance(event, LoginAdmitted))
    assert admitted.permission is Permission.READ_WRITE
    assert admitted.new_member is False
    assert len(submit.submissions) == 1


def _server_with_member(
    lounge: Entity, client: Entity, storage: MemoryStorage, logger: RecordingLogger
) -> RoomServer:
    room = room_record(name="lounge")
    existing = member_record(
        room,
        client.identity.public_key,
        permission=Permission.READ_WRITE,
        last_timestamp=1_700_000_500,
    )
    return RoomServer(
        entity=lounge,
        room=room,
        storage=storage,
        paths=PathStore(),
        submit=RecordingSubmit(),
        acks=AckRegistry(logger=RecordingLogger()),
        members=[existing],
        hasher=PasswordHasher(),
        clock=TickingClock(START),
        radio=EU868_NARROW,
        logger=logger,
    )


async def test_a_blank_password_relogin_leaves_the_recorded_timestamp_alone() -> None:
    """Design D4, `MyMesh.cpp:335-376`: the short-circuit skips the write too."""
    lounge, client = Entity("lounge"), Entity("client")
    storage = MemoryStorage()
    logger = RecordingLogger()
    server = _server_with_member(lounge, client, storage, logger)

    await server.handle(
        _packet_for(
            login_packet(client=client, server=lounge, password="", timestamp=1_700_000_515)
        )
    )

    assert server.members[client.identity.public_key].last_timestamp == 1_700_000_500
    assert storage.members.of(server.room.id)[0].last_timestamp == 1_700_000_500
    assert logger.of("room_login_admitted")[0]["empty_password"] is True


async def test_a_password_login_raises_the_recorded_timestamp() -> None:
    lounge, client = Entity("lounge"), Entity("client")
    storage = MemoryStorage()
    logger = RecordingLogger()
    server = _server_with_member(lounge, client, storage, logger)

    await server.handle(
        _packet_for(login_packet(client=client, server=lounge, timestamp=1_700_000_515))
    )

    assert server.members[client.identity.public_key].last_timestamp == 1_700_000_515
    assert storage.members.of(server.room.id)[0].last_timestamp == 1_700_000_515
    assert logger.of("room_login_admitted")[0]["empty_password"] is False


async def test_an_unknown_sender_with_an_empty_password_gets_no_shortcut() -> None:
    """The short-circuit is for *existing* members; a stranger takes the long way."""
    lounge, client = Entity("lounge"), Entity("client")
    submit = RecordingSubmit()
    events: list = []
    server = server_for(lounge, submit=submit, events=events)

    await server.handle(_packet_for(login_packet(client=client, server=lounge, password="")))

    assert submit.submissions == []
    refused = next(event for event in events if isinstance(event, LoginRefused))
    assert refused.reason is RefusalReason.BAD_PASSWORD


# --- 6.6 The reply's shape --------------------------------------------------


def _decrypt_reply(server: RoomServer, client: Entity, packet: bytes) -> tuple[PayloadType, bytes]:
    from sighop.protocol.crypto import mac_then_decrypt
    from sighop.protocol.packet import decode as decode_packet

    decoded = decode_packet(packet)
    assert not isinstance(decoded, type(None))
    envelope = parse_direct_envelope(decoded.payload_type, decoded.payload)
    assert isinstance(envelope, DirectEnvelope)
    secret = SharedSecretCache().get(client.identity, server.entity.identity.public_key)
    _candidate, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
    assert plaintext is not None
    return envelope.payload_type, plaintext


async def test_a_flooded_login_is_answered_with_a_path_return_carrying_the_response() -> None:
    """6.6, design D7: the client learns a route home in the same packet."""
    lounge, client = Entity("lounge"), Entity("client")
    submit = RecordingSubmit()
    server = server_for(lounge, submit=submit)

    await server.handle(
        _packet_for(
            login_packet(
                client=client,
                server=lounge,
                route_type=RouteType.FLOOD,
                path=b"\xab\xcd",
            )
        )
    )

    assert len(submit.submissions) == 1
    packet = submit.submissions[0].packet
    payload_type, plaintext = _decrypt_reply(server, client, packet)
    assert payload_type is PayloadType.PATH

    body = parse_returned_path_body(plaintext)
    assert not isinstance(body, type(None))
    assert body.path == b"\xab\xcd", "the route the request took, returned"
    assert body.extra_type is PayloadType.RESPONSE
    response = parse_room_login_response_body(body.extra_raw)
    assert response.result == 0
    assert response.permissions == int(Permission.ADMIN)
    assert response.client_kind is ClientKind.ADMIN

    from sighop.protocol.packet import decode as decode_packet

    decoded = decode_packet(packet)
    assert decoded.route_type is RouteType.FLOOD
    assert decoded.hash_size == 3, "the default width for a flood we originate"
    assert (decoded.hop_count, decoded.path) == (0, b"")


@pytest.mark.parametrize("size", [1, 2, 3])
async def test_a_path_return_to_a_member_with_no_route_floods_at_the_configured_width(
    size: int,
) -> None:
    """The request came in at 1-byte hashes; our flood home uses our own width."""
    from sighop.protocol.packet import decode as decode_packet

    lounge, client = Entity("lounge"), Entity("client")
    submit = RecordingSubmit()
    server = server_for(lounge, submit=submit)
    server.path_hash_size = size

    await server.handle(
        _packet_for(
            login_packet(client=client, server=lounge, route_type=RouteType.FLOOD, path=b"\xab")
        )
    )

    decoded = decode_packet(submit.submissions[0].packet)
    assert decoded.route_type is RouteType.FLOOD
    assert (decoded.hash_size, decoded.hop_count, decoded.path) == (size, 0, b"")
    _, plaintext = _decrypt_reply(server, client, submit.submissions[0].packet)
    body = parse_returned_path_body(plaintext)
    assert (body.hash_size, body.path) == (1, b"\xab"), "the returned path keeps its width"


async def test_a_direct_login_is_answered_with_a_response_along_the_known_route() -> None:
    lounge, client = Entity("lounge"), Entity("client")
    paths = PathStore()
    zero_hop_route_to(paths, client.identity.public_key, at=START)
    submit = RecordingSubmit()
    server = server_for(lounge, submit=submit, paths=paths)

    await server.handle(
        _packet_for(login_packet(client=client, server=lounge, route_type=RouteType.DIRECT))
    )

    packet = submit.submissions[0].packet
    payload_type, plaintext = _decrypt_reply(server, client, packet)
    assert payload_type is PayloadType.RESPONSE
    response = parse_room_login_response_body(plaintext)
    assert response.permissions == int(Permission.ADMIN)

    from sighop.protocol.packet import decode as decode_packet

    decoded = decode_packet(packet)
    assert decoded.route_type is RouteType.DIRECT


async def test_a_spectator_is_identified_as_one_in_the_response() -> None:
    lounge, client = Entity("lounge"), Entity("client")
    submit = RecordingSubmit()
    server = server_for(lounge, submit=submit, allow_read_only=True)

    await server.handle(_packet_for(login_packet(client=client, server=lounge, password="nope")))

    _type, plaintext = _decrypt_reply(server, client, submit.submissions[0].packet)
    response = parse_room_login_response_body(plaintext)
    assert response.client_kind is ClientKind.SPECTATOR
    assert response.permissions == int(Permission.GUEST)


# --- 6.7 The throttle -------------------------------------------------------


async def test_a_burst_from_one_source_is_answered_only_to_its_limit() -> None:
    lounge = Entity("lounge")
    submit = RecordingSubmit()
    events: list = []
    throttle = ReplyThrottle(source_limit=2, global_limit=100)
    server = server_for(lounge, submit=submit, events=events, throttle=throttle)

    client = Entity("client")
    for index in range(5):
        await server.handle(
            _packet_for(login_packet(client=client, server=lounge, timestamp=1_700_000_000 + index))
        )

    assert len(submit.submissions) == 2
    assert throttle.refusals["throttled_source"] == 3
    assert any(
        isinstance(event, LoginRefused) and event.reason is RefusalReason.THROTTLED_SOURCE
        for event in events
    )


async def test_a_burst_from_many_sources_is_bounded_in_total() -> None:
    """6.7, design D8: a room server must not be usable as a flood amplifier."""
    lounge = Entity("lounge")
    submit = RecordingSubmit()
    throttle = ReplyThrottle(source_limit=10, global_limit=3)
    server = server_for(lounge, submit=submit, throttle=throttle)

    for index in range(8):
        await server.handle(
            _packet_for(
                login_packet(
                    client=Entity(f"client{index}"),
                    server=lounge,
                    timestamp=1_700_000_000 + index,
                )
            )
        )

    assert len(submit.submissions) == 3
    assert throttle.refusals["throttled_global"] == 5


async def test_a_source_over_its_own_limit_does_not_consume_the_global_budget() -> None:
    """One noisy peer must not become an outage for everybody else."""
    lounge = Entity("lounge")
    submit = RecordingSubmit()
    throttle = ReplyThrottle(source_limit=1, global_limit=4)
    server = server_for(lounge, submit=submit, throttle=throttle)

    noisy = Entity("noisy")
    for index in range(5):
        await server.handle(
            _packet_for(login_packet(client=noisy, server=lounge, timestamp=1_700_000_000 + index))
        )
    quiet = Entity("quiet")
    await server.handle(_packet_for(login_packet(client=quiet, server=lounge)))

    assert len(submit.submissions) == 2, "the quiet peer was starved by the noisy one"


def test_every_refusal_reason_is_exposed_for_the_status_line() -> None:
    """6.7: a throttle that drops silently looks like a mesh that went quiet."""
    throttle = ReplyThrottle()
    throttle.refuse(RefusalReason.BAD_PASSWORD)
    throttle.refuse(RefusalReason.BAD_PASSWORD)
    throttle.refuse(RefusalReason.REPLAY)

    assert throttle.as_json() == {
        "replies_refused": {"bad_password": 2, "replay": 1},
        "replies_refused_total": 3,
    }


# --- 6.8 Membership across a restart ----------------------------------------


async def test_a_member_that_logged_in_before_a_restart_is_recognised_after_it() -> None:
    """6.8, §7: "ACLs must survive restart or every member re-authenticates"."""
    lounge, client = Entity("lounge"), Entity("client")
    storage = MemoryStorage()
    first = server_for(lounge, storage=storage)
    await first.handle(_packet_for(login_packet(client=client, server=lounge)))

    restarted = RoomServer(
        entity=lounge,
        room=first.room,
        storage=storage,
        paths=PathStore(),
        submit=RecordingSubmit(),
        acks=AckRegistry(logger=RecordingLogger()),
        members=storage.members.of(first.room.id),
        clock=TickingClock(START),
        radio=EU868_NARROW,
        logger=RecordingLogger(),
    )

    assert client.identity.public_key in restarted.members
    assert restarted.members[client.identity.public_key].permission is Permission.ADMIN


# --- 6.9 Revocation ---------------------------------------------------------


async def test_a_revoked_member_is_unknown_and_returns_only_by_logging_in() -> None:
    lounge, client = Entity("lounge"), Entity("client")
    storage = MemoryStorage()
    server = server_for(lounge, storage=storage)
    await server.handle(
        _packet_for(login_packet(client=client, server=lounge, timestamp=1_700_000_000))
    )

    assert await server.revoke(client.identity.public_key) is True

    assert client.identity.public_key not in server.members
    assert storage.members.of(server.room.id) == []

    # And the replay guard went with it, so the same timestamp is admitted again
    # — which is exactly what `sighop room revoke` promises a stuck client.
    await server.handle(
        _packet_for(login_packet(client=client, server=lounge, timestamp=1_700_000_000))
    )
    assert client.identity.public_key in server.members


async def test_revoking_somebody_who_is_not_a_member_says_so() -> None:
    server = server_for(Entity("lounge"))
    assert await server.revoke(b"\x00" * 32) is False
