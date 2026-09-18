"""The contact table (milestone 4, `contacts`, design D5).

The two properties worth naming: an unverified advert cannot reach the store at
all — that is enforced by the type, and the test that says so is a type-level
statement rather than a runtime one — and a node-hash lookup returns a *set*,
because §3 says one byte of identity collides at 1 in 256 and this milestone
makes those collisions likelier, not rarer.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sighop.net.contacts import (
    AmbiguousContactError,
    ContactError,
    ContactStore,
    UnknownContactError,
    parse_public_key,
)
from sighop.protocol.crypto import AdvertVerificationFailure, sign_advert, verify_advert
from sighop.protocol.identity import LocalIdentity, generate_identity
from sighop.protocol.payloads import NodeType, build_appdata

NOW = dt.datetime(2026, 9, 5, 20, 0, tzinfo=dt.UTC)


def verified_advert(identity: LocalIdentity, name: str, *, timestamp: int = 1_700_000_000):
    advert = sign_advert(identity, timestamp, build_appdata(NodeType.CHAT, name=name))
    verification = verify_advert(advert)
    assert not isinstance(verification, AdvertVerificationFailure)
    return verification


def _identity_with_node_hash(node_hash: int) -> LocalIdentity:
    while True:
        candidate = generate_identity()
        if candidate.node_hash == node_hash:
            return candidate


# --- Only verified adverts become contacts ---------------------------------


def test_a_verified_advert_creates_a_contact() -> None:
    store = ContactStore()
    identity = generate_identity()

    observation = store.observe_advert(verified_advert(identity, "skogen"), at=NOW)

    assert observation.created is True
    contact = observation.contact
    assert contact.public_key == identity.public_key
    assert contact.node_hash == identity.node_hash
    assert contact.name is not None and contact.name.text == "skogen"
    assert contact.node_type is NodeType.CHAT
    assert contact.first_heard == NOW
    assert contact.last_heard == NOW
    assert contact.advert_verified is True


def test_an_unverified_advert_cannot_be_recorded_at_the_type_level() -> None:
    """`observe_advert` takes a `VerifiedAdvert`; a failure is not one.

    This is the whole guarantee: there is no runtime check to forget, because
    the only thing that produces a `VerifiedAdvert` is a signature that verified
    (design D5, milestone 1 design D7).
    """
    failure = AdvertVerificationFailure(reason="bad_signature", node_hash=0x42)

    assert not hasattr(failure, "appdata"), (
        "a verification failure must carry no advert content to record"
    )
    assert not hasattr(failure, "public_key")


def test_the_same_peer_heard_twice_stays_one_contact() -> None:
    store = ContactStore()
    identity = generate_identity()
    later = NOW + dt.timedelta(minutes=30)

    first = store.observe_advert(verified_advert(identity, "skogen"), at=NOW)
    second = store.observe_advert(
        verified_advert(identity, "skogen", timestamp=1_700_000_100), at=later
    )

    assert len(store) == 1
    assert second.created is False
    assert first.contact is second.contact
    assert second.contact.first_heard == NOW
    assert second.contact.last_heard == later


def test_a_name_change_is_reported_rather_than_swapped_silently() -> None:
    store = ContactStore()
    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen"), at=NOW)

    observation = store.observe_advert(
        verified_advert(identity, "annan", timestamp=1_700_000_100), at=NOW
    )

    assert observation.name_changed == ("skogen", "annan")
    assert observation.contact.name is not None
    assert observation.contact.name.text == "annan"


# --- Node hash is a candidate set, never a peer ----------------------------


def test_two_contacts_sharing_a_node_hash_are_both_returned() -> None:
    store = ContactStore()
    first = generate_identity()
    second = _identity_with_node_hash(first.node_hash)
    store.observe_advert(verified_advert(first, "one"), at=NOW)
    store.observe_advert(verified_advert(second, "two"), at=NOW)

    candidates = store.by_node_hash(first.node_hash)

    assert len(candidates) == 2
    assert {contact.public_key for contact in candidates} == {
        first.public_key,
        second.public_key,
    }


def test_an_unknown_node_hash_returns_an_empty_set() -> None:
    store = ContactStore()

    result = store.by_node_hash(0x7F)

    assert result == frozenset()
    assert isinstance(result, frozenset)


def _prefix_store() -> ContactStore:
    store = ContactStore()
    for key in ("a1b2c3" + "00" * 29, "a1b2ff" + "00" * 29, "a1ff00" + "00" * 29):
        store.add_public_key(key)
    return store


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("a1", {"a1b2c3", "a1b2ff", "a1ff00"}),
        ("a1b2", {"a1b2c3", "a1b2ff"}),
        ("a1b2c3", {"a1b2c3"}),
        ("a1b200", set()),
        ("7f", set()),
    ],
)
def test_a_key_prefix_selects_every_contact_it_begins(prefix: str, expected: set[str]) -> None:
    result = _prefix_store().by_prefix(bytes.fromhex(prefix))

    assert isinstance(result, frozenset)
    assert {contact.public_key.hex()[:6] for contact in result} == expected


def test_an_empty_prefix_selects_nobody() -> None:
    assert _prefix_store().by_prefix(b"") == frozenset()


def test_a_contact_stays_the_same_set_member_after_an_update() -> None:
    """`last_heard` moves on every advert; the set membership must not."""
    store = ContactStore()
    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen"), at=NOW)
    contact = next(iter(store.by_node_hash(identity.node_hash)))

    store.observe_advert(
        verified_advert(identity, "skogen", timestamp=1_700_000_100),
        at=NOW + dt.timedelta(hours=1),
    )

    assert store.by_node_hash(identity.node_hash) == frozenset({contact})


# --- Manual addition -------------------------------------------------------


def test_a_contact_can_be_added_from_a_hex_key_and_is_marked_unverified() -> None:
    store = ContactStore()
    identity = generate_identity()

    contact = store.add_public_key(identity.public_key.hex())

    assert contact.public_key == identity.public_key
    assert contact.node_hash == identity.node_hash
    assert contact.advert_verified is False
    assert contact.name is None


def test_adding_a_key_already_known_returns_it_unchanged() -> None:
    store = ContactStore()
    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen"), at=NOW)

    contact = store.add_public_key(identity.public_key.hex())

    assert contact.advert_verified is True, "a paste downgraded a verified contact"
    assert contact.name is not None and contact.name.text == "skogen"
    assert len(store) == 1


@pytest.mark.parametrize("bad", ["zz" * 32, "ab", ""])
def test_a_key_that_is_not_thirty_two_hex_bytes_is_refused(bad: str) -> None:
    with pytest.raises(ContactError):
        parse_public_key(bad)


# --- Selection -------------------------------------------------------------


def test_a_peer_resolves_by_exact_name_or_key_prefix() -> None:
    store = ContactStore()
    identity = generate_identity()
    store.observe_advert(verified_advert(identity, "skogen"), at=NOW)

    assert store.resolve("skogen").public_key == identity.public_key
    assert store.resolve(identity.public_key.hex()[:8]).public_key == identity.public_key


def test_an_ambiguous_reference_lists_every_candidate() -> None:
    store = ContactStore()
    first = generate_identity()
    second = _identity_with_node_hash(first.node_hash)
    store.observe_advert(verified_advert(first, "one"), at=NOW)
    store.observe_advert(verified_advert(second, "two"), at=NOW)

    with pytest.raises(AmbiguousContactError) as excinfo:
        store.resolve(f"{first.node_hash:02x}")

    message = str(excinfo.value)
    assert "one" in message
    assert "two" in message
    assert first.public_key.hex()[:16] in message


def test_an_unknown_reference_says_the_advert_may_not_have_been_heard() -> None:
    store = ContactStore()

    with pytest.raises(UnknownContactError, match="may not have been heard"):
        store.resolve("nobody")


# --- The bus ---------------------------------------------------------------


async def test_replaying_a_corpus_file_populates_contacts() -> None:
    """The advert path against real traffic, end to end through the bus."""
    import io

    from sighop.radio.modem import EU868_NARROW
    from sighop.runtime import Runtime, RuntimeConfig
    from tests.protocol.corpus import CAPTURES_DIR
    from tests.test_runtime import _events, _startup
    from tests.test_tx import ManualClock, RecordingLogger

    store = ContactStore(logger=RecordingLogger())
    run = Runtime(
        source=_events(CAPTURES_DIR / "2026-09-04-03.jsonl"),
        startup=_startup,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600),
        radio=EU868_NARROW,
        clock=ManualClock(),
        out=io.StringIO(),
        logger=RecordingLogger(),
    )
    store.subscribe(run.bus)

    await run.run()

    # Three distinct keys advertise in this capture. Recorded rather than
    # derived from the run, so a decode change that loses an advert fails here.
    assert len(store) == 3
    assert all(contact.advert_verified for contact in store)
    assert len({contact.public_key for contact in store}) == len(store)
