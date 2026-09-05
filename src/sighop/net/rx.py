"""The live RX decode stage: a modem event in, a structured record out.

This is the wire between milestone 0's `radio/` and milestone 1's
`protocol/`, and it adds no wire-format rules of its own — it composes
`decode_packet`, `parse_payload` and `verify_advert` and reports what they
returned.

Three properties are load-bearing:

- **Every event produces an outcome.** A frame that fails structural decode,
  a payload that will not parse, an advert whose signature is bad, a frame the
  modem itself could not read: all are outcomes carrying their reason, never
  silent drops. DESIGN.md §4.1 — sighop's logging is the only observability
  the radio layer has.
- **Advert content is unreachable without its verification.** The record holds
  an `AdvertVerification`, not an `Advert`, so no consumer can reach a name
  without also holding the result of checking the signature over it
  (design D8, milestone 1 design D7).
- **Stateless.** No dedup, no path learning, no contact accumulation, no
  persistence — milestones 3 and 5. The monitor's raw reception counts are the
  unfiltered baseline milestone 3's dedup gets measured against (design D9).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass

from sighop.logging import Logger, get_logger
from sighop.protocol.crypto import AdvertVerification, VerifiedAdvert, verify_advert
from sighop.protocol.packet import Packet, PayloadType, RouteType
from sighop.protocol.packet import decode as decode_packet
from sighop.protocol.payloads import (
    Advert,
    AnonRequestEnvelope,
    DirectEnvelope,
    GroupEnvelope,
    ParsedPayload,
    UnparsedPayload,
    parse_payload,
)
from sighop.protocol.result import DecodeFailure
from sighop.radio.modem import ModemEvent, UnparsedEvent


@dataclass(frozen=True, slots=True)
class Payload:
    """A parsed payload that needed no verification to be trustworthy as
    structure: an envelope, an ACK, a TRACE.

    An encrypted envelope arrives here, not in a failure: a payload we hold no
    key for is the normal state of nearly every frame on this mesh.
    """

    payload: ParsedPayload


@dataclass(frozen=True, slots=True)
class AdvertOutcome:
    """An ADVERT, reachable only through its verification result.

    Deliberately carries no `Advert`: an unverified advert's name, node type
    and location are attacker-chosen, and a consumer that could reach them
    without the verification result would eventually render them.
    """

    verification: AdvertVerification

    @property
    def verified(self) -> VerifiedAdvert | None:
        return self.verification if isinstance(self.verification, VerifiedAdvert) else None


@dataclass(frozen=True, slots=True)
class Uninterpreted:
    """A payload type the codec preserves but does not interpret — MULTIPART,
    CONTROL, RAW_CUSTOM, a reserved value. An outcome, not a failure.
    """

    payload_type: PayloadType
    raw: bytes


@dataclass(frozen=True, slots=True)
class PayloadFailure:
    """The packet decoded, its payload did not. The header and path stay
    observable, which is most of what a malformed payload is worth.
    """

    failure: DecodeFailure


@dataclass(frozen=True, slots=True)
class StructuralFailure:
    """The bytes are not a packet: truncated, over a limit, or v2+."""

    failure: DecodeFailure


@dataclass(frozen=True, slots=True)
class ModemUnparsed:
    """The modem could not read the frame at all — forwarded, not dropped."""

    reason: str


RxOutcome = (
    Payload | AdvertOutcome | Uninterpreted | PayloadFailure | StructuralFailure | ModemUnparsed
)


@dataclass(frozen=True, slots=True)
class RxRecord:
    """One reception, decoded as far as it could be.

    `packet_id` identifies **this reception**, not the packet's content: two
    receptions of identical bytes carry different ids. Milestone 3's dedup has
    to be able to say "this reception duplicates that one", which needs the two
    ids to differ and a separate content hash to join on (design D7).
    """

    packet_id: str
    received_at: dt.datetime
    raw: bytes
    snr_db: float | None
    rssi_dbm: int | None
    packet: Packet | None
    outcome: RxOutcome

    @property
    def size_bytes(self) -> int:
        return len(self.raw)

    @property
    def route_type(self) -> RouteType | None:
        return None if self.packet is None else self.packet.route_type

    @property
    def payload_type(self) -> PayloadType | None:
        return None if self.packet is None else self.packet.payload_type

    @property
    def hop_count(self) -> int | None:
        return None if self.packet is None else self.packet.hop_count

    @property
    def hash_size(self) -> int | None:
        return None if self.packet is None else self.packet.hash_size

    @property
    def path(self) -> bytes:
        return b"" if self.packet is None else self.packet.path

    @property
    def failed(self) -> bool:
        return isinstance(self.outcome, StructuralFailure | PayloadFailure | ModemUnparsed)

    @property
    def src_hash(self) -> int | None:
        """The sender's node hash where the payload names one."""
        match self.outcome:
            case AdvertOutcome(verification=verification):
                return verification.node_hash
            case Payload(payload=DirectEnvelope() as envelope):
                return envelope.src_hash
            case Payload(payload=AnonRequestEnvelope() as envelope):
                return envelope.sender_hash
            case _:
                return None

    @property
    def dest_hash(self) -> int | None:
        match self.outcome:
            case Payload(payload=DirectEnvelope() as envelope):
                return envelope.dest_hash
            case Payload(payload=AnonRequestEnvelope() as envelope):
                return envelope.dest_hash
            case Payload(payload=GroupEnvelope() as envelope):
                return envelope.channel_hash
            case _:
                return None


def new_packet_id() -> str:
    """Mint a reception identifier. Random, not content-derived (design D7)."""
    return uuid.uuid4().hex[:16]


def decode_event(
    event: ModemEvent,
    *,
    packet_id: str | None = None,
    received_at: dt.datetime | None = None,
) -> RxRecord:
    """Decode one modem event. Pure: no I/O, no logging, no state."""
    packet_id = packet_id or new_packet_id()
    received_at = received_at or event.received_at or dt.datetime.now(dt.UTC)

    if isinstance(event, UnparsedEvent):
        return RxRecord(
            packet_id=packet_id,
            received_at=received_at,
            raw=event.raw,
            snr_db=None,
            rssi_dbm=None,
            packet=None,
            outcome=ModemUnparsed(reason=event.reason),
        )

    snr_db = event.rx_meta.snr_db if event.rx_meta is not None else None
    rssi_dbm = event.rx_meta.rssi_dbm if event.rx_meta is not None else None

    packet = decode_packet(event.packet)
    if isinstance(packet, DecodeFailure):
        return RxRecord(
            packet_id=packet_id,
            received_at=received_at,
            raw=event.packet,
            snr_db=snr_db,
            rssi_dbm=rssi_dbm,
            packet=None,
            outcome=StructuralFailure(failure=packet),
        )

    return RxRecord(
        packet_id=packet_id,
        received_at=received_at,
        raw=event.packet,
        snr_db=snr_db,
        rssi_dbm=rssi_dbm,
        packet=packet,
        outcome=_payload_outcome(packet),
    )


def _payload_outcome(packet: Packet) -> RxOutcome:
    parsed = parse_payload(packet.payload_type, packet.payload)
    if isinstance(parsed, DecodeFailure):
        return PayloadFailure(failure=parsed)
    if isinstance(parsed, Advert):
        # The only route to advert content, taken here so no consumer can
        # reach the name without the verification result beside it.
        return AdvertOutcome(verification=verify_advert(parsed))
    if isinstance(parsed, UnparsedPayload):
        return Uninterpreted(payload_type=parsed.payload_type, raw=parsed.raw)
    return Payload(payload=parsed)


async def decode_stream(
    events: AsyncIterable[ModemEvent],
    *,
    logger: Logger | None = None,
) -> AsyncIterator[RxRecord]:
    """Decode a stream of modem events, emitting one *Packet RX* wide event
    per frame.

    Knows nothing about where the events came from: the live modem and the
    capture replay source are the same thing here, which is the point
    (design D1).
    """
    log = logger or get_logger(component="rx")
    async for event in events:
        record = decode_event(event)
        emit_packet_rx(record, logger=log)
        yield record


def outcome_fields(record: RxRecord) -> dict[str, object]:
    """The outcome as log fields: what happened, and why if it went wrong."""
    match record.outcome:
        case StructuralFailure(failure=failure):
            return {
                "outcome": "structural_failure",
                "failure_reason": str(failure.reason),
                "failure_offset": failure.offset,
                "failure_detail": failure.detail,
            }
        case PayloadFailure(failure=failure):
            return {
                "outcome": "payload_failure",
                "failure_reason": str(failure.reason),
                "failure_offset": failure.offset,
                "failure_detail": failure.detail,
            }
        case ModemUnparsed(reason=reason):
            return {"outcome": "modem_unparsed", "failure_reason": reason}
        case AdvertOutcome(verification=verification):
            if isinstance(verification, VerifiedAdvert):
                return {"outcome": "advert_verified"}
            return {
                "outcome": "advert_unverified",
                "failure_reason": verification.reason,
                "failure_detail": verification.detail,
            }
        case Uninterpreted():
            return {"outcome": "uninterpreted_payload"}
        case _:
            # An encrypted payload with no key held is a normal outcome, and
            # is reported as one: not decrypted, not failed.
            return {"outcome": "parsed", "decrypt_outcome": "no_key_held"}


def emit_packet_rx(
    record: RxRecord,
    *,
    logger: Logger,
    extra: Mapping[str, object] | None = None,
) -> None:
    """The DESIGN.md §9 *Packet RX* wide event, once per frame.

    `extra` carries the fields this stage cannot know on its own, because it is
    stateless by design: `dup` and the duplicate's join fields, the learned path,
    `matched_entities`, `airtime_ms`. Milestone 3's `IngressPipeline` supplies
    them. They stay absent rather than being filled with placeholders when
    nothing supplies them — a field that always says `false` is worse than one
    that is not there.
    """
    fields = outcome_fields(record)
    if extra:
        fields.update(extra)
    outcome = fields.pop("outcome")
    emit = logger.error if record.failed else logger.info
    emit(
        "packet_rx",
        packet_id=record.packet_id,
        outcome=outcome,
        ts=record.received_at.isoformat(),
        route_type=record.route_type.name if record.route_type is not None else None,
        payload_type=record.payload_type.name if record.payload_type is not None else None,
        path_len=record.hop_count,
        path=record.path.hex(),
        hash_size=record.hash_size,
        size_bytes=record.size_bytes,
        snr=record.snr_db,
        rssi=record.rssi_dbm,
        src_hash=record.src_hash,
        **fields,
    )
