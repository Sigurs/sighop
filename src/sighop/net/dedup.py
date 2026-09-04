"""The shared duplicate cache (DESIGN.md §4.2 step 3).

Flood routing means the same packet arrives repeatedly, by different routes and
at different hop counts. The key is therefore the payload alone — payload type
and payload bytes — with the path, hop count, transport codes and route type
excluded, because those are exactly the fields that mutate at each hop
(design D10).

Two properties are deliberate:

* **A duplicate is reported, not dropped silently.** It is counted, it gets its
  wide event, and only then is it kept off the bus. DESIGN.md §4.1's rule about
  never losing a frame invisibly applies to the ones we drop on purpose too.
* **Undecodable frames are never deduplicated.** A corrupt frame has no payload
  to key on, and two corrupt frames are two pieces of evidence about a link that
  is losing bytes, not one piece repeated.

Time comes from the record's own `received_at`, not from a wall clock, so a
replayed capture ages entries in capture time and the cache measured offline
behaves as it did on air.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import blake2b

from sighop.net.rx import RxRecord

DEFAULT_TTL_SECONDS = 300.0
"""TTL is the correctness bound (design D11), and the live evidence bounds it
from both sides. Most repeats arrive within a second, but the 2 h 54 min session
on 2026-09-04 saw a flood copy arrive **200.7 s** late by a different path, so a
shorter TTL discards real duplicates. The same session also saw two byte-identical
DIRECT frames **3158 s** apart — a sender retransmitting an unacked message, not
a copy of one transmission — so a longer TTL starts swallowing retries the user
is entitled to see. Five minutes sits between those, at 1.5x margin over the
worst real duplicate. Do not re-derive it from a capture shorter than the TTL
itself: the 442-frame corpus put the worst case at 31.1 s and was simply too
short to sample the tail."""

DEFAULT_MAX_ENTRIES = 4096
"""The memory bound. Repetition rate is not a property of the mesh (41.6% over
the milestone 0 nights, 11.0% over milestone 2's), so nothing about observed
traffic bounds the distinct-packet count and the cap has to."""

KEY_BYTES = 16


def content_key(payload_type: int, payload: bytes) -> bytes:
    """The dedup key: payload type and payload bytes, nothing else."""
    return blake2b(bytes((payload_type,)) + payload, digest_size=KEY_BYTES).digest()


def key_for(record: RxRecord) -> bytes | None:
    """The record's key, or None when it has no payload to key on."""
    if record.failed or record.packet is None:
        return None
    return content_key(int(record.packet.payload_type), record.packet.payload)


@dataclass(frozen=True, slots=True)
class FirstReception:
    """Not seen before — or not dedupable at all, when `key` is None."""

    key: bytes | None

    @property
    def is_duplicate(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class Duplicate:
    """Seen before, with everything needed to join it to the original."""

    key: bytes
    first_packet_id: str
    first_seen_at: dt.datetime
    interval_seconds: float
    copy_number: int
    """2 for the first repeat, 3 for the next, and so on."""

    @property
    def is_duplicate(self) -> bool:
        return True

    def as_json(self) -> dict[str, object]:
        return {
            "dup_key": self.key.hex(),
            "dup_of_packet_id": self.first_packet_id,
            "dup_interval_seconds": round(self.interval_seconds, 3),
            "dup_copy_number": self.copy_number,
        }


Verdict = FirstReception | Duplicate


@dataclass(slots=True)
class _Entry:
    packet_id: str
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    copies: int


@dataclass(frozen=True, slots=True)
class DedupStats:
    """What the status line reports, so the sizing is measured (design D11)."""

    considered: int
    duplicates: int
    passed_through: int
    entries: int
    peak_entries: int
    max_entries: int
    ttl_seconds: float
    widest_interval_seconds: float | None
    evictions_by_age: int
    evictions_by_cap: int

    @property
    def hit_rate(self) -> float:
        return 0.0 if self.considered == 0 else self.duplicates / self.considered

    def as_json(self) -> dict[str, object]:
        return {
            "considered": self.considered,
            "duplicates": self.duplicates,
            "passed_through": self.passed_through,
            "hit_rate": round(self.hit_rate, 4),
            "entries": self.entries,
            "peak_entries": self.peak_entries,
            "max_entries": self.max_entries,
            "ttl_seconds": self.ttl_seconds,
            "widest_interval_seconds": (
                None
                if self.widest_interval_seconds is None
                else round(self.widest_interval_seconds, 3)
            ),
            "evictions_by_age": self.evictions_by_age,
            "evictions_by_cap": self.evictions_by_cap,
        }


class DedupCache:
    """Bounded by age and by entry count, because they bound different things."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[bytes, _Entry] = OrderedDict()
        self._considered = 0
        self._duplicates = 0
        self._passed_through = 0
        self._widest_interval: float | None = None
        self._evictions_by_age = 0
        self._evictions_by_cap = 0
        # The cap is sized off the peak, not off whatever happens to be resident
        # when someone looks (task 8.3).
        self._peak_entries = 0

    def observe(self, record: RxRecord) -> Verdict:
        """Classify a reception and record it. The only mutating entry point."""
        key = key_for(record)
        if key is None:
            self._passed_through += 1
            return FirstReception(key=None)

        now = record.received_at
        self._expire(now)
        self._considered += 1

        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = _Entry(
                packet_id=record.packet_id,
                first_seen_at=now,
                last_seen_at=now,
                copies=1,
            )
            self._enforce_cap()
            self._peak_entries = max(self._peak_entries, len(self._entries))
            self._passed_through += 1
            return FirstReception(key=key)

        entry.copies += 1
        entry.last_seen_at = now
        self._entries.move_to_end(key)
        interval = (now - entry.first_seen_at).total_seconds()
        if self._widest_interval is None or interval > self._widest_interval:
            self._widest_interval = interval
        self._duplicates += 1
        return Duplicate(
            key=key,
            first_packet_id=entry.packet_id,
            first_seen_at=entry.first_seen_at,
            interval_seconds=interval,
            copy_number=entry.copies,
        )

    def _expire(self, now: dt.datetime) -> None:
        cutoff = now - dt.timedelta(seconds=self.ttl_seconds)
        # Insertion order is not age order once entries are touched, so this
        # walks rather than popping from the front: a heavily repeated packet
        # would otherwise keep younger entries alive behind it.
        stale = [key for key, entry in self._entries.items() if entry.last_seen_at < cutoff]
        for key in stale:
            del self._entries[key]
        self._evictions_by_age += len(stale)

    def _enforce_cap(self) -> None:
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self._evictions_by_cap += 1

    @property
    def stats(self) -> DedupStats:
        return DedupStats(
            considered=self._considered,
            duplicates=self._duplicates,
            passed_through=self._passed_through,
            entries=len(self._entries),
            peak_entries=self._peak_entries,
            max_entries=self.max_entries,
            ttl_seconds=self.ttl_seconds,
            widest_interval_seconds=self._widest_interval,
            evictions_by_age=self._evictions_by_age,
            evictions_by_cap=self._evictions_by_cap,
        )
