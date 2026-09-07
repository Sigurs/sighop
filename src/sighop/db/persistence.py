"""The wiring: one object the runtime holds, and everything durable behind it.

This is where the three write policies of design D2 become three objects, and
where the asymmetry between them is visible in one place:

* **contacts** — a queue that never drops, an unpersisted marker for whatever it
  cannot take, and a backfill on recovery. Re-acquiring a contact means waiting
  for the peer to advert and the floor is 24 h (design D15).
* **paths** — a queue that drops its oldest and counts the drop. A route is
  relearned from the next reception.
* **packet log** — the same, batched, plus a pruner. Highest volume, lowest
  value per row, and explicitly not an audit trail.
* **direct messages** — the contact policy again, and for a sharper reason
  (milestone 8 design D8): a lost route is relearned from the next reception and
  a lost conversation entry is not re-acquirable at all. It refuses rather than
  displacing, and what it refuses is counted and reported.

Nothing here is on the reception path. `ContactStore` and `PathStore` call
`offer`, which neither awaits nor raises; everything else happens in tasks the
radio does not wait for. A database that is unreachable, slow or blackholing
costs counters and a `degraded` flag, and costs the radio nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from sighop.db.engine import Database, Succeeded
from sighop.db.packetlog import (
    DEFAULT_PRUNE_INTERVAL_SECONDS,
    PacketLogPruner,
    rx_row,
    tx_row,
)
from sighop.db.repositories import (
    DEFAULT_PACKET_LOG_MAX_ROWS,
    BotRepository,
    BotStateRepository,
    ContactRepository,
    DirectMessageRepository,
    EntityRepository,
    MessageRepository,
    PacketLogRepository,
    PacketLogRow,
    PathRepository,
    RoomMemberRepository,
    RoomRepository,
)
from sighop.db.writer import WriteBehind
from sighop.logging import Logger, get_logger
from sighop.net.bus import Submission, TxOutcome
from sighop.net.contacts import Contact, ContactStore
from sighop.net.dm import DirectMessageRecord
from sighop.net.paths import LearnedPath, PathKey, PathStore
from sighop.net.rx import RxRecord

CONTACT_QUEUE_CAPACITY = 256
"""Small on purpose. Adverts are rare — 92 in the 1003-record corpus — and this
queue refuses rather than drops, so its size only decides how many contacts the
marker set has to carry during an outage (design D16)."""

PATH_QUEUE_CAPACITY = 512
PACKET_LOG_QUEUE_CAPACITY = 2048

DIRECT_MESSAGE_QUEUE_CAPACITY = 512
"""Two offers per send and one per reception, on a link whose ceiling is a
handful of messages a minute. Larger than the contact queue because it refuses
in the same way and a refusal here costs a conversation entry — this is the
size of the outage the queue can ride out without losing one."""


@dataclass(frozen=True, slots=True)
class RestoredCounts:
    """What came back, reported at startup so "nothing restored" is visible."""

    entities: int = 0
    contacts: int = 0
    paths: int = 0
    conversations: int = 0
    """Distinct identity-and-peer pairs the database holds. Counted rather than
    loaded: a conversation is read a page at a time when somebody opens it, not
    mirrored in memory (design D5's asymmetry, applied to a second unbounded
    table)."""

    direct_messages: int = 0


@dataclass(slots=True)
class Persistence:
    """Everything durable, wired to the stores that stay authoritative in memory."""

    database: Database
    packet_log_max_rows: int = DEFAULT_PACKET_LOG_MAX_ROWS
    prune_interval: float = DEFAULT_PRUNE_INTERVAL_SECONDS
    writes_enabled: bool = True
    """False for a replay run (design D13): the pipeline runs and nothing is
    written, because a replayed reception carries an earlier session's timestamps
    and storing them would make a week-old contact look like a live one."""

    logger: Logger | None = None

    entities: EntityRepository = field(init=False)
    contacts: ContactRepository = field(init=False)
    paths: PathRepository = field(init=False)
    packet_log: PacketLogRepository = field(init=False)
    pruner: PacketLogPruner = field(init=False)

    # Rooms have no write-behind queue of their own, and that is design D5
    # rather than an omission: an ACL write is rare and must land before the
    # member is told it landed, and a post is not acknowledged until its row is
    # there. Neither is a thing to drop when a queue is full.
    rooms: RoomRepository = field(init=False)
    members: RoomMemberRepository = field(init=False)
    messages: MessageRepository = field(init=False)

    # Bots have no write-behind queue either, and for a sharper reason than
    # rooms (design D16): a dropped contact costs a re-learn, and a dropped
    # greeting record costs a *second* unsolicited direct message to a stranger
    # after the next restart. The greeter needs the record to have landed before
    # it transmits, which a queue cannot promise.
    bots: BotRepository = field(init=False)
    bot_state: BotStateRepository = field(init=False)

    # Direct messages do have a queue, and it is the contact lane rather than
    # the room lane. A room acknowledges a post because it promises to hold it,
    # so the promise must be true before the acknowledgement goes out; a direct
    # message acknowledgement is the protocol's own receipt, computed on decrypt,
    # against a sender whose retry window is 4-5 seconds. Delaying it on a
    # database write would trade a counted gap in our own history for a real
    # protocol failure (design D8).
    direct_messages: DirectMessageRepository = field(init=False)

    contact_writer: WriteBehind[Contact] = field(init=False)
    path_writer: WriteBehind[tuple[PathKey, LearnedPath]] = field(init=False)
    packet_log_writer: WriteBehind[PacketLogRow] = field(init=False)
    dm_writer: WriteBehind[DirectMessageRecord] = field(init=False)

    restored: RestoredCounts = field(default_factory=RestoredCounts)
    _contact_store: ContactStore | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="persistence")
        self.entities = EntityRepository(database=self.database)
        self.contacts = ContactRepository(database=self.database)
        self.paths = PathRepository(database=self.database)
        self.packet_log = PacketLogRepository(
            database=self.database, max_rows=self.packet_log_max_rows
        )
        self.pruner = PacketLogPruner(
            self.packet_log, interval=self.prune_interval, logger=self.logger
        )
        self.rooms = RoomRepository(database=self.database)
        self.members = RoomMemberRepository(database=self.database)
        self.messages = MessageRepository(database=self.database)
        self.bots = BotRepository(database=self.database)
        self.bot_state = BotStateRepository(database=self.database)
        self.direct_messages = DirectMessageRepository(database=self.database)
        self.contact_writer = WriteBehind(
            "contacts",
            self._flush_contacts,
            capacity=CONTACT_QUEUE_CAPACITY,
            batch_size=32,
            # Never drops: on overflow the unpersisted marker carries the contact
            # instead, so async does not become lossy (design D16).
            drop_oldest=False,
            logger=self.logger,
        )
        self.path_writer = WriteBehind(
            "routes",
            self._flush_paths,
            capacity=PATH_QUEUE_CAPACITY,
            batch_size=64,
            logger=self.logger,
        )
        self.packet_log_writer = WriteBehind(
            "packet_log",
            self._flush_packet_log,
            capacity=PACKET_LOG_QUEUE_CAPACITY,
            batch_size=128,
            logger=self.logger,
        )
        self.dm_writer = WriteBehind(
            "direct_messages",
            self._flush_direct_messages,
            capacity=DIRECT_MESSAGE_QUEUE_CAPACITY,
            batch_size=32,
            # Refuses rather than displacing, the contact lane's flag: a lost
            # conversation entry is not a re-learnable route, and displacing the
            # oldest would silently discard the beginning of a conversation to
            # keep its end (design D8).
            drop_oldest=False,
            logger=self.logger,
        )
        self.database.on_recovery(self.backfill_contacts)

    # --- Sinks the stores hold ---------------------------------------------

    def contact_sink(self) -> WriteBehind[Contact] | None:
        return self.contact_writer if self.writes_enabled else None

    def path_sink(self) -> WriteBehind[tuple[PathKey, LearnedPath]] | None:
        return self.path_writer if self.writes_enabled else None

    def dm_sink(self) -> WriteBehind[DirectMessageRecord] | None:
        """Where `DirectMessenger` offers what it sent and received.

        `None` on a replay run, like every other sink: a replayed reception
        carries an earlier session's timestamps, and recording it would put a
        conversation into the history that this run did not have (design D13).
        """
        return self.dm_writer if self.writes_enabled else None

    # --- Lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        """Connect and check the schema version, or fail startup saying why."""
        await self.database.open()

    def attach_contacts(self, store: ContactStore) -> None:
        """Remember the store the backfill flushes from."""
        self._contact_store = store

    async def restore(
        self,
        contacts: ContactStore,
        paths: PathStore,
        *,
        entities: int = 0,
    ) -> RestoredCounts:
        """Load contacts and paths before any traffic is processed.

        A read that fails here is *not* a startup failure: the schema check and
        the connection already succeeded, so this is a fault that appeared in the
        window between, and the run continues in memory with `degraded` set —
        the same posture every other read takes.
        """
        assert self.logger is not None
        self.attach_contacts(contacts)

        restored_contacts = 0
        loaded = await self.contacts.load_all()
        if isinstance(loaded, Succeeded):
            restored_contacts = contacts.restore(loaded.value)

        restored_paths = 0
        routes = await self.paths.load_all()
        if isinstance(routes, Succeeded):
            restored_paths = paths.restore(routes.value)

        # Counted rather than loaded. There is no in-memory conversation store
        # to restore into — a conversation is read a page at a time when
        # somebody opens it — but a restart that says nothing about what it is
        # holding is a restart after which "my messages are gone" and "the
        # interface has not been opened yet" look the same.
        conversations = 0
        held = await self.direct_messages.conversation_count()
        if isinstance(held, Succeeded):
            conversations = held.value
        messages = 0
        stored = await self.direct_messages.count()
        if isinstance(stored, Succeeded):
            messages = stored.value

        self.restored = RestoredCounts(
            entities=entities,
            contacts=restored_contacts,
            paths=restored_paths,
            conversations=conversations,
            direct_messages=messages,
        )
        self.logger.info(
            "persistence_restored",
            outcome="success",
            entities=entities,
            contacts=restored_contacts,
            paths=restored_paths,
            conversations=conversations,
            direct_messages=messages,
        )
        return self.restored

    def start(self) -> None:
        """Start the writer tasks, the pruner and the degraded-state probe."""
        if self.writes_enabled:
            self.contact_writer.start()
            self.path_writer.start()
            self.packet_log_writer.start()
            self.dm_writer.start()
            self.pruner.start()
        self.database.start_probe()

    async def stop(self) -> None:
        """Flush what is buffered, stop the tasks, and close the pool.

        Best effort by construction: a process killed outright never reaches
        here, which is exactly why contacts are written as they are observed
        rather than at shutdown (design D2).
        """
        await self.pruner.stop()
        for writer in (
            self.contact_writer,
            self.path_writer,
            self.packet_log_writer,
            self.dm_writer,
        ):
            await writer.stop()
        await self.database.dispose()

    # --- Writing -----------------------------------------------------------

    async def _flush_contacts(self, batch: Sequence[Contact]) -> bool:
        outcome = await self.contacts.upsert_many(list(batch))
        landed = isinstance(outcome, Succeeded)
        if self._contact_store is not None:
            for contact in batch:
                if landed:
                    self._contact_store.mark_persisted(contact)
                else:
                    # Still marked, so the recovery flush picks it up. The upsert
                    # is idempotent, so a redundant re-write costs nothing.
                    self._contact_store.mark_unpersisted(contact)
        if landed:
            self.database.stats.contacts_written += len(batch)
        return landed

    async def _flush_paths(self, batch: Sequence[tuple[PathKey, LearnedPath]]) -> bool:
        outcome = await self.paths.upsert_many(list(batch))
        if isinstance(outcome, Succeeded):
            self.database.stats.routes_written += len(batch)
            return True
        self.database.stats.routes_discarded += len(batch)
        return False

    async def _flush_packet_log(self, batch: Sequence[PacketLogRow]) -> bool:
        outcome = await self.packet_log.write_many(list(batch))
        if isinstance(outcome, Succeeded):
            self.database.stats.packet_log_written += len(batch)
            return True
        self.database.stats.packet_log_discarded += len(batch)
        return False

    async def _flush_direct_messages(self, batch: Sequence[DirectMessageRecord]) -> bool:
        outcome = await self.direct_messages.upsert_many(list(batch))
        if isinstance(outcome, Succeeded):
            self.database.stats.direct_messages_written += outcome.value
            return True
        self.database.stats.direct_messages_discarded += len(batch)
        return False

    # --- The packet log's producers ----------------------------------------

    def record_rx(self, record: RxRecord, *, airtime_ms: float | None = None) -> None:
        """Offer one reception to the feed. Never awaits, never raises."""
        if not self.writes_enabled:
            return
        self.packet_log_writer.offer(rx_row(record, airtime_ms=airtime_ms))

    def record_tx(self, submission: Submission, outcome: TxOutcome, *, at: dt.datetime) -> None:
        """Offer one resolved transmission to the feed — suppressed ones too."""
        if not self.writes_enabled:
            return
        self.packet_log_writer.offer(tx_row(submission, outcome, at=at))

    # --- Recovery (design D15) ---------------------------------------------

    async def backfill_contacts(self) -> None:
        """Write every contact whose latest state is not known to be stored.

        Fired by the database's own probe rather than by the next write, because
        adverts are hours apart and nothing may attempt a write for a long time
        after the database returns. One upsert per public key carrying the latest
        state, so a peer observed several times during the outage lands once.

        Deliberately not extended to paths or the packet log (design D15).
        Backfilling those would mean retaining rows the design has already
        decided are disposable, turning a bounded queue into unbounded state for
        data that either regenerates from the next reception or does not matter.
        """
        assert self.logger is not None
        if self._contact_store is None or not self.writes_enabled:
            return
        pending = self._contact_store.unpersisted()
        if not pending:
            return
        outcome = await self.contacts.upsert_many(list(pending))
        if isinstance(outcome, Succeeded):
            for contact in pending:
                self._contact_store.mark_persisted(contact)
            self.database.stats.contacts_written += len(pending)
            self.logger.info(
                "contacts_backfilled",
                outcome="success",
                contacts=len(pending),
                detail="written on recovery, without a restart or a further advert",
            )
            return
        self.logger.error(
            "contacts_backfill_failed",
            outcome="error",
            contacts=len(pending),
            error=str(outcome.error),
        )

    # --- Reporting ---------------------------------------------------------

    @property
    def state(self) -> str:
        return self.database.state

    @property
    def degraded(self) -> bool:
        """Whether the database is currently unreachable (`RoomStorage`).

        A room accepts nothing while this is true and says so, because a room is
        exactly as available as its history (design D6). Read from the database's
        own flag rather than mirrored, so it clears when the probe says it does
        and not when someone remembers to reset it.
        """
        return self.database.degraded

    def as_json(self) -> dict[str, object]:
        return {
            **self.database.as_json(),
            "restored_entities": self.restored.entities,
            "restored_contacts": self.restored.contacts,
            "restored_paths": self.restored.paths,
            "restored_conversations": self.restored.conversations,
            "restored_direct_messages": self.restored.direct_messages,
            "packet_log_pruned": self.pruner.deleted,
            **self.contact_writer.as_json(),
            **self.path_writer.as_json(),
            **self.packet_log_writer.as_json(),
            **self.dm_writer.as_json(),
        }

    async def wait_idle(self) -> None:
        """Drain every writer. For tests and for a deliberate flush."""
        await asyncio.gather(
            self.contact_writer.wait_idle(),
            self.path_writer.wait_idle(),
            self.packet_log_writer.wait_idle(),
            self.dm_writer.wait_idle(),
        )
