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

Milestone 5 gives the store a durable backing without changing any of that.
Memory stays the authority — every lookup is answered from it, including while
the database is unreachable — and a `sink` receives contacts to write in its own
time (design D2). Three properties of that arrangement are load-bearing:

* **The write never happens on the path that records the advert** (design D16).
  `sink.offer` neither awaits nor raises, and a sink that refuses hands the
  contact back rather than dropping it.
* **A contact whose write has not landed is marked, not lost** (design D15).
  Re-acquiring a contact means waiting for the peer to advert again and the
  advert floor is 24 h, so the marker is what the recovery flush writes.
* **Nothing here imports SQLAlchemy** (design D10). The sink is a protocol, the
  records crossing it are this module's own `Contact`, and a run with no
  database behaves exactly as it did before.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

from sighop.logging import Logger, get_logger
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

    changed: bool = True
    """Whether anything worth storing moved.

    False for the common case: the *same* advert reaching us again over another
    path. Adverts flood, so one advert produces several receptions, and a write
    per reception would make the write rate a function of how well we hear a
    peer rather than of what we learned about it (design D2). `last_heard` moves
    either way — it is in memory, and the next real change carries it."""


class ContactSink(Protocol):
    """Where contacts go to be written. Never awaits, never raises.

    Implemented by the write-behind queue in `db/`. `offer` returning False
    means the queue is full and the caller keeps the contact marked unpersisted —
    the queue refuses rather than drops, because a lost contact costs a 24 h wait
    (design D15, D16).
    """

    def offer(self, contact: Contact) -> bool: ...


type ObservationListener = Callable[[ContactObservation, RxRecord], None]
"""Who is told what a verified advert did to the store (milestone 7 design D2).

The store has always known the one fact that matters — whether an advert
*created* a contact — and never told anyone. A component that must act on the
first sighting of a peer could subscribe to the bus for advert records instead
and ask the store afterwards, but subscribers have independent queues: the
store's own subscriber may or may not have processed that record yet, so the
answer would be a race whose wrong branch greets a peer twice or not at all.

Invoked synchronously, after the store has been updated and before the next
observation is processed, so a first sighting is an ordered fact. It is expected
to *offer* work to a queue of its own, not to do it here.

A store holds any number of listeners (the bot host, the webhook dispatcher),
invoked in registration order with the same observation. Each is isolated from
the others: one that raises is counted and reported, and the rest still hear
about the advert (webhook-notifications design D1)."""


def _listener_name(listener: ObservationListener) -> str:
    """Which listener failed, for the log: a bound method's qualified name."""
    return getattr(listener, "__qualname__", None) or type(listener).__qualname__


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
    """Contacts by public key, indexed by node hash. Memory is the authority."""

    def __init__(
        self,
        *,
        logger: Logger | None = None,
        sink: ContactSink | None = None,
        on_observation: ObservationListener | None = None,
    ) -> None:
        self._contacts: dict[bytes, Contact] = {}
        self._by_node_hash: dict[int, set[Contact]] = {}
        self._log = logger or get_logger(component="contacts")
        self._sink = sink
        self._listeners: tuple[ObservationListener, ...] = (
            () if on_observation is None else (on_observation,)
        )
        self._unpersisted: set[bytes] = set()
        self.adverts_recorded = 0
        self.restored = 0
        self.writes_offered = 0
        self.writes_refused = 0
        self.listener_failures = 0

    def add_observation_listener(self, listener: ObservationListener) -> None:
        """Wire another observer. Add before any traffic is processed; a
        listener attached mid-run would miss the sightings it exists for."""
        self._listeners = (*self._listeners, listener)

    @property
    def observation_listeners(self) -> tuple[ObservationListener, ...]:
        return self._listeners

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
            self._persist(contact)
            return ContactObservation(contact=contact, created=True)

        previous = None if existing.name is None else existing.name.text
        current = None if name is None else name.text
        renamed = previous != current and current is not None
        # What makes this worth a write: a different advert, not another copy of
        # the same one. A flood advert reaches us over several paths, and the
        # advert's own timestamp is what tells the copies apart (design D2).
        changed = (
            renamed
            or not existing.advert_verified
            or existing.advert_timestamp != advert.timestamp
            or existing.node_type != advert.appdata.node_type
            or existing.flags != advert.appdata.flags
        )
        existing.name = name if name is not None else existing.name
        existing.node_type = advert.appdata.node_type
        existing.flags = advert.appdata.flags
        existing.last_heard = at
        existing.advert_timestamp = advert.timestamp
        existing.advert_verified = True
        if changed:
            self._persist(existing)
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
                changed=True,
                name_changed=(previous or "(unnamed)", current or "(unnamed)"),
            )
        return ContactObservation(contact=existing, created=False, changed=changed)

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
        self._persist(contact)
        return contact

    def _insert(self, contact: Contact) -> None:
        self._contacts[contact.public_key] = contact
        self._by_node_hash.setdefault(contact.node_hash, set()).add(contact)

    # --- Durability (design D2, D15, D16) ----------------------------------

    def restore(self, contacts: Iterable[Contact]) -> int:
        """Adopt contacts read from the store, before any traffic is processed.

        Restored contacts are *not* offered back to the sink: they came from it,
        and re-writing them at startup would turn every restart into a full
        table rewrite. They are not marked unpersisted either, for the same
        reason — the database already holds them.
        """
        restored = 0
        for contact in contacts:
            if contact.public_key in self._contacts:
                continue
            self._insert(contact)
            restored += 1
        self.restored += restored
        self._log.info("contacts_restored", outcome="success", restored=restored)
        return restored

    def _persist(self, contact: Contact) -> None:
        """Hand a changed contact to the sink. Never awaits, never raises.

        The contact is marked unpersisted the moment it is offered and stays
        marked until a write actually lands: queued is not written, and the
        difference is the whole of what the recovery backfill acts on.
        """
        if self._sink is None:
            return
        self._unpersisted.add(contact.public_key)
        self.writes_offered += 1
        if not self._sink.offer(contact):
            # The queue refuses rather than drops (design D16). The marker is
            # what carries the contact now, and the next flush writes it.
            self.writes_refused += 1
            self._log.error(
                "contact_write_queue_full",
                outcome="error",
                public_key=contact.public_key.hex(),
                awaiting_backfill=len(self._unpersisted),
            )

    def mark_persisted(self, contact: Contact) -> None:
        """A write landed. Clearing a marker that was never set is harmless."""
        self._unpersisted.discard(contact.public_key)

    def mark_unpersisted(self, contact: Contact) -> None:
        """A write did not land. Idempotent, and safe to call redundantly: the
        write is an upsert on the public key, so a marker wrongly set costs one
        extra write and a marker wrongly cleared is what the tests target."""
        self._unpersisted.add(contact.public_key)

    def unpersisted(self) -> tuple[Contact, ...]:
        """Every contact whose latest state is not known to be stored.

        Latest state, not every observation: the write is an upsert on the
        public key, so a peer observed several times during an outage lands
        once rather than replaying each observation (design D15).
        """
        return tuple(
            contact
            for key, contact in self._contacts.items()
            if key in self._unpersisted
        )

    @property
    def awaiting_backfill(self) -> int:
        return len(self._unpersisted)

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
                    observation = self.observe_advert(verified, at=record.received_at)
                    self._report(observation, record)
            case _:
                return

    def _report(self, observation: ContactObservation, record: RxRecord) -> None:
        """Tell every listener, and survive each of them.

        A listener is untrusted with respect to the store and to every other
        listener: the contact is already recorded and already offered for
        persistence by the time this runs, and a listener that raises must cost
        none of that, must not keep the observation from the listeners after
        it, and must not stop the next advert being processed. The failure is
        reported — a listener failing silently is a bot or a webhook that has
        quietly stopped seeing the mesh.
        """
        for listener in self._listeners:
            try:
                listener(observation, record)
            except Exception as exc:
                self.listener_failures += 1
                self._log.error(
                    "contact_listener_failed",
                    outcome="error",
                    listener=_listener_name(listener),
                    packet_id=record.packet_id,
                    public_key=observation.contact.public_key.hex(),
                    error=f"{type(exc).__name__}: {exc}",
                    listener_failures=self.listener_failures,
                )

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
            "restored": self.restored,
            "awaiting_backfill": self.awaiting_backfill,
        }
