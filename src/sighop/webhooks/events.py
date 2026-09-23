"""The fact a webhook is told, independent of how it is rendered (design D2).

An event is built once, in the contact-store listener, and carries everything a
renderer needs; its `event_id` is fixed at that moment so every retry of it —
and every webhook it goes to — names the same event.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Protocol

from sighop.net.contacts import Contact, ContactObservation
from sighop.net.rx import AdvertOutcome, RxRecord
from sighop.protocol.payloads import NodeType
from sighop.webhooks.triggers import Trigger

SAMPLE_PUBLIC_KEY = bytes(32)
SAMPLE_NAME = "dev-sample"
SAMPLE_HASH_SIZE = 2
SAMPLE_HOP_NAME = "dev-hop"
SAMPLE_LATITUDE = 59.329460
SAMPLE_LONGITUDE = 18.068580

UNKNOWN_HOP = "<unknown>"
AMBIGUOUS_HOP = "<ambiguous>"
HOP_KEY_PREFIX_LENGTH = 12
"""Hex characters shown for a hop's single matching contact that has no name,
the same prefix `Contact.display_name` falls back to."""


class HopLookup(Protocol):
    """The part of `ContactStore` that resolves a hop hash. Faked in tests."""

    def by_prefix(self, prefix: bytes) -> frozenset[Contact]: ...


@dataclass(frozen=True, slots=True)
class Position:
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class PathHop:
    """One hop of the reception's path, resolved when the event was raised (design D1).

    `key_prefix` is set only for a single match with no name, so a renderer can
    label the hop without the store.
    """

    hash: bytes
    name: str | None
    matches: int
    key_prefix: str | None = None

    @property
    def label(self) -> str:
        if self.matches == 0:
            return UNKNOWN_HOP
        if self.matches > 1:
            return AMBIGUOUS_HOP
        if self.name is not None:
            return self.name
        return self.key_prefix or UNKNOWN_HOP


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    event_id: str
    trigger: Trigger
    occurred_at: dt.datetime
    public_key: bytes
    name: str | None
    node_type: NodeType | int | None
    position: Position | None
    hop_count: int | None
    snr_db: float | None
    rssi_dbm: int | None
    received_at: dt.datetime | None
    hash_size: int = 1
    path: tuple[PathHop, ...] = ()
    test: bool = False

    @property
    def node_hash(self) -> int:
        return self.public_key[0]

    @property
    def sized_hash(self) -> str:
        """The node hash at the size the advert was heard, in hex."""
        return self.public_key[: self.hash_size].hex()

    @property
    def node_type_name(self) -> str | None:
        return node_type_name(self.node_type)

    def as_log_fields(self) -> dict[str, object]:
        """What a log event says about this event. No advert content beyond the hash."""
        return {
            "event_id": self.event_id,
            "trigger": self.trigger.value,
            "node_hash": f"{self.node_hash:02x}",
            "test": self.test,
        }


def node_type_name(node_type: NodeType | int | None) -> str | None:
    """`repeater`, `chat`, … or `type_N` for a type MeshCore has not defined."""
    if node_type is None:
        return None
    try:
        return NodeType(int(node_type)).name.lower()
    except ValueError:
        return f"type_{int(node_type)}"


def resolve_hop(hop: bytes, contacts: HopLookup | None, advertiser: bytes) -> PathHop:
    """A hop against the contacts other than the advertising node (design D2).

    The advertiser is left out: a node never repeats its own advert, and
    counting it would turn a 1-byte collision with it into a false ambiguity.
    """
    if contacts is None:
        return PathHop(hash=hop, name=None, matches=0)
    candidates = [c for c in contacts.by_prefix(hop) if c.public_key != advertiser]
    if len(candidates) != 1:
        return PathHop(hash=hop, name=None, matches=len(candidates))
    [contact] = candidates
    if contact.name is not None:
        return PathHop(hash=hop, name=contact.name.text, matches=1)
    return PathHop(
        hash=hop,
        name=None,
        matches=1,
        key_prefix=contact.public_key.hex()[:HOP_KEY_PREFIX_LENGTH],
    )


def event_from_observation(
    observation: ContactObservation,
    record: RxRecord,
    trigger: Trigger,
    now: dt.datetime,
    contacts: HopLookup | None = None,
) -> WebhookEvent:
    """An event from a verified-advert observation and the reception behind it.

    The position comes from the reception's advert: the contact row does not
    carry one, and the advert that created the contact is the one being told.
    The path is resolved now, so every retry and every webhook names the same
    hops (design D1); without `contacts` every hop is unknown.
    """
    contact = observation.contact
    position = None
    match record.outcome:
        case AdvertOutcome() as outcome if outcome.verified is not None:
            appdata = outcome.verified.appdata
            latitude, longitude = appdata.latitude_degrees, appdata.longitude_degrees
            if latitude is not None and longitude is not None:
                position = Position(latitude=latitude, longitude=longitude)
        case _:
            pass
    hops = () if record.packet is None else record.packet.hops
    return WebhookEvent(
        event_id=str(uuid.uuid4()),
        trigger=trigger,
        occurred_at=now,
        public_key=contact.public_key,
        name=None if contact.name is None else contact.name.text,
        node_type=contact.node_type,
        position=position,
        hop_count=record.hop_count,
        snr_db=record.snr_db,
        rssi_dbm=record.rssi_dbm,
        received_at=record.received_at,
        hash_size=record.hash_size or 1,
        path=tuple(resolve_hop(hop, contacts, contact.public_key) for hop in hops),
    )


def sample_event(trigger: Trigger, now: dt.datetime) -> WebhookEvent:
    """A made-up event marked as a test, for the panel's webhook test (design D8).

    It carries a position so a test message exercises the location link, the one
    field an operator has to click to check.
    """
    node_type = NodeType.REPEATER if trigger is Trigger.NEW_REPEATER else NodeType.CHAT
    return WebhookEvent(
        event_id=str(uuid.uuid4()),
        trigger=trigger,
        occurred_at=now,
        public_key=SAMPLE_PUBLIC_KEY,
        name=SAMPLE_NAME,
        node_type=node_type,
        position=Position(latitude=SAMPLE_LATITUDE, longitude=SAMPLE_LONGITUDE),
        hop_count=2,
        snr_db=5.0,
        rssi_dbm=-90,
        received_at=now,
        hash_size=SAMPLE_HASH_SIZE,
        path=(
            PathHop(hash=bytes.fromhex("c3d4"), name=SAMPLE_HOP_NAME, matches=1),
            PathHop(hash=bytes.fromhex("e5f6"), name=None, matches=0),
        ),
        test=True,
    )
