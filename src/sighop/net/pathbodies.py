"""Explicit path bodies: the route a peer states, and what it bundles (D12).

Path learning up to now has been reverse-path learning from the frame a
reception arrived on (`net/paths.py`). A `PATH` payload carries the peer's
*explicit* statement of the route, and — the part that actually breaks something
when it is missing — the firmware puts an **acknowledgement inside it**
(`MyMesh.cpp:601-620`): a client that answers a flooded push with a path return
encodes the ACK as the return's extra payload. Without decrypting that body the
acknowledgement is invisible, the push is retried three times for nothing, and
the member's cursor never advances, which looks exactly like a client that is
not receiving.

This lives beside `net/paths.py` rather than inside it because `net/bus.py`
already imports `PathStore`, and a subscriber is by definition something that
knows about the bus.

Decryption is the same candidate-key trial a text message gets, with the same
caveat carried through unchanged: **a MAC match selects a key, it never
authenticates a sender** (milestone 4 design D7). The route is recorded as a
candidate keyed by the matched public key and marked claimed, and
most-recently-confirmed-wins is untouched.

A bus subscriber, never part of `net/rx.py`: decryption needs keys and contacts,
and the decode stage stays a pure function of one frame (design D6).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

from sighop.logging import Logger, get_logger
from sighop.net.acks import AckRegistry, AckUnowned
from sighop.net.bus import NetworkBus, Subscription
from sighop.net.contacts import Contact, ContactStore
from sighop.net.paths import LearnedPath, PathKey, PathSource, PathStore
from sighop.net.rx import Payload, RxRecord
from sighop.protocol.crypto import SharedSecretCache, mac_then_decrypt
from sighop.protocol.identity import LocalIdentity
from sighop.protocol.packet import PayloadType
from sighop.protocol.payloads import (
    Acknowledgement,
    DirectEnvelope,
    ReturnedPathBody,
    parse_returned_path_body,
)
from sighop.protocol.result import DecodeFailure


def adopt_path_body(
    paths: PathStore,
    body: ReturnedPathBody,
    *,
    public_key: bytes,
    record: RxRecord,
) -> LearnedPath:
    """Record the route a decrypted path body declares, as a claimed candidate.

    Shared with `net/room.py`, which owns the `PATH` packets addressed to a room
    server entity for the same reason the direct messenger does not own its
    `TXT_MSG` ones (design D10) — one component decrypts a given packet.
    """
    learned = LearnedPath(
        # Not reversed: a path body is the peer stating the route *to* it, which
        # is already the route out. Reversing it here is the mistake this
        # comment exists to stop.
        path=body.path,
        hash_size=body.hash_size,
        hop_count=body.hop_count,
        snr_db=record.snr_db,
        confirmed_at=record.received_at,
        packet_id=record.packet_id,
        source=PathSource.PATH_BODY,
    )
    # Keyed by the public key whose shared secret decrypted it, and subject to
    # the same candidate limits and the same most-recently-confirmed-wins
    # selection as any other candidate.
    key = PathKey.for_public_key(public_key)
    paths._insert(key, learned)
    if paths._sink is not None:
        paths._sink.offer((key, learned))
    return learned


def deliver_bundled_ack(
    body: ReturnedPathBody, *, record: RxRecord, acks: AckRegistry | None
) -> bool:
    """Hand a bundled payload on for handling as though it had arrived alone.

    Only an acknowledgement is acted on today, because that is the one the
    firmware bundles (`MyMesh.cpp:615-618`) and the one whose absence costs
    three retries and a stalled cursor. Any other bundled type is reported by
    the caller's event and not interpreted.
    """
    if body.extra_ack is None or acks is None:
        return False
    result = acks.deliver(
        Acknowledgement(checksum=body.extra_ack.checksum),
        packet_id=record.packet_id,
        received_at=record.received_at,
        bundled=True,
    )
    return not isinstance(result, AckUnowned)


class _PathEntity(Protocol):
    """The slice of a local entity this needs. `dm.LocalEntity` satisfies it."""

    entity_id: str
    name: str
    identity: LocalIdentity

    @property
    def node_hash(self) -> int: ...


@dataclass(frozen=True, slots=True)
class PathBodyLearned:
    """A route a peer declared, decrypted. `contact` is a **claimed** sender."""

    entity_name: str
    contact: Contact
    packet_id: str
    candidates_tried: int
    path: bytes
    hash_size: int
    hop_count: int
    bundled_type: PayloadType | None
    bundled_matched: bool


@dataclass(frozen=True, slots=True)
class PathBodyUndecryptable:
    packet_id: str
    dest_hash: int
    src_hash: int
    candidates_tried: int


type PathBodyEvent = PathBodyLearned | PathBodyUndecryptable


@dataclass(slots=True)
class PathBodyReader:
    """Decrypts inbound `PATH` payloads — for what is inside them as much as the route.

    Path learning up to now has been reverse-path learning from the frame a
    reception arrived on. A `PATH` payload carries the peer's *explicit*
    statement of the route, and — the part that actually breaks something when
    it is missing — the firmware puts an **acknowledgement inside it**
    (`MyMesh.cpp:601-620`): a client that answers a flooded push with a path
    return encodes the ACK as the return's extra payload. Without decrypting
    that body the acknowledgement is invisible, the push is retried three times
    for nothing, and the member's cursor never advances — which looks exactly
    like a client that is not receiving.

    Decryption is the same candidate-key trial a text message gets, with the
    same caveat carried through unchanged: **a MAC match selects a key, it never
    authenticates a sender** (milestone 4 design D7). The route is recorded as a
    candidate keyed by the matched public key and marked claimed, and
    most-recently-confirmed-wins is untouched.

    A bus subscriber, never part of `net/rx.py`: decryption needs keys and
    contacts, and the decode stage stays a pure function of one frame (design
    D6).
    """

    paths: PathStore
    contacts: ContactStore
    entities: Sequence[_PathEntity] = ()
    secrets: SharedSecretCache | None = None
    acks: AckRegistry | None = None
    on_event: Callable[[PathBodyEvent], None] | None = None
    on_bundled_response: Callable[[_PathEntity, Contact, bytes, RxRecord], None] | None = None
    """Given a bundled `RESPONSE` — the answer to a flooded request — after the
    route is adopted, so the answer's owner can send the next request direct.
    The repeater collector's; any other bundled type never reaches it."""

    logger: Logger | None = None
    learned: int = field(default=0, init=False)
    undecryptable: int = field(default=0, init=False)
    bundled_acks: int = field(default=0, init=False)
    _room_entity_ids: set[str] = field(default_factory=set, init=False)
    """Entities a room server has claimed, excluded from matching here — the
    same problem `DirectMessenger._room_entity_ids` solves and the same way
    (design D10): a room server decrypts and acknowledges its own PATH
    returns, and a second subscriber doing that for one packet is a second
    decryption and a second acknowledgement on the air. A set kept beside
    `entities` rather than entries removed *from* it, on purpose — `entities`
    is the same list object as `adverts.stubs` (see `__post_init__`), and
    removing from it would desync the two the moment a room claims anything."""

    def __post_init__(self) -> None:
        self.secrets = self.secrets or SharedSecretCache()
        self.logger = self.logger or get_logger(component="path-bodies")
        # Normalise to a list, but only when the caller did not already hand
        # one over — `runtime.py` hands this the *same* list object as
        # `adverts.stubs` and relies on mutating it in place from then on
        # (design D5); copying it here, as this used to, would silently
        # detach the two the moment a reader is constructed.
        if not isinstance(self.entities, list):
            self.entities = list(self.entities)

    def add_entity(self, entity: _PathEntity) -> None:
        cast(list[_PathEntity], self.entities).append(entity)

    def claim_for_room(self, entity_id: str) -> None:
        """Mark an entity as a room server's, so this reader leaves it alone."""
        self._room_entity_ids.add(entity_id)

    def release_from_room(self, entity_id: str) -> None:
        """Undo `claim_for_room` — the room has stopped being served, and the
        identity outlives it and goes back to being an ordinary one."""
        self._room_entity_ids.discard(entity_id)

    def subscribe(self, bus: NetworkBus, *, name: str = "path-bodies") -> Subscription:
        return bus.subscribe(name, handler=self.handle)

    async def handle(self, record: RxRecord) -> None:
        match record.outcome:
            case Payload(payload=DirectEnvelope() as envelope) if (
                envelope.payload_type is PayloadType.PATH
            ):
                self._handle_path(record, envelope)
            case _:
                return

    def _handle_path(self, record: RxRecord, envelope: DirectEnvelope) -> None:
        entities = [
            entity
            for entity in self.entities
            if entity.node_hash == envelope.dest_hash
            and entity.entity_id not in self._room_entity_ids
        ]
        if not entities:
            return  # addressed to a hash none of our entities carries
        contacts = tuple(self.contacts.by_node_hash(envelope.src_hash)) or tuple(
            self.contacts.contacts()
        )

        tried = 0
        assert self.secrets is not None
        assert self.logger is not None
        for entity in entities:
            for contact in contacts:
                tried += 1
                secret = self.secrets.get(entity.identity, contact.public_key)
                candidate, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
                if not candidate.matched or plaintext is None:
                    continue
                body = parse_returned_path_body(plaintext)
                if isinstance(body, DecodeFailure):
                    # A MAC match over something that is not a path body: either
                    # a ~2^-16 false match or a shape we do not parse. Reported,
                    # and nothing is learned from it.
                    self.logger.error(
                        "path_body_unparsable",
                        packet_id=record.packet_id,
                        entity_id=entity.entity_id,
                        failure_reason=str(body.reason),
                        failure_detail=body.detail,
                    )
                    return
                self._adopt(record, entity, contact, body, tried)
                return

        self.undecryptable += 1
        self.logger.info(
            "path_body_undecryptable",
            packet_id=record.packet_id,
            dest_hash=envelope.dest_hash,
            src_hash=envelope.src_hash,
            candidates_tried=tried,
        )
        self._emit(
            PathBodyUndecryptable(
                packet_id=record.packet_id,
                dest_hash=envelope.dest_hash,
                src_hash=envelope.src_hash,
                candidates_tried=tried,
            )
        )

    def _adopt(
        self,
        record: RxRecord,
        entity: _PathEntity,
        contact: Contact,
        body: ReturnedPathBody,
        tried: int,
    ) -> None:
        adopt_path_body(self.paths, body, public_key=contact.public_key, record=record)
        self.learned += 1

        bundled_matched = deliver_bundled_ack(body, record=record, acks=self.acks)
        if body.extra_ack is not None and self.acks is not None:
            self.bundled_acks += 1
        if body.extra_type is PayloadType.RESPONSE and self.on_bundled_response is not None:
            self.on_bundled_response(entity, contact, body.extra_raw, record)
        assert self.logger is not None
        self.logger.info(
            "path_body_learned",
            packet_id=record.packet_id,
            entity_id=entity.entity_id,
            claimed_sender=contact.public_key.hex(),
            candidates_tried=tried,
            hop_count=body.hop_count,
            bundled_type=None if body.extra_type is None else str(body.extra_type),
            bundled_matched=bundled_matched,
        )
        self._emit(
            PathBodyLearned(
                entity_name=entity.name,
                contact=contact,
                packet_id=record.packet_id,
                candidates_tried=tried,
                path=body.path,
                hash_size=body.hash_size,
                hop_count=body.hop_count,
                bundled_type=body.extra_type,
                bundled_matched=bundled_matched,
            )
        )

    def _emit(self, event: PathBodyEvent) -> None:
        if self.on_event is not None:
            self.on_event(event)

    def as_json(self) -> dict[str, object]:
        return {
            "path_bodies_learned": self.learned,
            "path_bodies_undecryptable": self.undecryptable,
            "bundled_acks": self.bundled_acks,
        }
