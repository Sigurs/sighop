"""The received-message report names the entity that received it (design D15).

One field, and the reason it is not `entity_name`: a consumer that wants to
*reply as* the addressed entity has to resolve a display name back to an
identity, and two entities may share a display name — at which point the
consumer is guessing which of our own identities was addressed. The messenger
already holds the entity at the call site, so it carries it.

The second property here is milestone 6's, restated because milestone 7 adds the
first consumer that could break it: the acknowledgement is submitted *before*
the report reaches anybody, so no consumer can delay or prevent it.
"""

from __future__ import annotations

import pytest

from sighop.net.bus import PriorityClass
from sighop.net.contacts import ContactStore
from sighop.net.dm import MessageReceived
from sighop.protocol.crypto import SharedSecretCache
from sighop.protocol.identity import generate_identity
from tests.test_dm import (
    Entity,
    RecordingSubmit,
    _packet_for,
    message_packet,
    messenger,
    zero_hop_route_to,
)

pytest_plugins = ()


def _colliding(name: str, other: Entity) -> Entity:
    """A second entity with the same *display name* as the first.

    Not a node-hash collision — a name collision, which is the case
    `entity_name` cannot answer and is the whole reason for design D15.
    """
    while True:
        candidate = Entity(name, generate_identity())
        if candidate.node_hash != other.node_hash:
            candidate.entity_id = f"{name}-second"
            return candidate


async def test_the_report_names_the_receiving_entity_itself() -> None:
    """5.1: the identity, not only its display name."""
    from sighop.net.paths import PathStore

    alice, bob = Entity("alice"), Entity("bob")
    contacts = ContactStore()
    contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    events: list = []
    dm = messenger(bob, contacts=contacts, paths=paths, events=events)

    secret = SharedSecretCache().get(alice.identity, bob.identity.public_key)
    packet, _ = message_packet(
        sender=alice, recipient_node_hash=bob.node_hash, secret=secret
    )
    await dm.handle(_packet_for(packet))

    received = next(event for event in events if isinstance(event, MessageReceived))
    assert received.entity_name == "bob", "the display name is still there"
    assert received.entity is bob, "and so is the identity that was addressed"


async def test_two_entities_sharing_a_display_name_are_still_distinguishable() -> None:
    """5.1: the case a name cannot answer, which is why the entity travels."""
    from sighop.net.paths import PathStore

    alice = Entity("alice")
    first = Entity("companion")
    second = _colliding("companion", first)
    contacts = ContactStore()
    contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    events: list = []
    dm = messenger(first, second, contacts=contacts, paths=paths, events=events)

    secret = SharedSecretCache().get(alice.identity, second.identity.public_key)
    packet, _ = message_packet(
        sender=alice, recipient_node_hash=second.node_hash, secret=secret
    )
    await dm.handle(_packet_for(packet))

    received = next(event for event in events if isinstance(event, MessageReceived))
    assert received.entity_name == "companion", "the name identifies neither of them"
    assert received.entity is second, "the report does"
    assert received.entity is not first


async def test_the_acknowledgement_is_submitted_before_a_consumer_that_raises() -> None:
    """5.2: a consumer cannot delay or prevent the acknowledgement.

    Asserted by wiring one that raises: the submission is already at the
    scheduler by the time the report is handed over, so the exception costs the
    report and costs the sender nothing.
    """
    from sighop.net.paths import PathStore

    alice, bob = Entity("alice"), Entity("bob")
    contacts = ContactStore()
    contacts.add_public_key(alice.identity.public_key)
    paths = PathStore()
    zero_hop_route_to(paths, alice.identity.public_key)
    submit = RecordingSubmit()

    def explode(event: object) -> None:
        raise RuntimeError("a driver's queue exploded")

    dm = messenger(bob, contacts=contacts, paths=paths, submit=submit)
    dm._on_event = explode

    secret = SharedSecretCache().get(alice.identity, bob.identity.public_key)
    packet, _ = message_packet(
        sender=alice, recipient_node_hash=bob.node_hash, secret=secret
    )
    with pytest.raises(RuntimeError):
        await dm.handle(_packet_for(packet))

    assert submit.submissions, "the acknowledgement reached the scheduler anyway"
    assert submit.submissions[0].priority is PriorityClass.ACK
    assert submit.submissions[0].origin == "ack"
