"""Direct messages, both directions (DESIGN.md §4.2 step 5, §4.3, §5).

Every wire detail here is taken from the firmware rather than from the payload
documentation, because the documentation does not describe the cryptography and
was wrong about the acknowledgement construction until `BaseChatMesh.cpp` was
read (§5):

* **Composition** (`BaseChatMesh.cpp:425-433`): `timestamp` (4 bytes, LE) ‖
  `flags` ‖ `text`, where `flags` is `(attempt & 3) | (txt_type << 2)`. The
  firmware copies a NUL terminator into its own buffer and then transmits
  `5 + text_len` bytes, so the terminator is neither sent nor hashed.
* **Expected acknowledgement** (`:431`): the first 4 bytes of
  `sha256(transmitted plaintext ‖ our public key)`. The acknowledgement we emit
  for a message we received (`:243`) is the same hash over the received
  plaintext with the *sender's* key.
* **Comparison** (`:245`, `:740`): an acknowledgement payload may be 4 or 6
  bytes — one firmware path appends an extended attempt byte and a random one —
  and only the first 4 are compared.
* **Retry cadence** (`MyMesh.cpp:851-858`): `500 ms + 16 x airtime` flooded,
  `500 ms + (6 x airtime + 250 ms) x (hops + 1)` direct. Copying the peer's own
  arithmetic is what keeps our resend from arriving while it still considers the
  previous attempt live.

Attempts stop at 3. The firmware's `attempt > 3` variant hides the attempt
number in a tail after a NUL (`:434-437`); it is understood and deliberately not
produced, because it would be untested code on the air.

Two rules that are not wire details:

* **A MAC match selects a key. It never authenticates a sender** (design D7). Two
  bytes is roughly 1 in 2^16 per candidate, and the 1-byte destination hash that
  produced the candidate set collides at 1 in 256. A decrypted message's
  originator is a *claimed* contact, and the renderer says so.
* **Flooding requires an explicit flag** (design D4), inverting the firmware's
  default. On a desk-to-desk exercise a flood is the only way this milestone can
  put load on other people's repeaters, and it must not be reachable by
  mistyping a peer name.

Inbound handling is a bus subscriber, never part of `net/rx.py`: decryption
needs keys and contacts, and the decode stage stays a pure function of one frame
so that replaying a capture keeps reproducing every reception exactly (design
D6).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from sighop.logging import Logger, get_logger
from sighop.net.acks import AckMatch, AckRegistry, AckUnowned
from sighop.net.airtime import NoRadioReadback, require_params, time_on_air_ms
from sighop.net.bus import NetworkBus, PriorityClass, Submission, Subscription, TxHandle
from sighop.net.contacts import Contact, ContactStore
from sighop.net.paths import PathStore
from sighop.net.readback import wait_for_readback
from sighop.net.rx import Payload, RxRecord
from sighop.net.tx import Clock, SystemClock
from sighop.protocol.crypto import (
    SharedSecretCache,
    ack_checksum_for,
    encrypt_then_mac,
    mac_then_decrypt,
)
from sighop.protocol.identity import LocalIdentity
from sighop.protocol.packet import (
    PAYLOAD_VERSION_1,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
)
from sighop.protocol.packet import encode as encode_packet
from sighop.protocol.payloads import (
    Acknowledgement,
    DirectEnvelope,
    TextMessageBody,
    TextType,
    WireText,
    build_ack,
    build_direct_envelope,
    build_text_message_body,
    parse_text_message_body,
)
from sighop.protocol.result import DecodeFailure
from sighop.radio.modem import RadioParams

MAX_TEXT_LEN = 160
"""`BaseChatMesh.h`: `10 * CIPHER_BLOCK_SIZE`."""

MAX_ATTEMPT = 3
"""Attempts 0-3. Above that the firmware switches to a tail encoding we do not
produce (`BaseChatMesh.cpp:434-437`, design D8)."""

SEND_TIMEOUT_BASE_MS = 500.0
FLOOD_SEND_TIMEOUT_FACTOR = 16.0
DIRECT_SEND_PERHOP_FACTOR = 6.0
DIRECT_SEND_PERHOP_EXTRA_MS = 250.0
"""`MyMesh.cpp:103-106`. These are `examples/companion_radio` defines rather than
library constants, so a different peer build could differ; the measured
acknowledgement latency in the first-transmit exercise is what settles it."""

ACK_OWNER = "direct-messages"
"""What this messenger registers its expectations under in the shared registry
(design D11), and what a match is attributed to in the status line."""

ACK_POLL_SECONDS = 0.05
"""How often the send loop looks at its own deadline. The wait is driven by the
injected clock rather than by `asyncio.wait_for`, so a simulated retry sequence
runs in a test without four real timeouts elapsing."""

DEFAULT_ACK_GRACE_MS = 0.0
"""How long a resolved-unacknowledged send keeps listening, by default: not at all.

A caller that asks for a grace window is saying its *decision* is expensive —
the greeter's next move is a flood advert — and that it would rather wait than
act on an acknowledgement that was merely late. Nothing extra is transmitted
during the window; the expectations that are already registered simply stay
registered, so a late acknowledgement is matched instead of being counted
`ack_unmatched`. It is off by default because an interactive send that returned
seconds after it had already failed would read as a hang."""


class DirectMessageError(RuntimeError):
    """A message that could not be composed or routed as asked."""


class MessageTooLongError(DirectMessageError):
    """Text above the firmware's maximum. Refused before anything is queued."""


class NoRouteError(DirectMessageError):
    """No route is known and flooding was not explicitly permitted (design D4)."""


# --- Routing ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Route:
    """How a packet is addressed onto the mesh.

    An empty path with `flood` false is a **zero-hop direct** route — a real
    route and the most useful one there is, not the absence of one. `ambiguous`
    is true when the route was found by node hash rather than by public key,
    which is one byte of identity and travels with the route so the output can
    say so.
    """

    flood: bool
    path: bytes = b""
    hash_size: int = 1
    hop_count: int = 0
    ambiguous: bool = False

    @property
    def route_type(self) -> RouteType:
        return RouteType.FLOOD if self.flood else RouteType.DIRECT

    @property
    def label(self) -> str:
        if self.flood:
            return "FLOOD"
        suffix = "?" if self.ambiguous else ""
        return f"DIRECT h{self.hop_count}{suffix}"


def choose_route(paths: PathStore, contact: Contact, *, allow_flood: bool = False) -> Route:
    """The route to a contact, or a refusal (design D4).

    A route keyed by the peer's public key is preferred; a route keyed by its
    node hash is used when that is all there is — an inbound direct message
    names only a hash, so its acknowledgement has nothing else to go on — and is
    marked ambiguous rather than presented as certain.
    """
    learned = paths.lookup_public_key(contact.public_key)
    ambiguous = False
    if learned is None:
        learned = paths.lookup_node_hash(contact.node_hash)
        ambiguous = learned is not None
    if learned is None:
        if not allow_flood:
            raise NoRouteError(
                f"no route is known to {contact.display_name} "
                f"({contact.public_key.hex()[:16]}); flooding was not permitted, "
                "and a flood is repeated by every repeater in the mesh"
            )
        return Route(flood=True)
    return Route(
        flood=False,
        path=learned.path,
        hash_size=learned.hash_size,
        hop_count=learned.hop_count,
        ambiguous=ambiguous,
    )


def ack_timeout_ms(airtime_ms: float, route: Route) -> float:
    """The peer's own acknowledgement timeout for this packet (`MyMesh.cpp:851`)."""
    if route.flood:
        return SEND_TIMEOUT_BASE_MS + FLOOD_SEND_TIMEOUT_FACTOR * airtime_ms
    return SEND_TIMEOUT_BASE_MS + (
        airtime_ms * DIRECT_SEND_PERHOP_FACTOR + DIRECT_SEND_PERHOP_EXTRA_MS
    ) * (route.hop_count + 1)


# --- Composition -----------------------------------------------------------


def compose_body(
    *,
    timestamp: int,
    attempt: int,
    text: bytes,
    txt_type: TextType | int = TextType.PLAIN,
) -> TextMessageBody:
    """The plaintext body of an outbound text message.

    Refuses over-length text here, before anything is composed, encrypted or
    queued — the limit belongs to the message, not to the packet it becomes.
    """
    if len(text) > MAX_TEXT_LEN:
        raise MessageTooLongError(
            f"message text is {len(text)} bytes; the firmware's MAX_TEXT_LEN is {MAX_TEXT_LEN}"
        )
    if not 0 <= attempt <= MAX_ATTEMPT:
        raise DirectMessageError(
            f"attempt {attempt} is outside 0..{MAX_ATTEMPT}; the extended-attempt "
            "encoding above 3 is deliberately not produced"
        )
    return TextMessageBody(
        timestamp=timestamp,
        txt_type=txt_type,
        attempt=attempt,
        text=WireText.from_bytes(text),
    )


def build_message_packet(
    *,
    sender: LocalIdentity,
    recipient_node_hash: int,
    secret: bytes,
    body: TextMessageBody,
    route: Route,
) -> tuple[bytes, bytes]:
    """Encrypt-then-MAC a body into a `TXT_MSG` packet.

    Returns `(packet bytes, transmitted plaintext)`. The plaintext is returned
    because the acknowledgement is computed over exactly what went out, and
    reconstructing it later is how the two drift apart.
    """
    plaintext = build_text_message_body(body)
    mac, ciphertext = encrypt_then_mac(secret, plaintext)
    envelope = DirectEnvelope(
        payload_type=PayloadType.TXT_MSG,
        dest_hash=recipient_node_hash,
        src_hash=sender.node_hash,
        mac=mac,
        ciphertext=ciphertext,
    )
    packet = encode_packet(
        Packet(
            header=PacketHeader(
                route_type=route.route_type,
                payload_type=PayloadType.TXT_MSG,
                payload_version=PAYLOAD_VERSION_1,
            ),
            transport_codes=None,
            hop_count=route.hop_count,
            hash_size=route.hash_size,
            path=route.path,
            payload=build_direct_envelope(envelope),
        )
    )
    return packet, plaintext


def build_ack_packet(*, checksum: bytes, route: Route) -> bytes:
    """The 4-byte acknowledgement form. We accept 6 and emit 4 (design D8)."""
    return encode_packet(
        Packet(
            header=PacketHeader(
                route_type=route.route_type,
                payload_type=PayloadType.ACK,
                payload_version=PAYLOAD_VERSION_1,
            ),
            transport_codes=None,
            hop_count=route.hop_count,
            hash_size=route.hash_size,
            path=route.path,
            payload=build_ack(Acknowledgement(checksum=checksum)),
        )
    )


# --- What the runtime is told ----------------------------------------------


class LocalEntity(Protocol):
    """The slice of an entity the messenger needs. `EntityStub` satisfies it."""

    entity_id: str
    name: str
    identity: LocalIdentity

    @property
    def node_hash(self) -> int: ...


class SendResult(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    UNACKNOWLEDGED = "unacknowledged"
    """Every attempt was transmitted and none was answered."""

    DROPPED = "dropped"
    """Never reached the air: the scheduler dropped or failed it."""


@dataclass(frozen=True, slots=True)
class SendOutcome:
    """How a send ended. Every send resolves as one of these and reports it."""

    result: SendResult
    message_id: str
    attempts: int
    route: Route
    packet_ids: tuple[str, ...] = ()
    ack_latency_ms: float | None = None
    reason: str = ""

    @property
    def acknowledged(self) -> bool:
        return self.result is SendResult.ACKNOWLEDGED

    def as_json(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "send_result": str(self.result),
            "attempts": self.attempts,
            "route": self.route.label,
            "packet_ids": list(self.packet_ids),
            "ack_latency_ms": (
                None if self.ack_latency_ms is None else round(self.ack_latency_ms, 1)
            ),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MessageSent:
    message_id: str
    entity_name: str
    contact: Contact
    text: str
    attempt: int
    route: Route
    packet_id: str
    size_bytes: int
    airtime_ms: float
    ack_timeout_ms: float
    expected_ack: bytes
    transmitted: bool


@dataclass(frozen=True, slots=True)
class AckMatched:
    message_id: str
    contact: Contact
    checksum: bytes
    attempt: int
    latency_ms: float
    packet_id: str
    payload_bytes: int


@dataclass(frozen=True, slots=True)
class AckUnmatched:
    checksum: bytes
    packet_id: str
    outstanding: int


@dataclass(frozen=True, slots=True)
class SendResolved:
    outcome: SendOutcome
    contact: Contact
    text: str


@dataclass(frozen=True, slots=True)
class MessageReceived:
    """A decrypted message. `contact` is a **claimed** sender, never a proven one.

    `entity` is the local identity that received it, carried beside the display
    name rather than instead of it (milestone 7 design D15). A consumer that
    wants to *reply as* the addressed entity cannot resolve `entity_name` back
    to an identity when two entities share a display name, and inventing a
    lookup at the consumer would put the ambiguity somewhere it cannot be
    resolved. `monitor/render.py` keeps using the name; nothing else changes,
    and the acknowledgement is still submitted before this report is delivered.

    Optional only so that a report constructed by hand — a renderer's test —
    stays constructible; every report the messenger produces carries one.
    """

    entity_name: str
    contact: Contact
    body: TextMessageBody
    packet_id: str
    candidates_tried: int
    acknowledged: bool
    entity: LocalEntity | None = None


@dataclass(frozen=True, slots=True)
class MessageUndecryptable:
    packet_id: str
    dest_hash: int
    src_hash: int
    candidates_tried: int


@dataclass(frozen=True, slots=True)
class MessageUnparsable:
    """A MAC matched and the plaintext did not parse — reported, never acknowledged."""

    packet_id: str
    entity_name: str
    contact: Contact
    reason: str


type DirectMessageEvent = (
    MessageSent
    | AckMatched
    | AckUnmatched
    | SendResolved
    | MessageReceived
    | MessageUndecryptable
    | MessageUnparsable
)


# --- What is offered to be made durable (milestone 8, design D7/D8) ---------


class RecordedOutcome(StrEnum):
    """How a recorded message stood when it was last offered.

    The three send results, plus the two states a `SendOutcome` cannot express
    because they are not endings:

    * `IN_FLIGHT` — submitted, not yet resolved. What a record written at
      submission says, and therefore what a restart mid-send leaves behind: an
      outcome that is *unknown*, rather than a claim of delivery or a row that
      quietly never existed.
    * `RECEIVED` — a message that arrived and was decrypted. It has no delivery
      outcome of its own; the acknowledgement we sent for it is a transmission
      of ours and is recorded in the packet log like any other.
    """

    IN_FLIGHT = "in_flight"
    ACKNOWLEDGED = "acknowledged"
    UNACKNOWLEDGED = "unacknowledged"
    DROPPED = "dropped"
    RECEIVED = "received"


OUTBOUND = "out"
INBOUND = "in"
"""`direct_message.direction`, as `packet_log.direction` is `tx` | `rx`."""


@dataclass(frozen=True, slots=True)
class DirectMessageRecord:
    """One direct message, in the shape something durable can store it.

    Keyed by `(entity_public_key, ref)`: `ref` is the send's `message_id`
    outbound and the reception's `packet_id` inbound, so a send offered at
    submission and again when it resolves is *one* message offered twice, not
    two messages. Whatever holds these is expected to update in place.

    `text` is the bytes that were on the wire, untranscoded — the same rule
    `message.text` follows, for the same reason (milestone 6 design D4): text
    off the wire is `WireText` and is not guaranteed valid UTF-8.

    `wire_timestamp` is the peer's own clock as it travels; `handled_at` is
    ours. Nothing may order a conversation by the first.

    `row_id` is filled in by whatever read the record back and is `None` on one
    the messenger just built. It exists so a reader can page a conversation
    stably when several messages share a `handled_at`.
    """

    entity_public_key: bytes
    peer_public_key: bytes
    direction: str
    text: bytes
    wire_timestamp: int
    handled_at: dt.datetime
    ref: str
    outcome: RecordedOutcome
    packet_ids: tuple[str, ...] = ()
    attempts: int = 0
    route_flood: bool | None = None
    route_path: bytes | None = None
    ack_latency_ms: float | None = None
    row_id: int | None = None

    @property
    def inbound(self) -> bool:
        return self.direction == INBOUND

    @property
    def resolved(self) -> bool:
        """Whether this message's fate is known. An in-flight one's is not."""
        return self.outcome is not RecordedOutcome.IN_FLIGHT

    def rendered(self) -> WireText:
        """The text as something displayable, marked as a rendering."""
        return WireText.from_bytes(self.text)

    def as_json(self) -> dict[str, object]:
        return {
            "entity_public_key": self.entity_public_key.hex(),
            "peer_public_key": self.peer_public_key.hex(),
            "direction": self.direction,
            "ref": self.ref,
            "message_outcome": str(self.outcome),
            "attempts": self.attempts,
            "text_bytes": len(self.text),
            "packet_ids": list(self.packet_ids),
            "ack_latency_ms": (
                None if self.ack_latency_ms is None else round(self.ack_latency_ms, 1)
            ),
        }


class DirectMessageSink(Protocol):
    """Where direct messages go to be recorded. Never awaits, never raises.

    The contact and path sinks' contract exactly (`ContactSink`), and for the
    same reason: this is offered from the message path, and a database that is
    slow, unreachable or blackholing must not be able to delay an
    acknowledgement the protocol computes on decrypt. `offer` returning False
    means the buffer refused the record — the caller is not expected to do
    anything about it beyond having been told (design D8).
    """

    def offer(self, record: DirectMessageRecord) -> bool: ...


# --- The messenger ---------------------------------------------------------


@dataclass(slots=True)
class _Pending:
    """One send in flight, with the expected acknowledgement of every attempt."""

    message_id: str
    entity: LocalEntity
    contact: Contact
    text: str
    raw: bytes = b""
    """The bytes composed, kept beside the rendering: what is recorded is what
    was on the wire, and `text` above is already lossy by construction."""

    timestamp: int = 0
    """The message's own wire timestamp, fixed for the whole send."""

    submitted_at: dt.datetime | None = None
    route: Route | None = None
    expectations: list[bytes] = field(default_factory=list)
    attempts: int = 0
    sent_at: dt.datetime | None = None
    matched: asyncio.Event = field(default_factory=asyncio.Event)
    matched_checksum: bytes | None = None
    matched_at: dt.datetime | None = None
    matched_attempt: int = 0


class DirectMessenger:
    """Sends direct messages, and handles the ones that arrive.

    Holds no policy of its own beyond what the firmware and design D4/D8/D9/D10
    fix: the scheduler owns the budget and the gate, `net/paths.py` owns routes,
    and `net/contacts.py` owns who is who.
    """

    def __init__(
        self,
        *,
        contacts: ContactStore,
        paths: PathStore,
        submit: Callable[[Submission], TxHandle],
        entities: Sequence[LocalEntity] = (),
        secrets: SharedSecretCache | None = None,
        clock: Clock | None = None,
        radio: RadioParams | None = None,
        radio_ready: asyncio.Event | None = None,
        allow_flood: bool = False,
        on_event: Callable[[DirectMessageEvent], None] | None = None,
        logger: Logger | None = None,
        acks: AckRegistry | None = None,
        records: DirectMessageSink | None = None,
    ) -> None:
        self.contacts = contacts
        self.paths = paths
        self.submit = submit
        self.entities: list[LocalEntity] = list(entities)
        self.secrets = secrets or SharedSecretCache()
        self.clock = clock or SystemClock()
        self.radio = radio
        self.radio_ready = radio_ready
        """Set while the board's parameters are current, cleared by a reconnect
        that loses them. `None` where nobody supplied one — every unit test that
        builds a messenger directly, and the replay path, which has the radio
        from the capture's provenance before it starts — and then nothing
        waits, which is exactly the behaviour those had before this existed."""
        self.allow_flood = allow_flood
        self._on_event = on_event
        self._log = logger or get_logger(component="dm")
        # Design D11: the expectation table is shared. When one is handed in,
        # something else in the process also waits on acknowledgements and a
        # dispatcher of its own is subscribed to the bus, so this messenger
        # stops taking acknowledgement records off it — one subscriber matches
        # them, or "unmatched" means two different things at once.
        self.acks = acks or AckRegistry(logger=self._log)
        self._owns_acks = acks is None
        # A list rather than one sink: a run can be both recording durably and
        # showing a conversation in a browser, and those are two consumers of
        # the same offer rather than one wrapping the other.
        self._records: list[DirectMessageSink] = [] if records is None else [records]
        self._pending_by_checksum: dict[bytes, _Pending] = {}
        self._room_entity_ids: set[str] = set()
        # One lock per (identity, peer): sends to different peers proceed
        # together, sends within one conversation do not overlap (§
        # `direct-messaging`). Kept rather than reaped — one `asyncio.Lock` per
        # contact we have ever talked to is bounded by the contact table, and a
        # lock removed while a waiter held a reference to it would be a lock
        # that stopped ordering anything.
        self._conversations: dict[tuple[str, bytes], asyncio.Lock] = {}
        self.received = 0
        self.undecryptable = 0
        self.sent = 0
        self.records_offered = 0
        self.records_refused = 0
        """Offers the sink turned away. Counted here as well as in the sink,
        because from this side "the record was not taken" is the whole of what
        the messenger can know or do about it (design D8)."""

    @property
    def _outstanding(self) -> dict[bytes, _Pending]:
        """This messenger's own share of the shared table, for reporting only."""
        return self._pending_by_checksum

    def add_entity(self, entity: LocalEntity) -> None:
        self.entities.append(entity)

    def set_radio(self, radio: RadioParams | None) -> None:
        self.radio = radio

    def _emit(self, event: DirectMessageEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)

    # --- Recording (design D8) ---------------------------------------------

    def add_record_sink(self, sink: DirectMessageSink) -> None:
        """Attach another consumer of what this messenger sends and receives.

        Same contract as the first, and offered the same records in the same
        order. One sink refusing or raising changes nothing for the others: they
        are independent consumers of one offer, not a chain.
        """
        self._records.append(sink)

    def _record(self, record: DirectMessageRecord) -> None:
        """Offer one message to every sink. Never awaits, never raises.

        The contact and path sinks' contract, and the reason it is a method
        rather than a call site: this is invoked from the send loop and from
        inside message handling, and *both* have to be places where a sink that
        raises changes nothing — not the acknowledgement, not the retry
        schedule, not the reported outcome.
        """
        for sink in self._records:
            try:
                taken = sink.offer(record)
            except Exception as exc:
                # A sink that raises is a broken sink, not a broken message.
                # Report it and carry on: there is nothing on this path that a
                # failure to write history should be allowed to change.
                self.records_refused += 1
                self._log.error(
                    "direct_message_record_raised",
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
                "direct_message_record_refused",
                outcome="error",
                ref=record.ref,
                direction=record.direction,
                detail=(
                    "a record sink refused the record; this message is not in that sink's history"
                ),
            )

    def _sent_record(
        self,
        pending: _Pending,
        outcome: RecordedOutcome,
        packet_ids: Sequence[str],
    ) -> DirectMessageRecord:
        route = pending.route
        return DirectMessageRecord(
            entity_public_key=pending.entity.identity.public_key,
            peer_public_key=pending.contact.public_key,
            direction=OUTBOUND,
            text=pending.raw,
            wire_timestamp=pending.timestamp,
            handled_at=pending.submitted_at or self.clock.now(),
            ref=pending.message_id,
            outcome=outcome,
            packet_ids=tuple(packet_ids),
            attempts=pending.attempts,
            route_flood=None if route is None else route.flood,
            route_path=None if route is None else route.path,
            ack_latency_ms=(
                None
                if pending.matched_at is None or pending.sent_at is None
                else (pending.matched_at - pending.sent_at).total_seconds() * 1000.0
            ),
        )

    # --- Outbound ----------------------------------------------------------

    async def send(
        self,
        entity: LocalEntity,
        contact: Contact,
        text: str | bytes,
        *,
        allow_flood: bool | None = None,
        txt_type: TextType | int = TextType.PLAIN,
        ack_grace_ms: float = DEFAULT_ACK_GRACE_MS,
    ) -> SendOutcome:
        """Send one message, retrying to `MAX_ATTEMPT`, and report the outcome.

        The timestamp is fixed for the whole send and the attempt counter moves,
        which is what the field exists for: an identical retransmission is what
        every repeater in the path would deduplicate, so the flags byte — hence
        the ciphertext, hence the packet hash — has to differ (design D9).

        `ack_grace_ms` extends only the *listening*, never the transmitting: see
        `DEFAULT_ACK_GRACE_MS`. The retry count is untouched, so a caller that
        asks for a grace window still puts exactly `MAX_ATTEMPT + 1` packets on
        the air at most.
        """
        raw = text.encode("utf-8") if isinstance(text, str) else text
        flooding = self.allow_flood if allow_flood is None else allow_flood
        # Composition limits are checked before anything is routed or queued.
        compose_body(timestamp=0, attempt=0, text=raw, txt_type=txt_type)
        route = choose_route(self.paths, contact, allow_flood=flooding)

        timestamp = int(self.clock.now().timestamp())
        pending = _Pending(
            message_id=uuid.uuid4().hex[:16],
            entity=entity,
            contact=contact,
            text=raw.decode("utf-8", errors="replace"),
            raw=raw,
            timestamp=timestamp,
            submitted_at=self.clock.now(),
            route=route,
        )
        # Recorded at submission rather than at first transmission, so a message
        # waiting behind another in its own conversation is visible as submitted
        # instead of appearing only once it starts. The record is updated in
        # place when the send resolves; `ref` is what joins the two.
        self._record(self._sent_record(pending, RecordedOutcome.IN_FLIGHT, ()))
        async with self._conversation(entity, contact):
            return await self._send_locked(
                pending,
                raw=raw,
                route=route,
                timestamp=timestamp,
                txt_type=txt_type,
                ack_grace_ms=ack_grace_ms,
            )

    def _conversation(self, entity: LocalEntity, contact: Contact) -> asyncio.Lock:
        """The lock that keeps one conversation in submission order.

        Per identity *and* peer, which is the same key the history is stored
        under: two sends to different peers are unrelated and must not wait on
        each other's acknowledgement window, and two sends within one
        conversation must not be able to reach the air out of the order they
        were submitted in.
        """
        key = (entity.entity_id, contact.public_key)
        lock = self._conversations.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._conversations[key] = lock
        return lock

    async def _send_locked(
        self,
        pending: _Pending,
        *,
        raw: bytes,
        route: Route,
        timestamp: int,
        txt_type: TextType | int,
        ack_grace_ms: float,
    ) -> SendOutcome:
        """The attempt loop, with this conversation's turn already taken."""
        entity = pending.entity
        contact = pending.contact
        packet_ids: list[str] = []
        secret = self.secrets.get(entity.identity, contact.public_key)

        try:
            for attempt in range(MAX_ATTEMPT + 1):
                body = compose_body(
                    timestamp=timestamp, attempt=attempt, text=raw, txt_type=txt_type
                )
                packet, _plaintext = build_message_packet(
                    sender=entity.identity,
                    recipient_node_hash=contact.node_hash,
                    secret=secret,
                    body=body,
                    route=route,
                )
                # Over exactly the transmitted plaintext: `ack_prefix` is what
                # `build_message_packet` encrypted, so the two cannot drift.
                expected = ack_checksum_for(body, entity.identity.public_key)
                pending.expectations.append(expected)
                self._pending_by_checksum[expected] = pending
                self.acks.register(expected, owner=ACK_OWNER, on_match=self._on_ack_match)

                try:
                    await wait_for_readback(self.radio_ready)
                    airtime = time_on_air_ms(len(packet), require_params(self.radio))
                except NoRadioReadback as exc:
                    return self._resolve_send(
                        pending, SendResult.DROPPED, route, packet_ids, str(exc)
                    )
                timeout = ack_timeout_ms(airtime, route)

                now = self.clock.now()
                pending.attempts = attempt + 1
                pending.sent_at = now
                handle = self.submit(
                    Submission(
                        packet=packet,
                        priority=PriorityClass.MESSAGE,
                        entity_id=entity.entity_id,
                        entity_name=entity.name,
                        entity_type="entity",
                        # A message that cannot reach the air inside its own
                        # retry window is better dropped and logged than sent
                        # late into a retry (design D10).
                        deadline=now + dt.timedelta(milliseconds=timeout),
                        origin="direct_message",
                    )
                )
                outcome = await handle
                packet_ids.append(outcome.packet_id)
                self._emit(
                    MessageSent(
                        message_id=pending.message_id,
                        entity_name=entity.name,
                        contact=contact,
                        text=pending.text,
                        attempt=attempt,
                        route=route,
                        packet_id=outcome.packet_id,
                        size_bytes=len(packet),
                        airtime_ms=airtime,
                        ack_timeout_ms=timeout,
                        expected_ack=expected,
                        transmitted=outcome.sent,
                    )
                )
                self._log.info(
                    "direct_message_sent",
                    message_id=pending.message_id,
                    send_attempt=attempt,
                    route=route.label,
                    expected_ack=expected.hex(),
                    ack_timeout_ms=round(timeout, 1),
                    **outcome.as_json(),
                )
                if outcome.result.value in ("dropped", "failed"):
                    return self._resolve_send(
                        pending,
                        SendResult.DROPPED,
                        route,
                        packet_ids,
                        outcome.reason or str(outcome.result),
                    )
                self.sent += 1
                if await self._await_ack(pending, timeout):
                    return self._resolve_send(pending, SendResult.ACKNOWLEDGED, route, packet_ids)
            # Every attempt is spent. Before calling it unacknowledged, keep
            # listening if the caller asked to: an acknowledgement returning
            # over a different, longer path is late rather than absent, and the
            # expectations for all four attempts are still registered.
            if ack_grace_ms > 0 and await self._await_ack(pending, ack_grace_ms):
                return self._resolve_send(pending, SendResult.ACKNOWLEDGED, route, packet_ids)
            return self._resolve_send(
                pending,
                SendResult.UNACKNOWLEDGED,
                route,
                packet_ids,
                f"no acknowledgement after {MAX_ATTEMPT + 1} attempts"
                + (f" and {ack_grace_ms / 1000:.0f}s grace" if ack_grace_ms > 0 else ""),
            )
        finally:
            for expectation in pending.expectations:
                self._pending_by_checksum.pop(expectation, None)
                self.acks.release(expectation)

    async def _await_ack(self, pending: _Pending, timeout_ms: float) -> bool:
        """Wait out this attempt's acknowledgement window on the injected clock."""
        deadline = self.clock.now() + dt.timedelta(milliseconds=timeout_ms)
        while True:
            if pending.matched.is_set():
                return True
            if self.clock.now() >= deadline:
                return False
            await self.clock.sleep(ACK_POLL_SECONDS)

    def _resolve_send(
        self,
        pending: _Pending,
        result: SendResult,
        route: Route,
        packet_ids: list[str],
        reason: str = "",
    ) -> SendOutcome:
        latency: float | None = None
        if pending.matched_at is not None and pending.sent_at is not None:
            latency = (pending.matched_at - pending.sent_at).total_seconds() * 1000.0
        outcome = SendOutcome(
            result=result,
            message_id=pending.message_id,
            attempts=pending.attempts,
            route=route,
            packet_ids=tuple(packet_ids),
            ack_latency_ms=latency,
            reason=reason,
        )
        # Every send resolves out loud. An unacknowledged message is a reported
        # outcome, never a silent one.
        emit = self._log.info if outcome.acknowledged else self._log.error
        emit("direct_message_resolved", **outcome.as_json())
        # The same message the sink was offered at submission, identified by the
        # same `ref` and now carrying its outcome. Offered before the event is
        # emitted, so a consumer told the send resolved can read the resolved
        # record rather than racing it.
        pending.route = route
        self._record(self._sent_record(pending, RecordedOutcome(str(result)), packet_ids))
        self._emit(SendResolved(outcome=outcome, contact=pending.contact, text=pending.text))
        return outcome

    # --- Inbound -----------------------------------------------------------

    async def handle(self, record: RxRecord) -> None:
        """Bus handler: `TXT_MSG` receptions, and the acknowledgements we wait on.

        Acknowledgements are taken here too because the send path is what waits
        for them; nothing else in the system holds the outstanding expectations.
        """
        match record.outcome:
            case Payload(payload=DirectEnvelope() as envelope) if (
                envelope.payload_type is PayloadType.TXT_MSG
            ):
                await self._handle_text_message(record, envelope)
            case Payload(payload=Acknowledgement() as ack) if self._owns_acks:
                self._handle_ack(record, ack)
            case _:
                return

    def subscribe(self, bus: NetworkBus, *, name: str = "direct-messages") -> Subscription:
        return bus.subscribe(name, handler=self.handle)

    def claim_for_room(self, entity_id: str) -> None:
        """Mark an entity as a room server's, so this messenger leaves it alone.

        Design D10: without this, both subscribers decrypt a member's post and
        both acknowledge it — two decryptions and two acknowledgements on the
        air for one packet, with two contradictory log lines to explain it. The
        rule is static and needs no coordination at reception time; the runtime
        applies it once, at wiring time, which is when the ambiguity is
        resolvable.
        """
        self._room_entity_ids.add(entity_id)

    def release_from_room(self, entity_id: str) -> None:
        """Undo `claim_for_room`, so this messenger owns the entity again.

        The one case: the room has been deleted. Its identity outlives it and
        goes back to being an ordinary one, and leaving it claimed would make
        a direct message to it fall to nobody — the room server that used to
        answer is gone, and this messenger would still be standing aside for it.
        """
        self._room_entity_ids.discard(entity_id)

    def _candidates(
        self, envelope: DirectEnvelope
    ) -> tuple[list[LocalEntity], tuple[Contact, ...]]:
        """(local entities on the destination hash) x (possible senders), design D7.

        An entity serving a room is not among them. Note that this filters
        *entities*, not the destination hash: an ordinary entity that happens to
        share a node hash with a room server is still tried, because one byte of
        identity collides at 1 in 256 (§3) and dropping the packet for the
        collision would be dropping someone's message.
        """
        entities = [
            entity
            for entity in self.entities
            if entity.node_hash == envelope.dest_hash
            and entity.entity_id not in self._room_entity_ids
        ]
        contacts = tuple(self.contacts.by_node_hash(envelope.src_hash))
        if not contacts:
            # A contact we have never heard from is still a possible sender, and
            # a source hash that matches nothing tells us only that.
            contacts = self.contacts.contacts()
        return entities, contacts

    async def _handle_text_message(self, record: RxRecord, envelope: DirectEnvelope) -> None:
        entities, contacts = self._candidates(envelope)
        if not entities:
            return  # addressed to a hash none of our entities carries

        tried = 0
        for entity in entities:
            for contact in contacts:
                tried += 1
                secret = self.secrets.get(entity.identity, contact.public_key)
                candidate, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
                if not candidate.matched or plaintext is None:
                    continue
                body = parse_text_message_body(plaintext)
                if isinstance(body, DecodeFailure):
                    # The MAC matched and the plaintext is not a message. That is
                    # either a ~2^-16 false match or a shape we do not parse;
                    # either way it is reported and NOT acknowledged.
                    self._log.error(
                        "direct_message_unparsable",
                        packet_id=record.packet_id,
                        entity_id=entity.entity_id,
                        failure_reason=str(body.reason),
                        failure_detail=body.detail,
                    )
                    self._emit(
                        MessageUnparsable(
                            packet_id=record.packet_id,
                            entity_name=entity.name,
                            contact=contact,
                            reason=f"{body.reason}: {body.detail}",
                        )
                    )
                    return
                self.received += 1
                acknowledged = await self._acknowledge(entity, contact, body, record)
                # Recorded *after* the acknowledgement has been submitted, never
                # before (design D8). The acknowledgement is the protocol's own
                # receipt, computed on decrypt, against a sender whose retry
                # window is 4-5 seconds; putting a durable write in front of it
                # would trade a counted gap in our own history for a real
                # protocol failure.
                self._record(
                    DirectMessageRecord(
                        entity_public_key=entity.identity.public_key,
                        peer_public_key=contact.public_key,
                        direction=INBOUND,
                        text=body.text.raw,
                        wire_timestamp=body.timestamp,
                        handled_at=record.received_at,
                        ref=record.packet_id,
                        outcome=RecordedOutcome.RECEIVED,
                        packet_ids=(record.packet_id,),
                    )
                )
                self._log.info(
                    "direct_message_received",
                    packet_id=record.packet_id,
                    entity_id=entity.entity_id,
                    claimed_sender=contact.public_key.hex(),
                    candidates_tried=tried,
                    text_bytes=len(body.text.raw),
                    message_timestamp=body.timestamp,
                    acknowledged=acknowledged,
                )
                self._emit(
                    MessageReceived(
                        entity_name=entity.name,
                        contact=contact,
                        body=body,
                        packet_id=record.packet_id,
                        candidates_tried=tried,
                        acknowledged=acknowledged,
                        entity=entity,
                    )
                )
                return

        self.undecryptable += 1
        self._log.info(
            "direct_message_undecryptable",
            packet_id=record.packet_id,
            dest_hash=envelope.dest_hash,
            src_hash=envelope.src_hash,
            candidates_tried=tried,
        )
        self._emit(
            MessageUndecryptable(
                packet_id=record.packet_id,
                dest_hash=envelope.dest_hash,
                src_hash=envelope.src_hash,
                candidates_tried=tried,
            )
        )

    async def _acknowledge(
        self,
        entity: LocalEntity,
        contact: Contact,
        body: TextMessageBody,
        record: RxRecord,
    ) -> bool:
        """Answer a decrypted message at class 0, routed as an outbound one is."""
        checksum = ack_checksum_for(body, contact.public_key)
        try:
            route = choose_route(self.paths, contact, allow_flood=self.allow_flood)
        except NoRouteError as exc:
            self._log.error(
                "ack_not_routed",
                packet_id=record.packet_id,
                claimed_sender=contact.public_key.hex(),
                reason=str(exc),
            )
            return False
        packet = build_ack_packet(checksum=checksum, route=route)
        try:
            # A message decrypted before the board answered its readback is
            # exactly as owed an answer as one that arrives an hour later, and a
            # sender that gets none retries into silence. Waiting here stalls
            # this subscriber only — `bus.py::subscribe` gives each its own task
            # and queue — so decode, dedup and path learning carry on.
            await wait_for_readback(self.radio_ready)
            airtime = time_on_air_ms(len(packet), require_params(self.radio))
        except NoRadioReadback as exc:
            self._log.error("ack_not_routed", packet_id=record.packet_id, reason=str(exc))
            return False
        now = self.clock.now()
        self.submit(
            Submission(
                packet=packet,
                priority=PriorityClass.ACK,
                entity_id=entity.entity_id,
                entity_name=entity.name,
                entity_type="entity",
                deadline=now + dt.timedelta(milliseconds=ack_timeout_ms(airtime, route)),
                packet_id=record.packet_id,
                origin="ack",
            )
        )
        return True

    def _handle_ack(self, record: RxRecord, ack: Acknowledgement) -> None:
        """Hand an acknowledgement to the shared registry (design D11).

        Matching is on the first 4 bytes and nothing else (`BaseChatMesh.cpp:740`);
        `parse_ack` has already split the 4-or-6-byte payload into a checksum and
        a tail, so the tail — an extended attempt byte and a random one — is
        never compared, exactly as the firmware does not compare it.

        The registry answers with the owner, so an acknowledgement another
        component was waiting for reaches that component and is *not* reported
        here as unmatched. `unmatched` keeps meaning nobody in this process was
        waiting for it.
        """
        result = self.acks.deliver(ack, packet_id=record.packet_id, received_at=record.received_at)
        if isinstance(result, AckUnowned):
            self._emit(
                AckUnmatched(
                    checksum=result.checksum,
                    packet_id=result.packet_id,
                    outstanding=result.outstanding,
                )
            )

    def _on_ack_match(self, match: AckMatch) -> None:
        """Our own expectation matched: resolve the send that was waiting on it."""
        pending = self._pending_by_checksum.get(match.checksum)
        if pending is None or pending.matched.is_set():
            return
        received_at = match.received_at or self.clock.now()
        pending.matched_checksum = match.checksum
        pending.matched_at = received_at
        pending.matched_attempt = pending.expectations.index(match.checksum)
        pending.matched.set()
        latency = 0.0
        if pending.sent_at is not None:
            latency = (received_at - pending.sent_at).total_seconds() * 1000.0
        self._log.info(
            "ack_matched",
            packet_id=match.packet_id,
            message_id=pending.message_id,
            checksum=match.checksum.hex(),
            matched_attempt=pending.matched_attempt,
            latency_ms=round(latency, 1),
            bundled=match.bundled,
        )
        self._emit(
            AckMatched(
                message_id=pending.message_id,
                contact=pending.contact,
                checksum=match.checksum,
                attempt=pending.matched_attempt,
                latency_ms=latency,
                packet_id=match.packet_id,
                payload_bytes=match.payload_bytes,
            )
        )

    def as_json(self) -> dict[str, object]:
        return {
            "entities": len(self.entities),
            "messages_sent": self.sent,
            "messages_received": self.received,
            "undecryptable": self.undecryptable,
            "outstanding_acks": len(self._pending_by_checksum),
            "secret_cache": len(self.secrets),
            "records_offered": self.records_offered,
            "records_refused": self.records_refused,
        }
