"""MeshCore v1 payload codec: envelopes, adverts, and the plaintext bodies.

Reference: `related-repos/MeshCore/docs/payloads.md`, with
`src/helpers/AdvertDataHelpers.cpp`, `src/Mesh.cpp` and `src/MeshCore.h`
settling field order, sizes and constants. All multi-byte integers are
little-endian.

Design D2 splits payload handling into three stages with different trust
levels, and the types keep them apart:

- **Envelope** — parseable from the packet alone (hashes, MAC, ciphertext
  extent). Always available. `parse_payload()` gets you here.
- **Ciphertext** — needs a key to go further, and may be un-openable forever.
  That is the normal state of every encrypted frame in the capture corpus, not
  an error.
- **Body** — only exists after MAC verification and decryption. The `parse_*_body`
  functions take already-decrypted bytes; they are never reached from
  `parse_payload()`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

from sighop.protocol.packet import MAX_PATH_SIZE, PayloadType
from sighop.protocol.result import (
    DecodeFailure,
    DecodeResult,
    EncodeError,
    FailureReason,
)

# MeshCore.h
PUB_KEY_SIZE = 32
SIGNATURE_SIZE = 64
CIPHER_MAC_SIZE = 2
CIPHER_BLOCK_SIZE = 16
MAX_ADVERT_DATA_SIZE = 32

ADVERT_FIXED_SIZE = PUB_KEY_SIZE + 4 + SIGNATURE_SIZE  # pubkey + timestamp + signature

ACK_CHECKSUM_SIZE = 4
ACK_TAIL_SIZE = 2

# The payload types that share the dest-hash/src-hash/MAC/ciphertext envelope.
DIRECT_ENVELOPE_TYPES = frozenset(
    {
        PayloadType.REQ,
        PayloadType.RESPONSE,
        PayloadType.TXT_MSG,
        PayloadType.PATH,
    }
)
GROUP_ENVELOPE_TYPES = frozenset({PayloadType.GRP_TXT, PayloadType.GRP_DATA})

# AdvertDataHelpers.h
ADV_LATLON_MASK = 0x10
ADV_FEAT1_MASK = 0x20
ADV_FEAT2_MASK = 0x40
ADV_NAME_MASK = 0x80
ADV_TYPE_MASK = 0x0F

GEO_SCALE = 1_000_000


class NodeType(IntEnum):
    """Advert appdata flags, low nibble — an enum, not a bit field.

    This is the correction recorded in the proposal: `0x03` is `ROOM_SERVER`,
    not `CHAT | REPEATER`. See `AdvertDataHelpers.cpp::encodeTo`, which writes
    `app_data[0] = _type` and only then OR-masks the high-nibble bits.
    """

    NONE = 0
    CHAT = 1
    REPEATER = 2
    ROOM_SERVER = 3
    SENSOR = 4


class TextType(IntEnum):
    """The upper six bits of a text-message body's flags byte."""

    PLAIN = 0
    CLI_DATA = 1
    SIGNED_PLAIN = 2


PERMISSION_ROLE_MASK = 0x03
"""`PERM_ACL_ROLE_MASK` (`ClientACL.h:7`): the role is the low two bits, and the
upper six are reserved for flags this milestone does not set."""


class Permission(IntEnum):
    """A member's role, as the byte the login response carries (`ClientACL.h`).

    One name here is a trap worth reading twice. The firmware's `PERM_ACL_GUEST`
    is **zero**, and it is what a room server grants a client whose password
    matched nothing when `allow_read_only` is set (`MyMesh.cpp:350`); posts from
    it are refused (`:479`). So `GUEST` is what everything else in sighop calls
    *read-only* — a member that receives history and may not post.

    `READ_ONLY` (1) is declared by the firmware and never assigned by the room
    server. It is here because the byte can carry it and a member restored from
    a database written by other firmware might, not because we produce it.
    """

    GUEST = 0
    READ_ONLY = 1
    READ_WRITE = 2
    ADMIN = 3

    @property
    def may_post(self) -> bool:
        """Whether this role may add to a room's history (`MyMesh.cpp:479`)."""
        return self in (Permission.READ_WRITE, Permission.ADMIN)

    @property
    def is_admin(self) -> bool:
        return self is Permission.ADMIN


@dataclass(frozen=True, slots=True)
class WireText:
    """Text lifted off the wire, which is not guaranteed to be valid UTF-8.

    `raw` is always the exact bytes. `text` is a rendering safe to display;
    when `is_valid_utf8` is false it contains U+FFFD replacement characters and
    must not be treated as the node's actual name or message.
    """

    raw: bytes
    text: str
    is_valid_utf8: bool

    @classmethod
    def from_bytes(cls, raw: bytes) -> WireText:
        try:
            return cls(raw=raw, text=raw.decode("utf-8"), is_valid_utf8=True)
        except UnicodeDecodeError:
            return cls(raw=raw, text=raw.decode("utf-8", errors="replace"), is_valid_utf8=False)


# --- Envelopes (stage 1) ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class DirectEnvelope:
    """REQ, RESPONSE, TXT_MSG and PATH share this shape."""

    payload_type: PayloadType
    dest_hash: int
    src_hash: int
    mac: bytes
    ciphertext: bytes


@dataclass(frozen=True, slots=True)
class AnonRequestEnvelope:
    """ANON_REQ carries the sender's full public key where the others carry a hash."""

    dest_hash: int
    sender_public_key: bytes
    mac: bytes
    ciphertext: bytes

    @property
    def sender_hash(self) -> int:
        return self.sender_public_key[0]


@dataclass(frozen=True, slots=True)
class GroupEnvelope:
    """GRP_TXT and GRP_DATA: a channel hash instead of node hashes."""

    payload_type: PayloadType
    channel_hash: int
    mac: bytes
    ciphertext: bytes


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    """A delivery acknowledgement.

    `checksum` is the 4-byte truncated SHA-256 defined in `crypto.py`. `tail`
    holds the 2 extra bytes current firmware appends — an extended attempt byte
    and a random byte, to keep the packet hash unique
    (`BaseChatMesh.cpp:245-247`) — and is empty for the 4-byte form older
    firmware sends. Both are live on the captured mesh.

    A match is delivery evidence only, never sender authentication: the value
    is an unkeyed hash over data any observer of the plaintext could reproduce.
    """

    checksum: bytes
    tail: bytes = b""


@dataclass(frozen=True, slots=True)
class Advert:
    """An ADVERT payload, still unverified.

    Nothing here may be presented to a user: `crypto.verify_advert()` is the
    only route to trustworthy advert content (design D7).
    """

    public_key: bytes
    timestamp: int
    signature: bytes
    appdata: bytes

    @property
    def node_hash(self) -> int:
        return self.public_key[0]

    @property
    def signed_message(self) -> bytes:
        """The bytes the signature covers (`Mesh.cpp::createAdvert`)."""
        return self.public_key + self.timestamp.to_bytes(4, "little") + self.appdata


@dataclass(frozen=True, slots=True)
class TracePayload:
    """A TRACE payload, preserved whole. Per-hop SNR is not interpreted in v1."""

    raw: bytes


@dataclass(frozen=True, slots=True)
class UnparsedPayload:
    """A payload type this milestone recognizes but does not interpret.

    MULTIPART (reassembly is an explicit v1 non-goal), CONTROL, RAW_CUSTOM and
    the reserved type values. The bytes are preserved rather than dropped so a
    frame carrying one still decodes, round-trips and counts in the corpus.
    """

    payload_type: PayloadType
    raw: bytes


type ParsedPayload = (
    DirectEnvelope
    | AnonRequestEnvelope
    | GroupEnvelope
    | Acknowledgement
    | Advert
    | TracePayload
    | UnparsedPayload
)


def parse_payload(payload_type: PayloadType, payload: bytes) -> DecodeResult[ParsedPayload]:
    """Parse payload bytes according to the packet header's payload type.

    Returns the envelope stage only: nothing here decrypts, and nothing here
    needs a key.
    """
    if payload_type in DIRECT_ENVELOPE_TYPES:
        return parse_direct_envelope(payload_type, payload)
    if payload_type is PayloadType.ANON_REQ:
        return parse_anon_request(payload)
    if payload_type in GROUP_ENVELOPE_TYPES:
        return parse_group_envelope(payload_type, payload)
    if payload_type is PayloadType.ACK:
        return parse_ack(payload)
    if payload_type is PayloadType.ADVERT:
        return parse_advert(payload)
    if payload_type is PayloadType.TRACE:
        return TracePayload(raw=payload)
    return UnparsedPayload(payload_type=payload_type, raw=payload)


def _check_ciphertext(ciphertext: bytes, raw: bytes, offset: int) -> DecodeFailure | None:
    if not ciphertext:
        return DecodeFailure(
            reason=FailureReason.CIPHERTEXT_NOT_BLOCK_ALIGNED,
            offset=offset,
            raw=raw,
            detail="ciphertext is empty",
        )
    if len(ciphertext) % CIPHER_BLOCK_SIZE:
        return DecodeFailure(
            reason=FailureReason.CIPHERTEXT_NOT_BLOCK_ALIGNED,
            offset=offset,
            raw=raw,
            detail=(
                f"{len(ciphertext)} ciphertext bytes is not a multiple of "
                f"{CIPHER_BLOCK_SIZE}; AES-128-ECB output always is"
            ),
        )
    return None


def parse_direct_envelope(
    payload_type: PayloadType, payload: bytes
) -> DecodeResult[DirectEnvelope]:
    """Parse the REQ/RESPONSE/TXT_MSG/PATH envelope."""
    if payload_type not in DIRECT_ENVELOPE_TYPES:
        return DecodeFailure(
            reason=FailureReason.PAYLOAD_TYPE_MISMATCH,
            offset=0,
            raw=payload,
            detail=f"{payload_type.name} does not use the direct envelope",
        )
    header_size = 1 + 1 + CIPHER_MAC_SIZE
    if len(payload) < header_size:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(payload),
            raw=payload,
            detail=f"envelope needs {header_size} bytes before its ciphertext",
        )
    ciphertext = payload[header_size:]
    if (failure := _check_ciphertext(ciphertext, payload, header_size)) is not None:
        return failure
    return DirectEnvelope(
        payload_type=payload_type,
        dest_hash=payload[0],
        src_hash=payload[1],
        mac=payload[2:header_size],
        ciphertext=ciphertext,
    )


def parse_anon_request(payload: bytes) -> DecodeResult[AnonRequestEnvelope]:
    header_size = 1 + PUB_KEY_SIZE + CIPHER_MAC_SIZE
    if len(payload) < header_size:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(payload),
            raw=payload,
            detail=f"ANON_REQ needs {header_size} bytes before its ciphertext",
        )
    ciphertext = payload[header_size:]
    if (failure := _check_ciphertext(ciphertext, payload, header_size)) is not None:
        return failure
    return AnonRequestEnvelope(
        dest_hash=payload[0],
        sender_public_key=payload[1 : 1 + PUB_KEY_SIZE],
        mac=payload[1 + PUB_KEY_SIZE : header_size],
        ciphertext=ciphertext,
    )


def parse_group_envelope(payload_type: PayloadType, payload: bytes) -> DecodeResult[GroupEnvelope]:
    if payload_type not in GROUP_ENVELOPE_TYPES:
        return DecodeFailure(
            reason=FailureReason.PAYLOAD_TYPE_MISMATCH,
            offset=0,
            raw=payload,
            detail=f"{payload_type.name} is not a group payload",
        )
    header_size = 1 + CIPHER_MAC_SIZE
    if len(payload) < header_size:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(payload),
            raw=payload,
            detail=f"group payload needs {header_size} bytes before its ciphertext",
        )
    ciphertext = payload[header_size:]
    if (failure := _check_ciphertext(ciphertext, payload, header_size)) is not None:
        return failure
    return GroupEnvelope(
        payload_type=payload_type,
        channel_hash=payload[0],
        mac=payload[1:header_size],
        ciphertext=ciphertext,
    )


def parse_ack(payload: bytes) -> DecodeResult[Acknowledgement]:
    """Parse an ACK payload: 4 bytes, or 6 with the firmware's extra tail."""
    if len(payload) not in (ACK_CHECKSUM_SIZE, ACK_CHECKSUM_SIZE + ACK_TAIL_SIZE):
        return DecodeFailure(
            reason=FailureReason.BAD_PAYLOAD_LENGTH,
            offset=0,
            raw=payload,
            detail=(
                f"ACK payload is {len(payload)} bytes; only "
                f"{ACK_CHECKSUM_SIZE} or {ACK_CHECKSUM_SIZE + ACK_TAIL_SIZE} are valid"
            ),
        )
    return Acknowledgement(checksum=payload[:ACK_CHECKSUM_SIZE], tail=payload[ACK_CHECKSUM_SIZE:])


def parse_advert(payload: bytes) -> DecodeResult[Advert]:
    """Parse an ADVERT payload. The appdata block is left raw; see `parse_appdata`."""
    if len(payload) < ADVERT_FIXED_SIZE + 1:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(payload),
            raw=payload,
            detail=(
                f"ADVERT needs at least {ADVERT_FIXED_SIZE + 1} bytes "
                "(pubkey, timestamp, signature and an appdata flags byte)"
            ),
        )
    return Advert(
        public_key=payload[:PUB_KEY_SIZE],
        timestamp=int.from_bytes(payload[PUB_KEY_SIZE : PUB_KEY_SIZE + 4], "little"),
        signature=payload[PUB_KEY_SIZE + 4 : ADVERT_FIXED_SIZE],
        appdata=payload[ADVERT_FIXED_SIZE:],
    )


# --- Advert appdata --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdvertAppData:
    """The parsed appdata block of an advert.

    `node_type` is the flags byte's low nibble as an enum where the value is
    one MeshCore defines, and the bare integer for the 5-15 range reserved for
    future node types.
    """

    flags: int
    node_type: NodeType | int
    latitude: int | None = None
    longitude: int | None = None
    feature1: int | None = None
    feature2: int | None = None
    name: WireText | None = None
    trailing: bytes = b""

    @property
    def latitude_degrees(self) -> float | None:
        return None if self.latitude is None else self.latitude / GEO_SCALE

    @property
    def longitude_degrees(self) -> float | None:
        return None if self.longitude is None else self.longitude / GEO_SCALE


def parse_appdata(appdata: bytes) -> DecodeResult[AdvertAppData]:
    """Parse an advert appdata block (`AdvertDataHelpers.cpp::AdvertDataParser`).

    Field order is fixed: flags, then lat/lon, feature 1, feature 2, and the
    name as whatever remains — each present only if its flag bit is set.
    """
    if not appdata:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=0,
            raw=appdata,
            detail="appdata is empty, so it has no flags byte",
        )

    flags = appdata[0]
    offset = 1

    def truncated(field: str, needed: int) -> DecodeFailure:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(appdata),
            raw=appdata,
            detail=(
                f"flags 0x{flags:02x} declare {field}, needing {needed} bytes at "
                f"offset {offset}, but only {len(appdata) - offset} remain"
            ),
        )

    latitude = longitude = None
    if flags & ADV_LATLON_MASK:
        if len(appdata) - offset < 8:
            return truncated("a location", 8)
        latitude = int.from_bytes(appdata[offset : offset + 4], "little", signed=True)
        longitude = int.from_bytes(appdata[offset + 4 : offset + 8], "little", signed=True)
        offset += 8

    feature1 = feature2 = None
    if flags & ADV_FEAT1_MASK:
        if len(appdata) - offset < 2:
            return truncated("feature 1", 2)
        feature1 = int.from_bytes(appdata[offset : offset + 2], "little")
        offset += 2
    if flags & ADV_FEAT2_MASK:
        if len(appdata) - offset < 2:
            return truncated("feature 2", 2)
        feature2 = int.from_bytes(appdata[offset : offset + 2], "little")
        offset += 2

    name: WireText | None = None
    trailing = b""
    remainder = appdata[offset:]
    if flags & ADV_NAME_MASK:
        # The name runs to the end of the appdata with no terminator on the wire.
        if remainder:
            name = WireText.from_bytes(remainder)
    else:
        trailing = remainder

    type_value = flags & ADV_TYPE_MASK
    try:
        node_type: NodeType | int = NodeType(type_value)
    except ValueError:
        node_type = type_value

    return AdvertAppData(
        flags=flags,
        node_type=node_type,
        latitude=latitude,
        longitude=longitude,
        feature1=feature1,
        feature2=feature2,
        name=name,
        trailing=trailing,
    )


def build_appdata(
    node_type: NodeType | int,
    *,
    latitude: int | None = None,
    longitude: int | None = None,
    feature1: int | None = None,
    feature2: int | None = None,
    name: bytes | str | None = None,
) -> bytes:
    """Build an advert appdata block, enforcing `MAX_ADVERT_DATA_SIZE`.

    Latitude and longitude are micro-degrees (the wire units), and must be
    given together or not at all.
    """
    if not 0 <= int(node_type) <= ADV_TYPE_MASK:
        raise EncodeError(f"node type {node_type} does not fit in the flags low nibble")
    if (latitude is None) != (longitude is None):
        raise EncodeError("latitude and longitude must be supplied together")

    flags = int(node_type)
    out = bytearray([0])  # flags byte is patched in below
    if latitude is not None and longitude is not None:
        flags |= ADV_LATLON_MASK
        out += latitude.to_bytes(4, "little", signed=True)
        out += longitude.to_bytes(4, "little", signed=True)
    if feature1 is not None:
        flags |= ADV_FEAT1_MASK
        out += feature1.to_bytes(2, "little")
    if feature2 is not None:
        flags |= ADV_FEAT2_MASK
        out += feature2.to_bytes(2, "little")
    if name:
        name_bytes = name.encode("utf-8") if isinstance(name, str) else name
        flags |= ADV_NAME_MASK
        out += name_bytes
    out[0] = flags

    if len(out) > MAX_ADVERT_DATA_SIZE:
        raise EncodeError(
            f"appdata of {len(out)} bytes exceeds MAX_ADVERT_DATA_SIZE {MAX_ADVERT_DATA_SIZE}"
        )
    return bytes(out)


# --- Payload building ------------------------------------------------------


def build_advert(advert: Advert) -> bytes:
    if len(advert.public_key) != PUB_KEY_SIZE:
        raise EncodeError(f"public key must be {PUB_KEY_SIZE} bytes")
    if len(advert.signature) != SIGNATURE_SIZE:
        raise EncodeError(f"signature must be {SIGNATURE_SIZE} bytes")
    if len(advert.appdata) > MAX_ADVERT_DATA_SIZE:
        raise EncodeError(
            f"appdata of {len(advert.appdata)} bytes exceeds MAX_ADVERT_DATA_SIZE "
            f"{MAX_ADVERT_DATA_SIZE}"
        )
    return (
        advert.public_key
        + advert.timestamp.to_bytes(4, "little")
        + advert.signature
        + advert.appdata
    )


def build_ack(ack: Acknowledgement) -> bytes:
    if len(ack.checksum) != ACK_CHECKSUM_SIZE:
        raise EncodeError(f"ACK checksum must be {ACK_CHECKSUM_SIZE} bytes")
    if len(ack.tail) not in (0, ACK_TAIL_SIZE):
        raise EncodeError(f"ACK tail must be empty or {ACK_TAIL_SIZE} bytes")
    return ack.checksum + ack.tail


def _check_buildable_ciphertext(ciphertext: bytes) -> None:
    if not ciphertext or len(ciphertext) % CIPHER_BLOCK_SIZE:
        raise EncodeError(
            f"ciphertext of {len(ciphertext)} bytes is not a positive multiple of "
            f"{CIPHER_BLOCK_SIZE}"
        )


def build_direct_envelope(envelope: DirectEnvelope) -> bytes:
    if len(envelope.mac) != CIPHER_MAC_SIZE:
        raise EncodeError(f"cipher MAC must be {CIPHER_MAC_SIZE} bytes")
    _check_buildable_ciphertext(envelope.ciphertext)
    return bytes([envelope.dest_hash, envelope.src_hash]) + envelope.mac + envelope.ciphertext


def build_anon_request(envelope: AnonRequestEnvelope) -> bytes:
    if len(envelope.sender_public_key) != PUB_KEY_SIZE:
        raise EncodeError(f"sender public key must be {PUB_KEY_SIZE} bytes")
    if len(envelope.mac) != CIPHER_MAC_SIZE:
        raise EncodeError(f"cipher MAC must be {CIPHER_MAC_SIZE} bytes")
    _check_buildable_ciphertext(envelope.ciphertext)
    return (
        bytes([envelope.dest_hash])
        + envelope.sender_public_key
        + envelope.mac
        + envelope.ciphertext
    )


def build_group_envelope(envelope: GroupEnvelope) -> bytes:
    if len(envelope.mac) != CIPHER_MAC_SIZE:
        raise EncodeError(f"cipher MAC must be {CIPHER_MAC_SIZE} bytes")
    _check_buildable_ciphertext(envelope.ciphertext)
    return bytes([envelope.channel_hash]) + envelope.mac + envelope.ciphertext


def build_payload(parsed: ParsedPayload) -> bytes:
    """Rebuild the wire bytes of any payload `parse_payload()` produced."""
    match parsed:
        case DirectEnvelope():
            return build_direct_envelope(parsed)
        case AnonRequestEnvelope():
            return build_anon_request(parsed)
        case GroupEnvelope():
            return build_group_envelope(parsed)
        case Acknowledgement():
            return build_ack(parsed)
        case Advert():
            return build_advert(parsed)
        case TracePayload():
            return parsed.raw
        case UnparsedPayload():
            return parsed.raw


# --- Bodies (stage 3: post-decryption) -------------------------------------


def _strip_padding(data: bytes) -> bytes:
    """Strip the trailing zeros AES-128-ECB block alignment left behind.

    Per design D6 this happens here, in a parser that knows the body's layout,
    never in the cipher — which cannot tell padding from a plaintext that
    genuinely ends in `0x00`.
    """
    return data.rstrip(b"\x00")


@dataclass(frozen=True, slots=True)
class TextMessageBody:
    """A decrypted text-message body (`payloads.md`, "Plain text message")."""

    timestamp: int
    txt_type: TextType | int
    attempt: int
    text: WireText
    sender_key_prefix: bytes | None = None

    @property
    def ack_prefix(self) -> bytes:
        """The body bytes the acknowledgement checksum is computed over.

        `BaseChatMesh.cpp:243` hashes `data[0 .. 5 + strlen(text))` — the
        timestamp, the flags byte, and the unpadded text.
        """
        flags = (int(self.txt_type) << 2) | self.attempt
        payload = (self.sender_key_prefix or b"") + self.text.raw
        return self.timestamp.to_bytes(4, "little") + bytes([flags]) + payload


def parse_text_message_body(body: bytes) -> DecodeResult[TextMessageBody]:
    """Parse a decrypted text-message body, stripping block padding from the text."""
    if len(body) < 5:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(body),
            raw=body,
            detail="text message body needs a 4-byte timestamp and a flags byte",
        )
    timestamp = int.from_bytes(body[:4], "little")
    flags = body[4]
    type_value = flags >> 2
    attempt = flags & 0x03
    try:
        txt_type: TextType | int = TextType(type_value)
    except ValueError:
        txt_type = type_value

    text_bytes = _strip_padding(body[5:])
    sender_key_prefix: bytes | None = None
    if txt_type is TextType.SIGNED_PLAIN:
        if len(text_bytes) < 4:
            return DecodeFailure(
                reason=FailureReason.TRUNCATED,
                offset=len(body),
                raw=body,
                detail="signed text message needs a 4-byte sender pubkey prefix",
            )
        sender_key_prefix = text_bytes[:4]
        text_bytes = text_bytes[4:]

    return TextMessageBody(
        timestamp=timestamp,
        txt_type=txt_type,
        attempt=attempt,
        text=WireText.from_bytes(text_bytes),
        sender_key_prefix=sender_key_prefix,
    )


def build_text_message_body(message: TextMessageBody) -> bytes:
    """Build a text-message body. Block padding is the cipher's job, not this one."""
    if not 0 <= message.attempt <= 3:
        raise EncodeError(f"attempt {message.attempt} does not fit in two bits")
    if not 0 <= int(message.txt_type) <= 0x3F:
        raise EncodeError(f"txt_type {message.txt_type} does not fit in six bits")
    if message.txt_type is TextType.SIGNED_PLAIN:
        if message.sender_key_prefix is None or len(message.sender_key_prefix) != 4:
            raise EncodeError("a signed text message needs a 4-byte sender key prefix")
    elif message.sender_key_prefix is not None:
        raise EncodeError("a sender key prefix is only carried by txt_type SIGNED_PLAIN")
    return message.ack_prefix


@dataclass(frozen=True, slots=True)
class GroupTextBody:
    """A decrypted GRP_TXT body.

    The sender name is whatever the sender typed. Group messages carry no
    signature, so any holder of the channel key can claim any name — hence the
    field name, which no consumer can read without seeing the claim.
    """

    message: TextMessageBody
    unverified_sender_name: str | None
    body: str


GROUP_NAME_SEPARATOR = ": "


def parse_group_text_body(body: bytes) -> DecodeResult[GroupTextBody]:
    """Parse a decrypted group message: a text message whose text is `name: body`."""
    message = parse_text_message_body(body)
    if isinstance(message, DecodeFailure):
        return message
    name, separator, remainder = message.text.text.partition(GROUP_NAME_SEPARATOR)
    if not separator:
        # No separator: report the whole text as the body rather than guess a split.
        return GroupTextBody(message=message, unverified_sender_name=None, body=message.text.text)
    return GroupTextBody(message=message, unverified_sender_name=name, body=remainder)


@dataclass(frozen=True, slots=True)
class ReturnedPathBody:
    """A decrypted PATH body: the route back, plus an optionally bundled extra.

    The path length byte uses the **same packed encoding as the packet header**
    — hop count in bits 0-5, hash size minus one in bits 6-7 (`Mesh.cpp:167-168`,
    `Packet::isValidPathLen`). `payloads.md` still documents it as a plain count
    of single-byte hashes, which is only true for the 1-byte case.

    `extra_raw` is the remainder verbatim, block padding included: the firmware
    passes it on that way and notes it "may be padded with zeroes"
    (`Mesh.cpp:172`), and nothing at this layer can tell padding from content.
    The bundled extra is already-decrypted content of `extra_type`, not another
    envelope, so it is not run through `parse_payload()`.
    """

    hop_count: int
    hash_size: int
    path: bytes
    extra_type: PayloadType | None = None
    extra_ack: Acknowledgement | None = None
    extra_raw: bytes = b""

    @property
    def hops(self) -> tuple[bytes, ...]:
        size = self.hash_size
        return tuple(self.path[i : i + size] for i in range(0, len(self.path), size))


def parse_returned_path_body(body: bytes) -> DecodeResult[ReturnedPathBody]:
    """Parse a decrypted PATH body (`payloads.md`, "Returned path")."""
    if not body:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=0,
            raw=body,
            detail="returned path body needs a path length byte",
        )
    path_length_byte = body[0]
    hop_count = path_length_byte & 0x3F
    hash_size_code = path_length_byte >> 6
    if hash_size_code == 0b11:
        return DecodeFailure(
            reason=FailureReason.RESERVED_HASH_SIZE,
            offset=0,
            raw=body,
            detail="returned path hash size code 0b11 is reserved",
        )
    hash_size = hash_size_code + 1
    path_extent = hop_count * hash_size
    if path_extent > MAX_PATH_SIZE:
        return DecodeFailure(
            reason=FailureReason.PATH_SIZE_LIMIT,
            offset=0,
            raw=body,
            detail=(f"returned path of {path_extent} bytes exceeds MAX_PATH_SIZE {MAX_PATH_SIZE}"),
        )

    path_end = 1 + path_extent
    if len(body) < path_end:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(body),
            raw=body,
            detail=f"path declares {path_extent} bytes but {len(body) - 1} remain",
        )
    path = body[1:path_end]

    remainder = body[path_end:]
    if not remainder:
        return ReturnedPathBody(hop_count=hop_count, hash_size=hash_size, path=path)

    # Upper 4 bits of the extra type byte are reserved for future use.
    extra_type = PayloadType(remainder[0] & 0x0F)
    extra_raw = remainder[1:]
    extra_ack: Acknowledgement | None = None
    if extra_type is PayloadType.ACK and len(extra_raw) >= ACK_CHECKSUM_SIZE:
        # The firmware reads only the leading 4 bytes here, since it cannot tell
        # the tail from padding either (`BaseChatMesh.cpp:336`).
        extra_ack = Acknowledgement(checksum=extra_raw[:ACK_CHECKSUM_SIZE])
    return ReturnedPathBody(
        hop_count=hop_count,
        hash_size=hash_size,
        path=path,
        extra_type=extra_type,
        extra_ack=extra_ack,
        extra_raw=extra_raw,
    )


def build_returned_path_body(path_body: ReturnedPathBody) -> bytes:
    if not 1 <= path_body.hash_size <= 3:
        raise EncodeError(f"returned path hash size {path_body.hash_size} is not encodable (1-3)")
    if not 0 <= path_body.hop_count <= 0x3F:
        raise EncodeError(f"returned path hop count {path_body.hop_count} does not fit in 6 bits")
    if len(path_body.path) != path_body.hop_count * path_body.hash_size:
        raise EncodeError(
            f"returned path is {len(path_body.path)} bytes but "
            f"{path_body.hop_count} hops of {path_body.hash_size} bytes needs "
            f"{path_body.hop_count * path_body.hash_size}"
        )
    packed = (path_body.hash_size - 1) << 6 | path_body.hop_count
    out = bytes([packed]) + path_body.path
    if path_body.extra_type is None:
        return out
    extra = path_body.extra_raw
    if not extra and path_body.extra_ack is not None:
        extra = build_ack(path_body.extra_ack)
    return out + bytes([int(path_body.extra_type)]) + extra


@dataclass(frozen=True, slots=True)
class RoomLoginBody:
    """A decrypted room-server login body (`payloads.md`, "Room server login")."""

    timestamp: int
    sync_timestamp: int
    password: WireText


def parse_room_login_body(body: bytes) -> DecodeResult[RoomLoginBody]:
    if len(body) < 8:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(body),
            raw=body,
            detail="room login body needs two 4-byte timestamps",
        )
    return RoomLoginBody(
        timestamp=int.from_bytes(body[:4], "little"),
        sync_timestamp=int.from_bytes(body[4:8], "little"),
        password=WireText.from_bytes(_strip_padding(body[8:])),
    )


def build_room_login_body(login: RoomLoginBody) -> bytes:
    return (
        login.timestamp.to_bytes(4, "little")
        + login.sync_timestamp.to_bytes(4, "little")
        + login.password.raw
    )


# --- What a room server answers with (milestone 6) --------------------------


ROOM_LOGIN_RESPONSE_SIZE = 13
"""`MyMesh.cpp:382-394` writes exactly this many bytes and passes 13 to
`createDatagram`."""

RESP_SERVER_LOGIN_OK = 0
"""`MyMesh.cpp:20`. The only result code the room server ever sends: a login
that fails is answered with silence, not with a code."""

FIRMWARE_VER_LEVEL = 1
"""The protocol level we implement (`MyMesh.cpp:13`, MeshCore v1.17.1)."""


class ClientKind(IntEnum):
    """Byte 6 of the login response, computed at `MyMesh.cpp:387`.

    Derived from the permission byte rather than independent of it: `1` for an
    administrator, `2` for a member whose whole permission byte is zero — the
    read-only spectator — and `0` for everyone else.
    """

    MEMBER = 0
    ADMIN = 1
    SPECTATOR = 2

    @classmethod
    def for_permissions(cls, permissions: int) -> ClientKind:
        if permissions & PERMISSION_ROLE_MASK == Permission.ADMIN:
            return cls.ADMIN
        if permissions == 0:
            return cls.SPECTATOR
        return cls.MEMBER


@dataclass(frozen=True, slots=True)
class RoomLoginResponseBody:
    """The 13 bytes a room server returns for a successful login.

    Byte 7 is the one worth knowing about: firmware older than `v1.17.1` reads it
    as the client's unsynced count, where this version puts the permission byte
    (`MyMesh.cpp:390`, whose own comment keeps the legacy line beside the new
    one). We emit the current form; a client old enough to disagree shows a
    wrong badge rather than failing to log in.

    `blob` is four random bytes whose only job is to make the packet hash unique
    (`:391`), so a retried reply is not deduplicated as the first one.
    """

    server_timestamp: int
    result: int = RESP_SERVER_LOGIN_OK
    keep_alive_interval: int = 0
    """Byte 5. Legacy: was a recommended keep-alive interval in units of 16 s,
    and is written as zero (`MyMesh.cpp:386`)."""

    client_kind: ClientKind | int = ClientKind.MEMBER
    permissions: int = 0
    blob: bytes = b"\x00\x00\x00\x00"
    protocol_level: int = FIRMWARE_VER_LEVEL


def parse_room_login_response_body(body: bytes) -> DecodeResult[RoomLoginResponseBody]:
    if len(body) < ROOM_LOGIN_RESPONSE_SIZE:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(body),
            raw=body,
            detail=(
                f"room login response is {ROOM_LOGIN_RESPONSE_SIZE} bytes and {len(body)} arrived"
            ),
        )
    try:
        client_kind: ClientKind | int = ClientKind(body[6])
    except ValueError:
        client_kind = body[6]
    return RoomLoginResponseBody(
        server_timestamp=int.from_bytes(body[:4], "little"),
        result=body[4],
        keep_alive_interval=body[5],
        client_kind=client_kind,
        permissions=body[7],
        blob=body[8:12],
        protocol_level=body[12],
    )


def build_room_login_response_body(response: RoomLoginResponseBody) -> bytes:
    if len(response.blob) != 4:
        raise EncodeError(f"the login response blob is 4 bytes and {len(response.blob)} were given")
    for name, value in (
        ("result", response.result),
        ("keep_alive_interval", response.keep_alive_interval),
        ("client_kind", int(response.client_kind)),
        ("permissions", response.permissions),
        ("protocol_level", response.protocol_level),
    ):
        if not 0 <= int(value) <= 0xFF:
            raise EncodeError(f"login response {name} {value} is not a byte")
    return (
        response.server_timestamp.to_bytes(4, "little")
        + bytes(
            [
                response.result,
                response.keep_alive_interval,
                int(response.client_kind),
                response.permissions,
            ]
        )
        + response.blob
        + bytes([response.protocol_level])
    )


class RequestType(IntEnum):
    """`MyMesh.cpp:15-18`. `GET_ACCESS_LIST` is named and not implemented."""

    GET_STATUS = 0x01
    KEEP_ALIVE = 0x02
    GET_TELEMETRY_DATA = 0x03
    GET_ACCESS_LIST = 0x05


@dataclass(frozen=True, slots=True)
class RequestBody:
    """A decrypted `REQ` body: a timestamp, a request type, and its arguments.

    `arguments` is the remainder verbatim, block padding included. Nothing at
    this layer can tell padding from content — the firmware has the same problem
    and solves it per request type (`MyMesh.cpp:558-563` reads a keep-alive's
    optional position only when the body is long enough, and notes that what it
    reads "may be 0, if part of decrypted PADDING"). A type this codec does not
    interpret keeps its arguments so the caller can report them.
    """

    timestamp: int
    request_type: RequestType | int
    arguments: bytes = b""

    @property
    def keep_alive_since(self) -> int | None:
        """The sync position a keep-alive may carry, or None when it carries none.

        Zero is None here rather than a position: the firmware treats
        `forceSince > 0` as the condition for adopting it (`:565`), precisely
        because a zero it read may be padding rather than a claim.
        """
        if self.request_type != RequestType.KEEP_ALIVE or len(self.arguments) < 4:
            return None
        since = int.from_bytes(self.arguments[:4], "little")
        return since or None


def parse_request_body(body: bytes) -> DecodeResult[RequestBody]:
    if len(body) < 5:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(body),
            raw=body,
            detail="request body needs a 4-byte timestamp and a request type byte",
        )
    try:
        request_type: RequestType | int = RequestType(body[4])
    except ValueError:
        request_type = body[4]
    return RequestBody(
        timestamp=int.from_bytes(body[:4], "little"),
        request_type=request_type,
        arguments=body[5:],
    )


def build_request_body(request: RequestBody) -> bytes:
    if not 0 <= int(request.request_type) <= 0xFF:
        raise EncodeError(f"request type {request.request_type} is not a byte")
    return (
        request.timestamp.to_bytes(4, "little")
        + bytes([int(request.request_type)])
        + request.arguments
    )


SERVER_STATS_SIZE = 52
"""`sizeof(ServerStats)` at `MyMesh.cpp:24-39`: four 16-bit fields, eight 32-bit
fields and six 16-bit fields, naturally aligned with no padding."""

STATUS_BODY_SIZE = 4 + SERVER_STATS_SIZE
"""The whole answer: the echoed request timestamp, then the struct (`:155`, `:178`)."""

_SERVER_STATS_FIELDS: tuple[tuple[str, int, bool], ...] = (
    ("batt_milli_volts", 2, False),
    ("curr_tx_queue_len", 2, False),
    ("noise_floor", 2, True),
    ("last_rssi", 2, True),
    ("n_packets_recv", 4, False),
    ("n_packets_sent", 4, False),
    ("total_air_time_secs", 4, False),
    ("total_up_time_secs", 4, False),
    ("n_sent_flood", 4, False),
    ("n_sent_direct", 4, False),
    ("n_recv_flood", 4, False),
    ("n_recv_direct", 4, False),
    ("err_events", 2, False),
    ("last_snr", 2, True),
    ("n_direct_dups", 2, False),
    ("n_flood_dups", 2, False),
    ("n_posted", 2, False),
    ("n_post_push", 2, False),
)

SERVER_STATS_OFFSETS = {
    name: sum(size for _, size, _ in _SERVER_STATS_FIELDS[:index])
    for index, (name, _, _) in enumerate(_SERVER_STATS_FIELDS)
}
"""Each field's offset within the 52-byte struct, so a test can assert layout
rather than only round-tripping."""

ROOM_SERVER_DIVERGENT_OFFSETS = (48, 52)
"""**The four bytes two implementations disagree about** (design D13).

At offsets 48..52 of the struct a *room server* writes `n_posted` and
`n_post_push` (`MyMesh.cpp:175-176`). `meshcore_py`'s generic `parse_status`
reads the same four bytes as a repeater's `rx_airtime`, because that is what a
repeater puts there. Both readings are correct for their own firmware and there
is nothing to reconcile.

We emit what the room-server firmware emits, because interop is with the
firmware: a client using the generic parser will misread these four bytes
against a stock room server exactly as it will against ours. This constant and
the test that names both interpretations exist so the divergence cannot later be
"fixed" into a bug.
"""


@dataclass(frozen=True, slots=True)
class ServerStats:
    """The room server's own statistics struct, field for field.

    Which of these sighop can honestly fill is a separate question from how they
    are encoded, and it is answered where the values come from rather than here
    — this is a byte layout and knows nothing about rooms or radios.
    """

    batt_milli_volts: int = 0
    curr_tx_queue_len: int = 0
    noise_floor: int = 0
    last_rssi: int = 0
    n_packets_recv: int = 0
    n_packets_sent: int = 0
    total_air_time_secs: int = 0
    total_up_time_secs: int = 0
    n_sent_flood: int = 0
    n_sent_direct: int = 0
    n_recv_flood: int = 0
    n_recv_direct: int = 0
    err_events: int = 0
    last_snr: int = 0
    """Signed, and scaled by four (`MyMesh.cpp:172`)."""

    n_direct_dups: int = 0
    n_flood_dups: int = 0
    n_posted: int = 0
    n_post_push: int = 0


@dataclass(frozen=True, slots=True)
class StatusBody:
    """A status answer: the request's timestamp echoed as a tag, then the stats."""

    tag: int
    stats: ServerStats


def build_status_body(status: StatusBody) -> bytes:
    out = bytearray(status.tag.to_bytes(4, "little"))
    for name, size, signed in _SERVER_STATS_FIELDS:
        value = int(getattr(status.stats, name))
        try:
            out += value.to_bytes(size, "little", signed=signed)
        except OverflowError as error:
            raise EncodeError(
                f"server stats {name}={value} does not fit in {size} "
                f"{'signed' if signed else 'unsigned'} bytes"
            ) from error
    return bytes(out)


def parse_status_body(body: bytes) -> DecodeResult[StatusBody]:
    if len(body) < STATUS_BODY_SIZE:
        return DecodeFailure(
            reason=FailureReason.TRUNCATED,
            offset=len(body),
            raw=body,
            detail=(f"a status body is {STATUS_BODY_SIZE} bytes and {len(body)} arrived"),
        )
    values: dict[str, int] = {}
    offset = 4
    for name, size, signed in _SERVER_STATS_FIELDS:
        values[name] = int.from_bytes(body[offset : offset + size], "little", signed=signed)
        offset += size
    return StatusBody(tag=int.from_bytes(body[:4], "little"), stats=ServerStats(**values))


TELEM_CHANNEL_SELF = 1
"""`SensorManager.h:10`: the LPP channel a device reports itself on."""


class LppType(IntEnum):
    """The CayenneLPP data types this milestone emits.

    sighop exposes no external sensors, so this is the whole set: what the board
    told us about itself and nothing else.
    """

    TEMPERATURE = 0x67
    VOLTAGE = 0x74


@dataclass(frozen=True, slots=True)
class TelemetryEntry:
    """One CayenneLPP reading: a channel, a type, and a scaled integer value."""

    channel: int
    lpp_type: LppType
    value: int
    """Already in the type's own units — hundredths of a volt, tenths of a
    degree — because rounding a float is a decision the caller makes once, where
    the reading came from, rather than one this codec makes silently."""


def voltage_entry(volts: float, *, channel: int = TELEM_CHANNEL_SELF) -> TelemetryEntry:
    return TelemetryEntry(channel=channel, lpp_type=LppType.VOLTAGE, value=round(volts * 100))


def temperature_entry(celsius: float, *, channel: int = TELEM_CHANNEL_SELF) -> TelemetryEntry:
    return TelemetryEntry(channel=channel, lpp_type=LppType.TEMPERATURE, value=round(celsius * 10))


_LPP_WIDTHS = {LppType.VOLTAGE: (2, False), LppType.TEMPERATURE: (2, True)}


def build_telemetry_frame(entries: Sequence[TelemetryEntry]) -> bytes:
    """Build a CayenneLPP frame: channel, type, value — **most significant byte
    first** (design D14).

    This is the one place in MeshCore where the byte order is big-endian, and it
    is the detail that gets built wrong: every other integer in the protocol is
    little-endian, so a frame written with the project's usual order parses as
    garbage on the client and looks like a hardware fault.
    """
    out = bytearray()
    for entry in entries:
        if not 0 <= entry.channel <= 0xFF:
            raise EncodeError(f"telemetry channel {entry.channel} is not a byte")
        size, signed = _LPP_WIDTHS[entry.lpp_type]
        try:
            encoded = entry.value.to_bytes(size, "big", signed=signed)
        except OverflowError as error:
            raise EncodeError(
                f"telemetry {entry.lpp_type.name} value {entry.value} does not fit "
                f"in {size} {'signed' if signed else 'unsigned'} bytes"
            ) from error
        out += bytes([entry.channel, int(entry.lpp_type)]) + encoded
    return bytes(out)


def parse_telemetry_frame(frame: bytes) -> DecodeResult[list[TelemetryEntry]]:
    """Parse a frame back. Only the types above are known; anything else stops it."""
    entries: list[TelemetryEntry] = []
    offset = 0
    while offset < len(frame):
        if len(frame) - offset < 2:
            return DecodeFailure(
                reason=FailureReason.TRUNCATED,
                offset=offset,
                raw=frame,
                detail="a telemetry entry needs a channel byte and a type byte",
            )
        channel, raw_type = frame[offset], frame[offset + 1]
        try:
            lpp_type = LppType(raw_type)
        except ValueError:
            return DecodeFailure(
                reason=FailureReason.PAYLOAD_TYPE_MISMATCH,
                offset=offset + 1,
                raw=frame,
                detail=f"telemetry type 0x{raw_type:02x} is not one this codec emits",
            )
        size, signed = _LPP_WIDTHS[lpp_type]
        start = offset + 2
        if len(frame) - start < size:
            return DecodeFailure(
                reason=FailureReason.TRUNCATED,
                offset=start,
                raw=frame,
                detail=f"telemetry {lpp_type.name} needs {size} value bytes",
            )
        entries.append(
            TelemetryEntry(
                channel=channel,
                lpp_type=lpp_type,
                value=int.from_bytes(frame[start : start + size], "big", signed=signed),
            )
        )
        offset = start + size
    return entries
