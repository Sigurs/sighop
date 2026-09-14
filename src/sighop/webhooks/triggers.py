"""What makes an event, and which event it is (webhook-notifications design D2).

Only `trigger_for` decides whether an observation is news. A trigger that does
not come from the contact store — `new_chatter`, later — gets its own source and
calls `WebhookDispatcher.offer` itself; nothing here changes for it.
"""

from __future__ import annotations

from enum import StrEnum

from sighop.net.contacts import ContactObservation
from sighop.protocol.payloads import NodeType


class Trigger(StrEnum):
    NEW_REPEATER = "new_repeater"
    NEW_COMPANION = "new_companion"

    @property
    def heading(self) -> str:
        """The trigger in words, for a human-facing message."""
        return TRIGGER_TITLES[self]


TRIGGER_TITLES: dict[Trigger, str] = {
    Trigger.NEW_REPEATER: "New repeater heard",
    Trigger.NEW_COMPANION: "New companion heard",
}

_BY_NODE_TYPE: dict[NodeType, Trigger] = {
    NodeType.REPEATER: Trigger.NEW_REPEATER,
    NodeType.CHAT: Trigger.NEW_COMPANION,
}


def trigger_for(observation: ContactObservation) -> Trigger | None:
    """The event a verified-advert observation raises, if any.

    Only a *created* contact is news: a key heard before, restored from the
    database, or pasted in by an operator already exists, so its advert is an
    update. Room servers, sensors and node types MeshCore has not defined raise
    nothing.
    """
    if not observation.created:
        return None
    node_type = observation.contact.node_type
    if not isinstance(node_type, NodeType):
        return None
    return _BY_NODE_TYPE.get(node_type)
