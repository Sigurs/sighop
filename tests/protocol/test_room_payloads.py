"""The bodies a room server puts on the wire (milestone 6, `payload-codec`).

Each of these is a byte layout taken from the firmware rather than from
`payloads.md`, and each is tested against a hand-built vector as well as by
round trip: a round trip proves the codec agrees with itself, which is exactly
the property a wrong layout also has.

Nothing here learns about rooms, members or storage — that boundary is asserted
by `tests/protocol/test_import_boundary.py`, not restated here.
"""

from __future__ import annotations

import pytest

from sighop.protocol.packet import PayloadType
from sighop.protocol.payloads import (
    FIRMWARE_VER_LEVEL,
    RESP_SERVER_LOGIN_OK,
    ROOM_LOGIN_RESPONSE_SIZE,
    ROOM_SERVER_DIVERGENT_OFFSETS,
    SERVER_STATS_OFFSETS,
    SERVER_STATS_SIZE,
    STATUS_BODY_SIZE,
    TELEM_CHANNEL_SELF,
    Acknowledgement,
    ClientKind,
    LppType,
    Permission,
    RequestBody,
    RequestType,
    ReturnedPathBody,
    RoomLoginResponseBody,
    ServerStats,
    StatusBody,
    TelemetryEntry,
    build_ack,
    build_request_body,
    build_returned_path_body,
    build_room_login_response_body,
    build_status_body,
    build_telemetry_frame,
    parse_request_body,
    parse_returned_path_body,
    parse_room_login_response_body,
    parse_status_body,
    parse_telemetry_frame,
    temperature_entry,
    voltage_entry,
)
from sighop.protocol.result import DecodeFailure, EncodeError, FailureReason

# --- 3.1 The login response -------------------------------------------------


def test_a_login_response_matches_the_bytes_the_firmware_writes() -> None:
    """3.1: hand-built from `MyMesh.cpp:382-391`, field by field.

    An administrator logging in at server time 0x11223344, whose permission byte
    is `PERM_ACL_ADMIN`, with a known blob standing in for the random one.
    """
    built = build_room_login_response_body(
        RoomLoginResponseBody(
            server_timestamp=0x11223344,
            result=RESP_SERVER_LOGIN_OK,
            keep_alive_interval=0,
            client_kind=ClientKind.ADMIN,
            permissions=int(Permission.ADMIN),
            blob=bytes([0xDE, 0xAD, 0xBE, 0xEF]),
            protocol_level=FIRMWARE_VER_LEVEL,
        )
    )
    assert built == bytes(
        [
            0x44,
            0x33,
            0x22,
            0x11,  # server timestamp, little-endian
            0x00,  # RESP_SERVER_LOGIN_OK
            0x00,  # legacy keep-alive interval
            0x01,  # client kind: administrator
            0x03,  # permissions
            0xDE,
            0xAD,
            0xBE,
            0xEF,  # blob, for packet-hash uniqueness
            0x01,  # FIRMWARE_VER_LEVEL
        ]
    )
    assert len(built) == ROOM_LOGIN_RESPONSE_SIZE == 13


def test_a_login_response_round_trips() -> None:
    response = RoomLoginResponseBody(
        server_timestamp=1_700_000_000,
        client_kind=ClientKind.SPECTATOR,
        permissions=int(Permission.GUEST),
        blob=b"\x01\x02\x03\x04",
    )
    assert parse_room_login_response_body(build_room_login_response_body(response)) == response


def test_a_short_login_response_is_truncated_rather_than_half_read() -> None:
    failure = parse_room_login_response_body(b"\x00" * 12)
    assert isinstance(failure, DecodeFailure)
    assert failure.reason is FailureReason.TRUNCATED


def test_the_client_kind_follows_the_permission_byte() -> None:
    """`MyMesh.cpp:387`: derived from the permissions, never independent of them."""
    assert ClientKind.for_permissions(int(Permission.ADMIN)) is ClientKind.ADMIN
    # Zero is the read-only spectator, which is `PERM_ACL_GUEST`.
    assert ClientKind.for_permissions(int(Permission.GUEST)) is ClientKind.SPECTATOR
    assert ClientKind.for_permissions(int(Permission.READ_WRITE)) is ClientKind.MEMBER


def test_a_login_response_blob_that_is_not_four_bytes_is_refused() -> None:
    with pytest.raises(EncodeError):
        build_room_login_response_body(
            RoomLoginResponseBody(server_timestamp=1, blob=b"\x00\x01")
        )


# --- 3.2 Request bodies -----------------------------------------------------


def test_a_keep_alive_carrying_a_position_yields_it() -> None:
    body = build_request_body(
        RequestBody(
            timestamp=1_700_000_000,
            request_type=RequestType.KEEP_ALIVE,
            arguments=(1_699_999_000).to_bytes(4, "little"),
        )
    )
    parsed = parse_request_body(body)
    assert not isinstance(parsed, DecodeFailure)
    assert parsed.timestamp == 1_700_000_000
    assert parsed.request_type is RequestType.KEEP_ALIVE
    assert parsed.keep_alive_since == 1_699_999_000


def test_a_keep_alive_without_a_position_reads_none_rather_than_past_the_body() -> None:
    parsed = parse_request_body(
        build_request_body(RequestBody(timestamp=5, request_type=RequestType.KEEP_ALIVE))
    )
    assert not isinstance(parsed, DecodeFailure)
    assert parsed.arguments == b""
    assert parsed.keep_alive_since is None


def test_a_zero_position_is_not_a_position_because_it_may_be_padding() -> None:
    """`MyMesh.cpp:560`: "this may be 0, if part of decrypted PADDING"."""
    parsed = parse_request_body(
        build_request_body(
            RequestBody(
                timestamp=5,
                request_type=RequestType.KEEP_ALIVE,
                arguments=b"\x00\x00\x00\x00",
            )
        )
    )
    assert not isinstance(parsed, DecodeFailure)
    assert parsed.keep_alive_since is None


def test_an_uninterpreted_request_type_keeps_its_arguments() -> None:
    """The caller reports what it could not act on, rather than a bare type."""
    raw = (7).to_bytes(4, "little") + bytes([0x7F]) + b"\xaa\xbb\xcc"
    parsed = parse_request_body(raw)
    assert not isinstance(parsed, DecodeFailure)
    assert parsed.request_type == 0x7F
    assert parsed.arguments == b"\xaa\xbb\xcc"
    assert parsed.keep_alive_since is None
    assert build_request_body(parsed) == raw


def test_a_request_body_shorter_than_a_type_byte_is_truncated() -> None:
    failure = parse_request_body(b"\x01\x02\x03\x04")
    assert isinstance(failure, DecodeFailure)
    assert failure.reason is FailureReason.TRUNCATED


def test_the_access_list_request_type_is_named_and_not_implemented() -> None:
    """Named so an unimplemented type can be *reported* by name (`room-server`)."""
    assert int(RequestType.GET_ACCESS_LIST) == 0x05


# --- 3.3 / 3.4 The status body ----------------------------------------------


def test_a_status_body_is_four_bytes_of_tag_and_fifty_two_of_struct() -> None:
    built = build_status_body(StatusBody(tag=0x01020304, stats=ServerStats()))
    assert len(built) == STATUS_BODY_SIZE == 56
    assert SERVER_STATS_SIZE == 52
    assert built[:4] == bytes([0x04, 0x03, 0x02, 0x01])


def test_every_field_sits_where_the_struct_puts_it() -> None:
    """3.3: `MyMesh.cpp:24-39` — naturally aligned, no padding, little-endian."""
    assert SERVER_STATS_OFFSETS == {
        "batt_milli_volts": 0,
        "curr_tx_queue_len": 2,
        "noise_floor": 4,
        "last_rssi": 6,
        "n_packets_recv": 8,
        "n_packets_sent": 12,
        "total_air_time_secs": 16,
        "total_up_time_secs": 20,
        "n_sent_flood": 24,
        "n_sent_direct": 28,
        "n_recv_flood": 32,
        "n_recv_direct": 36,
        "err_events": 40,
        "last_snr": 42,
        "n_direct_dups": 44,
        "n_flood_dups": 46,
        "n_posted": 48,
        "n_post_push": 50,
    }
    # And each really lands there, rather than only being described as landing there.
    for name, offset in SERVER_STATS_OFFSETS.items():
        marked = build_status_body(StatusBody(tag=0, stats=ServerStats(**{name: 1})))
        assert marked[4 + offset] == 1, f"{name} is not at offset {offset}"


def test_the_status_body_round_trips_every_field() -> None:
    stats = ServerStats(
        batt_milli_volts=4050,
        curr_tx_queue_len=3,
        noise_floor=-95,
        last_rssi=-40,
        n_packets_recv=1234,
        n_packets_sent=567,
        total_air_time_secs=89,
        total_up_time_secs=10_000,
        n_sent_flood=11,
        n_sent_direct=12,
        n_recv_flood=13,
        n_recv_direct=14,
        err_events=1,
        last_snr=-24,
        n_direct_dups=5,
        n_flood_dups=6,
        n_posted=7,
        n_post_push=8,
    )
    body = StatusBody(tag=42, stats=stats)
    assert parse_status_body(build_status_body(body)) == body


def test_a_value_too_wide_for_its_field_is_refused_rather_than_wrapped() -> None:
    with pytest.raises(EncodeError) as excinfo:
        build_status_body(StatusBody(tag=0, stats=ServerStats(n_posted=70_000)))
    assert "n_posted" in str(excinfo.value)


def test_the_last_four_bytes_mean_two_different_things_and_both_are_recorded() -> None:
    """3.4, design D13: a divergence, not a bug — so it cannot be "fixed".

    At offsets 48..52 of the struct a **room server** writes `n_posted` and
    `n_post_push` (`MyMesh.cpp:175-176`). `meshcore_py`'s generic `parse_status`
    reads the same four bytes as a **repeater**'s `rx_airtime`. We emit the
    room-server form, because interop is with the firmware; a client using the
    generic parser misreads them against a stock room server exactly as it will
    against ours.
    """
    assert ROOM_SERVER_DIVERGENT_OFFSETS == (48, 52)
    assert SERVER_STATS_OFFSETS["n_posted"] == 48
    assert SERVER_STATS_OFFSETS["n_post_push"] == 50

    body = build_status_body(
        StatusBody(tag=0, stats=ServerStats(n_posted=0x1111, n_post_push=0x2222))
    )
    divergent = body[4 + 48 : 4 + 52]
    # The room server's reading:
    assert int.from_bytes(divergent[:2], "little") == 0x1111
    assert int.from_bytes(divergent[2:], "little") == 0x2222
    # The generic client's reading of the very same bytes, named so that anyone
    # who later "corrects" one of them has to delete the other on purpose.
    generic_rx_airtime = int.from_bytes(divergent, "little")
    assert generic_rx_airtime == 0x22221111

    # And the codec's own documentation names both readings. Python discards a
    # string literal that follows an assignment, so the note lives in the file —
    # which is where a reviewer looks, and therefore where this looks too.
    import inspect

    from sighop.protocol import payloads

    source = inspect.getsource(payloads)
    note = source[source.index("ROOM_SERVER_DIVERGENT_OFFSETS = ") :][:1500]
    assert "rx_airtime" in note, "the generic client's reading is not recorded"
    assert "n_posted" in note and "n_post_push" in note


def test_a_short_status_body_is_truncated() -> None:
    failure = parse_status_body(b"\x00" * 55)
    assert isinstance(failure, DecodeFailure)
    assert failure.reason is FailureReason.TRUNCATED


# --- 3.5 Telemetry ----------------------------------------------------------


def test_a_voltage_entry_is_channel_type_and_hundredths_most_significant_first() -> None:
    """3.5, design D14: LPP is the one big-endian encoding in this protocol."""
    frame = build_telemetry_frame([voltage_entry(4.05)])
    assert frame == bytes([TELEM_CHANNEL_SELF, 0x74, 0x01, 0x95])
    assert int.from_bytes(frame[2:], "big") == 405
    # Little-endian would have produced this instead, and would have parsed as
    # 38.13 V on the client.
    assert frame[2:] != (405).to_bytes(2, "little")


def test_a_negative_temperature_is_signed_tenths_most_significant_first() -> None:
    frame = build_telemetry_frame([temperature_entry(-12.5)])
    assert frame == bytes([TELEM_CHANNEL_SELF, 0x67, 0xFF, 0x83])
    assert int.from_bytes(frame[2:], "big", signed=True) == -125


def test_an_empty_frame_is_empty_rather_than_a_placeholder() -> None:
    """A value the board did not report is absent, never defaulted (§4.1)."""
    assert build_telemetry_frame([]) == b""


def test_a_frame_round_trips_both_types() -> None:
    entries = [voltage_entry(3.30), temperature_entry(21.4)]
    parsed = parse_telemetry_frame(build_telemetry_frame(entries))
    assert not isinstance(parsed, DecodeFailure)
    assert parsed == entries
    assert [entry.lpp_type for entry in parsed] == [LppType.VOLTAGE, LppType.TEMPERATURE]


def test_a_truncated_entry_is_reported_rather_than_read_past() -> None:
    failure = parse_telemetry_frame(bytes([TELEM_CHANNEL_SELF, 0x74, 0x01]))
    assert isinstance(failure, DecodeFailure)
    assert failure.reason is FailureReason.TRUNCATED


def test_a_type_this_codec_does_not_emit_is_reported_not_guessed() -> None:
    failure = parse_telemetry_frame(bytes([TELEM_CHANNEL_SELF, 0x02, 0x00, 0x00]))
    assert isinstance(failure, DecodeFailure)
    assert "0x02" in (failure.detail or "")


def test_a_voltage_beyond_the_field_is_refused_rather_than_wrapped() -> None:
    with pytest.raises(EncodeError):
        build_telemetry_frame([TelemetryEntry(1, LppType.VOLTAGE, 70_000)])


# --- 3.6 A returned path that bundles a reply -------------------------------


def test_a_returned_path_bundling_a_login_response_round_trips() -> None:
    """3.6: `Mesh.cpp:449-486` — packed byte, path, extra type, extra payload."""
    response = build_room_login_response_body(
        RoomLoginResponseBody(
            server_timestamp=1_700_000_000,
            client_kind=ClientKind.ADMIN,
            permissions=int(Permission.ADMIN),
            blob=b"\x09\x08\x07\x06",
        )
    )
    body = ReturnedPathBody(
        hop_count=2,
        hash_size=1,
        path=b"\xab\xcd",
        extra_type=PayloadType.RESPONSE,
        extra_raw=response,
    )
    built = build_returned_path_body(body)
    assert built[0] == 0b00_000010, "hash size 1 in bits 6-7, hop count 2 in bits 0-5"
    assert built[1:3] == b"\xab\xcd"
    assert built[3] == int(PayloadType.RESPONSE)
    assert built[4:] == response

    parsed = parse_returned_path_body(built)
    assert not isinstance(parsed, DecodeFailure)
    assert parsed.path == b"\xab\xcd"
    assert parsed.extra_type is PayloadType.RESPONSE
    assert parsed.extra_raw == response
    assert parse_room_login_response_body(parsed.extra_raw) == parse_room_login_response_body(
        response
    )


def test_a_returned_path_bundling_an_acknowledgement_round_trips() -> None:
    """3.6: the case design D12 exists for — an ACK arriving inside a path."""
    ack = Acknowledgement(checksum=b"\xde\xad\xbe\xef")
    body = ReturnedPathBody(
        hop_count=0,
        hash_size=1,
        path=b"",
        extra_type=PayloadType.ACK,
        extra_raw=build_ack(ack),
    )
    parsed = parse_returned_path_body(build_returned_path_body(body))
    assert not isinstance(parsed, DecodeFailure)
    assert parsed.extra_ack == ack


def test_a_returned_path_with_no_bundle_is_unchanged() -> None:
    body = ReturnedPathBody(hop_count=1, hash_size=1, path=b"\x7f")
    parsed = parse_returned_path_body(build_returned_path_body(body))
    assert not isinstance(parsed, DecodeFailure)
    assert parsed.extra_type is None and parsed.extra_raw == b""
