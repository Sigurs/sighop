"""The synthetic protocol corpus: a generated mesh, written as capture JSONL.

The corpus used to be live LoRa traffic recorded off a real mesh. That put other
people's node names, keys and Public-channel chat in the repository, and it made
coverage a matter of volume: about a third of the receptions were flood repeats of
a packet already seen. This module builds a fictional mesh instead — seeded
identities, `syn-` prefixed names, written-out chat lines — with sighop's own
encoders, and writes it in the format `CaptureWriter` writes, so every consumer
of a capture file reads it unchanged (`synthetic-corpus`, `protocol-corpus`).

Determinism is the contract. Every identity, timestamp and signal reading is drawn
from `SEED` and from nothing else, so the same seed and `GENERATOR_VERSION` give
the same bytes, and a change to the corpus is a change to this module that shows up
as a diff. `test_synthetic_corpus.py` regenerates in memory and compares.

Regenerate the committed files deliberately, and review the diff:

    uv run python -m tests.protocol.generate_corpus

**What a synthetic corpus proves.** It is written by sighop's own encoders, so a
green run shows the decoders agree with the encoders and with the expectations
recorded in the tests. It does not show interoperability with any other MeshCore
implementation; `CORPUS.md` says so.

**Repetition is declared.** The recorded corpus was a third repeats. Here every
duplicate goes through `_File.repeat`, which takes a `reason`, so each one is
traceable to a line that says why a test needs it. Everything else is unique.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from sighop.net.airtime import time_on_air_ms
from sighop.protocol.crypto import (
    PUBLIC_CHANNEL_KEY,
    ChannelKey,
    ack_checksum_for,
    calc_shared_secret,
    channel_key_from_hashtag,
    encrypt_then_mac,
    sign_advert,
)
from sighop.protocol.identity import LocalIdentity
from sighop.protocol.packet import (
    PAYLOAD_VERSION_1,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
    decode,
    encode,
)
from sighop.protocol.payloads import (
    Acknowledgement,
    AnonRequestEnvelope,
    ClientKind,
    DirectEnvelope,
    DiscoverRequest,
    DiscoverResponse,
    GroupEnvelope,
    NodeType,
    Permission,
    RequestBody,
    RequestType,
    ReturnedPathBody,
    RoomLoginBody,
    RoomLoginResponseBody,
    TextMessageBody,
    TextType,
    WireText,
    build_ack,
    build_advert,
    build_anon_request,
    build_appdata,
    build_direct_envelope,
    build_discover_request,
    build_discover_response,
    build_group_envelope,
    build_group_text_body,
    build_request_body,
    build_returned_path_body,
    build_room_login_body,
    build_room_login_response_body,
    build_telemetry_frame,
    build_text_message_body,
    parse_appdata,
    temperature_entry,
    voltage_entry,
)
from sighop.protocol.result import DecodeFailure
from sighop.radio.modem import EU868_NARROW
from tests.protocol.corpus import AMBIENT, CHANNELS, EXCHANGE

SEED = 20260926
GENERATOR_VERSION = 1
"""Bump when the generator's output is meant to change. The drift test compares
bytes, so a bump is not what makes it fail — a changed file is — but the version
in every `capture_meta` says which generator wrote what is committed."""

AMBIENT_START = dt.datetime(2026, 9, 5, 17, 0, tzinfo=dt.UTC)
CHANNELS_START = dt.datetime(2026, 9, 5, 19, 30, tzinfo=dt.UTC)
EXCHANGE_START = dt.datetime(2026, 9, 5, 21, 0, tzinfo=dt.UTC)

REPEAT_CEILING = 0.12
"""At most this share of the corpus's receptions may repeat a packet already seen."""
MAX_COPIES = 3
"""No packet has more copies than this."""
LATE_ECHO_MIN_SECONDS = 60.0
RETRANSMIT_MIN_SECONDS = 30 * 60.0

CLUB_HASHTAG = "#syn-hill-club"
"""The second synthetic channel. Its key is derived from a public name, so it is
addressing, not confidentiality — the point is only that its channel hash is not the
Public channel's `0x11`, nor the `0x17` a mis-taken hash would give."""

ROOM_PASSWORD = "syn-room-password"
GUEST_PASSWORD = "syn-guest"
SYNTHETIC_RADIO = {
    "freq_hz": EU868_NARROW.freq_hz,
    "bw_hz": EU868_NARROW.bw_hz,
    "sf": EU868_NARROW.sf,
    "cr": EU868_NARROW.cr,
}


# --- The cast ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CastNode:
    """One fictional node: a seeded identity, a `syn-` name, an advert shape."""

    name: str
    role: str
    node_type: NodeType
    identity: LocalIdentity
    location: tuple[int, int] | None
    """Micro-degrees, in a small square around 0°N 0°E — plainly fictional."""

    @property
    def public_key(self) -> bytes:
        return self.identity.public_key

    def hash_bytes(self, size: int) -> bytes:
        """The leading bytes of the key a repeater writes into a path."""
        return self.identity.public_key[:size]

    @property
    def appdata(self) -> bytes:
        latitude, longitude = self.location if self.location else (None, None)
        return build_appdata(self.node_type, latitude=latitude, longitude=longitude, name=self.name)


REPEATER_NAMES = (
    "syn-ridge-repeater",
    "syn-harbor-repeater",
    "syn-tower-repeater",
    "syn-mill-repeater",
    "syn-orchard-repeater",
    "syn-quarry-repeater",
    "syn-summit-repeater",
    "syn-valley-repeater",
    "syn-bridge-repeater",
    "syn-lighthouse-repeater",
    "syn-meadow-repeater",
    "syn-ferry-repeater",
)
ROOM_NAMES = ("syn-cellar-room", "syn-attic-room", "syn-lounge-room")
"""The third is the room sighop hosts in the exchange file, and it advertises with
no location, so the corpus holds the room-server-without-a-location shape."""
CHAT_NAMES = (
    "syn-alder",
    "syn-birch",
    "syn-cedar",
    "syn-dogwood",
    "syn-elm",
    "syn-fir",
    "syn-gorse",
    "syn-hazel",
)
SIGHOP_NAME = "syn-sighop"
PEER_NAME = "syn-peer"


@dataclass(frozen=True, slots=True)
class Cast:
    """Every identity in the corpus, derived from one seed."""

    seed: int
    repeaters: tuple[CastNode, ...]
    rooms: tuple[CastNode, ...]
    chat: tuple[CastNode, ...]
    sighop: CastNode
    peer: CastNode

    @property
    def everyone(self) -> tuple[CastNode, ...]:
        return (*self.repeaters, *self.rooms, *self.chat, self.sighop, self.peer)

    @property
    def lounge(self) -> CastNode:
        return self.rooms[2]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(node.name for node in self.everyone)

    @property
    def public_keys(self) -> frozenset[bytes]:
        return frozenset(node.public_key for node in self.everyone)

    def by_key(self, public_key: bytes) -> CastNode | None:
        for node in self.everyone:
            if node.public_key == public_key:
                return node
        return None


def _identity(seed: int, role: str, n: int, taken: set[int]) -> LocalIdentity:
    """A seeded identity whose first byte no other cast member has.

    A 1-byte path hash names one node, so every identity in the cast gets its own,
    and neither reserved value (`0x00`, `0xFF`) is used. The counter only moves
    when a first byte collides, and it moves the same way every run.
    """
    attempt = 0
    while True:
        digest = hashlib.sha256(f"{seed}:{role}:{n}:{attempt}".encode()).digest()
        identity = LocalIdentity.from_seed(digest)
        first = identity.public_key[0]
        if first not in taken and first not in (0x00, 0xFF):
            taken.add(first)
            return identity
        attempt += 1


def build_cast(seed: int = SEED) -> Cast:
    taken: set[int] = set()

    def node(
        role: str, n: int, name: str, node_type: NodeType, located: bool, slot: int
    ) -> CastNode:
        location = (11_000 + slot * 4_700, 23_000 + slot * 3_100) if located else None
        return CastNode(
            name=name,
            role=role,
            node_type=node_type,
            identity=_identity(seed, role, n, taken),
            location=location,
        )

    repeaters = tuple(
        node("repeater", n, name, NodeType.REPEATER, True, n)
        for n, name in enumerate(REPEATER_NAMES)
    )
    rooms = tuple(
        node("room", n, name, NodeType.ROOM_SERVER, n < 2, 20 + n)
        for n, name in enumerate(ROOM_NAMES)
    )
    chat = tuple(
        node("chat", n, name, NodeType.CHAT, n < 4, 30 + n) for n, name in enumerate(CHAT_NAMES)
    )
    sighop = node("local", 0, SIGHOP_NAME, NodeType.CHAT, False, 40)
    peer = node("local", 1, PEER_NAME, NodeType.CHAT, False, 41)
    return Cast(seed, repeaters, rooms, chat, sighop, peer)


# --- The written-out text ---------------------------------------------------

PUBLIC_MESSAGES: tuple[tuple[str, str], ...] = (
    ("syn-alder", "morning all, radio check from the hill"),
    ("syn-birch", "hearing you at two hops, signal is steady"),
    ("syn-cedar", "anyone up for a range test this weekend?"),
    ("syn-dogwood", "count me in, I can bring the tall antenna"),
    ("syn-elm", "my node rebooted overnight, back on air now"),
    ("syn-fir", "fog is thick over the harbour today"),
    ("syn-gorse", "does the tower repeater still take three hops?"),
    ("syn-hazel", "yes, three hops and it forwards fine"),
    ("syn-alder", "testing a new battery, will report in a week"),
    ("syn-birch", "the mill site has power again"),
    ("syn-cedar", "please keep adverts to once an hour"),
    ("syn-dogwood", "agreed, the band gets busy in the evening"),
    ("syn-elm", "good night everyone"),
    ("syn-fir", "morning, the fog cleared by nine"),
    ("syn-gorse", "swapped my antenna, hearing more nodes already"),
    ("syn-hazel", "nice, how many do you count now?"),
    ("syn-alder", "twelve repeaters and a handful of chat nodes"),
    ("syn-birch", "the quarry node needs a new cable"),
    ("syn-cedar", "I can look at it on Saturday"),
    ("syn-dogwood", "thanks, the mount is the rusty one"),
    ("syn-elm", "reminder that the club net starts at eight"),
    ("syn-fir", "which channel is the net on?"),
    ("syn-gorse", "hill-club, same as last time"),
    ("syn-hazel", "got it, see you there"),
    ("syn-alder", "signal report: strong from the ridge"),
    ("syn-birch", "the lighthouse is barely in range from here"),
    ("syn-cedar", "try pointing north a little"),
    ("syn-dogwood", "that helped, thank you"),
    ("syn-elm", "battery is at eighty percent"),
    ("syn-fir", "weather turning, bringing the mast down"),
    ("syn-gorse", "safe travels"),
    ("syn-hazel", "see you all next week"),
)
CLUB_MESSAGES: tuple[tuple[str, str], ...] = (
    ("syn-alder", "net is open, roll call please"),
    ("syn-birch", "birch checking in"),
    ("syn-cedar", "cedar here, two hops from the ridge"),
    ("syn-dogwood", "dogwood checking in"),
    ("syn-elm", "elm present"),
    ("syn-fir", "fir present, low battery"),
    ("syn-gorse", "gorse here"),
    ("syn-hazel", "hazel here, weak signal"),
    ("syn-alder", "topic tonight: repeater placement"),
    ("syn-birch", "the harbour repeater covers the south well"),
    ("syn-cedar", "the summit one covers the north"),
    ("syn-dogwood", "gap in the middle valley"),
    ("syn-elm", "I can host a node near the orchard"),
    ("syn-fir", "the orchard has good line of sight"),
    ("syn-gorse", "power there is only a solar panel"),
    ("syn-hazel", "a small panel is enough for a node"),
    ("syn-alder", "any objections to a new repeater?"),
    ("syn-birch", "none from me"),
    ("syn-cedar", "none"),
    ("syn-dogwood", "go ahead"),
    ("syn-alder", "net closed, thanks all"),
    ("syn-elm", "thanks alder"),
)
GROUP_DATA_PAYLOADS: tuple[bytes, ...] = (
    b"syn-data:status:ok",
    b"syn-data:beacon:hill",
    b"syn-data:ping:1",
    b"syn-data:ping:2",
)
DIRECT_MESSAGES: tuple[str, ...] = (
    "are you around this evening?",
    "yes, I will be on the air until ten",
    "can you forward the range test notes?",
    "sending them now",
    "got the notes, thanks",
    "the harbour repeater dropped for a minute",
    "it came back on its own",
    "battery swap done",
    "please confirm the schedule",
    "confirmed for saturday",
    "meeting at the quarry",
    "I am five minutes away",
    "bring the spare cable",
    "will do",
    "how is the signal from your side?",
    "strong, two hops",
    "thanks for the help",
    "no problem",
    "see you tomorrow",
    "good night",
    "the mount is fixed",
    "great, testing now",
    "all working",
    "talk later",
)
EXCHANGE_MESSAGES: dict[str, str] = {
    "sighop_first": "first message from sighop",
    "peer_first": "hello sighop, this is the peer",
    "peer_flooded": "your node came up on my list",
    "sighop_reply": "thanks, I can hear you at one hop",
    "sighop_routed": "sending this over the learned route",
    "peer_long": "the retransmitted message: did you get the schedule for saturday?",
    "peer_after": "one more from the peer, after the retry",
    "peer_scan": "checking the ack forms",
    "sighop_scan": "acknowledged with the four byte form",
}
ROOM_POSTS: tuple[tuple[str, str], ...] = (
    ("peer", "first post to the lounge"),
    ("peer", "second post, a little longer than the first one"),
    ("guest", "a guest post the room does not accept"),
    ("guest", "a guest post the room does not accept, retried"),
    ("peer", "third post after the guest"),
)
ROOM_PUSHES: tuple[tuple[str, str], ...] = (
    ("syn-cedar", "welcome to the lounge"),
    ("syn-elm", "the net starts at eight"),
)


def cast_texts() -> frozenset[str]:
    """Every text the corpus can decrypt to, for the no-real-data guard."""
    texts: set[str] = {text for _, text in PUBLIC_MESSAGES}
    texts |= {text for _, text in CLUB_MESSAGES}
    texts |= set(DIRECT_MESSAGES)
    texts |= set(EXCHANGE_MESSAGES.values())
    texts |= {text for _, text in ROOM_POSTS}
    texts |= {text for _, text in ROOM_PUSHES}
    texts |= {ROOM_PASSWORD, GUEST_PASSWORD}
    return frozenset(texts)


def cast_senders(cast: Cast) -> frozenset[str]:
    return cast.names


def club_key() -> ChannelKey:
    return channel_key_from_hashtag(CLUB_HASHTAG)


# --- Packet building --------------------------------------------------------


def _packet(
    route: RouteType,
    payload_type: PayloadType,
    payload: bytes,
    *,
    relays: Sequence[CastNode] = (),
    hash_size: int = 1,
    codes: tuple[int, int] | None = None,
) -> bytes:
    """Encode a frame whose path is the given relays' leading key bytes."""
    path = b"".join(node.hash_bytes(hash_size) for node in relays)
    return encode(
        Packet(
            header=PacketHeader(route, payload_type, PAYLOAD_VERSION_1),
            transport_codes=codes,
            hop_count=len(relays),
            hash_size=hash_size,
            path=path,
            payload=payload,
        )
    )


def _flood(
    payload_type: PayloadType,
    payload: bytes,
    relays: Sequence[CastNode] = (),
    hash_size: int = 1,
) -> bytes:
    return _packet(RouteType.FLOOD, payload_type, payload, relays=relays, hash_size=hash_size)


def _direct(
    payload_type: PayloadType,
    payload: bytes,
    relays: Sequence[CastNode] = (),
    hash_size: int = 1,
) -> bytes:
    return _packet(RouteType.DIRECT, payload_type, payload, relays=relays, hash_size=hash_size)


def _advert_payload(node: CastNode, timestamp: int) -> bytes:
    return build_advert(sign_advert(node.identity, timestamp, node.appdata))


def _text_plaintext(
    timestamp: int,
    text: str,
    *,
    attempt: int = 0,
    txt_type: TextType = TextType.PLAIN,
    author_prefix: bytes | None = None,
) -> tuple[bytes, TextMessageBody]:
    body = TextMessageBody(
        timestamp=timestamp,
        txt_type=txt_type,
        attempt=attempt,
        text=WireText.from_bytes(text.encode("utf-8")),
        sender_key_prefix=author_prefix,
    )
    return build_text_message_body(body), body


def _direct_payload(
    payload_type: PayloadType, sender: CastNode, recipient: CastNode, plaintext: bytes
) -> bytes:
    secret = calc_shared_secret(sender.identity, recipient.public_key)
    mac, ciphertext = encrypt_then_mac(secret, plaintext)
    return build_direct_envelope(
        DirectEnvelope(
            payload_type=payload_type,
            dest_hash=recipient.identity.node_hash,
            src_hash=sender.identity.node_hash,
            mac=mac,
            ciphertext=ciphertext,
        )
    )


def text_payload(
    sender: CastNode,
    recipient: CastNode,
    timestamp: int,
    text: str,
    *,
    attempt: int = 0,
    txt_type: TextType = TextType.PLAIN,
    author_prefix: bytes | None = None,
) -> tuple[bytes, TextMessageBody]:
    """A `TXT_MSG` payload, and the parsed body its acknowledgement answers."""
    plaintext, body = _text_plaintext(
        timestamp, text, attempt=attempt, txt_type=txt_type, author_prefix=author_prefix
    )
    return _direct_payload(PayloadType.TXT_MSG, sender, recipient, plaintext), body


def ack_payload(message: TextMessageBody, sender: CastNode, *, tail: bytes = b"") -> bytes:
    """The acknowledgement of `message`, which `sender` sent (its key is hashed in)."""
    return build_ack(
        Acknowledgement(checksum=ack_checksum_for(message, sender.public_key), tail=tail)
    )


def path_payload(
    sender: CastNode,
    recipient: CastNode,
    returned: Sequence[CastNode],
    hash_size: int,
    *,
    ack: Acknowledgement | None = None,
    response: bytes | None = None,
) -> bytes:
    """A returned `PATH`, bundling an acknowledgement or a response."""
    path = b"".join(node.hash_bytes(hash_size) for node in returned)
    if ack is None and response is None:
        body = ReturnedPathBody(hop_count=len(returned), hash_size=hash_size, path=path)
    elif ack is not None:
        body = ReturnedPathBody(
            hop_count=len(returned),
            hash_size=hash_size,
            path=path,
            extra_type=PayloadType.ACK,
            extra_ack=ack,
        )
    else:
        body = ReturnedPathBody(
            hop_count=len(returned),
            hash_size=hash_size,
            path=path,
            extra_type=PayloadType.RESPONSE,
            extra_raw=response or b"",
        )
    return _direct_payload(PayloadType.PATH, sender, recipient, build_returned_path_body(body))


def login_payload(
    client: CastNode, server: CastNode, timestamp: int, password: str, sync_timestamp: int = 0
) -> bytes:
    secret = calc_shared_secret(client.identity, server.public_key)
    plaintext = build_room_login_body(
        RoomLoginBody(
            timestamp=timestamp,
            sync_timestamp=sync_timestamp,
            password=WireText.from_bytes(password.encode()),
        )
    )
    mac, ciphertext = encrypt_then_mac(secret, plaintext)
    return build_anon_request(
        AnonRequestEnvelope(
            dest_hash=server.identity.node_hash,
            sender_public_key=client.public_key,
            mac=mac,
            ciphertext=ciphertext,
        )
    )


def group_payload(
    key: ChannelKey,
    payload_type: PayloadType,
    plaintext: bytes,
) -> bytes:
    mac, ciphertext = encrypt_then_mac(key.secret, plaintext)
    return build_group_envelope(
        GroupEnvelope(
            payload_type=payload_type,
            channel_hash=key.channel_hash,
            mac=mac,
            ciphertext=ciphertext,
        )
    )


def group_text_payload(key: ChannelKey, timestamp: int, sender: str, text: str) -> bytes:
    return group_payload(key, PayloadType.GRP_TXT, build_group_text_body(timestamp, sender, text))


DISCOVER_TAG = bytes.fromhex("9a7d3916")
"""A discovery tag the hand-built fixtures share. Tags are random on the wire, so
this identifies nobody."""


def discover_responder(seed: int = SEED) -> CastNode:
    """The cast repeater the hand-built discovery fixtures name as the responder."""
    return build_cast(seed).repeaters[0]


def discover_response_payload(
    *, tag: bytes = DISCOVER_TAG, snr_quarter_db: int = 47, key: bytes | None = None
) -> bytes:
    """A full-key `NODE_DISCOVER_RESP` payload (38 bytes) from the cast's repeater.

    The default reports `+11.75 dB`, the value the fixtures that call this assert.
    """
    return build_discover_response(
        DiscoverResponse(
            node_type=int(NodeType.REPEATER),
            snr_quarter_db=snr_quarter_db,
            tag=tag,
            claimed_key=key if key is not None else discover_responder().public_key,
        )
    )


def request_payload(
    client: CastNode, server: CastNode, timestamp: int, request_type: RequestType
) -> bytes:
    plaintext = build_request_body(RequestBody(timestamp=timestamp, request_type=request_type))
    return _direct_payload(PayloadType.REQ, client, server, plaintext)


def telemetry_response_payload(
    server: CastNode, client: CastNode, timestamp: int, volts: float
) -> bytes:
    plaintext = timestamp.to_bytes(4, "little") + build_telemetry_frame(
        [voltage_entry(volts), temperature_entry(18.5)]
    )
    return _direct_payload(PayloadType.RESPONSE, server, client, plaintext)


# --- Records ----------------------------------------------------------------


@dataclass(slots=True)
class _Item:
    order: int
    at: float
    kind: str  # "rx" | "tx" | "unparsed"
    raw: bytes
    snr: float | None = None
    rssi: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DeclaredRepeat:
    """A duplicate the generator emitted on purpose, and why."""

    file: str
    reason: str
    of: int
    copy: int


@dataclass(slots=True)
class _File:
    """One corpus file under construction.

    Times are seconds from the file's start. Signal readings come from a
    `random.Random` seeded with this file's name, so adding a frame to one file
    does not perturb another's.
    """

    name: str
    start: dt.datetime
    seed: int
    rng: random.Random = field(init=False)
    items: list[_Item] = field(default_factory=list)
    repeats: list[DeclaredRepeat] = field(default_factory=list)
    _order: int = 0

    def __post_init__(self) -> None:
        self.rng = random.Random(f"{self.seed}:{self.name}")

    def clock(self, at: float) -> int:
        """Unix seconds at `at`, for the timestamps nodes write into payloads."""
        return int((self.start + dt.timedelta(seconds=at)).timestamp())

    def _next(self) -> int:
        self._order += 1
        return self._order

    @staticmethod
    def _at(at: float) -> float:
        """Whole milliseconds, so the file reads as a capture and not as floats."""
        return round(at, 3)

    def signal(self, hops: int, *, near: bool = False) -> tuple[float, int]:
        """A plausible SNR / RSSI: weaker with distance, quantised as the modem does."""
        if near or hops == 0:
            snr = self.rng.uniform(6.0, 14.0)
            rssi = self.rng.uniform(-60.0, -20.0)
        else:
            snr = self.rng.uniform(-11.0, 12.0) - hops * 0.5
            rssi = self.rng.uniform(-118.0, -55.0) - hops * 2.0
        return round(snr * 4) / 4, max(-125, round(rssi))

    def rx(self, at: float, raw: bytes, *, hops: int | None = None) -> int:
        if hops is None:
            packet = decode(raw)
            assert not isinstance(packet, DecodeFailure)
            hops = packet.hop_count
        snr, rssi = self.signal(hops)
        item = _Item(self._next(), self._at(at), "rx", raw, snr, rssi)
        self.items.append(item)
        return item.order

    def tx(self, at: float, raw: bytes) -> int:
        item = _Item(self._next(), self._at(at), "tx", raw)
        self.items.append(item)
        return item.order

    def unparsed(self, at: float, raw: bytes, reason: str) -> int:
        item = _Item(self._next(), self._at(at), "unparsed", raw, reason=reason)
        self.items.append(item)
        return item.order

    def item(self, order: int) -> _Item:
        return next(i for i in self.items if i.order == order)

    def repeat(
        self,
        of: int,
        *,
        reason: str,
        delay: float,
        pool: Sequence[CastNode] = (),
        extra_hops: int = 1,
        identical: bool = False,
    ) -> int:
        """Emit a duplicate of an earlier reception, and declare why.

        The one place a duplicate is made. A flood repeat keeps the payload and the
        hash size and changes what forwarding changes — a longer, different path,
        so the hop count, and the signal readings — and arrives later. `identical`
        is the sender's own retransmission: the same bytes and the same signal.
        """
        original = self.item(of)
        copy = 2 + sum(1 for r in self.repeats if r.file == self.name and r.of == of)
        if identical:
            item = _Item(
                self._next(),
                self._at(original.at + delay),
                "rx",
                original.raw,
                original.snr,
                original.rssi,
            )
        else:
            packet = decode(original.raw)
            assert not isinstance(packet, DecodeFailure)
            assert packet.route_type is RouteType.FLOOD, (
                "only a flood packet is repeated by another node"
            )
            hops = packet.hop_count + extra_hops
            while True:
                relays = self.rng.sample(list(pool), hops)
                path = b"".join(node.hash_bytes(packet.hash_size) for node in relays)
                if path != packet.path:
                    break
            snr, rssi = self.signal(hops)
            while (snr, rssi) == (original.snr, original.rssi):
                snr, rssi = self.signal(hops)
            rebuilt = replace(packet, hop_count=hops, path=path)
            item = _Item(
                self._next(), self._at(original.at + delay), "rx", encode(rebuilt), snr, rssi
            )
        self.items.append(item)
        self.repeats.append(DeclaredRepeat(self.name, reason, of, copy))
        return item.order

    def render(self) -> str:
        lines = [json.dumps(self._meta())]
        packet_number = 0
        for item in sorted(self.items, key=lambda i: (i.at, i.order)):
            stamp = (self.start + dt.timedelta(seconds=item.at)).isoformat()
            if item.kind == "rx":
                record: dict = {
                    "ts": stamp,
                    "kind": "rx_frame",
                    "raw_hex": item.raw.hex(),
                    "rx_meta": {"snr_db": item.snr, "rssi_dbm": item.rssi},
                }
            elif item.kind == "tx":
                packet_number += 1
                record = {
                    "ts": stamp,
                    "kind": "tx_frame",
                    "raw_hex": item.raw.hex(),
                    "packet_id": f"syn-{self.name.split('-')[1].split('.')[0]}-{packet_number:04d}",
                    "airtime_ms": round(time_on_air_ms(len(item.raw), EU868_NARROW), 3),
                }
            else:
                record = {
                    "ts": stamp,
                    "kind": "unparsed",
                    "raw_hex": item.raw.hex(),
                    "reason": item.reason,
                }
            lines.append(json.dumps(record))
        return "\n".join(lines) + "\n"

    def _meta(self) -> dict:
        return {
            "ts": self.start.isoformat(),
            "kind": "capture_meta",
            "synthetic": True,
            "generator": {
                "module": "tests.protocol.synthetic",
                "version": GENERATOR_VERSION,
                "seed": self.seed,
            },
            "file": self.name,
            "note": (
                "Generated by tests.protocol.synthetic from sighop's own encoders. "
                "No real device, board, firmware, mesh, node or message."
            ),
            "sighop": {"version": "synthetic", "commit_hash": "synthetic"},
            "device_name": {"value": "synthetic (no device)", "reason": None},
            "radio": {"value": dict(SYNTHETIC_RADIO), "reason": None},
        }


# --- The three files --------------------------------------------------------


def _pick(
    rng: random.Random, pool: Sequence[CastNode], count: int, *, avoid: CastNode | None = None
) -> list[CastNode]:
    candidates = [node for node in pool if node is not avoid]
    return rng.sample(candidates, count)


def _spread(rng: random.Random, count: int, low: float, high: float) -> list[float]:
    return sorted(round(rng.uniform(low, high), 3) for _ in range(count))


def ambient(cast: Cast, seed: int = SEED) -> _File:
    """Receive-only mesh: adverts of every form, routing, discovery, third-party mail."""
    file = _File(AMBIENT, AMBIENT_START, seed)
    rng = file.rng
    window = 3 * 3600.0
    relays_pool = cast.repeaters

    # --- Adverts. Every repeater four times, chat and room nodes twice.
    plan: list[tuple[CastNode, int]] = []
    for node in cast.repeaters:
        plan += [(node, 4)]
    for node in (*cast.chat, cast.rooms[0], cast.rooms[1], cast.rooms[2]):
        plan += [(node, 2)]
    hop_cycle = [0, 2, 1, 3, 5, 2, 4, 1, 3, 0, 2, 4]
    size_cycle = [1, 2, 3, 2, 1, 3, 2, 3, 1, 2, 3, 1]
    advert_orders: dict[int, CastNode] = {}
    counter = 0
    transport_done = False
    for node, times in plan:
        for at in _spread(rng, times, 20.0, window - 60.0):
            hops = hop_cycle[counter % len(hop_cycle)]
            size = size_cycle[(counter // 2) % len(size_cycle)]
            counter += 1
            payload = _advert_payload(node, file.clock(at))
            relays = _pick(rng, relays_pool, hops, avoid=node) if hops else []
            if node.role == "chat" and not transport_done and hops == 1:
                transport_done = True
                raw = _packet(
                    RouteType.TRANSPORT_FLOOD,
                    PayloadType.ADVERT,
                    payload,
                    relays=relays,
                    hash_size=size,
                    codes=(0x2C41, 0x0000),
                )
            elif hops == 0 and counter % 3 == 0:
                # A zero-hop advert: sent direct, heard by its neighbours only.
                raw = _direct(PayloadType.ADVERT, payload)
            else:
                raw = _flood(PayloadType.ADVERT, payload, relays, size)
            advert_orders[file.rx(at, raw)] = node

    # --- Discovery: five requests, each answered by four repeaters, all zero-hop.
    for index, at in enumerate(_spread(rng, 5, 200.0, window - 200.0)):
        tag = rng.randbytes(4)
        since = None if index < 2 else 0
        request = DiscoverRequest(flags=0, type_filter=0x04, tag=tag, since=since)
        file.rx(at, _direct(PayloadType.CONTROL, build_discover_request(request)))
        for offset, responder in zip(
            _spread(rng, 4, 0.4, 7.5), _pick(rng, cast.repeaters, 4), strict=True
        ):
            response = DiscoverResponse(
                node_type=int(NodeType.REPEATER),
                snr_quarter_db=rng.randint(-40, 52),
                tag=tag,
                claimed_key=responder.public_key,
            )
            file.rx(at + offset, _direct(PayloadType.CONTROL, build_discover_response(response)))

    # --- TRACE: two payload lengths.
    for index, at in enumerate(_spread(rng, 6, 300.0, window - 300.0)):
        hashes = rng.randbytes(1 if index % 2 == 0 else 4)
        trace = rng.randbytes(4) + rng.randbytes(4) + b"\x00" + hashes
        relays = _pick(rng, relays_pool, index % 3)
        file.rx(at, _direct(PayloadType.TRACE, trace, relays))

    # --- Third-party mail between chat nodes: sighop holds no key for any of it.
    chat = cast.chat
    texts = list(DIRECT_MESSAGES)
    rng.shuffle(texts)
    message_times = _spread(rng, len(texts), 120.0, window - 120.0)
    ack_forms = 0
    for index, (at, text) in enumerate(zip(message_times, texts, strict=True)):
        sender, recipient = rng.sample(chat, 2)
        hops = index % 4
        size = 1 + (index % 3)
        relays = _pick(rng, relays_pool, hops)
        payload, body = text_payload(sender, recipient, file.clock(at), text)
        flooded = index % 3 != 0
        route = _flood if flooded else _direct
        file.rx(at, route(PayloadType.TXT_MSG, payload, relays, size))
        if index % 2 == 0:
            # The acknowledgement comes back: the 4-byte form, and for a third of
            # them the 6-byte form current firmware appends a tail to.
            ack_forms += 1
            tail = bytes([0x00, rng.randrange(256)]) if ack_forms % 3 == 0 else b""
            file.rx(
                at + rng.uniform(0.8, 3.5),
                _direct(PayloadType.ACK, ack_payload(body, sender, tail=tail), relays[::-1], size),
            )
        if index % 3 == 1:
            # The flooded message's route, returned bundling the acknowledgement.
            returned = _pick(rng, relays_pool, 1 + index % 3)
            path_body = path_payload(
                recipient,
                sender,
                returned,
                size,
                ack=Acknowledgement(checksum=ack_checksum_for(body, sender.public_key)),
            )
            file.rx(at + rng.uniform(1.0, 4.0), _flood(PayloadType.PATH, path_body, relays, size))

    # --- Room logins, requests and responses.
    login_late: int | None = None
    for index, at in enumerate(_spread(rng, 8, 400.0, window - 400.0)):
        client = chat[index % len(chat)]
        room = cast.rooms[index % 2]
        relays = _pick(rng, relays_pool, 1 + index % 3)
        order = file.rx(
            at,
            _flood(
                PayloadType.ANON_REQ,
                login_payload(client, room, file.clock(at), ROOM_PASSWORD),
                relays,
                1 + index % 2,
            ),
        )
        if login_late is None:
            login_late = order
    for index, at in enumerate(_spread(rng, 4, 500.0, window - 500.0)):
        client = chat[(index + 3) % len(chat)]
        server = cast.repeaters[index]
        file.rx(
            at,
            _direct(
                PayloadType.REQ,
                request_payload(client, server, file.clock(at), RequestType.GET_STATUS),
                _pick(rng, relays_pool, 1),
            ),
        )
    for index, at in enumerate(_spread(rng, 6, 500.0, window - 500.0)):
        server = cast.repeaters[4 + index]
        client = chat[index]
        file.rx(
            at,
            _direct(
                PayloadType.RESPONSE,
                telemetry_response_payload(server, client, file.clock(at), 3.6 + index / 20),
                _pick(rng, relays_pool, 1 + index % 2),
            ),
        )

    # --- The declared repeats. Flood packets only; nothing else is duplicated.
    floods = [
        item
        for item in file.items
        if item.kind == "rx" and _is_plain_flood(item.raw) and _hops(item.raw) < 4
    ]
    flood_targets = rng.sample(floods, 14)
    for index, target in enumerate(flood_targets):
        file.repeat(
            target.order,
            reason=(
                "flood repeat: a repeater forwarded the packet, so it arrives again "
                "over another path"
            ),
            delay=round(rng.uniform(0.4, 5.5), 3),
            pool=relays_pool,
            extra_hops=1 + index % 2,
        )
        if index < 2:
            file.repeat(
                target.order,
                reason="second flood repeat: a third path, still within seconds",
                delay=round(rng.uniform(5.6, 8.5), 3),
                pool=relays_pool,
                extra_hops=1,
            )
    assert login_late is not None
    file.repeat(
        login_late,
        reason=(
            "the late echo: a flood ANON_REQ whose copy arrives over a different path "
            "well past 60 s, which is what shows a 60 s dedup TTL is too short"
        ),
        delay=200.7,
        pool=relays_pool,
        extra_hops=1,
    )
    return file


def _is_plain_flood(raw: bytes) -> bool:
    """A `FLOOD` frame. The one transport-routed frame is kept out of the repeats
    so the corpus holds exactly one, as `protocol-corpus` asserts."""
    packet = decode(raw)
    assert not isinstance(packet, DecodeFailure)
    return packet.route_type is RouteType.FLOOD


def _hops(raw: bytes) -> int:
    packet = decode(raw)
    assert not isinstance(packet, DecodeFailure)
    return packet.hop_count


def _hash_size(raw: bytes) -> int:
    packet = decode(raw)
    assert not isinstance(packet, DecodeFailure)
    return packet.hash_size


def channels(cast: Cast, seed: int = SEED) -> _File:
    """Group text on Public and on a second channel, plus a few group-data frames."""
    file = _File(CHANNELS, CHANNELS_START, seed)
    rng = file.rng
    window = 2 * 3600.0
    club = club_key()
    posted: list[int] = []

    for index, (at, (sender, text)) in enumerate(
        zip(_spread(rng, len(PUBLIC_MESSAGES), 10.0, window), PUBLIC_MESSAGES, strict=True)
    ):
        relays = _pick(rng, cast.repeaters, index % 4)
        raw = _flood(
            PayloadType.GRP_TXT,
            group_text_payload(PUBLIC_CHANNEL_KEY, file.clock(at), sender, text),
            relays,
            1 + index % 3,
        )
        posted.append(file.rx(at, raw))
    for index, (at, (sender, text)) in enumerate(
        zip(_spread(rng, len(CLUB_MESSAGES), 10.0, window), CLUB_MESSAGES, strict=True)
    ):
        relays = _pick(rng, cast.repeaters, (index + 1) % 4)
        raw = _flood(
            PayloadType.GRP_TXT,
            group_text_payload(club, file.clock(at), sender, text),
            relays,
            1 + (index + 1) % 3,
        )
        posted.append(file.rx(at, raw))
    for index, (at, data) in enumerate(
        zip(_spread(rng, len(GROUP_DATA_PAYLOADS), 60.0, window), GROUP_DATA_PAYLOADS, strict=True)
    ):
        relays = _pick(rng, cast.repeaters, 1 + index % 2)
        plaintext = file.clock(at).to_bytes(4, "little") + data
        file.rx(
            at,
            _flood(
                PayloadType.GRP_DATA, group_payload(club, PayloadType.GRP_DATA, plaintext), relays
            ),
        )

    for index, target in enumerate(rng.sample(posted, 5)):
        file.repeat(
            target,
            reason="flood repeat: a repeater forwarded the group message over another path",
            delay=round(rng.uniform(0.5, 4.5), 3),
            pool=cast.repeaters,
            extra_hops=1 + index % 2,
        )
    return file


def exchange(cast: Cast, seed: int = SEED) -> _File:
    """sighop and a synthetic peer: direct messages, acknowledgements, a room."""
    file = _File(EXCHANGE, EXCHANGE_START, seed)
    rng = file.rng
    sighop, peer, lounge = cast.sighop, cast.peer, cast.lounge
    guest = cast.chat[0]
    relay_a, relay_b = cast.repeaters[0], cast.repeaters[3]

    # The stray metadata line a modem emits at startup, before any frame.
    file.unparsed(0.2, bytes.fromhex("06f90e3a"), "RxMeta with no preceding Data frame")

    # --- Adverts, all zero-hop: the peer's, and the two sighop transmits.
    file.rx(1.0, _direct(PayloadType.ADVERT, _advert_payload(peer, file.clock(1.0))), hops=0)
    file.tx(5.2, _direct(PayloadType.ADVERT, _advert_payload(sighop, file.clock(5.2))))
    file.tx(5.9, _direct(PayloadType.ADVERT, _advert_payload(lounge, file.clock(5.9))))

    # --- Direct messages, zero hops, both directions and both acknowledgement forms.
    at = 60.0
    payload, sent = text_payload(sighop, peer, file.clock(at), EXCHANGE_MESSAGES["sighop_first"])
    file.tx(at, _direct(PayloadType.TXT_MSG, payload))
    file.rx(
        at + 1.8,
        _direct(PayloadType.ACK, ack_payload(sent, sighop, tail=bytes([0x00, 0x91]))),
        hops=0,
    )
    at = 120.0
    payload, received = text_payload(peer, sighop, file.clock(at), EXCHANGE_MESSAGES["peer_first"])
    file.rx(at, _direct(PayloadType.TXT_MSG, payload), hops=0)
    file.tx(at + 0.9, _direct(PayloadType.ACK, ack_payload(received, peer)))

    # --- The peer's flooded message, answered by a returned path bundling the ack.
    at = 300.0
    payload, received = text_payload(
        peer, sighop, file.clock(at), EXCHANGE_MESSAGES["peer_flooded"]
    )
    file.rx(at, _flood(PayloadType.TXT_MSG, payload, [relay_a], 2))
    returned = path_payload(
        sighop,
        peer,
        [relay_a],
        2,
        ack=Acknowledgement(checksum=ack_checksum_for(received, peer.public_key)),
    )
    file.tx(at + 1.1, _flood(PayloadType.PATH, returned, [], 1))
    # The peer's own path back, learned from the same flood.
    peer_path = path_payload(peer, sighop, [relay_a], 2)
    file.rx(at + 2.6, _flood(PayloadType.PATH, peer_path, [relay_a], 2))

    # --- Over the learned one-hop route.
    at = 420.0
    payload, sent = text_payload(sighop, peer, file.clock(at), EXCHANGE_MESSAGES["sighop_reply"])
    file.tx(at, _direct(PayloadType.TXT_MSG, payload, [relay_a], 2))
    file.rx(
        at + 3.0,
        _direct(PayloadType.ACK, ack_payload(sent, sighop, tail=bytes([0x00, 0x3C])), [relay_a], 2),
    )
    at = 480.0
    payload, sent = text_payload(sighop, peer, file.clock(at), EXCHANGE_MESSAGES["sighop_routed"])
    file.tx(at, _direct(PayloadType.TXT_MSG, payload, [relay_a], 2))
    file.rx(at + 2.7, _direct(PayloadType.ACK, ack_payload(sent, sighop), [relay_a], 2))

    # --- The retransmitted DM: sent, not acknowledged, sent again identically.
    at = 900.0
    payload, received = text_payload(peer, sighop, file.clock(at), EXCHANGE_MESSAGES["peer_long"])
    first = file.rx(at, _direct(PayloadType.TXT_MSG, payload), hops=0)
    file.repeat(
        first,
        reason=(
            "the sender retransmits an unacknowledged direct message: byte-identical, "
            "more than 30 minutes later, which is why the dedup TTL must stay short "
            "enough not to swallow a sender's retry"
        ),
        delay=RETRANSMIT_MIN_SECONDS + 93.0,
        identical=True,
    )
    file.tx(
        at + RETRANSMIT_MIN_SECONDS + 94.2, _direct(PayloadType.ACK, ack_payload(received, peer))
    )
    at = 2900.0
    payload, received = text_payload(peer, sighop, file.clock(at), EXCHANGE_MESSAGES["peer_after"])
    file.rx(at, _direct(PayloadType.TXT_MSG, payload), hops=0)
    file.tx(at + 1.0, _direct(PayloadType.ACK, ack_payload(received, peer)))
    at = 3000.0
    payload, received = text_payload(peer, sighop, file.clock(at), EXCHANGE_MESSAGES["peer_scan"])
    file.rx(at, _direct(PayloadType.TXT_MSG, payload), hops=0)
    file.tx(at + 0.8, _direct(PayloadType.ACK, ack_payload(received, peer)))

    # --- The room sighop hosts: login, requests, posts, a refusal, pushes.
    room_start = 3400.0
    login = login_payload(peer, lounge, file.clock(room_start), ROOM_PASSWORD)
    file.rx(room_start, _flood(PayloadType.ANON_REQ, login, [relay_b], 1))
    answer = build_room_login_response_body(
        RoomLoginResponseBody(
            server_timestamp=file.clock(room_start + 1.3),
            client_kind=ClientKind.MEMBER,
            permissions=int(Permission.READ_WRITE),
            blob=rng.randbytes(4),
        )
    )
    file.tx(
        room_start + 1.3,
        _flood(
            PayloadType.PATH,
            path_payload(lounge, peer, [relay_b], 1, response=answer),
            [],
            1,
        ),
    )
    at = room_start + 20.0
    file.rx(
        at,
        _direct(
            PayloadType.REQ,
            request_payload(peer, lounge, file.clock(at), RequestType.GET_TELEMETRY_DATA),
            [relay_b],
        ),
    )
    file.tx(
        at + 1.4,
        _direct(
            PayloadType.RESPONSE,
            telemetry_response_payload(lounge, peer, file.clock(at + 1.4), 4.05),
            [relay_b],
        ),
    )
    for slot, (who, text) in enumerate(ROOM_POSTS):
        at = room_start + 60.0 + slot * 45.0
        if who == "guest":
            if text == ROOM_POSTS[2][1]:
                file.rx(
                    at - 12.0,
                    _flood(
                        PayloadType.ANON_REQ,
                        login_payload(guest, lounge, file.clock(at - 12.0), GUEST_PASSWORD),
                        [relay_a],
                        1,
                    ),
                )
                file.tx(
                    at - 10.6,
                    _flood(
                        PayloadType.PATH,
                        path_payload(
                            lounge,
                            guest,
                            [relay_a],
                            1,
                            response=build_room_login_response_body(
                                RoomLoginResponseBody(
                                    server_timestamp=file.clock(at - 10.6),
                                    client_kind=ClientKind.SPECTATOR,
                                    permissions=int(Permission.GUEST),
                                    blob=rng.randbytes(4),
                                )
                            ),
                        ),
                        [],
                        1,
                    ),
                )
            # A guest's post is refused: no acknowledgement is ever sent, and the
            # client retries with the next attempt number.
            payload, _ = text_payload(guest, lounge, file.clock(at), text, attempt=slot - 2)
            file.rx(at, _direct(PayloadType.TXT_MSG, payload, [relay_a]))
            continue
        payload, body = text_payload(peer, lounge, file.clock(at), text)
        file.rx(at, _direct(PayloadType.TXT_MSG, payload, [relay_b]))
        file.tx(at + 1.2, _direct(PayloadType.ACK, ack_payload(body, peer), [relay_b]))
    for slot, (author_name, text) in enumerate(ROOM_PUSHES):
        author = next(node for node in cast.chat if node.name == author_name)
        at = room_start + 330.0 + slot * 40.0
        payload, body = text_payload(
            lounge,
            peer,
            file.clock(at),
            text,
            txt_type=TextType.SIGNED_PLAIN,
            author_prefix=author.public_key[:4],
        )
        file.tx(at, _direct(PayloadType.TXT_MSG, payload, [relay_b]))
        file.rx(at + 2.2, _direct(PayloadType.ACK, ack_payload(body, lounge), [relay_b]))
    return file


# --- Generation -------------------------------------------------------------


def build_files(seed: int = SEED) -> dict[str, _File]:
    cast = build_cast(seed)
    files = (ambient(cast, seed), channels(cast, seed), exchange(cast, seed))
    return {file.name: file for file in files}


def generate(seed: int = SEED) -> dict[str, str]:
    """The corpus as file name to text. The same seed gives the same text."""
    return {name: file.render() for name, file in build_files(seed).items()}


def declared_repeats(seed: int = SEED) -> tuple[DeclaredRepeat, ...]:
    return tuple(r for file in build_files(seed).values() for r in file.repeats)


# --- Manifest ---------------------------------------------------------------


def _records(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def manifest(files: dict[str, str] | None = None) -> str:
    """What the corpus holds, counted the way the tests count it.

    Printed by the generator and typed into the tests' recorded expectations from
    a reviewed run — never written into them by tooling.
    """
    files = files if files is not None else generate()
    out: list[str] = []
    total: Counter[str] = Counter()
    types: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    hops: Counter[int] = Counter()
    sizes: Counter[int] = Counter()
    adverts: Counter[str] = Counter()
    names: set[str] = set()
    acks: Counter[int] = Counter()
    control: Counter[tuple[str, int]] = Counter()
    transport: list[tuple[int, int]] = []
    per_file: dict[str, tuple[int, int, int]] = {}
    seen: Counter[tuple[int, bytes]] = Counter()
    receptions = 0

    for name, text in files.items():
        records = _records(text)
        rx = sum(1 for r in records if r["kind"] == "rx_frame")
        tx = sum(1 for r in records if r["kind"] == "tx_frame")
        unparsed = sum(1 for r in records if r["kind"] == "unparsed")
        per_file[name] = (rx, tx, unparsed)
        for record in records:
            if record["kind"] not in ("rx_frame", "tx_frame"):
                continue
            raw = bytes.fromhex(record["raw_hex"])
            packet = decode(raw)
            assert not isinstance(packet, DecodeFailure), record
            types[packet.payload_type.name] += 1
            routes[packet.route_type.name] += 1
            hops[packet.hop_count] += 1
            sizes[packet.hash_size] += 1
            if packet.transport_codes is not None:
                transport.append(packet.transport_codes)
            if record["kind"] == "rx_frame":
                receptions += 1
                seen[(int(packet.payload_type), packet.payload)] += 1
            if packet.payload_type is PayloadType.ADVERT:
                appdata = parse_appdata(packet.payload[100:])
                assert not isinstance(appdata, DecodeFailure)
                adverts[f"0x{appdata.flags:02x}"] += 1
                if appdata.name is not None:
                    names.add(appdata.name.text)
            elif packet.payload_type is PayloadType.ACK:
                acks[len(packet.payload)] += 1
            elif packet.payload_type is PayloadType.CONTROL:
                kind = (
                    "discover_request" if packet.payload[0] & 0xF0 == 0x80 else "discover_response"
                )
                control[kind, len(packet.payload)] += 1
        total["rx"] += rx
        total["tx"] += tx
        total["unparsed"] += unparsed

    repeats = sum(count - 1 for count in seen.values())
    out.append("synthetic corpus manifest")
    out.append(f"seed={SEED} generator_version={GENERATOR_VERSION}")
    for name, (rx, tx, unparsed) in per_file.items():
        out.append(f"file {name}: rx={rx} tx={tx} unparsed={unparsed}")
    out.append(
        f"frames={total['rx'] + total['tx']} received={total['rx']} transmitted={total['tx']}"
    )
    out.append("payload_types=" + _fmt(types))
    out.append("route_types=" + _fmt(routes))
    out.append("hop_counts=" + _fmt(hops))
    out.append("hash_sizes=" + _fmt(sizes))
    out.append("advert_forms=" + _fmt(adverts) + f" total={sum(adverts.values())}")
    out.append(f"advert_names={len(names)}")
    out.append("ack_lengths=" + _fmt(acks))
    out.append("control_forms=" + _fmt({f"{k}/{n}": c for (k, n), c in control.items()}))
    out.append(f"transport_codes={transport}")
    share = repeats / receptions if receptions else 0.0
    out.append(
        f"repeats={repeats} of {receptions} receptions ({share:.1%}); "
        f"max copies={max(seen.values()) if seen else 0}"
    )
    return "\n".join(out) + "\n"


def _fmt(counter: Iterable | dict) -> str:
    items = counter.items() if isinstance(counter, dict) else Counter(counter).items()
    return "{" + ", ".join(f"{k}: {v}" for k, v in sorted(items, key=lambda kv: str(kv[0]))) + "}"
