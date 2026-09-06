"""Shared reverse-path learning (DESIGN.md §4.2 step 4).

A flood packet carries the path it took to reach us; reversed, that is a route
back to its sender. Learning it is what lets a later reply go DIRECT instead of
flooding the mesh again.

The store is **platform-wide, not per-entity** — a route is a property of the RF
neighbourhood, and duplicating it per entity would waste memory and learn slower
(§4.2).

Milestone 5 adds a durable backing behind it and changes nothing else. Memory is
still the authority and answers every lookup at full speed, including while the
database is unreachable; an optional `sink` receives learned routes and writes
them behind the reception path, dropping freely when it cannot keep up, because
a route is relearned from the next reception (design D2). Restored routes keep
the confirmation time they were learned with — restoring is not confirming, so a
live reception still beats an old route under most-recently-confirmed-wins.
Nothing here imports SQLAlchemy (design D10).

Two things this deliberately does *not* do:

* **It does not score.** Resolution is most-recently-confirmed-wins, with SNR
  and hop count recorded beside each candidate. DESIGN.md §13's third unknown —
  whether scoring needs to be cleverer — is only answerable once multiple routes
  to one peer have actually been observed, and this is what will observe them.
* **It does not resolve node-hash ambiguity.** A 1-byte hash may denote several
  nodes (§3). An entry keyed by hash is *marked* ambiguous and left that way;
  resolving it needs contacts, which is milestone 5.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sighop.net.rx import AdvertOutcome, Payload, RxRecord
from sighop.protocol.packet import RouteType
from sighop.protocol.payloads import AnonRequestEnvelope

DEFAULT_MAX_DESTINATIONS = 1024
DEFAULT_MAX_CANDIDATES_PER_DESTINATION = 4


@dataclass(frozen=True, slots=True)
class PathKey:
    """Who a path leads to: a public key when we have one, else a node hash.

    `ambiguous` is True exactly when the key is a bare node hash, and it travels
    with the entry so no consumer can forget that it is one byte of identity.
    """

    public_key: bytes | None
    node_hash: int
    ambiguous: bool

    @classmethod
    def for_public_key(cls, public_key: bytes) -> PathKey:
        return cls(public_key=public_key, node_hash=public_key[0], ambiguous=False)

    @classmethod
    def for_node_hash(cls, node_hash: int) -> PathKey:
        return cls(public_key=None, node_hash=node_hash, ambiguous=True)

    def as_json(self) -> dict[str, object]:
        return {
            "public_key": None if self.public_key is None else self.public_key.hex(),
            "node_hash": self.node_hash,
            "ambiguous": self.ambiguous,
        }


class PathSource(StrEnum):
    """How a route came to be known. Both are candidates; neither is proof.

    `REVERSE` is the frame's own path, reversed — what §4.2 has always learned.
    `PATH_BODY` is the peer's *explicit* statement of a route, decrypted from a
    `PATH` payload (design D12). The distinction is worth carrying because a
    path body is content a MAC match selected a key for, and **a MAC match
    selects a key, it never authenticates a sender** (§5, milestone 4's design
    D7): the route is claimed by whoever holds that key, and the renderer says
    so in the same convention it uses for every other unverified claim.
    """

    REVERSE = "reverse"
    PATH_BODY = "path_body"


@dataclass(frozen=True, slots=True)
class LearnedPath:
    """One candidate route back, as observed from one reception."""

    path: bytes
    """The *reverse* of the received path — the route out, not the route in.

    For a `PATH_BODY` route this is the route the peer declared, verbatim: it is
    already the route *out*, because that is what the peer was telling us."""

    hash_size: int
    hop_count: int
    snr_db: float | None
    confirmed_at: dt.datetime
    packet_id: str
    source: PathSource = PathSource.REVERSE
    """Not persisted, and deliberately so: the `path` table stores a route, and
    a restored route is not being claimed by anyone at the moment it is read
    back. It comes back as `REVERSE`, which is the store's own default and the
    weaker statement of the two."""

    @property
    def is_zero_hop(self) -> bool:
        """Direct reception. A route, and not the absence of one."""
        return self.hop_count == 0

    @property
    def claimed(self) -> bool:
        """Whether a sender asserted this route rather than a reception showing it."""
        return self.source is PathSource.PATH_BODY

    def as_json(self) -> dict[str, object]:
        return {
            "path": self.path.hex(),
            "hash_size": self.hash_size,
            "hop_count": self.hop_count,
            "snr_db": self.snr_db,
            "confirmed_at": self.confirmed_at.isoformat(),
            "packet_id": self.packet_id,
            "source": str(self.source),
        }


def reverse_path(path: bytes, hash_size: int) -> bytes:
    """Reverse a path hop-wise, keeping each hop's bytes in order.

    Hops are 1, 2 or 3 bytes (§4.2); reversing the byte string would corrupt
    every multi-byte hop, and 510 of the corpus's 997 frames use one.
    """
    if hash_size <= 0:
        raise ValueError(f"hash_size must be positive, got {hash_size}")
    if len(path) % hash_size:
        raise ValueError(f"path of {len(path)} bytes is not whole hops of {hash_size}")
    hops = [path[i : i + hash_size] for i in range(0, len(path), hash_size)]
    return b"".join(reversed(hops))


def sender_key(record: RxRecord) -> PathKey | None:
    """Who sent this, as well as the payload allows. None when it says nothing."""
    match record.outcome:
        case AdvertOutcome(verification=verification):
            verified = record.outcome.verified
            if verified is not None:
                return PathKey.for_public_key(verified.public_key)
            # An unverified advert's public key is attacker-chosen, so it is not
            # used as a key; the node hash is at least what the radio delivered.
            return PathKey.for_node_hash(verification.node_hash)
        case Payload(payload=AnonRequestEnvelope() as envelope):
            return PathKey.for_public_key(envelope.sender_public_key)
        case _:
            source = record.src_hash
            return None if source is None else PathKey.for_node_hash(source)


class PathSink(Protocol):
    """Where learned routes go to be written. Never awaits, never raises.

    Implemented by the write-behind queue in `db/`. Unlike the contact queue this
    one drops when it is full and reports the drop as a counter: a route costs
    one reception to relearn, so an unbounded backlog would buy nothing.
    """

    def offer(self, entry: tuple[PathKey, LearnedPath]) -> bool: ...


class PathStore:
    """Learned routes, keyed by sender, bounded, and authoritative in memory."""

    def __init__(
        self,
        *,
        max_destinations: int = DEFAULT_MAX_DESTINATIONS,
        max_candidates: int = DEFAULT_MAX_CANDIDATES_PER_DESTINATION,
        sink: PathSink | None = None,
    ) -> None:
        if max_destinations <= 0 or max_candidates <= 0:
            raise ValueError("bounds must be positive")
        self.max_destinations = max_destinations
        self.max_candidates = max_candidates
        self._sink = sink
        self._paths: OrderedDict[tuple[bytes | None, int], list[LearnedPath]] = OrderedDict()
        self._keys: dict[tuple[bytes | None, int], PathKey] = {}
        self._learned = 0
        self._evictions = 0
        self.restored = 0

    def observe(self, record: RxRecord) -> tuple[PathKey, LearnedPath] | None:
        """Learn from a reception. Returns what was learned, or None.

        Flood receptions teach us the reverse of the path they took. A DIRECT
        reception is following a route someone else chose, so its remaining
        path is *not* a route back — with one exception that matters in
        practice: a DIRECT packet that arrives with an **empty** path reached
        us over the air with no repeater in between, which is a zero-hop route
        and the most useful one there is. MeshCore's zero-hop adverts arrive
        exactly this way, and reading only flood packets throws them away
        (observed replaying `captures/2026-09-04-03.jsonl`, where most adverts
        are DIRECT/h0 and only two destinations were learned).
        """
        if record.packet is None:
            return None
        route = record.route_type
        flooded = route in (RouteType.FLOOD, RouteType.TRANSPORT_FLOOD)
        direct_neighbour = (
            route in (RouteType.DIRECT, RouteType.TRANSPORT_DIRECT)
            and record.packet.hop_count == 0
            and not record.packet.path
        )
        if not (flooded or direct_neighbour):
            return None

        key = sender_key(record)
        if key is None:
            return None

        learned = LearnedPath(
            path=reverse_path(record.packet.path, record.packet.hash_size),
            hash_size=record.packet.hash_size,
            hop_count=record.packet.hop_count,
            snr_db=record.snr_db,
            confirmed_at=record.received_at,
            packet_id=record.packet_id,
        )
        self._insert(key, learned)
        if self._sink is not None:
            # Offered, not written: `offer` returns immediately and a full queue
            # drops rather than back-pressuring the decode stage (design D2).
            self._sink.offer((key, learned))
        return key, learned

    def restore(self, entries: Iterable[tuple[PathKey, LearnedPath]]) -> int:
        """Adopt routes read from the store, before traffic arrives.

        Restored routes carry the `confirmed_at` they were learned with, so a
        route confirmed by a live reception wins over an older restored one
        under the existing most-recently-confirmed rule — restoration is not
        confirmation. They are not offered back to the sink: they came from it.
        """
        restored = 0
        for key, learned in entries:
            self._insert(key, learned)
            restored += 1
        self.restored += restored
        return restored

    def _insert(self, key: PathKey, learned: LearnedPath) -> None:
        index = (key.public_key, key.node_hash)
        candidates = self._paths.get(index)
        if candidates is None:
            candidates = []
            self._paths[index] = candidates
            self._keys[index] = key
        self._paths.move_to_end(index)

        # A repeat of a route we already know is a re-confirmation, not a second
        # candidate: the timestamp moves, the list does not grow.
        for position, existing in enumerate(candidates):
            if existing.path == learned.path and existing.hash_size == learned.hash_size:
                candidates[position] = learned
                break
        else:
            candidates.append(learned)
            if len(candidates) > self.max_candidates:
                candidates.sort(key=lambda candidate: candidate.confirmed_at)
                del candidates[0]

        self._learned += 1
        while len(self._paths) > self.max_destinations:
            evicted, _ = self._paths.popitem(last=False)
            self._keys.pop(evicted, None)
            self._evictions += 1

    def lookup(self, key: PathKey) -> LearnedPath | None:
        """The most recently confirmed route, or None for "no route known".

        None rather than an empty path: an empty path means zero hops, which is
        a real and useful route, and conflating the two would send a reply out
        direct to a node we have never heard from.
        """
        candidates = self._paths.get((key.public_key, key.node_hash))
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate.confirmed_at)

    def lookup_public_key(self, public_key: bytes) -> LearnedPath | None:
        return self.lookup(PathKey.for_public_key(public_key))

    def lookup_node_hash(self, node_hash: int) -> LearnedPath | None:
        return self.lookup(PathKey.for_node_hash(node_hash))

    def candidates(self, key: PathKey) -> tuple[LearnedPath, ...]:
        """Every route recorded for this destination, newest last."""
        candidates = self._paths.get((key.public_key, key.node_hash), [])
        return tuple(sorted(candidates, key=lambda candidate: candidate.confirmed_at))

    def keys(self) -> tuple[PathKey, ...]:
        return tuple(self._keys.values())

    @property
    def destination_count(self) -> int:
        return len(self._paths)

    def as_json(self) -> dict[str, object]:
        return {
            "destinations": self.destination_count,
            "max_destinations": self.max_destinations,
            "learned": self._learned,
            "evictions": self._evictions,
            "restored": self.restored,
            "ambiguous_destinations": sum(1 for key in self._keys.values() if key.ambiguous),
        }
