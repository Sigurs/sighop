"""The bodies sighop exchanges with a repeater as its client (`payload-codec`).

Hand-built vectors from `simple_repeater/MyMesh.cpp` and `MyMesh.h`, not only
round trips: a wrong layout round-trips just as well as a right one.
"""

from __future__ import annotations

import dataclasses
import struct

from sighop.protocol.payloads import (
    REPEATER_STATS_OFFSETS,
    REPEATER_STATS_SIZE,
    SERVER_STATS_OFFSETS,
    LoginAnswerBody,
    NeighbourEntry,
    NeighbourOrder,
    NeighboursBody,
    NeighboursRequest,
    RepeaterLoginBody,
    RepeaterStatusBody,
    RequestType,
    build_neighbours_body,
    build_neighbours_request,
    build_repeater_login_body,
    build_repeater_status_body,
    parse_login_answer_body,
    parse_neighbours_body,
    parse_repeater_status_body,
)
from sighop.protocol.result import DecodeFailure, FailureReason

_STRUCT = "<HHhhIIIIIIIIHhHHII"
"""`RepeaterStats` as `struct` spells it: little-endian, no padding."""

_VALUES = (
    4012,  # batt_milli_volts
    3,  # curr_tx_queue_len
    -110,  # noise_floor
    -87,  # last_rssi
    100_001,
    100_002,
    3_600,  # total_air_time_secs
    86_400,  # total_up_time_secs
    11,
    12,
    13,
    14,
    0x0005,  # err_events
    -26,  # last_snr, quarter-dB
    21,
    22,
    7_200,  # total_rx_air_time_secs
    42,  # n_recv_errors
)


def _full_body(tag: int = 0x01020304) -> bytes:
    return tag.to_bytes(4, "little") + struct.pack(_STRUCT, *_VALUES)


# --- 1.1 Status -------------------------------------------------------------


def test_the_repeater_struct_is_fifty_six_bytes_and_diverges_only_at_the_tail() -> None:
    assert struct.calcsize(_STRUCT) == REPEATER_STATS_SIZE
    assert REPEATER_STATS_OFFSETS["n_flood_dups"] == SERVER_STATS_OFFSETS["n_flood_dups"] == 46
    assert REPEATER_STATS_OFFSETS["total_rx_air_time_secs"] == 48
    assert REPEATER_STATS_OFFSETS["n_recv_errors"] == 52


def test_a_full_repeater_status_body_yields_every_field_at_its_offset() -> None:
    body = _full_body()
    assert len(body) == 60
    parsed = parse_repeater_status_body(body)
    assert isinstance(parsed, RepeaterStatusBody)
    assert parsed.tag == 0x01020304
    stats = parsed.stats
    assert (
        stats.batt_milli_volts,
        stats.curr_tx_queue_len,
        stats.noise_floor,
        stats.last_rssi,
        stats.n_packets_recv,
        stats.n_packets_sent,
        stats.total_air_time_secs,
        stats.total_up_time_secs,
        stats.n_sent_flood,
        stats.n_sent_direct,
        stats.n_recv_flood,
        stats.n_recv_direct,
        stats.err_events,
        stats.last_snr,
        stats.n_direct_dups,
        stats.n_flood_dups,
        stats.total_rx_air_time_secs,
        stats.n_recv_errors,
    ) == _VALUES
    assert stats.last_snr_db == -6.5


def test_an_older_repeater_without_receive_counters_parses_with_them_absent() -> None:
    parsed = parse_repeater_status_body(_full_body()[:52])
    assert isinstance(parsed, RepeaterStatusBody)
    assert parsed.stats.n_flood_dups == 22
    assert parsed.stats.total_rx_air_time_secs is None
    assert parsed.stats.n_recv_errors is None


def test_a_body_ending_inside_the_uptime_field_is_truncated() -> None:
    uptime = 4 + REPEATER_STATS_OFFSETS["total_up_time_secs"]
    parsed = parse_repeater_status_body(_full_body()[: uptime + 2])
    assert isinstance(parsed, DecodeFailure)
    assert parsed.reason is FailureReason.TRUNCATED


def test_a_body_ending_inside_a_receive_counter_is_truncated() -> None:
    parsed = parse_repeater_status_body(_full_body()[:54])
    assert isinstance(parsed, DecodeFailure)
    assert parsed.reason is FailureReason.TRUNCATED


def test_a_negative_noise_floor_is_read_signed() -> None:
    parsed = parse_repeater_status_body(_full_body())
    assert isinstance(parsed, RepeaterStatusBody)
    assert parsed.stats.noise_floor == -110


def test_cipher_padding_past_the_struct_is_ignored() -> None:
    parsed = parse_repeater_status_body(_full_body() + bytes(4))
    assert isinstance(parsed, RepeaterStatusBody)
    assert parsed.stats.n_recv_errors == 42


def test_the_status_body_round_trips() -> None:
    parsed = parse_repeater_status_body(_full_body())
    assert isinstance(parsed, RepeaterStatusBody)
    assert build_repeater_status_body(parsed) == _full_body()
    older = RepeaterStatusBody(
        tag=1,
        stats=dataclasses.replace(parsed.stats, total_rx_air_time_secs=None, n_recv_errors=None),
    )
    assert len(build_repeater_status_body(older)) == 52


# --- 1.2 Neighbours ---------------------------------------------------------


def test_a_neighbours_request_is_type_version_count_offset_order_prefix_blob() -> None:
    request = NeighboursRequest(
        count=11,
        offset=22,
        order=NeighbourOrder.NEWEST_FIRST,
        prefix_length=6,
        blob=b"\xaa\xbb\xcc\xdd",
    )
    assert build_neighbours_request(request) == bytes(
        [RequestType.GET_NEIGHBOURS, 0, 11, 22, 0, 0, 6, 0xAA, 0xBB, 0xCC, 0xDD]
    )
    assert int(RequestType.GET_NEIGHBOURS) == 0x06


def test_a_neighbours_page_yields_total_and_entries() -> None:
    first = bytes.fromhex("0102030405ff")
    second = bytes.fromhex("a1a2a3a4a5a6")
    body = (
        (77).to_bytes(4, "little")
        + (25).to_bytes(2, "little")
        + (2).to_bytes(2, "little")
        + first
        + (30).to_bytes(4, "little")
        + (40).to_bytes(1, "little", signed=True)
        + second
        + (3600).to_bytes(4, "little")
        + (-10).to_bytes(1, "little", signed=True)
        + bytes(6)  # cipher padding
    )
    parsed = parse_neighbours_body(body, prefix_length=6)
    assert isinstance(parsed, NeighboursBody)
    assert parsed.tag == 77
    assert parsed.total == 25
    assert parsed.entries == (
        NeighbourEntry(prefix=first, heard_seconds_ago=30, snr=40),
        NeighbourEntry(prefix=second, heard_seconds_ago=3600, snr=-10),
    )
    assert [entry.snr_db for entry in parsed.entries] == [10.0, -2.5]
    assert build_neighbours_body(parsed) == body[:-6]


def test_a_lying_entry_count_fails_rather_than_yielding_a_partial_entry() -> None:
    page = NeighboursBody(
        tag=1,
        total=3,
        entries=tuple(NeighbourEntry(bytes([i]) * 6, i, i) for i in range(2)),
    )
    body = bytearray(build_neighbours_body(page))
    body[6:8] = (3).to_bytes(2, "little")
    parsed = parse_neighbours_body(bytes(body), prefix_length=6)
    assert isinstance(parsed, DecodeFailure)
    assert parsed.reason is FailureReason.BAD_PAYLOAD_LENGTH


def test_a_neighbours_answer_without_counts_is_truncated() -> None:
    parsed = parse_neighbours_body(bytes(6), prefix_length=6)
    assert isinstance(parsed, DecodeFailure)
    assert parsed.reason is FailureReason.TRUNCATED


# --- 1.3 Login --------------------------------------------------------------


def test_a_blank_password_login_body_is_the_timestamp_alone() -> None:
    body = build_repeater_login_body(RepeaterLoginBody(timestamp=1_700_000_000))
    assert body == (1_700_000_000).to_bytes(4, "little")
    padded = body + bytes(16 - len(body))
    assert padded[4] == 0


def test_a_twelve_byte_login_answer_has_no_protocol_level() -> None:
    body = (1_700_000_123).to_bytes(4, "little") + bytes([0, 0, 0, 0]) + b"\x01\x02\x03\x04"
    parsed = parse_login_answer_body(body)
    assert parsed == LoginAnswerBody(
        server_timestamp=1_700_000_123,
        result=0,
        permissions=0,
        blob=b"\x01\x02\x03\x04",
        protocol_level=None,
    )


def test_a_thirteen_byte_login_answer_carries_the_protocol_level() -> None:
    body = (5).to_bytes(4, "little") + bytes([0, 0, 0, 0]) + bytes(4) + b"\x01"
    parsed = parse_login_answer_body(body)
    assert isinstance(parsed, LoginAnswerBody)
    assert parsed.protocol_level == 1


def test_a_short_login_answer_is_truncated() -> None:
    parsed = parse_login_answer_body(bytes(11))
    assert isinstance(parsed, DecodeFailure)
    assert parsed.reason is FailureReason.TRUNCATED
