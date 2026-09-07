"""The live feed's hub: one subscription, many connections (design D4).

This is the one place in milestone 8 where getting it wrong would let a browser
slow the radio, so the contract is the contact sink's, stated as sharply as it
can be stated:

* **`offer` never awaits and never raises.** It is called from the reception
  path — from the bus subscriber and from the pipeline's observer — and a
  browser is the slowest thing yet proposed to attach to that path.
* **A connection that cannot keep up loses its oldest records**, and the drops
  are counted per connection and reported *into that connection*, so the
  browser can say "feed incomplete, 412 dropped" rather than quietly showing a
  version of the mesh that is missing things.
* **One `NetworkBus.subscribe("web-feed")` for the process**, however many tabs
  are open. The bus's subscriber list is a property of the platform — it is in
  the status line and in a room server's `ServerStats` — and making it grow with
  the number of browser tabs would turn an operator's second tab into a change
  in what the platform reports about itself.

The socket write happens in each connection's own task, reading from its own
bounded `deque`. Nothing on the reception path ever touches a socket.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from sighop.logging import Logger, get_logger
from sighop.net.bus import NetworkBus, Submission, Subscription, TxOutcome
from sighop.net.rx import RxRecord
from sighop.web.serialize import rx_record, tx_record

FEED_SUBSCRIBER = "web-feed"
"""What the hub registers on the bus as. One, for the process."""

DEFAULT_CONNECTION_QUEUE = 256
"""How far behind one browser may fall before it starts losing records.

A guess until a session runs against real advert volume — 555 receptions in
2 h 54 min was the busiest measured, so this is minutes of history for a slow
tab. It is the design's first open question and it changes a default, not a
design."""


@dataclass(slots=True)
class Connection:
    """One browser's share of the feed: a bounded queue and its losses."""

    name: str
    capacity: int = DEFAULT_CONNECTION_QUEUE
    records: deque[dict[str, Any]] = field(default_factory=deque)
    delivered: int = 0
    dropped: int = 0
    opened_at: dt.datetime | None = None
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _closed: bool = False

    def offer(self, row: dict[str, Any]) -> None:
        """Take one row, dropping the oldest if this connection is behind.

        Never awaits, never raises. Dropping the *oldest* rather than refusing
        the newest is the right way round for a feed: what a watcher wants is
        the present, and the drop count is what makes the gap honest.
        """
        if self._closed:
            return
        while len(self.records) >= self.capacity:
            self.records.popleft()
            self.dropped += 1
        self.records.append(row)
        self._wake.set()

    async def drain(self) -> list[dict[str, Any]]:
        """Everything queued, waiting for at least one. Cancels cleanly."""
        while not self.records and not self._closed:
            self._wake.clear()
            await self._wake.wait()
        batch = list(self.records)
        self.records.clear()
        self.delivered += len(batch)
        return batch

    @property
    def incomplete(self) -> bool:
        return self.dropped > 0

    def status(self) -> dict[str, Any]:
        """What the browser is told about its own feed's completeness."""
        return {
            "kind": "status",
            "delivered": self.delivered,
            "dropped": self.dropped,
            "incomplete": self.incomplete,
            "capacity": self.capacity,
        }

    def close(self) -> None:
        self._closed = True
        self._wake.set()

    @property
    def closed(self) -> bool:
        return self._closed


class FeedHub:
    """One subscription and one TX callback, fanned out to open connections."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CONNECTION_QUEUE,
        logger: Logger | None = None,
    ) -> None:
        self.capacity = capacity
        self.log = logger or get_logger(component="web-feed")
        self.connections: list[Connection] = []
        self.subscription: Subscription | None = None
        self.traffic = EntityTraffic()
        self.published = 0
        self._next = 0

    # --- Attaching to the platform -----------------------------------------

    def subscribe(self, bus: NetworkBus) -> Subscription:
        """Take this process's one bus subscription.

        Used for nothing but a name and the platform's own accounting: the rows
        themselves come through `on_reception`, which the pipeline's observer
        calls for duplicates too. Both would work; having exactly one path into
        the hub is what keeps a record from being shown twice.
        """
        self.subscription = bus.subscribe(FEED_SUBSCRIBER, handler=self._ignore)
        return self.subscription

    async def _ignore(self, record: RxRecord) -> None:
        """The bus subscription's handler. Deliberately does nothing.

        The hub is fed by `IngressPipeline.observer`, which sees duplicates as
        well — and a record delivered by both paths would appear twice in every
        browser. The subscription is still taken, because the platform's own
        reporting is where an operator sees that something is watching.
        """
        return None

    def on_reception(self, record: RxRecord, duplicate: bool) -> None:
        """The pipeline's observer. Never awaits, never raises."""
        if not duplicate:
            # Counted once per frame the platform acted on, so the per-entity
            # numbers and the dedup counters do not double-count the same
            # packet in two places on one screen.
            self.traffic.on_reception(record)
        self._fan_out(rx_record(record, duplicate=duplicate))

    def on_transmission(
        self, submission: Submission, outcome: TxOutcome, *, at: dt.datetime
    ) -> None:
        """The runtime's TX resolution callback. Never awaits, never raises."""
        self.traffic.on_transmission(submission, outcome)
        self._fan_out(tx_record(submission, outcome, at=at))

    def _fan_out(self, row: dict[str, Any]) -> None:
        self.published += 1
        for connection in self.connections:
            connection.offer(row)

    # --- Connections --------------------------------------------------------

    def connect(self, *, at: dt.datetime | None = None) -> Connection:
        self._next += 1
        connection = Connection(
            name=f"feed-{self._next}", capacity=self.capacity, opened_at=at
        )
        self.connections.append(connection)
        return connection

    def disconnect(self, connection: Connection) -> None:
        connection.close()
        if connection in self.connections:
            self.connections.remove(connection)

    def close(self) -> None:
        """Close every connection. For a stopping run."""
        for connection in list(self.connections):
            self.disconnect(connection)

    @property
    def open_connections(self) -> int:
        return len(self.connections)

    def as_json(self) -> dict[str, Any]:
        return {
            "feed_connections": self.open_connections,
            "feed_published": self.published,
            "feed_dropped": sum(c.dropped for c in self.connections),
        }


# --- Per-entity counters (§8) -----------------------------------------------


@dataclass(slots=True)
class EntityTraffic:
    """TX and RX counted per local identity, from the traffic the hub sees.

    Two different kinds of fact, and the difference is stated rather than
    averaged away:

    * **TX is exact.** Every submission carries the `entity_id` that originated
      it, so a transmission belongs to precisely one identity.
    * **RX is by node hash**, and a node hash is one byte. §3 puts a collision
      at 1 in 256, so what this counts is *frames addressed to this identity's
      hash* — which is what the platform itself has to work with, and is not
      the same claim as "frames for this identity". The panel says so where it
      shows the number.

    Kept here rather than in `net/`: these are a display's counters, and adding
    them to the scheduler would be adding state to the transmit path for the
    benefit of a browser.
    """

    transmitted: dict[str, int] = field(default_factory=dict)
    suppressed: dict[str, int] = field(default_factory=dict)
    addressed: dict[int, int] = field(default_factory=dict)

    def on_transmission(self, submission: Submission, outcome: TxOutcome) -> None:
        table = self.transmitted if outcome.sent else self.suppressed
        table[submission.entity_id] = table.get(submission.entity_id, 0) + 1

    def on_reception(self, record: RxRecord) -> None:
        dest = record.dest_hash
        if dest is None:
            return
        self.addressed[dest] = self.addressed.get(dest, 0) + 1

    def for_entity(self, entity_id: str, node_hash: int) -> dict[str, int]:
        return {
            "transmitted": self.transmitted.get(entity_id, 0),
            "suppressed": self.suppressed.get(entity_id, 0),
            "addressed": self.addressed.get(node_hash, 0),
        }
