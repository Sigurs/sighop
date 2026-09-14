"""The fact a webhook is told, independent of how it is rendered (design D2).

An event is built once, in the contact-store listener, and carries everything a
renderer needs; its `event_id` is fixed at that moment so every retry of it —
and every webhook it goes to — names the same event.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sighop.net.contacts import ContactObservation
from sighop.net.rx import AdvertOutcome, RxRecord
from sighop.protocol.payloads import NodeType
from sighop.webhooks.triggers import Trigger

SAMPLE_PUBLIC_KEY = bytes(32)
SAMPLE_NAME = "dev-sample"


@dataclass(frozen=True, slots=True)
class Position:
    latitude: float
    longitude: float


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
    test: bool = False

    @property
    def node_hash(self) -> int:
        return self.public_key[0]

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


def event_from_observation(
    observation: ContactObservation,
    record: RxRecord,
    trigger: Trigger,
    now: dt.datetime,
) -> WebhookEvent:
    """An event from a verified-advert observation and the reception behind it.

    The position comes from the reception's advert: the contact row does not
    carry one, and the advert that created the contact is the one being told.
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
    )


def sample_event(trigger: Trigger, now: dt.datetime) -> WebhookEvent:
    """A made-up event marked as a test, for `sighop webhook test` (design D8)."""
    node_type = NodeType.REPEATER if trigger is Trigger.NEW_REPEATER else NodeType.CHAT
    return WebhookEvent(
        event_id=str(uuid.uuid4()),
        trigger=trigger,
        occurred_at=now,
        public_key=SAMPLE_PUBLIC_KEY,
        name=SAMPLE_NAME,
        node_type=node_type,
        position=None,
        hop_count=1,
        snr_db=5.0,
        rssi_dbm=-90,
        received_at=now,
        test=True,
    )
