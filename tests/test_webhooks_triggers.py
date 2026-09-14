"""Which observations are news (webhook-notifications task 3.1)."""

from __future__ import annotations

import pytest

from sighop.net.contacts import Contact, ContactObservation
from sighop.protocol.payloads import NodeType
from sighop.webhooks.triggers import Trigger, trigger_for


def _observation(node_type: NodeType | int | None, *, created: bool = True) -> ContactObservation:
    contact = Contact(public_key=bytes(32), node_type=node_type, advert_verified=True)
    return ContactObservation(contact=contact, created=created)


@pytest.mark.parametrize(
    ("node_type", "expected"),
    [
        (NodeType.REPEATER, Trigger.NEW_REPEATER),
        (NodeType.CHAT, Trigger.NEW_COMPANION),
    ],
)
def test_a_created_repeater_or_companion_raises_its_trigger(
    node_type: NodeType, expected: Trigger
) -> None:
    assert trigger_for(_observation(node_type)) is expected


@pytest.mark.parametrize("node_type", [NodeType.REPEATER, NodeType.CHAT])
def test_an_update_raises_nothing(node_type: NodeType) -> None:
    assert trigger_for(_observation(node_type, created=False)) is None


@pytest.mark.parametrize(
    "node_type", [NodeType.ROOM_SERVER, NodeType.SENSOR, NodeType.NONE, 9, None]
)
def test_other_node_types_raise_nothing(node_type: NodeType | int | None) -> None:
    assert trigger_for(_observation(node_type)) is None


def test_trigger_values_are_the_documented_names() -> None:
    assert [trigger.value for trigger in Trigger] == ["new_repeater", "new_companion"]
