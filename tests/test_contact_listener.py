"""The contact store's observation listener (`contacts`, milestone 7 design D2).

The listener exists because a component that must act on the *first* sighting of
a peer cannot ask the store afterwards: subscribers have independent queues, so
the store's own subscriber may or may not have processed the record yet, and the
answer would be a race whose wrong branch greets a peer twice or not at all.

Three properties are pinned here, and the third is the one that costs something
if it is wrong: a listener is untrusted with respect to the store, so a listener
that raises must not cost a contact, its persistence offer, or the next advert.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sighop.net.contacts import Contact, ContactObservation, ContactStore
from sighop.net.rx import RxRecord
from sighop.protocol.identity import generate_identity
from tests.botfixtures import advert_record, verified_advert

NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


class Recorder:
    def __init__(self) -> None:
        self.seen: list[tuple[ContactObservation, RxRecord]] = []

    def __call__(self, observation: ContactObservation, record: RxRecord) -> None:
        self.seen.append((observation, record))


# --- 4.1 What the listener is told ------------------------------------------


async def test_a_created_contact_reaches_the_listener_with_the_reception() -> None:
    """4.1: `created` is the store's own answer, and the hop count and signal
    quality are the reception's — which is what a decision to transmit rests on."""
    listener = Recorder()
    store = ContactStore(on_observation=listener)
    identity = generate_identity()
    record = advert_record(verified_advert(identity, name="stranger"), hop_count=2, snr_db=-3.5)

    await store.handle(record)

    assert len(listener.seen) == 1
    observation, seen = listener.seen[0]
    assert observation.created is True
    assert observation.contact.public_key == identity.public_key
    assert seen.hop_count == 2
    assert seen.snr_db == -3.5
    assert seen.packet_id == record.packet_id


async def test_a_second_advert_reaches_the_listener_not_marked_as_created() -> None:
    listener = Recorder()
    store = ContactStore(on_observation=listener)
    identity = generate_identity()
    verified = verified_advert(identity, name="stranger")

    await store.handle(advert_record(verified, packet_id="first"))
    await store.handle(advert_record(verified, packet_id="second"))

    assert [observation.created for observation, _ in listener.seen] == [True, False]


async def test_a_rename_reaches_the_listener_with_both_names() -> None:
    """4.1: a name that moves is either a rename or an impersonation, and the
    listener is told which names were involved rather than only that it moved."""
    listener = Recorder()
    store = ContactStore(on_observation=listener)
    identity = generate_identity()

    await store.handle(advert_record(verified_advert(identity, name="first")))
    await store.handle(
        advert_record(
            verified_advert(identity, name="second", timestamp=1_700_000_100),
            packet_id="two",
        )
    )

    assert listener.seen[-1][0].name_changed == ("first", "second")


async def test_the_store_already_reflects_the_observation_when_the_listener_runs() -> None:
    """4.1: a first sighting is an ordered fact, not a race between observers."""
    found: list[Contact | None] = []
    identity = generate_identity()

    def listener(observation: ContactObservation, record: RxRecord) -> None:
        found.append(store.get(identity.public_key))

    store = ContactStore(on_observation=listener)
    await store.handle(advert_record(verified_advert(identity)))

    assert found[0] is not None, "a lookup from inside the listener finds the contact"
    assert found[0].advert_verified is True


# --- 4.2 A listener that raises ---------------------------------------------


class Sink:
    def __init__(self) -> None:
        self.offered: list[Contact] = []

    def offer(self, contact: Contact) -> bool:
        self.offered.append(contact)
        return True


async def test_a_raising_listener_costs_neither_the_contact_nor_the_next_advert() -> None:
    """4.2: the store treats a listener as untrusted, and reports the failure.

    A listener failing silently is a bot that has quietly stopped seeing the
    mesh, which looks exactly like a mesh that has gone quiet.
    """
    seen = 0

    def listener(observation: ContactObservation, record: RxRecord) -> None:
        nonlocal seen
        seen += 1
        raise RuntimeError("the driver's queue exploded")

    sink = Sink()
    store = ContactStore(sink=sink, on_observation=listener)
    first, second = generate_identity(), generate_identity()

    await store.handle(advert_record(verified_advert(first, name="one")))
    await store.handle(advert_record(verified_advert(second, name="two"), packet_id="two"))

    assert len(store) == 2, "both contacts were recorded"
    assert len(sink.offered) == 2, "both were offered for persistence"
    assert seen == 2, "the second advert was still processed"
    assert store.listener_failures == 2, "and both failures were counted"


# --- 4.3 With no listener, nothing changed ----------------------------------


def test_with_no_listener_the_store_reports_exactly_what_it_did_before() -> None:
    """4.3: `as_json` gains no field, so no status line and no wide event moves.

    A counter that appeared only because a feature exists would make every
    milestone-6 log line and this one incomparable.
    """
    keys = set(ContactStore().as_json())

    assert keys == {
        "contacts",
        "advert_verified",
        "node_hashes",
        "adverts_recorded",
        "restored",
        "awaiting_backfill",
    }


async def test_with_no_listener_observation_and_persistence_are_unchanged() -> None:
    """4.3: the same adverts through a store with and without a listener leave
    both stores in the same state and produce the same offers."""
    identities = [generate_identity() for _ in range(3)]
    adverts = [verified_advert(identity, name=f"peer-{index}") for index, identity in enumerate(identities)]

    plain_sink, wired_sink = Sink(), Sink()
    plain = ContactStore(sink=plain_sink)
    wired = ContactStore(sink=wired_sink, on_observation=Recorder())

    for store in (plain, wired):
        for index, verified in enumerate(adverts):
            await store.handle(advert_record(verified, packet_id=f"p{index}"))
            # The same advert again: the store's own "nothing changed" path.
            await store.handle(advert_record(verified, packet_id=f"p{index}b"))

    assert plain.as_json() == wired.as_json()
    assert len(plain_sink.offered) == len(wired_sink.offered)
    assert plain.awaiting_backfill == wired.awaiting_backfill


@pytest.mark.parametrize("hop_count", [0, 1, 5])
async def test_the_reception_the_listener_gets_is_the_one_that_arrived(hop_count: int) -> None:
    """4.1: the hop count is the packet's own path length, not a store field."""
    listener = Recorder()
    store = ContactStore(on_observation=listener)

    await store.handle(
        advert_record(verified_advert(generate_identity()), hop_count=hop_count)
    )

    assert listener.seen[0][1].hop_count == hop_count


# --- webhook-notifications 1.1: several listeners ---------------------------


async def test_two_listeners_each_receive_a_created_observation_once() -> None:
    first, second = Recorder(), Recorder()
    store = ContactStore(on_observation=first)
    store.add_observation_listener(second)

    await store.handle(advert_record(verified_advert(generate_identity(), name="new")))

    assert [observation.created for observation, _ in first.seen] == [True]
    assert [observation.created for observation, _ in second.seen] == [True]
    assert first.seen[0][0] is second.seen[0][0], "both got the same observation"


async def test_a_raising_first_listener_does_not_starve_the_second() -> None:
    def raising(observation: ContactObservation, record: RxRecord) -> None:
        raise RuntimeError("bot host fell over")

    second = Recorder()
    store = ContactStore()
    store.add_observation_listener(raising)
    store.add_observation_listener(second)

    await store.handle(advert_record(verified_advert(generate_identity())))

    assert len(second.seen) == 1
    assert store.listener_failures == 1


async def test_listeners_run_in_registration_order() -> None:
    order: list[str] = []
    store = ContactStore()
    store.add_observation_listener(lambda observation, record: order.append("a"))
    store.add_observation_listener(lambda observation, record: order.append("b"))

    await store.handle(advert_record(verified_advert(generate_identity())))

    assert order == ["a", "b"]
