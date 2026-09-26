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
from sighop.protocol.packet import MAX_PATH_SIZE, RouteType
from sighop.protocol.payloads import AnonRequestEnvelope

DEFAULT_MAX_DESTINATIONS = 1024
DEFAULT_MAX_CANDIDATES_PER_DESTINATION = 4
MAX_HOP_COUNT = 63
"""The six bits of `path_length` that count hops (packet_format.md)."""


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


class RouteRewrite(StrEnum):
    """What the preferred first hop did to a route (`route-preference`).

    `PREFERRED` is a learned candidate that already started with the preferred
    repeater and was used as learned; only `PREPENDED` and `SHORTENED` change
    the bytes that are sent.
    """

    NONE = "none"
    PREFERRED = "preferred"
    PREPENDED = "prepended"
    SHORTENED = "shortened"

    @property
    def rewritten(self) -> bool:
        return self in (RouteRewrite.PREPENDED, RouteRewrite.SHORTENED)


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """The route a send will actually use, and the candidate it came from."""

    path: bytes
    hash_size: int
    hop_count: int
    rewrite: RouteRewrite
    learned: LearnedPath

    @property
    def snr_db(self) -> float | None:
        return self.learned.snr_db

    @property
    def confirmed_at(self) -> dt.datetime:
        return self.learned.confirmed_at

    @classmethod
    def as_learned(
        cls, learned: LearnedPath, rewrite: RouteRewrite = RouteRewrite.NONE
    ) -> ResolvedPath:
        return cls(
            path=learned.path,
            hash_size=learned.hash_size,
            hop_count=learned.hop_count,
            rewrite=rewrite,
            learned=learned,
        )


def _hops(path: bytes, hash_size: int) -> list[bytes]:
    return [path[i : i + hash_size] for i in range(0, len(path), hash_size)]


def reverse_path(path: bytes, hash_size: int) -> bytes:
    """Reverse a path hop-wise, keeping each hop's bytes in order.

    Hops are 1, 2 or 3 bytes (§4.2); reversing the byte string would corrupt
    every multi-byte hop, and 510 of the corpus's 997 frames use one.
    """
    if hash_size <= 0:
        raise ValueError(f"hash_size must be positive, got {hash_size}")
    if len(path) % hash_size:
        raise ValueError(f"path of {len(path)} bytes is not whole hops of {hash_size}")
    return b"".join(reversed(_hops(path, hash_size)))


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
        self._preferred_first_hop: bytes | None = None
        self.prepend_overflow = 0
        """Sends left unrewritten because prepending would exceed the path limits."""

    @property
    def preferred_first_hop(self) -> bytes | None:
        """The repeater every DIRECT send leaves through first, by public key."""
        return self._preferred_first_hop

    @preferred_first_hop.setter
    def preferred_first_hop(self, public_key: bytes | None) -> None:
        if public_key is not None and len(public_key) != 32:
            raise ValueError(f"a public key is 32 bytes, got {len(public_key)}")
        self._preferred_first_hop = None if public_key is None else bytes(public_key)

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

    def resolve(self, key: PathKey, *, count: bool = True) -> ResolvedPath | None:
        """The route a DIRECT send to `key` uses, after the preferred first hop.

        With no preference this is `lookup` unchanged. Otherwise (design D2): a
        destination that *is* the preferred repeater is sent to as learned; a
        candidate already starting with it wins over newer ones; else the newest
        candidate is shortened to start at the preferred repeater when it passes
        through it, or has it prepended. Hops are compared at each candidate's
        own width, never as a byte search across hop boundaries.

        `count` is false for callers that only display the route, so rendering
        the contact table does not inflate `prepend_overflow`.
        """
        newest = self.lookup(key)
        preferred = self._preferred_first_hop
        if newest is None or preferred is None:
            return None if newest is None else ResolvedPath.as_learned(newest)
        if key.public_key == preferred or (
            key.public_key is None and key.node_hash == preferred[0]
        ):
            return ResolvedPath.as_learned(newest)

        through = [
            candidate
            for candidate in self._paths.get((key.public_key, key.node_hash), [])
            if candidate.hop_count > 0
            and candidate.path[: candidate.hash_size] == preferred[: candidate.hash_size]
        ]
        if through:
            best = max(through, key=lambda candidate: candidate.confirmed_at)
            return ResolvedPath.as_learned(best, RouteRewrite.PREFERRED)

        width = newest.hash_size
        hop = preferred[:width]
        hops = _hops(newest.path, width)
        if hop in hops:
            position = hops.index(hop)
            return ResolvedPath(
                path=b"".join(hops[position:]),
                hash_size=width,
                hop_count=newest.hop_count - position,
                rewrite=RouteRewrite.SHORTENED,
                learned=newest,
            )
        path = hop + newest.path
        if len(path) > MAX_PATH_SIZE or newest.hop_count + 1 > MAX_HOP_COUNT:
            if count:
                self.prepend_overflow += 1
            return ResolvedPath.as_learned(newest)
        return ResolvedPath(
            path=path,
            hash_size=width,
            hop_count=newest.hop_count + 1,
            rewrite=RouteRewrite.PREPENDED,
            learned=newest,
        )

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
            "preferred_first_hop": (
                None if self._preferred_first_hop is None else self._preferred_first_hop.hex()
            ),
            "prepend_overflow": self.prepend_overflow,
        }


def resolve_for_contact(
    paths: PathStore, public_key: bytes, node_hash: int, *, count: bool = True
) -> tuple[ResolvedPath, bool] | None:
    """The route to a peer and whether it is ambiguous, or None for no route.

    Public key first; node hash second, when that is all there is, and marked
    ambiguous — one byte of identity (§3). Every sender and the contact table
    call this, so none can choose a route another would not.
    """
    resolved = paths.resolve(PathKey.for_public_key(public_key), count=count)
    if resolved is not None:
        return resolved, False
    resolved = paths.resolve(PathKey.for_node_hash(node_hash), count=count)
    if resolved is not None:
        return resolved, True
    return None
