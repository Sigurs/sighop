"""Packet codec tests.

Corpus frames cover flood/direct routing, hop counts 0-5 and all three hash
sizes. Everything the corpus lacks — `TRANSPORT_*` routing, hop counts above 5,
the reserved hash size code, mid-path truncation, reserved payload types — is
covered here with synthetic fixtures, per the `protocol-corpus` spec's rule
that gaps get deliberate tests rather than assumed coverage.
"""

from __future__ import annotations

import pytest

from sighop.protocol.packet import (
    MAX_PACKET_PAYLOAD,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
    decode,
    encode,
)
from sighop.protocol.result import DecodeFailure, EncodeError, FailureReason


def build_raw(
    header: int,
    path_length: int,
    *,
    transport_codes: bytes = b"",
    path: bytes = b"",
    payload: bytes = b"",
) -> bytes:
    return bytes([header]) + transport_codes + bytes([path_length]) + path + payload


def decoded(raw: bytes) -> Packet:
    result = decode(raw)
    assert isinstance(result, Packet), result
    return result


def failure(raw: bytes) -> DecodeFailure:
    result = decode(raw)
    assert isinstance(result, DecodeFailure), result
    return result


# --- Header decoding -------------------------------------------------------


def test_flood_routed_advert_header() -> None:
    packet = decoded(build_raw(0x11, 0x00, payload=b"x"))
    assert packet.route_type is RouteType.FLOOD
    assert packet.payload_type is PayloadType.ADVERT
    assert packet.header.payload_version == 1


def test_direct_routed_group_text_header() -> None:
    packet = decoded(build_raw(0x16, 0x00, payload=b"x"))
    assert packet.route_type is RouteType.DIRECT
    assert packet.payload_type is PayloadType.GRP_TXT
    assert packet.header.payload_version == 1


@pytest.mark.parametrize("value", [0x0C, 0x0D, 0x0E])
def test_reserved_payload_type_is_preserved_not_coerced(value: int) -> None:
    packet = decoded(build_raw(0x01 | value << 2, 0x00, payload=b"x"))
    assert int(packet.payload_type) == value
    assert not packet.payload_type.is_recognized


@pytest.mark.parametrize(
    ("bits", "version"), [(0b01, 2), (0b10, 3), (0b11, 4)]
)
def test_non_v1_payload_version_is_rejected(bits: int, version: int) -> None:
    result = failure(build_raw(bits << 6 | 0x11, 0x00, payload=b"x"))
    assert result.reason is FailureReason.UNSUPPORTED_PAYLOAD_VERSION
    assert f"version {version}" in result.detail


# --- Transport codes -------------------------------------------------------


@pytest.mark.parametrize(
    "route", [RouteType.TRANSPORT_FLOOD, RouteType.TRANSPORT_DIRECT]
)
def test_transport_routed_packet_carries_transport_codes(route: RouteType) -> None:
    raw = build_raw(
        0x10 | route,
        0x02,
        transport_codes=bytes.fromhex("3412 7856".replace(" ", "")),
        path=b"\xaa\xbb",
        payload=b"payload",
    )
    packet = decoded(raw)
    assert packet.transport_codes == (0x1234, 0x5678)
    assert packet.hop_count == 2
    assert packet.path == b"\xaa\xbb"
    assert packet.payload == b"payload"
    assert encode(packet) == raw


def test_non_transport_packet_has_no_transport_codes() -> None:
    packet = decoded(build_raw(0x11, 0x00, payload=b"x"))
    assert packet.transport_codes is None


def test_transport_codes_truncated() -> None:
    result = failure(bytes([0x10]) + b"\x01\x02")
    assert result.reason is FailureReason.TRUNCATED
    assert "transport code" in result.detail


# --- Path length encoding --------------------------------------------------


def test_zero_hop_packet() -> None:
    packet = decoded(build_raw(0x11, 0x00, payload=b"body"))
    assert (packet.hop_count, packet.hash_size, packet.path) == (0, 1, b"")
    assert packet.hops == ()
    assert packet.payload == b"body"


def test_legacy_one_byte_path_hashes() -> None:
    packet = decoded(build_raw(0x11, 0x05, path=bytes(range(5)), payload=b"p"))
    assert packet.hash_size == 1
    assert packet.hops == (b"\x00", b"\x01", b"\x02", b"\x03", b"\x04")


def test_two_byte_path_hashes() -> None:
    packet = decoded(build_raw(0x11, 0x45, path=bytes(range(10)), payload=b"p"))
    assert (packet.hop_count, packet.hash_size) == (5, 2)
    assert len(packet.hops) == 5
    assert packet.hops[0] == b"\x00\x01"


def test_three_byte_path_hashes() -> None:
    packet = decoded(build_raw(0x11, 0x8A, path=bytes(range(30)), payload=b"p"))
    assert (packet.hop_count, packet.hash_size) == (10, 3)
    assert len(packet.hops) == 10
    assert packet.hops[-1] == b"\x1b\x1c\x1d"


def test_hop_count_above_the_corpus_maximum() -> None:
    """The corpus tops out at 5 hops; the field holds 63."""
    packet = decoded(build_raw(0x11, 0x20, path=bytes(32), payload=b"p"))
    assert packet.hop_count == 32
    assert len(packet.hops) == 32


def test_reserved_hash_size_code_is_rejected() -> None:
    result = failure(build_raw(0x11, 0xC5, path=bytes(20), payload=b"p"))
    assert result.reason is FailureReason.RESERVED_HASH_SIZE


# --- Size limits -----------------------------------------------------------


def test_path_extent_exceeds_max_path_size() -> None:
    """23 hops of 3-byte hashes is 69 path bytes, though 23 hops alone is legal."""
    result = failure(build_raw(0x11, 0x80 | 23, path=bytes(69), payload=b"p"))
    assert result.reason is FailureReason.PATH_SIZE_LIMIT
    assert "69" in result.detail


def test_max_path_size_exactly_is_accepted() -> None:
    packet = decoded(build_raw(0x11, 0x40 | 32, path=bytes(64), payload=b"p"))
    assert packet.hop_count * packet.hash_size == 64


def test_payload_exceeds_max_packet_payload() -> None:
    result = failure(build_raw(0x11, 0x00, payload=bytes(MAX_PACKET_PAYLOAD + 1)))
    assert result.reason is FailureReason.PAYLOAD_SIZE_LIMIT


def test_total_length_exceeds_max_trans_unit() -> None:
    result = failure(build_raw(0x11, 0x40 | 32, path=bytes(64), payload=bytes(190)))
    assert result.reason is FailureReason.TOTAL_SIZE_LIMIT


def test_packet_truncated_mid_path() -> None:
    result = failure(build_raw(0x11, 0x8A, path=bytes(12)))
    assert result.reason is FailureReason.TRUNCATED
    assert "30 bytes" in result.detail


def test_packet_truncated_before_path_length_byte() -> None:
    result = failure(bytes([0x11]))
    assert result.reason is FailureReason.TRUNCATED
    assert "path length" in result.detail


def test_empty_frame() -> None:
    assert failure(b"").reason is FailureReason.TRUNCATED


def test_empty_payload_is_accepted() -> None:
    """Payload-level validation belongs to the payload codec, not here."""
    packet = decoded(build_raw(0x11, 0x03, path=b"\x01\x02\x03"))
    assert packet.payload == b""


# --- Encoding --------------------------------------------------------------


def test_encoding_computes_the_path_length_byte() -> None:
    packet = Packet(
        header=PacketHeader(RouteType.FLOOD, PayloadType.TXT_MSG, 1),
        transport_codes=None,
        hop_count=5,
        hash_size=2,
        path=bytes(range(10)),
        payload=b"p",
    )
    assert encode(packet)[1] == 0x45
    assert encode(packet)[2:12] == bytes(range(10))


def test_encode_rejects_an_over_limit_payload() -> None:
    packet = Packet(
        header=PacketHeader(RouteType.FLOOD, PayloadType.TXT_MSG, 1),
        transport_codes=None,
        hop_count=0,
        hash_size=1,
        path=b"",
        payload=bytes(MAX_PACKET_PAYLOAD + 1),
    )
    with pytest.raises(EncodeError, match="MAX_PACKET_PAYLOAD"):
        encode(packet)


def test_encode_rejects_an_over_limit_path() -> None:
    packet = Packet(
        header=PacketHeader(RouteType.FLOOD, PayloadType.TXT_MSG, 1),
        transport_codes=None,
        hop_count=23,
        hash_size=3,
        path=bytes(69),
        payload=b"p",
    )
    with pytest.raises(EncodeError, match="MAX_PATH_SIZE"):
        encode(packet)


def test_encode_rejects_a_path_inconsistent_with_its_hop_count() -> None:
    packet = Packet(
        header=PacketHeader(RouteType.FLOOD, PayloadType.TXT_MSG, 1),
        transport_codes=None,
        hop_count=3,
        hash_size=2,
        path=b"\x01\x02",
        payload=b"p",
    )
    with pytest.raises(EncodeError, match="needs 6"):
        encode(packet)


def test_encode_rejects_transport_codes_on_a_non_transport_route() -> None:
    packet = Packet(
        header=PacketHeader(RouteType.FLOOD, PayloadType.TXT_MSG, 1),
        transport_codes=(1, 2),
        hop_count=0,
        hash_size=1,
        path=b"",
        payload=b"p",
    )
    with pytest.raises(EncodeError, match="must not carry transport codes"):
        encode(packet)


def test_encode_requires_transport_codes_on_a_transport_route() -> None:
    packet = Packet(
        header=PacketHeader(RouteType.TRANSPORT_DIRECT, PayloadType.TXT_MSG, 1),
        transport_codes=None,
        hop_count=0,
        hash_size=1,
        path=b"",
        payload=b"p",
    )
    with pytest.raises(EncodeError, match="requires transport codes"):
        encode(packet)


@pytest.mark.parametrize(
    "raw_hex",
    [
        "1e0054[redacted]8ecd77dfc078d4386ea8326f26e124e257869087027731a53dc"
        "31db9c0ecc0ced37f0a645a90e11bf324210277fe",
        "0601be425431382924f0af5742afb44129753844ea8a8e",
        "0600425431382924f0af5742afb44129753844ea8a8e",
    ],
)
def test_round_trip_of_real_frames(raw_hex: str) -> None:
    raw = bytes.fromhex(raw_hex)
    assert encode(decoded(raw)) == raw
