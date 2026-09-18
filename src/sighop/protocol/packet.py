"""MeshCore v1 packet codec: structural decode and encode over `bytes`.

Wire layout (`related-repos/MeshCore/docs/packet_format.md`):

    [header][transport_codes(optional)][path_length][path][payload]

Per design D1 this module stops at the payload boundary: it never interprets
payload bytes, so a frame whose payload we cannot parse (MULTIPART, CONTROL, a
reserved type) still decodes structurally and round-trips. `payloads.py` is the
layer above.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from sighop.protocol.result import (
    DecodeFailure,
    DecodeResult,
    EncodeError,
    FailureReason,
)

# MeshCore.h
MAX_PATH_SIZE = 64
MAX_PACKET_PAYLOAD = 184
MAX_TRANS_UNIT = 255

# packet_format.md, "Header Format"
_ROUTE_MASK = 0x03
_PAYLOAD_TYPE_MASK = 0x3C
_PAYLOAD_TYPE_SHIFT = 2
_VERSION_MASK = 0xC0
_VERSION_SHIFT = 6

# path_length is packed, not a byte count: bits 0-5 hop count, bits 6-7 hash size - 1
_HOP_COUNT_MASK = 0x3F
_HASH_SIZE_SHIFT = 6

TRANSPORT_CODES_SIZE = 4

PAYLOAD_VERSION_1 = 1


class RouteType(IntEnum):
    """Header bits 0-1. Transport codes are present for the two `TRANSPORT_*` values."""

    TRANSPORT_FLOOD = 0x00
    FLOOD = 0x01
    DIRECT = 0x02
    TRANSPORT_DIRECT = 0x03

    @property
    def has_transport_codes(self) -> bool:
        return self in (RouteType.TRANSPORT_FLOOD, RouteType.TRANSPORT_DIRECT)


class PayloadType(IntEnum):
    """Header bits 2-5. All 16 values are named so a reserved value is preserved
    as its number rather than raising or being coerced to a known type.
    """

    REQ = 0x00
    RESPONSE = 0x01
    TXT_MSG = 0x02
    ACK = 0x03
    ADVERT = 0x04
    GRP_TXT = 0x05
    GRP_DATA = 0x06
    ANON_REQ = 0x07
    PATH = 0x08
    TRACE = 0x09
    MULTIPART = 0x0A
    CONTROL = 0x0B
    RESERVED_0C = 0x0C
    RESERVED_0D = 0x0D
    RESERVED_0E = 0x0E
    RAW_CUSTOM = 0x0F

    @property
    def is_recognized(self) -> bool:
        """False for the `0x0C`-`0x0E` values the format reserves for future use."""
        return self not in (
            PayloadType.RESERVED_0C,
            PayloadType.RESERVED_0D,
            PayloadType.RESERVED_0E,
        )


@dataclass(frozen=True, slots=True)
class PacketHeader:
    """The decoded header byte, as named values rather than raw integers."""

    route_type: RouteType
    payload_type: PayloadType
    payload_version: int

    def to_byte(self) -> int:
        return (
            (self.payload_version - 1) << _VERSION_SHIFT
            | self.payload_type << _PAYLOAD_TYPE_SHIFT
            | self.route_type
        )


@dataclass(frozen=True, slots=True)
class Packet:
    """A structurally decoded v1 packet — inert data over `bytes` (design D4).

    `path` holds the raw path bytes; `hash_size` says how to split them, which
    `hops` does. Storing both the raw bytes and the size is what makes
    byte-identical re-encoding possible.
    """

    header: PacketHeader
    transport_codes: tuple[int, int] | None
    hop_count: int
    hash_size: int
    path: bytes
    payload: bytes

    @property
    def hops(self) -> tuple[bytes, ...]:
        """The path split into `hop_count` hashes of `hash_size` bytes each."""
        size = self.hash_size
        return tuple(self.path[i : i + size] for i in range(0, len(self.path), size))

    @property
    def route_type(self) -> RouteType:
        return self.header.route_type

    @property
    def payload_type(self) -> PayloadType:
        return self.header.payload_type


def _hash_size_code(hash_size: int) -> int:
    return hash_size - 1


def decode(raw: bytes) -> DecodeResult[Packet]:
    """Decode wire bytes into a `Packet`, or return a `DecodeFailure` saying why not."""
    if len(raw) > MAX_TRANS_UNIT:
        return DecodeFailure(
            reason=FailureReason.TOTAL_SIZE_LIMIT,
            offset=MAX_TRANS_UNIT,
            raw=raw,
            detail=f"{len(raw)} bytes exceeds MAX_TRANS_UNIT {MAX_TRANS_UNIT}",
        )
    if not raw:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED, offset=0, raw=raw, detail="empty frame"
        )

    header_byte = raw[0]
    version = ((header_byte & _VERSION_MASK) >> _VERSION_SHIFT) + 1
    if version != PAYLOAD_VERSION_1:
        return DecodeFailure(
            reason=FailureReason.UNSUPPORTED_PAYLOAD_VERSION,
            offset=0,
            raw=raw,
            detail=f"payload version {version}, only v1 is supported",
        )
    header = PacketHeader(
        route_type=RouteType(header_byte & _ROUTE_MASK),
        payload_type=PayloadType((header_byte & _PAYLOAD_TYPE_MASK) >> _PAYLOAD_TYPE_SHIFT),
        payload_version=version,
    )

    offset = 1
    transport_codes: tuple[int, int] | None = None
    if header.route_type.has_transport_codes:
        end = offset + TRANSPORT_CODES_SIZE
        if len(raw) < end:
            return DecodeFailure(
                reason=FailureReason.TRUNCATED,
                offset=len(raw),
                raw=raw,
                detail="frame ends inside the transport code block",
            )
        transport_codes = (
            int.from_bytes(raw[offset : offset + 2], "little"),
            int.from_bytes(raw[offset + 2 : end], "little"),
        )
        offset = end

    if len(raw) <= offset:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=offset,
            raw=raw,
            detail="frame ends before the path length byte",
        )
    path_length_byte = raw[offset]
    offset += 1

    hop_count = path_length_byte & _HOP_COUNT_MASK
    hash_size_code = path_length_byte >> _HASH_SIZE_SHIFT
    if hash_size_code == 0b11:
        return DecodeFailure(
            reason=FailureReason.RESERVED_HASH_SIZE,
            offset=offset - 1,
            raw=raw,
            detail="path hash size code 0b11 is reserved",
        )
    hash_size = hash_size_code + 1

    path_extent = hop_count * hash_size
    if path_extent > MAX_PATH_SIZE:
        return DecodeFailure(
            reason=FailureReason.PATH_SIZE_LIMIT,
            offset=offset - 1,
            raw=raw,
            detail=(
                f"{hop_count} hops of {hash_size} bytes is {path_extent} bytes, "
                f"exceeding MAX_PATH_SIZE {MAX_PATH_SIZE}"
            ),
        )

    path_end = offset + path_extent
    if len(raw) < path_end:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(raw),
            raw=raw,
            detail=(f"path declares {path_extent} bytes but only {len(raw) - offset} remain"),
        )
    path = raw[offset:path_end]

    payload = raw[path_end:]
    if len(payload) > MAX_PACKET_PAYLOAD:
        return DecodeFailure(
            reason=FailureReason.PAYLOAD_SIZE_LIMIT,
            offset=path_end,
            raw=raw,
            detail=(
                f"{len(payload)} payload bytes exceeds MAX_PACKET_PAYLOAD {MAX_PACKET_PAYLOAD}"
            ),
        )

    return Packet(
        header=header,
        transport_codes=transport_codes,
        hop_count=hop_count,
        hash_size=hash_size,
        path=path,
        payload=payload,
    )


def encode(packet: Packet) -> bytes:
    """Encode a `Packet` back to wire bytes, applying the decode-side limits.

    Raises `EncodeError` rather than returning a failure: the input is ours, so
    an over-limit packet is a bug here, not malformed traffic (design D3).
    """
    if packet.header.payload_version != PAYLOAD_VERSION_1:
        raise EncodeError(f"cannot encode payload version {packet.header.payload_version}: only v1")
    if not 1 <= packet.hash_size <= 3:
        raise EncodeError(
            f"path hash size {packet.hash_size} is not encodable (1-3; the 4-byte code is reserved)"
        )
    if not 0 <= packet.hop_count <= _HOP_COUNT_MASK:
        raise EncodeError(f"hop count {packet.hop_count} does not fit in 6 bits")
    path_extent = packet.hop_count * packet.hash_size
    if len(packet.path) != path_extent:
        raise EncodeError(
            f"path is {len(packet.path)} bytes but {packet.hop_count} hops of "
            f"{packet.hash_size} bytes needs {path_extent}"
        )
    if path_extent > MAX_PATH_SIZE:
        raise EncodeError(f"path of {path_extent} bytes exceeds MAX_PATH_SIZE {MAX_PATH_SIZE}")
    if len(packet.payload) > MAX_PACKET_PAYLOAD:
        raise EncodeError(
            f"payload of {len(packet.payload)} bytes exceeds MAX_PACKET_PAYLOAD "
            f"{MAX_PACKET_PAYLOAD}"
        )

    has_codes = packet.header.route_type.has_transport_codes
    if has_codes and packet.transport_codes is None:
        raise EncodeError(f"route type {packet.header.route_type.name} requires transport codes")
    if not has_codes and packet.transport_codes is not None:
        raise EncodeError(
            f"route type {packet.header.route_type.name} must not carry transport codes"
        )

    out = bytearray()
    out.append(packet.header.to_byte())
    if packet.transport_codes is not None:
        for code in packet.transport_codes:
            if not 0 <= code <= 0xFFFF:
                raise EncodeError(f"transport code {code} does not fit in a uint16")
            out += code.to_bytes(2, "little")
    out.append(_hash_size_code(packet.hash_size) << _HASH_SIZE_SHIFT | packet.hop_count)
    out += packet.path
    out += packet.payload

    if len(out) > MAX_TRANS_UNIT:
        raise EncodeError(f"packet of {len(out)} bytes exceeds MAX_TRANS_UNIT {MAX_TRANS_UNIT}")
    return bytes(out)
