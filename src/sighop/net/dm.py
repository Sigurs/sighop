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
from sighop.net.airtime import NoRadioReadback, require_params, time_on_air_ms
from sighop.net.bus import NetworkBus, PriorityClass, Submission, Subscription, TxHandle
from sighop.net.contacts import Contact, ContactStore
from sighop.net.paths import PathStore
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

ACK_POLL_SECONDS = 0.05
"""How often the send loop looks at its own deadline. The wait is driven by the
injected clock rather than by `asyncio.wait_for`, so a simulated retry sequence
runs in a test without four real timeouts elapsing."""


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


def choose_route(
    paths: PathStore, contact: Contact, *, allow_flood: bool = False
) -> Route:
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
            f"message text is {len(text)} bytes; the firmware's MAX_TEXT_LEN is "
            f"{MAX_TEXT_LEN}"
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


def build_ack_packet(
    *, checksum: bytes, route: Route
) -> bytes:
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
    """A decrypted message. `contact` is a **claimed** sender, never a proven one."""

    entity_name: str
    contact: Contact
    body: TextMessageBody
    packet_id: str
    candidates_tried: int
    acknowledged: bool


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


# --- The messenger ---------------------------------------------------------


@dataclass(slots=True)
class _Pending:
    """One send in flight, with the expected acknowledgement of every attempt."""

    message_id: str
    entity: LocalEntity
    contact: Contact
    text: str
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
        allow_flood: bool = False,
        on_event: Callable[[DirectMessageEvent], None] | None = None,
        logger: Logger | None = None,
    ) -> None:
        self.contacts = contacts
        self.paths = paths
        self.submit = submit
        self.entities: list[LocalEntity] = list(entities)
        self.secrets = secrets or SharedSecretCache()
        self.clock = clock or SystemClock()
        self.radio = radio
        self.allow_flood = allow_flood
        self._on_event = on_event
        self._log = logger or get_logger(component="dm")
        self._outstanding: dict[bytes, _Pending] = {}
        self.received = 0
        self.undecryptable = 0
        self.sent = 0

    def add_entity(self, entity: LocalEntity) -> None:
        self.entities.append(entity)

    def set_radio(self, radio: RadioParams | None) -> None:
        self.radio = radio

    def _emit(self, event: DirectMessageEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)

    # --- Outbound ----------------------------------------------------------

    async def send(
        self,
        entity: LocalEntity,
        contact: Contact,
        text: str | bytes,
        *,
        allow_flood: bool | None = None,
        txt_type: TextType | int = TextType.PLAIN,
    ) -> SendOutcome:
        """Send one message, retrying to `MAX_ATTEMPT`, and report the outcome.

        The timestamp is fixed for the whole send and the attempt counter moves,
        which is what the field exists for: an identical retransmission is what
        every repeater in the path would deduplicate, so the flags byte — hence
        the ciphertext, hence the packet hash — has to differ (design D9).
        """
        raw = text.encode("utf-8") if isinstance(text, str) else text
        flooding = self.allow_flood if allow_flood is None else allow_flood
        # Composition limits are checked before anything is routed or queued.
        compose_body(timestamp=0, attempt=0, text=raw, txt_type=txt_type)
        route = choose_route(self.paths, contact, allow_flood=flooding)

        pending = _Pending(
            message_id=uuid.uuid4().hex[:16],
            entity=entity,
            contact=contact,
            text=raw.decode("utf-8", errors="replace"),
        )
        timestamp = int(self.clock.now().timestamp())
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
                self._outstanding[expected] = pending

                try:
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
                    return self._resolve_send(
                        pending, SendResult.ACKNOWLEDGED, route, packet_ids
                    )
            return self._resolve_send(
                pending,
                SendResult.UNACKNOWLEDGED,
                route,
                packet_ids,
                f"no acknowledgement after {MAX_ATTEMPT + 1} attempts",
            )
        finally:
            for expectation in pending.expectations:
                self._outstanding.pop(expectation, None)

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
        self._emit(
            SendResolved(outcome=outcome, contact=pending.contact, text=pending.text)
        )
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
            case Payload(payload=Acknowledgement() as ack):
                self._handle_ack(record, ack)
            case _:
                return

    def subscribe(self, bus: NetworkBus, *, name: str = "direct-messages") -> Subscription:
        return bus.subscribe(name, handler=self.handle)

    def _candidates(
        self, envelope: DirectEnvelope
    ) -> tuple[list[LocalEntity], tuple[Contact, ...]]:
        """(local entities on the destination hash) x (possible senders), design D7."""
        entities = [
            entity for entity in self.entities if entity.node_hash == envelope.dest_hash
        ]
        contacts = tuple(self.contacts.by_node_hash(envelope.src_hash))
        if not contacts:
            # A contact we have never heard from is still a possible sender, and
            # a source hash that matches nothing tells us only that.
            contacts = self.contacts.contacts()
        return entities, contacts

    async def _handle_text_message(
        self, record: RxRecord, envelope: DirectEnvelope
    ) -> None:
        entities, contacts = self._candidates(envelope)
        if not entities:
            return  # addressed to a hash none of our entities carries

        tried = 0
        for entity in entities:
            for contact in contacts:
                tried += 1
                secret = self.secrets.get(entity.identity, contact.public_key)
                candidate, plaintext = mac_then_decrypt(
                    secret, envelope.mac, envelope.ciphertext
                )
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
        """Match an acknowledgement on its first 4 bytes (`BaseChatMesh.cpp:740`).

        `parse_ack` already splits the 4-or-6-byte payload into a 4-byte checksum
        and a tail, so the tail — an extended attempt byte and a random one — is
        never compared, which is exactly what the firmware does.
        """
        pending = self._outstanding.get(ack.checksum)
        payload_bytes = len(ack.checksum) + len(ack.tail)
        if pending is None:
            self._log.info(
                "ack_unmatched",
                packet_id=record.packet_id,
                checksum=ack.checksum.hex(),
                outstanding=len(self._outstanding),
            )
            self._emit(
                AckUnmatched(
                    checksum=ack.checksum,
                    packet_id=record.packet_id,
                    outstanding=len(self._outstanding),
                )
            )
            return
        if pending.matched.is_set():
            return
        pending.matched_checksum = ack.checksum
        pending.matched_at = record.received_at
        pending.matched_attempt = pending.expectations.index(ack.checksum)
        pending.matched.set()
        latency = 0.0
        if pending.sent_at is not None:
            latency = (record.received_at - pending.sent_at).total_seconds() * 1000.0
        self._log.info(
            "ack_matched",
            packet_id=record.packet_id,
            message_id=pending.message_id,
            checksum=ack.checksum.hex(),
            matched_attempt=pending.matched_attempt,
            latency_ms=round(latency, 1),
        )
        self._emit(
            AckMatched(
                message_id=pending.message_id,
                contact=pending.contact,
                checksum=ack.checksum,
                attempt=pending.matched_attempt,
                latency_ms=latency,
                packet_id=record.packet_id,
                payload_bytes=payload_bytes,
            )
        )

    def as_json(self) -> dict[str, object]:
        return {
            "entities": len(self.entities),
            "messages_sent": self.sent,
            "messages_received": self.received,
            "undecryptable": self.undecryptable,
            "outstanding_acks": len(self._outstanding),
            "secret_cache": len(self.secrets),
        }
