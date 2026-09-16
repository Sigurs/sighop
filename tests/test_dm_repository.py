"""The `direct_message` repository and its writer lane (design D7/D8).

Two properties carry the design and are the reason this file exists:

* **A message is one row, written twice.** The record is offered at submission
  and again when the send resolves, and those two offers regularly land in one
  batch. Postgres refuses an `ON CONFLICT DO UPDATE` whose own batch proposes a
  conflict key twice — milestone 5's `cannot affect row a second time` — so the
  batch is collapsed to the latest, and the surviving row must carry the *later*
  state rather than whichever the collapse happened to keep.
* **The lane refuses rather than displacing.** A dropped route is relearned from
  the next reception; a dropped conversation entry is not re-acquirable at all,
  so the oldest message of a conversation is never sacrificed to keep its newest.
"""

from __future__ import annotations

import base64
import datetime as dt

import pytest

from sighop.config import DatabaseConfig, generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import DIRECT_MESSAGE_QUEUE_CAPACITY, Persistence
from sighop.db.repositories import (
    DirectMessageRepository,
    EntityRepository,
    MessageRepository,
    PacketLogRepository,
    PacketLogRow,
    RoomRepository,
)
from sighop.monitor.render import render_persistence
from sighop.net.contacts import ContactStore
from sighop.net.dm import INBOUND, OUTBOUND, DirectMessageRecord, RecordedOutcome
from sighop.net.paths import PathStore
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType

ENTITY = bytes(range(32))
OTHER_ENTITY = bytes(range(100, 132))
PEER = bytes(range(32, 64))
OTHER_PEER = bytes(range(64, 96))

NOW = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)


def _unopened() -> Database:
    """A `Database` that is configured and never connected.

    The writer lane's policy is decided when the lane is built, so asserting it
    needs configuration and no server — and a test that needed a server to check
    a constructor flag would be a test that skips on the machine most likely to
    get the flag wrong.
    """
    return Database(config=DatabaseConfig(url="postgresql+asyncpg://nobody@nowhere/none"))


def record(
    *,
    ref: str = "m1",
    entity: bytes = ENTITY,
    peer: bytes = PEER,
    direction: str = OUTBOUND,
    text: bytes = b"hello",
    outcome: RecordedOutcome = RecordedOutcome.IN_FLIGHT,
    handled_at: dt.datetime = NOW,
    attempts: int = 0,
    packet_ids: tuple[str, ...] = (),
    ack_latency_ms: float | None = None,
    wire_timestamp: int = 1_757_000_000,
) -> DirectMessageRecord:
    return DirectMessageRecord(
        entity_public_key=entity,
        peer_public_key=peer,
        direction=direction,
        text=text,
        wire_timestamp=wire_timestamp,
        handled_at=handled_at,
        ref=ref,
        outcome=outcome,
        packet_ids=packet_ids,
        attempts=attempts,
        route_flood=False if direction == OUTBOUND else None,
        route_path=b"" if direction == OUTBOUND else None,
        ack_latency_ms=ack_latency_ms,
    )


# --- 3.1 The repository -----------------------------------------------------


@pytest.mark.database
async def test_one_ref_twice_in_one_batch_becomes_one_row_carrying_the_later_state(
    database: Database,
) -> None:
    """3.1: the submission and the resolution, collapsed as they arrive together."""
    messages = DirectMessageRepository(database=database)
    submitted = record(ref="m1")
    resolved = record(
        ref="m1",
        outcome=RecordedOutcome.ACKNOWLEDGED,
        attempts=2,
        packet_ids=("aa", "bb"),
        ack_latency_ms=812.5,
        handled_at=NOW + dt.timedelta(seconds=3),
    )

    written = await messages.upsert_many([submitted, resolved])
    assert isinstance(written, Succeeded)
    assert written.value == 1, "the batch must be collapsed, not sent twice"

    page = await messages.conversation(ENTITY, PEER)
    assert isinstance(page, Succeeded)
    assert len(page.value) == 1
    stored = page.value[0]
    assert stored.outcome is RecordedOutcome.ACKNOWLEDGED
    assert stored.attempts == 2
    assert stored.packet_ids == ("aa", "bb")
    assert stored.ack_latency_ms == pytest.approx(812.5)


@pytest.mark.database
async def test_a_resolution_in_a_later_batch_updates_the_same_row(
    database: Database,
) -> None:
    """3.1: and when the two offers do *not* share a batch, which is the case
    the unique constraint exists for."""
    messages = DirectMessageRepository(database=database)
    assert isinstance(await messages.upsert(record(ref="m2")), Succeeded)
    assert isinstance(
        await messages.upsert(
            record(ref="m2", outcome=RecordedOutcome.UNACKNOWLEDGED, attempts=4)
        ),
        Succeeded,
    )

    page = await messages.conversation(ENTITY, PEER)
    assert isinstance(page, Succeeded)
    assert [(row.ref, row.outcome, row.attempts) for row in page.value] == [
        ("m2", RecordedOutcome.UNACKNOWLEDGED, 4)
    ]


@pytest.mark.database
async def test_an_update_does_not_move_a_message_in_its_conversation(
    database: Database,
) -> None:
    """3.1: `handled_at` is excluded from the update, as `first_heard` is.

    A message that resolves seconds after a later one was received must not jump
    to the end of the conversation because its outcome became known.
    """
    messages = DirectMessageRepository(database=database)
    assert isinstance(await messages.upsert(record(ref="first", handled_at=NOW)), Succeeded)
    assert isinstance(
        await messages.upsert(
            record(
                ref="second",
                direction=INBOUND,
                outcome=RecordedOutcome.RECEIVED,
                handled_at=NOW + dt.timedelta(seconds=10),
            )
        ),
        Succeeded,
    )
    assert isinstance(
        await messages.upsert(
            record(
                ref="first",
                outcome=RecordedOutcome.ACKNOWLEDGED,
                handled_at=NOW + dt.timedelta(seconds=30),
            )
        ),
        Succeeded,
    )

    page = await messages.conversation(ENTITY, PEER)
    assert isinstance(page, Succeeded)
    assert [row.ref for row in page.value] == ["second", "first"]


@pytest.mark.database
async def test_a_conversation_reads_newest_first_and_pages_stably(
    database: Database,
) -> None:
    """3.1, `dm-history`: bounded pages in a stable total order.

    Every message here shares one `handled_at` on purpose. That is what makes
    the page boundary the interesting part: ordered by the timestamp alone, a
    row could appear on both pages or on neither.
    """
    messages = DirectMessageRepository(database=database)
    batch = [record(ref=f"m{index:02d}", handled_at=NOW) for index in range(10)]
    assert isinstance(await messages.upsert_many(batch), Succeeded)

    first = await messages.conversation(ENTITY, PEER, limit=4)
    assert isinstance(first, Succeeded)
    assert len(first.value) == 4

    oldest = first.value[-1]
    assert oldest.row_id is not None
    second = await messages.conversation(
        ENTITY, PEER, limit=4, before=(oldest.handled_at, oldest.row_id)
    )
    assert isinstance(second, Succeeded)
    assert len(second.value) == 4

    seen = [row.ref for row in first.value] + [row.ref for row in second.value]
    assert len(set(seen)) == 8, "a message appeared on two pages"


@pytest.mark.database
async def test_a_peer_with_a_wrong_clock_cannot_reorder_a_conversation(
    database: Database,
) -> None:
    """`dm-history`: ordering is by when we handled it, never by the wire clock."""
    messages = DirectMessageRepository(database=database)
    assert isinstance(
        await messages.upsert_many(
            [
                record(ref="early", handled_at=NOW, wire_timestamp=2_000_000_000),
                record(
                    ref="late",
                    direction=INBOUND,
                    outcome=RecordedOutcome.RECEIVED,
                    handled_at=NOW + dt.timedelta(minutes=1),
                    # A peer whose clock is years behind ours.
                    wire_timestamp=1_000_000,
                ),
            ]
        ),
        Succeeded,
    )

    page = await messages.conversation(ENTITY, PEER)
    assert isinstance(page, Succeeded)
    assert [row.ref for row in page.value] == ["late", "early"]
    assert [row.wire_timestamp for row in page.value] == [1_000_000, 2_000_000_000], (
        "both times must remain available; only the ordering is ours"
    )


@pytest.mark.database
async def test_two_identities_talking_to_one_contact_are_two_conversations(
    database: Database,
) -> None:
    """3.1, `web-chat`: the pair is the key, so neither identity sees the other's."""
    messages = DirectMessageRepository(database=database)
    assert isinstance(
        await messages.upsert_many(
            [
                record(ref="a", entity=ENTITY, peer=PEER),
                record(ref="b", entity=OTHER_ENTITY, peer=PEER),
                record(ref="c", entity=ENTITY, peer=OTHER_PEER),
            ]
        ),
        Succeeded,
    )

    listed = await messages.conversations()
    assert isinstance(listed, Succeeded)
    assert {(row.entity_public_key, row.peer_public_key) for row in listed.value} == {
        (ENTITY, PEER),
        (OTHER_ENTITY, PEER),
        (ENTITY, OTHER_PEER),
    }

    mine = await messages.conversations(ENTITY)
    assert isinstance(mine, Succeeded)
    assert {row.peer_public_key for row in mine.value} == {PEER, OTHER_PEER}

    counted = await messages.conversation_count()
    assert isinstance(counted, Succeeded)
    assert counted.value == 3


@pytest.mark.database
async def test_a_conversation_summary_carries_its_latest_message_and_its_count(
    database: Database,
) -> None:
    """3.1: what the conversation list shows, without reading every message."""
    messages = DirectMessageRepository(database=database)
    assert isinstance(
        await messages.upsert_many(
            [
                record(ref="a", handled_at=NOW, text=b"first"),
                record(
                    ref="b",
                    direction=INBOUND,
                    outcome=RecordedOutcome.RECEIVED,
                    handled_at=NOW + dt.timedelta(minutes=2),
                    text=b"newest",
                ),
            ]
        ),
        Succeeded,
    )

    listed = await messages.conversations(ENTITY)
    assert isinstance(listed, Succeeded)
    assert len(listed.value) == 1
    summary = listed.value[0]
    assert summary.messages == 2
    assert summary.latest_text == b"newest"
    assert summary.latest_direction == INBOUND
    assert summary.latest_outcome is RecordedOutcome.RECEIVED


# --- 3.2 The writer lane ----------------------------------------------------


def test_the_lane_refuses_rather_than_displacing_the_oldest_message() -> None:
    """3.2, design D8: the contact lane's flag, for a sharper reason.

    Needs no database: what is being asserted is the buffer's policy, which is
    decided when it is built and not when a write is attempted.
    """
    persistence = Persistence(database=_unopened())
    writer = persistence.dm_writer
    assert writer.drop_oldest is False

    accepted = [writer.offer(record(ref=f"m{index}")) for index in range(writer.capacity)]
    assert all(accepted)
    assert writer.pending == DIRECT_MESSAGE_QUEUE_CAPACITY

    assert writer.offer(record(ref="one-too-many")) is False
    assert writer.overflowed == 0, "a refusing lane must displace nothing"
    assert writer.refused == 1
    assert writer.pending == DIRECT_MESSAGE_QUEUE_CAPACITY

    status = writer.as_json()
    assert status["direct_messages_refused"] == 1
    assert status["direct_messages_discarded"] == 0


def test_the_persistence_status_carries_the_lanes_counts() -> None:
    """3.2: the discarded count is in the status, beside the other lanes'."""
    persistence = Persistence(database=_unopened())
    status = persistence.as_json()
    for key in (
        "direct_messages_written",
        "direct_messages_discarded",
        "direct_messages_refused",
        "direct_messages_pending",
    ):
        assert key in status, f"{key} is not reported"


def test_a_replay_run_has_no_direct_message_sink() -> None:
    """3.2, design D13: a replayed conversation is not this run's conversation."""
    persistence = Persistence(database=_unopened(), writes_enabled=False)
    assert persistence.dm_sink() is None

    writing = Persistence(database=_unopened())
    assert writing.dm_sink() is writing.dm_writer


# --- 3.3 What a restart reports ---------------------------------------------


@pytest.mark.database
async def test_a_restart_reports_what_the_database_holds_before_the_first_frame(
    database: Database,
) -> None:
    """3.3: "my messages are gone" and "nobody has opened the panel" must not
    look the same after a restart.

    Conversations are counted rather than loaded — there is no in-memory store
    of them — so the count is the only thing that can say so, and it is taken
    during `restore`, which runs before any traffic is processed.
    """
    messages = DirectMessageRepository(database=database)
    assert isinstance(
        await messages.upsert_many(
            [
                record(ref="a", peer=PEER),
                record(ref="b", peer=PEER),
                record(ref="c", peer=OTHER_PEER),
                record(ref="d", entity=OTHER_ENTITY, peer=PEER),
            ]
        ),
        Succeeded,
    )

    persistence = Persistence(database=database)
    restored = await persistence.restore(ContactStore(), PathStore())
    assert restored.conversations == 3
    assert restored.direct_messages == 4

    status = persistence.as_json()
    assert status["restored_conversations"] == 3
    assert status["restored_direct_messages"] == 4

    line = render_persistence(
        database="postgresql+asyncpg://role:***@db.example:5432/sighop",
        schema_version="0004",
        conversations=restored.conversations,
        direct_messages=restored.direct_messages,
    )
    assert "conversations=3" in line
    assert "messages=4" in line


@pytest.mark.database
async def test_a_stop_writes_what_more_than_one_lane_had_buffered(
    database: Database, database_config: DatabaseConfig
) -> None:
    """`database`/`dm-history`: the stop drains every lane, past one batch each.

    Forty rows per lane against a batch of 32, so the stop has to keep writing
    batches rather than write one and abandon the rest.
    """
    persistence = Persistence(database=database)
    persistence.start()
    for index in range(40):
        assert persistence.dm_writer.offer(record(ref=f"stop-{index:03d}")) is True
        assert (
            persistence.packet_log_writer.offer(
                PacketLogRow(
                    packet_id=f"stop{index:03d}",
                    direction="rx",
                    at=NOW + dt.timedelta(seconds=index),
                    outcome="dispatched",
                )
            )
            is True
        )

    await persistence.stop()  # disposes the fixture's handle; read back on a new one

    assert persistence.dm_writer.written == 40
    assert persistence.dm_writer.discarded == 0
    assert persistence.packet_log_writer.written == 40
    assert persistence.packet_log_writer.discarded == 0

    reading = Database(config=database_config)
    await reading.open()
    try:
        conversations = await DirectMessageRepository(database=reading).conversations(ENTITY)
        assert isinstance(conversations, Succeeded)
        assert sum(summary.messages for summary in conversations.value) == 40
        counted = await PacketLogRepository(database=reading).count()
        assert isinstance(counted, Succeeded)
        assert counted.value == 40
    finally:
        await reading.dispose()


# --- 3.4 What the pruners do not touch --------------------------------------


@pytest.mark.database
async def test_pruning_the_packet_log_removes_no_recorded_message(
    database: Database,
) -> None:
    """3.4, `dm-history`: the feed is a sample and a conversation is content.

    The packet log is pruned to a cap by design; a direct message record joined
    to a pruned packet by its `packet_ids` must survive the packet going.
    """
    messages = DirectMessageRepository(database=database)
    assert isinstance(
        await messages.upsert_many(
            [record(ref=f"m{index}", packet_ids=(f"p{index}",)) for index in range(4)]
        ),
        Succeeded,
    )

    log = PacketLogRepository(database=database, max_rows=1)
    assert isinstance(
        await log.write_many(
            [
                PacketLogRow(
                    packet_id=f"p{index}",
                    direction="rx",
                    at=NOW + dt.timedelta(seconds=index),
                    outcome="dispatched",
                )
                for index in range(4)
            ]
        ),
        Succeeded,
    )

    pruned = await log.prune()
    assert isinstance(pruned, Succeeded)
    assert pruned.value == 3, "the packet log itself must actually have been pruned"

    held = await messages.count()
    assert isinstance(held, Succeeded)
    assert held.value == 4


@pytest.mark.database
async def test_room_retention_removes_no_recorded_message(database: Database) -> None:
    """3.4: room retention is a room's policy over a room's history.

    A direct message is in neither, and the two tables must not become one
    because both hold something somebody said.
    """
    messages = DirectMessageRepository(database=database)
    assert isinstance(await messages.upsert(record(ref="kept")), Succeeded)

    entity = await EntityRepository(database=database).store(
        name="room-host",
        identity=generate_identity(),
        secret=base64.b64decode(generate_secret_key()),
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(entity, Succeeded)
    room = await RoomRepository(database=database).create(
        entity_id=entity.value.id, name="a room", admin_password_hash="x"
    )
    assert isinstance(room, Succeeded)

    history = MessageRepository(database=database)
    assert isinstance(
        await history.store(
            room_id=room.value.id,
            author_public_key=PEER,
            text=b"a post",
            posted_at=NOW - dt.timedelta(days=30),
        ),
        Succeeded,
    )

    pruned = await history.prune(
        room.value.id, retention_days=1, retention_messages=None, now=NOW
    )
    assert isinstance(pruned, Succeeded)
    assert pruned.value[0] == 1, "the room's own history must actually have been pruned"

    held = await messages.count()
    assert isinstance(held, Succeeded)
    assert held.value == 1
