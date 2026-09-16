"""Group channels, both directions (DESIGN.md §7 Channels, change `channel-messaging`).

Wire details from the firmware rather than the payload documentation:

* **Receive** (`BaseChatMesh.cpp`, `Mesh.cpp`): the channel hash selects
  candidate keys, the 2-byte MAC selects one, and the plaintext is a text
  message whose text is `name: body`. The firmware trials up to four channels and
  breaks on the first MAC success; this trials every loaded channel under the
  hash, because a local store is small, and stops at the first whose MAC
  matches *and* whose plaintext parses. `onGroupDataRecv` drops any text type
  but plain, and so does this.
* **Send** (`sendGroupMessage`): `"<name>: " + text`, the whole at most
  `MAX_TEXT_LEN` (160) bytes, the timestamp in the plaintext "to make
  packet_hash unique", always flooded, no acknowledgement and no retry.

Three rules that are not wire details:

* **A sender name is a claim.** Group text carries no sender authentication.
  The name travels in `unverified_sender_name` everywhere, events and records
  included, so no consumer can read it without reading the claim.
* **Refuse, never truncate.** A post over the limit, by an identity whose name
  holds the separator, or while the gate is closed is refused before it is
  composed, and nothing is recorded (design D5).
* **Our own post heard back is a repeat.** The dedup cache sees receptions
  only, so a repeater's copy of our flood arrives fresh and would decrypt as
  somebody using our name. A transmitted post is remembered by its dedup key;
  a reception observer counts every copy, and the bus handler skips a key it
  holds (design D6). Matching is by payload and never by claimed name.

Like `dm.py`, this is a bus subscriber and never part of `net/rx.py`, and it
reaches storage only through record sinks, so `net/` imports nothing from `db/`
or `web/` (design D3).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from sighop.logging import Logger, get_logger
from sighop.net.bus import (
    NetworkBus,
    PriorityClass,
    Submission,
    Subscription,
    TxHandle,
    TxOutcome,
    TxResult,
)
from sighop.net.dedup import content_key
from sighop.net.dm import LocalEntity
from sighop.net.rx import Payload, RxRecord
from sighop.net.tx import Clock, SystemClock
from sighop.protocol.crypto import ChannelKey, encrypt_then_mac, mac_then_decrypt
from sighop.protocol.packet import (
    PAYLOAD_VERSION_1,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
)
from sighop.protocol.packet import encode as encode_packet
from sighop.protocol.payloads import (
    GROUP_NAME_SEPARATOR,
    GroupEnvelope,
    GroupTextBody,
    TextType,
    WireText,
    build_group_envelope,
    build_group_text_body,
    parse_group_text_body,
)
from sighop.protocol.result import DecodeFailure

MAX_CHANNEL_TEXT_LEN = 160
"""`BaseChatMesh.h` `MAX_TEXT_LEN`, which `sendGroupMessage` fits `name: text`
into. Stock clients display 165, so a 160-byte post shows in full at both ends."""

POST_DEADLINE_SECONDS = 120.0
"""How long a post may wait for the air. Nothing retries it and nobody
acknowledges it, so a post that has waited two minutes is dropped and reported
rather than sent into a conversation that has moved on."""

REPEAT_REGISTRY_CAPACITY = 256
REPEAT_REGISTRY_TTL_SECONDS = 3600.0
"""An hour and 256 posts: longer than any flood echo, and far more posts than a
station posts in an hour under an airtime ceiling."""


class ChannelKind(StrEnum):
    """How a channel's key is obtained — `channel.kind`."""

    PUBLIC = "public"
    """The stock Public channel; key `protocol.PUBLIC_CHANNEL_KEY`."""

    HASHTAG = "hashtag"
    """Key derived from the hashtag name. Anyone who guesses it can read and post."""

    PSK = "psk"
    """A pre-shared 16- or 32-byte key, sealed at rest."""

    @property
    def guessable(self) -> bool:
        """Whether anyone who knows or guesses the channel's name holds its key."""
        return self is not ChannelKind.PSK


# --- The channel set (design D3, D4) ----------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedChannel:
    """A stored channel with its key derived or opened, ready to trial."""

    id: int
    name: str
    kind: ChannelKind
    key: ChannelKey = field(repr=False)

    @property
    def channel_hash(self) -> int:
        return self.key.channel_hash

    @property
    def guessable(self) -> bool:
        return self.kind.guessable


@dataclass(frozen=True, slots=True)
class ChannelSet:
    """An immutable snapshot of the loaded channels, replaced whole.

    Held in memory and swapped atomically, so a reception is trialled against
    one consistent set and a reload that fails leaves the previous one standing
    (§6: memory is the authority).
    """

    channels: tuple[LoadedChannel, ...] = ()
    skipped: tuple[str, ...] = ()
    """Names of stored channels that could not be loaded — a pre-shared key that
    does not open under the configured secret — reported rather than hidden."""

    def by_hash(self, channel_hash: int) -> tuple[LoadedChannel, ...]:
        """Every loaded channel under one hash, in load order. One byte collides."""
        return tuple(c for c in self.channels if c.channel_hash == channel_hash)

    def by_id(self, channel_id: int) -> LoadedChannel | None:
        return next((c for c in self.channels if c.id == channel_id), None)

    def __len__(self) -> int:
        return len(self.channels)

    def __iter__(self) -> Iterator[LoadedChannel]:
        return iter(self.channels)


# --- Records (design D7) ----------------------------------------------------


class ChannelOutcome(StrEnum):
    """`channel_message.outcome`. Received messages carry `RECEIVED`."""

    AWAITING = "awaiting"
    """Submitted, not yet resolved — and what a restart mid-flight leaves as
    `UNKNOWN`, never as a claim of transmission."""

    TRANSMITTED = "transmitted"
    """On the air. Not a claim that anyone received it."""

    NOT_TRANSMITTED = "not_transmitted"
    UNKNOWN = "unknown"
    RECEIVED = "received"


OUTBOUND = "out"
INBOUND = "in"


@dataclass(frozen=True, slots=True)
class ChannelMessageRecord:
    """One channel message, in the shape something durable can store it.

    Keyed by `(channel_id, ref)`: `ref` is the post id outbound and the
    reception's packet id inbound, so a post offered at submission, on
    resolution and on every repeat heard is one message offered several times.

    `text` is the message body's bytes as they travel, without the
    `name: ` prefix — the name is `unverified_sender_name` inbound and the
    posting identity outbound.
    """

    channel_id: int
    direction: str
    ref: str
    text: bytes
    wire_timestamp: int
    handled_at: dt.datetime
    outcome: ChannelOutcome
    entity_public_key: bytes | None = None
    unverified_sender_name: str | None = None
    packet_id: str | None = None
    hop_count: int | None = None
    snr_db: float | None = None
    rssi_dbm: int | None = None
    repeats_heard: int = 0
    outcome_reason: str | None = None
    row_id: int | None = None

    @property
    def inbound(self) -> bool:
        return self.direction == INBOUND

    def rendered(self) -> WireText:
        return WireText.from_bytes(self.text)

    def as_json(self) -> dict[str, object]:
        return {
            "channel_id": self.channel_id,
            "direction": self.direction,
            "ref": self.ref,
            "message_outcome": str(self.outcome),
            "text_bytes": len(self.text),
            "repeats_heard": self.repeats_heard,
        }


class ChannelMessageSink(Protocol):
    """Where channel messages go to be recorded. Never awaits, never raises.

    `DirectMessageSink`'s contract: offered from the reception and transmission
    paths, which a slow database must not be able to delay. False means the
    record was refused.
    """

    def offer(self, record: ChannelMessageRecord) -> bool: ...


# --- Events -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelMessageReceived:
    channel_id: int
    channel_name: str
    packet_id: str
    unverified_sender_name: str | None
    body: str
    wire_timestamp: int
    hop_count: int | None
    snr_db: float | None
    rssi_dbm: int | None
    channels_tried: int


@dataclass(frozen=True, slots=True)
class ChannelUnknown:
    packet_id: str
    channel_hash: int


@dataclass(frozen=True, slots=True)
class ChannelUndecryptable:
    packet_id: str
    channel_hash: int
    channels_tried: int


@dataclass(frozen=True, slots=True)
class ChannelUnsupportedText:
    packet_id: str
    channel_name: str
    txt_type: int


@dataclass(frozen=True, slots=True)
class ChannelPostSubmitted:
    post_id: str
    channel_id: int
    channel_name: str
    entity_name: str
    text: str
    wire_timestamp: int
    packet_bytes: int
    actor: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelPostRefused:
    channel_id: int
    channel_name: str | None
    entity_name: str
    reason: str
    actor: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelPostResolved:
    post_id: str
    channel_name: str
    entity_name: str
    outcome: ChannelOutcome
    packet_id: str
    reason: str = ""
    airtime_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ChannelRepeatHeard:
    post_id: str
    channel_name: str
    packet_id: str
    hop_count: int | None
    snr_db: float | None
    repeats_heard: int
    duplicate: bool


@dataclass(frozen=True, slots=True)
class ChannelConfigReadFailed:
    error: str
    channels_kept: int


@dataclass(frozen=True, slots=True)
class ChannelSetChanged:
    """A reload that adopted a different set than the one in force.

    The CLI tells the operator that a change "applies to a running run within
    60 s"; without this the run says nothing when it does, and the only way to
    check the promise was a second surface. A reload that changes nothing stays
    silent — this is the event for a station whose state changed.
    """

    added: tuple[str, ...]
    removed: tuple[str, ...]
    loaded: int


type ChannelEvent = (
    ChannelMessageReceived
    | ChannelUnknown
    | ChannelUndecryptable
    | ChannelUnsupportedText
    | ChannelPostSubmitted
    | ChannelPostRefused
    | ChannelPostResolved
    | ChannelRepeatHeard
    | ChannelConfigReadFailed
    | ChannelSetChanged
)


# --- Refusals (design D5) ---------------------------------------------------


class ChannelPostError(RuntimeError):
    """A post refused before it was composed. The message is the reason."""


class ChannelNotLoadedError(ChannelPostError):
    pass


class IdentityNotLoadedError(ChannelPostError):
    pass


class SenderNameSeparatorError(ChannelPostError):
    pass


class ChannelTextTooLongError(ChannelPostError):
    pass


class TransmitDisabledError(ChannelPostError):
    pass


def check_post_length(sender_name: str, text: str) -> None:
    """Refuse `name: text` over the 160-byte limit, saying what counts and by how much."""
    prefix = f"{sender_name}{GROUP_NAME_SEPARATOR}".encode()
    body = text.encode("utf-8")
    total = len(prefix) + len(body)
    if total > MAX_CHANNEL_TEXT_LEN:
        raise ChannelTextTooLongError(
            f"A channel post carries at most {MAX_CHANNEL_TEXT_LEN} bytes, and the "
            f"identity's name and the {GROUP_NAME_SEPARATOR!r} separator count towards "
            f"it: {len(prefix)} bytes of name and separator plus {len(body)} bytes of "
            f"text is {total}, which is {total - MAX_CHANNEL_TEXT_LEN} over. Nothing "
            "was truncated and nothing was sent."
        )


def check_sender_name(sender_name: str) -> None:
    if GROUP_NAME_SEPARATOR in sender_name:
        raise SenderNameSeparatorError(
            f"The identity name {sender_name!r} contains {GROUP_NAME_SEPARATOR!r}, and "
            "receivers split a channel message at the first one, so part of the name "
            "would be read as the message. Nothing was sent."
        )


TRANSMIT_DISABLED = (
    "Transmission is disabled for this run, so nothing was queued: a post held "
    "now would go out later on a change of gate, which is not what posting meant."
)


def build_channel_packet(channel: LoadedChannel, plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt-then-MAC a group text body into one flooded `GRP_TXT` packet.

    Returns `(packet bytes, payload bytes)`: the payload is what deduplication
    keys on, and so what our own post heard back is recognised by.
    """
    mac, ciphertext = encrypt_then_mac(channel.key.secret, plaintext)
    envelope = GroupEnvelope(
        payload_type=PayloadType.GRP_TXT,
        channel_hash=channel.channel_hash,
        mac=mac,
        ciphertext=ciphertext,
    )
    payload = build_group_envelope(envelope)
    packet = encode_packet(
        Packet(
            header=PacketHeader(
                route_type=RouteType.FLOOD,
                payload_type=PayloadType.GRP_TXT,
                payload_version=PAYLOAD_VERSION_1,
            ),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=payload,
        )
    )
    return packet, payload


# --- The repeat registry (design D6) ----------------------------------------


@dataclass(slots=True)
class _Transmitted:
    record: ChannelMessageRecord
    channel_name: str
    registered_at: dt.datetime


class RepeatRegistry:
    """Posts this station transmitted, by dedup key, bounded in number and age."""

    def __init__(
        self,
        *,
        capacity: int = REPEAT_REGISTRY_CAPACITY,
        ttl_seconds: float = REPEAT_REGISTRY_TTL_SECONDS,
    ) -> None:
        self.capacity = capacity
        self.ttl = dt.timedelta(seconds=ttl_seconds)
        self._entries: OrderedDict[bytes, _Transmitted] = OrderedDict()

    def remember(
        self, key: bytes, record: ChannelMessageRecord, channel_name: str, now: dt.datetime
    ) -> None:
        self._expire(now)
        self._entries[key] = _Transmitted(record, channel_name, now)
        self._entries.move_to_end(key)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def get(self, key: bytes, now: dt.datetime) -> _Transmitted | None:
        self._expire(now)
        return self._entries.get(key)

    def __contains__(self, key: bytes) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def _expire(self, now: dt.datetime) -> None:
        cutoff = now - self.ttl
        while self._entries:
            key, entry = next(iter(self._entries.items()))
            if entry.registered_at >= cutoff:
                return
            del self._entries[key]


# --- The messenger ----------------------------------------------------------


@dataclass(slots=True)
class ChannelPost:
    """A submitted post. `resolution` completes with its outcome."""

    post_id: str
    record: ChannelMessageRecord
    resolution: asyncio.Task[ChannelMessageRecord]


class ChannelMessenger:
    """Decrypts group text on the loaded channels, and posts into them."""

    def __init__(
        self,
        *,
        submit: Callable[[Submission], TxHandle],
        entities: Sequence[LocalEntity] = (),
        transmit_enabled: Callable[[], bool] = lambda: False,
        channels: ChannelSet | None = None,
        clock: Clock | None = None,
        on_event: Callable[[ChannelEvent], None] | None = None,
        logger: Logger | None = None,
        records: ChannelMessageSink | None = None,
        registry: RepeatRegistry | None = None,
    ) -> None:
        self.submit = submit
        self.entities = entities
        """Live view of the loaded identities — the runtime's list, not a copy."""

        self.transmit_enabled = transmit_enabled
        self._channels = channels or ChannelSet()
        self._configured = channels is not None
        """Whether the set in force is one this messenger was told about. The
        first set a run loads is its startup state, not a change to report."""

        self.clock = clock or SystemClock()
        self._on_event = on_event
        self._log = logger or get_logger(component="channels")
        self._records: list[ChannelMessageSink] = [] if records is None else [records]
        self.registry = registry or RepeatRegistry()
        self._last_timestamp = 0
        self._resolutions: set[asyncio.Task[ChannelMessageRecord]] = set()
        self.decrypted = 0
        self.unknown = 0
        self.undecryptable = 0
        self.unsupported = 0
        self.posts_submitted = 0
        self.posts_transmitted = 0
        self.posts_refused = 0
        self.repeats_heard = 0
        self.records_offered = 0
        self.records_refused = 0

    # --- Configuration -------------------------------------------------------

    @property
    def channels(self) -> ChannelSet:
        return self._channels

    def replace_channels(self, channels: ChannelSet) -> None:
        """Adopt a new snapshot whole. Receptions in flight keep the one they had.

        A set that differs from the one in force is reported, so a channel added
        or removed from another process — the CLI's "within 60 s" — is visible in
        the run that adopts it. The first set a messenger is given is the run's
        startup state, which the startup report already names, so it is adopted
        without an event.
        """
        before, self._channels = self._channels, channels
        if not self._configured:
            self._configured = True
            return
        was = {(c.id, c.name) for c in before}
        now = {(c.id, c.name) for c in channels}
        added = tuple(name for _, name in sorted(now - was))
        removed = tuple(name for _, name in sorted(was - now))
        if not added and not removed:
            return
        self._log.info(
            "channel_set_changed",
            outcome="success",
            added=list(added),
            removed=list(removed),
            channels_loaded=len(channels),
        )
        self._emit(ChannelSetChanged(added=added, removed=removed, loaded=len(channels)))

    def config_read_failed(self, error: str) -> None:
        """Report a reload that failed; the current set stays in force."""
        self._log.error(
            "channel_config_read_failed",
            outcome="error",
            error=error,
            channels_kept=len(self._channels),
        )
        self._emit(ChannelConfigReadFailed(error=error, channels_kept=len(self._channels)))

    def add_record_sink(self, sink: ChannelMessageSink) -> None:
        self._records.append(sink)

    def subscribe(self, bus: NetworkBus, *, name: str = "channels") -> Subscription:
        return bus.subscribe(name, handler=self.handle)

    def _emit(self, event: ChannelEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)

    def _record(self, record: ChannelMessageRecord) -> None:
        """Offer one record to every sink. Never awaits, never raises."""
        for sink in self._records:
            try:
                taken = sink.offer(record)
            except Exception as exc:
                self.records_refused += 1
                self._log.error(
                    "channel_message_record_raised",
                    outcome="error",
                    ref=record.ref,
                    direction=record.direction,
                    error=repr(exc),
                )
                continue
            if taken:
                self.records_offered += 1
                continue
            self.records_refused += 1
            self._log.error(
                "channel_message_record_refused",
                outcome="error",
                ref=record.ref,
                direction=record.direction,
                detail="a record sink refused the record; it is not in that sink's history",
            )

    # --- Inbound -------------------------------------------------------------

    async def handle(self, record: RxRecord) -> None:
        """Bus handler: `GRP_TXT` receptions. `GRP_DATA` is never decrypted."""
        match record.outcome:
            case Payload(payload=GroupEnvelope() as envelope) if (
                envelope.payload_type is PayloadType.GRP_TXT
            ):
                self._handle_group_text(record, envelope)
            case _:
                return

    def _handle_group_text(self, record: RxRecord, envelope: GroupEnvelope) -> None:
        assert record.packet is not None
        key = content_key(int(PayloadType.GRP_TXT), record.packet.payload)
        if key in self.registry:
            # Our own post, heard back through a repeater. The observer counts it;
            # decrypting it here would record us as somebody using our name.
            return

        channels = self._channels
        candidates = channels.by_hash(envelope.channel_hash)
        if not candidates:
            self.unknown += 1
            self._log.info(
                "channel_unknown",
                packet_id=record.packet_id,
                channel_hash=f"{envelope.channel_hash:02x}",
            )
            self._emit(ChannelUnknown(record.packet_id, envelope.channel_hash))
            return

        tried = 0
        for channel in candidates:
            tried += 1
            match, plaintext = mac_then_decrypt(
                channel.key.secret, envelope.mac, envelope.ciphertext
            )
            if not match.matched or plaintext is None:
                continue
            body = parse_group_text_body(plaintext)
            if isinstance(body, DecodeFailure):
                continue
            if body.message.txt_type is not TextType.PLAIN:
                self.unsupported += 1
                txt_type = int(body.message.txt_type)
                self._log.info(
                    "channel_unsupported_text",
                    packet_id=record.packet_id,
                    channel=channel.name,
                    txt_type=txt_type,
                )
                self._emit(ChannelUnsupportedText(record.packet_id, channel.name, txt_type))
                return
            self._received(record, channel, body, tried)
            return

        self.undecryptable += 1
        self._log.info(
            "channel_undecryptable",
            packet_id=record.packet_id,
            channel_hash=f"{envelope.channel_hash:02x}",
            channels_tried=tried,
        )
        self._emit(ChannelUndecryptable(record.packet_id, envelope.channel_hash, tried))

    def _received(
        self,
        record: RxRecord,
        channel: LoadedChannel,
        body: GroupTextBody,
        tried: int,
    ) -> None:
        claimed = body.unverified_sender_name
        self.decrypted += 1
        raw = body.message.text.raw
        separator = GROUP_NAME_SEPARATOR.encode()
        text = raw.partition(separator)[2] if claimed is not None else raw
        self._record(
            ChannelMessageRecord(
                channel_id=channel.id,
                direction=INBOUND,
                ref=record.packet_id,
                text=text,
                wire_timestamp=body.message.timestamp,
                handled_at=record.received_at,
                outcome=ChannelOutcome.RECEIVED,
                unverified_sender_name=claimed,
                packet_id=record.packet_id,
                hop_count=record.hop_count,
                snr_db=record.snr_db,
                rssi_dbm=record.rssi_dbm,
            )
        )
        self._log.info(
            "channel_message_received",
            packet_id=record.packet_id,
            channel=channel.name,
            channel_hash=f"{channel.channel_hash:02x}",
            unverified_sender_name=claimed,
            channels_tried=tried,
            text_bytes=len(text),
            message_timestamp=body.message.timestamp,
            hop_count=record.hop_count,
        )
        self._emit(
            ChannelMessageReceived(
                channel_id=channel.id,
                channel_name=channel.name,
                packet_id=record.packet_id,
                unverified_sender_name=claimed,
                body=body.body,
                wire_timestamp=body.message.timestamp,
                hop_count=record.hop_count,
                snr_db=record.snr_db,
                rssi_dbm=record.rssi_dbm,
                channels_tried=tried,
            )
        )

    def observe(self, record: RxRecord, duplicate: bool) -> None:
        """Reception observer: count copies of our own posts. Never raises."""
        try:
            self._observe(record, duplicate)
        except Exception as exc:  # pragma: no cover - defensive, observers never raise
            self._log.error(
                "channel_repeat_observer_error",
                outcome="error",
                packet_id=record.packet_id,
                error=repr(exc),
            )

    def _observe(self, record: RxRecord, duplicate: bool) -> None:
        packet = record.packet
        if packet is None or packet.payload_type is not PayloadType.GRP_TXT:
            return
        key = content_key(int(PayloadType.GRP_TXT), packet.payload)
        entry = self.registry.get(key, self.clock.now())
        if entry is None:
            return
        entry.record = replace(entry.record, repeats_heard=entry.record.repeats_heard + 1)
        self.repeats_heard += 1
        self._record(entry.record)
        self._log.info(
            "channel_repeat_heard",
            post_id=entry.record.ref,
            channel=entry.channel_name,
            packet_id=record.packet_id,
            hop_count=record.hop_count,
            snr_db=record.snr_db,
            duplicate=duplicate,
            repeats_heard=entry.record.repeats_heard,
        )
        self._emit(
            ChannelRepeatHeard(
                post_id=entry.record.ref,
                channel_name=entry.channel_name,
                packet_id=record.packet_id,
                hop_count=record.hop_count,
                snr_db=record.snr_db,
                repeats_heard=entry.record.repeats_heard,
                duplicate=duplicate,
            )
        )

    # --- Outbound ------------------------------------------------------------

    def post(
        self,
        channel_id: int,
        entity: LocalEntity,
        text: str,
        *,
        actor: str | None = None,
    ) -> ChannelPost:
        """Refuse, or compose and submit one post. Returns without waiting for the air.

        Refusals are checked in design D5's order and raised as
        `ChannelPostError`; a refused post submits nothing and records nothing.
        """
        channel = self._channels.by_id(channel_id)
        try:
            if channel is None:
                raise ChannelNotLoadedError(
                    f"Channel {channel_id} is not loaded in this run, so nothing was sent."
                )
            if not any(
                loaded.identity.public_key == entity.identity.public_key
                for loaded in self.entities
            ):
                raise IdentityNotLoadedError(
                    f"The identity {entity.name!r} is not loaded in this run, so nothing "
                    "was sent."
                )
            check_sender_name(entity.name)
            check_post_length(entity.name, text)
            if not self.transmit_enabled():
                raise TransmitDisabledError(TRANSMIT_DISABLED)
        except ChannelPostError as exc:
            self.posts_refused += 1
            self._log.info(
                "channel_post_refused",
                outcome="refused",
                channel_id=channel_id,
                entity_name=entity.name,
                reason=str(exc),
                actor=actor,
            )
            self._emit(
                ChannelPostRefused(
                    channel_id=channel_id,
                    channel_name=None if channel is None else channel.name,
                    entity_name=entity.name,
                    reason=str(exc),
                    actor=actor,
                )
            )
            raise

        now = self.clock.now()
        # Station-wide and strictly increasing: two identical posts in one second
        # would otherwise share a packet hash, and a repeater would drop the second.
        timestamp = max(int(now.timestamp()), self._last_timestamp + 1)
        self._last_timestamp = timestamp
        plaintext = build_group_text_body(timestamp, entity.name, text)
        packet, payload = build_channel_packet(channel, plaintext)
        post_id = uuid.uuid4().hex[:16]
        record = ChannelMessageRecord(
            channel_id=channel.id,
            direction=OUTBOUND,
            ref=post_id,
            text=text.encode("utf-8"),
            wire_timestamp=timestamp,
            handled_at=now,
            outcome=ChannelOutcome.AWAITING,
            entity_public_key=entity.identity.public_key,
        )
        handle = self.submit(
            Submission(
                packet=packet,
                priority=PriorityClass.MESSAGE,
                entity_id=entity.entity_id,
                deadline=now + dt.timedelta(seconds=POST_DEADLINE_SECONDS),
                entity_name=entity.name,
                entity_type="entity",
                origin="channel_post",
            )
        )
        self.posts_submitted += 1
        self._record(record)
        self._log.info(
            "channel_post_submitted",
            post_id=post_id,
            channel=channel.name,
            entity_name=entity.name,
            message_timestamp=timestamp,
            text_bytes=len(record.text),
            size_bytes=len(packet),
            actor=actor,
        )
        self._emit(
            ChannelPostSubmitted(
                post_id=post_id,
                channel_id=channel.id,
                channel_name=channel.name,
                entity_name=entity.name,
                text=text,
                wire_timestamp=timestamp,
                packet_bytes=len(packet),
                actor=actor,
            )
        )
        resolution = asyncio.create_task(
            self._resolve(handle, record, channel, entity, payload),
            name=f"channel-post-{post_id}",
        )
        self._resolutions.add(resolution)
        resolution.add_done_callback(self._resolutions.discard)
        return ChannelPost(post_id=post_id, record=record, resolution=resolution)

    async def _resolve(
        self,
        handle: TxHandle,
        record: ChannelMessageRecord,
        channel: LoadedChannel,
        entity: LocalEntity,
        payload: bytes,
    ) -> ChannelMessageRecord:
        outcome: TxOutcome = await handle
        if outcome.result is TxResult.TRANSMITTED:
            resolved = replace(
                record, outcome=ChannelOutcome.TRANSMITTED, packet_id=outcome.packet_id
            )
            self.posts_transmitted += 1
            self.registry.remember(
                content_key(int(PayloadType.GRP_TXT), payload),
                resolved,
                channel.name,
                self.clock.now(),
            )
        else:
            resolved = replace(
                record,
                outcome=ChannelOutcome.NOT_TRANSMITTED,
                packet_id=outcome.packet_id,
                outcome_reason=outcome.reason or str(outcome.result),
            )
        self._record(resolved)
        emit = self._log.info if outcome.sent else self._log.error
        emit(
            "channel_post_resolved",
            post_id=record.ref,
            channel=channel.name,
            entity_name=entity.name,
            message_outcome=str(resolved.outcome),
            **outcome.as_json(),
        )
        self._emit(
            ChannelPostResolved(
                post_id=record.ref,
                channel_name=channel.name,
                entity_name=entity.name,
                outcome=resolved.outcome,
                packet_id=outcome.packet_id,
                reason=resolved.outcome_reason or "",
                airtime_ms=outcome.airtime_ms,
            )
        )
        return resolved

    def as_json(self) -> dict[str, object]:
        return {
            "channels_loaded": len(self._channels),
            "channel_decrypted": self.decrypted,
            "channel_unknown": self.unknown,
            "channel_undecryptable": self.undecryptable,
            "channel_unsupported": self.unsupported,
            "channel_posts_transmitted": self.posts_transmitted,
            "channel_repeats_heard": self.repeats_heard,
        }
