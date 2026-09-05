"""The contact table: who we have heard, and what we may believe about them.

Design D5. Two rules shape the whole module, and both come from DESIGN.md §3
and §5 rather than from convenience:

* **A contact is keyed by its full 32-byte public key.** The key is the
  identity. The name is advert content — it can change, and a change is
  reported rather than swapped in silently, because a name that moves between
  adverts is either a rename or an impersonation attempt and the operator is
  the one who can tell.
* **A node hash selects candidates, never a peer.** The index maps one byte to
  a *set*, and a lookup that finds nothing returns an empty set. Nothing
  downstream may treat a node hash as identifying anybody; the MAC trial in
  `net/dm.py` is what narrows a candidate set, and even that selects a key
  rather than authenticating a sender.

Only a `VerifiedAdvert` creates a contact from the air — the type, not a
convention, is what makes an unverified advert unrecordable. A contact may also
be added from a hex public key an operator pasted, which is marked as having no
verified advert behind it precisely so the two cannot be confused.

In memory only for this milestone. Milestone 5 owns the `contact` table; a
store that quietly persisted would be that milestone's design decision made by
accident, which is the same rule `net/paths.py` follows.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass

import structlog

from sighop.logging import get_logger
from sighop.net.bus import NetworkBus, Subscription
from sighop.net.rx import AdvertOutcome, RxRecord
from sighop.protocol.crypto import VerifiedAdvert
from sighop.protocol.identity import PUB_KEY_SIZE, Identity
from sighop.protocol.payloads import NodeType, WireText


class ContactError(ValueError):
    """A contact reference or key that could not be used as given."""


class AmbiguousContactError(ContactError):
    """A peer reference matched more than one contact. Lists every match."""


class UnknownContactError(ContactError):
    """A peer reference matched no contact."""


@dataclass(slots=True, eq=False)
class Contact:
    """A peer we hold a public key for.

    Mutable, and compared by identity rather than by value: `last_heard` moves
    every time the peer adverts, and a contact held in a node-hash set must stay
    the same member of that set across those updates. The public key is what
    makes two contacts the same contact, and the store enforces that by keying
    on it.
    """

    public_key: bytes
    name: WireText | None = None
    node_type: NodeType | int | None = None
    flags: int | None = None
    first_heard: dt.datetime | None = None
    last_heard: dt.datetime | None = None
    advert_timestamp: int | None = None
    advert_verified: bool = False
    """False for a contact an operator pasted in: no advert of theirs has
    verified, so their name and node type are unknown rather than empty."""

    @property
    def node_hash(self) -> int:
        return self.public_key[0]

    @property
    def identity(self) -> Identity:
        return Identity(public_key=self.public_key)

    @property
    def display_name(self) -> str:
        """A name to show, or the key prefix when no verified advert named one."""
        if self.name is not None:
            return self.name.text
        return self.public_key.hex()[:12]

    def as_json(self) -> dict[str, object]:
        return {
            "public_key": self.public_key.hex(),
            "node_hash": self.node_hash,
            "contact_name": None if self.name is None else self.name.text,
            "node_type": None if self.node_type is None else int(self.node_type),
            "advert_verified": self.advert_verified,
            "first_heard": None if self.first_heard is None else self.first_heard.isoformat(),
            "last_heard": None if self.last_heard is None else self.last_heard.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ContactObservation:
    """What a verified advert did to the store, so the runtime can report it."""

    contact: Contact
    created: bool
    name_changed: tuple[str, str] | None = None
    """`(previous, current)` when a verified advert renamed a known key."""


def parse_public_key(text: str) -> bytes:
    """A 32-byte public key from hex, with the failure naming what was wrong."""
    cleaned = text.strip().removeprefix("0x")
    try:
        key = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ContactError(f"{text!r} is not hex: {exc}") from exc
    if len(key) != PUB_KEY_SIZE:
        raise ContactError(
            f"a public key is {PUB_KEY_SIZE} bytes; {text!r} is {len(key)}"
        )
    return key


class ContactStore:
    """Contacts by public key, indexed by node hash, in memory only."""

    def __init__(self, *, logger: structlog.stdlib.BoundLogger | None = None) -> None:
        self._contacts: dict[bytes, Contact] = {}
        self._by_node_hash: dict[int, set[Contact]] = {}
        self._log = logger or get_logger(component="contacts")
        self.adverts_recorded = 0

    # --- Reading -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._contacts)

    def __iter__(self) -> Iterator[Contact]:
        return iter(self._contacts.values())

    def get(self, public_key: bytes) -> Contact | None:
        return self._contacts.get(public_key)

    def by_node_hash(self, node_hash: int) -> frozenset[Contact]:
        """Every contact whose key starts with this byte — a set, always.

        Never `None` and never a single contact: a 1-byte hash collides at
        1 in 256, and a caller handed one contact would have no way to know
        another was hidden behind it (§3 rule 1).
        """
        return frozenset(self._by_node_hash.get(node_hash, ()))

    def contacts(self) -> tuple[Contact, ...]:
        return tuple(self._contacts.values())

    # --- Writing -----------------------------------------------------------

    def observe_advert(
        self, advert: VerifiedAdvert, *, at: dt.datetime
    ) -> ContactObservation:
        """Record a contact from an advert whose signature verified.

        Takes a `VerifiedAdvert` rather than an `Advert` so an unverified one
        cannot reach this function at all (design D5): the discard happens in
        `net/rx.py`, where the verification result is produced, and nothing here
        has to remember to check.
        """
        self.adverts_recorded += 1
        name = advert.appdata.name
        existing = self._contacts.get(advert.public_key)
        if existing is None:
            contact = Contact(
                public_key=advert.public_key,
                name=name,
                node_type=advert.appdata.node_type,
                flags=advert.appdata.flags,
                first_heard=at,
                last_heard=at,
                advert_timestamp=advert.timestamp,
                advert_verified=True,
            )
            self._insert(contact)
            self._log.info("contact_created", **contact.as_json())
            return ContactObservation(contact=contact, created=True)

        previous = None if existing.name is None else existing.name.text
        current = None if name is None else name.text
        renamed = previous != current and current is not None
        existing.name = name if name is not None else existing.name
        existing.node_type = advert.appdata.node_type
        existing.flags = advert.appdata.flags
        existing.last_heard = at
        existing.advert_timestamp = advert.timestamp
        existing.advert_verified = True
        if renamed:
            # The key is the identity and the name is advert content: a name
            # that moves is either a rename or an impersonation, and only the
            # operator can tell which.
            self._log.info(
                "contact_renamed",
                previous_name=previous,
                **existing.as_json(),
            )
            return ContactObservation(
                contact=existing,
                created=False,
                name_changed=(previous or "(unnamed)", current or "(unnamed)"),
            )
        return ContactObservation(contact=existing, created=False)

    def add_public_key(self, key: bytes | str) -> Contact:
        """Add a contact from a public key alone, for a peer not yet heard.

        A key already known is returned unchanged: it may carry a name and flags
        a verified advert supplied, and pasting the same hex must not throw that
        away or downgrade the contact to unverified.
        """
        public_key = parse_public_key(key) if isinstance(key, str) else key
        if len(public_key) != PUB_KEY_SIZE:
            raise ContactError(f"a public key is {PUB_KEY_SIZE} bytes, got {len(public_key)}")
        existing = self._contacts.get(public_key)
        if existing is not None:
            return existing
        contact = Contact(public_key=public_key, advert_verified=False)
        self._insert(contact)
        self._log.info("contact_added_manually", **contact.as_json())
        return contact

    def _insert(self, contact: Contact) -> None:
        self._contacts[contact.public_key] = contact
        self._by_node_hash.setdefault(contact.node_hash, set()).add(contact)

    # --- Selection ---------------------------------------------------------

    def resolve(self, reference: str) -> Contact:
        """A contact by exact name or hex key prefix, or an error listing every match."""
        wanted = reference.strip()
        if not wanted:
            raise ContactError("a peer reference cannot be empty")
        prefix = wanted.removeprefix("0x").lower()
        matches = [
            contact
            for contact in self._contacts.values()
            if (contact.name is not None and contact.name.text == wanted)
            or contact.public_key.hex().startswith(prefix)
        ]
        if not matches:
            raise UnknownContactError(
                f"no contact matches {reference!r}; its advert may not have been "
                "heard yet"
            )
        if len(matches) > 1:
            listed = ", ".join(
                f"{contact.display_name} ({contact.public_key.hex()[:16]})"
                for contact in sorted(matches, key=lambda c: c.public_key)
            )
            raise AmbiguousContactError(
                f"{reference!r} matches {len(matches)} contacts: {listed}"
            )
        return matches[0]

    # --- The bus -----------------------------------------------------------

    async def handle(self, record: RxRecord) -> None:
        """Bus handler: verified adverts become contacts as they arrive."""
        match record.outcome:
            case AdvertOutcome() as outcome:
                verified = outcome.verified
                if verified is not None:
                    self.observe_advert(verified, at=record.received_at)
            case _:
                return

    def subscribe(self, bus: NetworkBus, *, name: str = "contacts") -> Subscription:
        return bus.subscribe(name, handler=self.handle)

    def as_json(self) -> dict[str, object]:
        return {
            "contacts": len(self._contacts),
            "advert_verified": sum(
                1 for contact in self._contacts.values() if contact.advert_verified
            ),
            "node_hashes": len(self._by_node_hash),
            "adverts_recorded": self.adverts_recorded,
        }
