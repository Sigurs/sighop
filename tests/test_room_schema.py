"""The three tables milestone 6 owns, and the four constraints that carry design.

Each of these is a shape §6's sketch did not have and design D2/D3 argued for, so
each is asserted rather than assumed:

* one room per identity (`uq_room_entity_id`),
* two members of one room may share a node hash but not a public key,
* one ordering value per room and never two (`uq_message_room_id_post_timestamp`),
* and retention defaults to unlimited, which is a NULL an operator has to replace.

The metadata assertions run with no database. The constraint assertions need a
real server, because a constraint SQLAlchemy declares and Postgres does not
enforce is a constraint that does not exist.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.models import Message, Room, RoomMember
from sighop.db.repositories import EntityRepository
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType

SECRET = base64.b64decode(generate_secret_key())


# --- 1.1 / 1.2 / 1.3 What the models declare, with no database --------------


def test_retention_is_unlimited_by_default_and_says_so_as_null() -> None:
    """1.1, design D15: nothing is deleted until an operator sets a policy."""
    columns = inspect(Room).columns
    for name in ("retention_days", "retention_messages"):
        column = columns[name]
        assert column.nullable, f"room.{name} must be nullable to mean unlimited"
        assert column.default is None, f"room.{name} must not default to a bound"
        assert column.server_default is None


def test_one_room_per_identity_is_a_constraint_not_a_convention() -> None:
    """1.1: a node is a room (design D2's Non-Goal), expressed as uniqueness."""
    assert inspect(Room).columns["entity_id"].unique is True


def test_a_member_is_keyed_by_its_public_key_and_its_node_hash_is_only_indexed() -> None:
    """1.2, §3: one byte collides at 1 in 256; the key is the whole key."""
    primary = {column.name for column in inspect(RoomMember).primary_key}
    assert primary == {"room_id", "public_key"}
    assert inspect(RoomMember).columns["node_hash"].unique is not True


def test_wire_seconds_are_bigint_and_instants_are_timestamptz() -> None:
    """1.2 / 1.3, design D2: the two representations never share a column."""
    from sqlalchemy import BigInteger, DateTime

    cases: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
        ("room_member", ("sync_since", "last_timestamp"), ("first_login", "last_activity")),
        ("message", ("post_timestamp", "sender_timestamp"), ("posted_at",)),
        ("room", (), ("created_at",)),
    )
    tables = {
        Room.__tablename__: inspect(Room).columns,
        RoomMember.__tablename__: inspect(RoomMember).columns,
        Message.__tablename__: inspect(Message).columns,
    }
    for table, wire, instants in cases:
        columns = tables[table]
        for name in wire:
            assert isinstance(columns[name].type, BigInteger), (
                f"{table}.{name} is epoch seconds off the wire, not an instant"
            )
        for name in instants:
            kind = columns[name].type
            assert isinstance(kind, DateTime) and kind.timezone, (
                f"{table}.{name} must carry a time zone (design D7)"
            )


# --- The same constraints, against a real server ----------------------------


async def _entity(database: Database, name: str = "room-server") -> uuid.UUID:
    outcome = await EntityRepository(database=database).store(
        name=name,
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(outcome, Succeeded)
    return outcome.value.id


def _room(entity_id: uuid.UUID, *, name: str = "the room") -> Room:
    return Room(
        id=uuid.uuid4(),
        entity_id=entity_id,
        name=name,
        admin_password_hash="$argon2id$not-a-real-hash",
        guest_open=False,
        allow_read_only=False,
        created_at=dt.datetime.now(dt.UTC),
    )


def _member(room_id: uuid.UUID, public_key: bytes, node_hash: int) -> RoomMember:
    now = dt.datetime.now(dt.UTC)
    return RoomMember(
        room_id=room_id,
        public_key=public_key,
        node_hash=node_hash,
        permissions=1,
        sync_since=0,
        last_timestamp=0,
        first_login=now,
        last_activity=now,
    )


def _message(room_id: uuid.UUID, post_timestamp: int) -> Message:
    return Message(
        room_id=room_id,
        author_public_key=b"\x01" * 32,
        post_timestamp=post_timestamp,
        sender_timestamp=None,
        text=b"hello",
        posted_at=dt.datetime.now(dt.UTC),
    )


async def test_an_identity_may_carry_only_one_room(database: Database) -> None:
    """1.1: a second room on one identity is refused by the schema itself."""
    entity_id = await _entity(database)
    async with database.sessions() as session:
        session.add(_room(entity_id, name="first"))
        await session.commit()

    with pytest.raises(IntegrityError):
        async with database.sessions() as session:
            session.add(_room(entity_id, name="second"))
            await session.commit()


async def test_a_new_room_keeps_everything_until_a_policy_is_set(database: Database) -> None:
    """1.1: both bounds come back NULL, which is what "unlimited" is stored as."""
    entity_id = await _entity(database)
    room = _room(entity_id)
    async with database.sessions() as session:
        session.add(room)
        await session.commit()

    async with database.sessions() as session:
        stored = (await session.execute(select(Room).where(Room.id == room.id))).scalar_one()
        assert stored.retention_days is None
        assert stored.retention_messages is None
        assert stored.guest_password_hash is None
        assert stored.guest_open is False
        assert stored.allow_read_only is False


async def test_two_members_of_one_room_may_share_a_node_hash(database: Database) -> None:
    """1.2, §3: at 1 in 256 they eventually will, and that is not an error."""
    entity_id = await _entity(database)
    room = _room(entity_id)
    async with database.sessions() as session:
        session.add(room)
        session.add(_member(room.id, b"\xaa" * 32, node_hash=7))
        session.add(_member(room.id, b"\xbb" * 32, node_hash=7))
        await session.commit()

    async with database.sessions() as session:
        rows = (
            (await session.execute(select(RoomMember).where(RoomMember.room_id == room.id)))
            .scalars()
            .all()
        )
        assert {row.node_hash for row in rows} == {7}
        assert len(rows) == 2


async def test_one_public_key_cannot_join_one_room_twice(database: Database) -> None:
    """1.2: the public key is the identity, and membership is keyed on it."""
    entity_id = await _entity(database)
    room = _room(entity_id)
    async with database.sessions() as session:
        session.add(room)
        session.add(_member(room.id, b"\xaa" * 32, node_hash=7))
        await session.commit()

    with pytest.raises(IntegrityError):
        async with database.sessions() as session:
            session.add(_member(room.id, b"\xaa" * 32, node_hash=9))
            await session.commit()


async def test_an_ordering_value_is_unique_within_its_room_and_not_across_rooms(
    database: Database,
) -> None:
    """1.3, design D3: what makes `max(now, last + 1)` safe rather than hopeful."""
    first_room = _room(await _entity(database, "first"), name="first")
    second_room = _room(await _entity(database, "second"), name="second")
    async with database.sessions() as session:
        session.add(first_room)
        session.add(second_room)
        await session.commit()

    async with database.sessions() as session:
        session.add(_message(first_room.id, 1_700_000_000))
        # The same ordering value in another room is an ordinary row: the order
        # is per room, because the cursor is per room.
        session.add(_message(second_room.id, 1_700_000_000))
        await session.commit()

    with pytest.raises(IntegrityError):
        async with database.sessions() as session:
            session.add(_message(first_room.id, 1_700_000_000))
            await session.commit()


async def test_removing_a_room_removes_its_members_and_its_history(database: Database) -> None:
    """1.4: `ON DELETE CASCADE`, so no orphan outlives the room it belonged to."""
    entity_id = await _entity(database)
    room = _room(entity_id)
    async with database.sessions() as session:
        session.add(room)
        await session.commit()

    async with database.sessions() as session:
        session.add(_member(room.id, b"\xaa" * 32, node_hash=7))
        session.add(_message(room.id, 1_700_000_000))
        await session.commit()

    async with database.sessions() as session:
        stored = (await session.execute(select(Room).where(Room.id == room.id))).scalar_one()
        await session.delete(stored)
        await session.commit()

    async with database.sessions() as session:
        assert (
            await session.execute(select(RoomMember).where(RoomMember.room_id == room.id))
        ).scalars().all() == []
        assert (
            await session.execute(select(Message).where(Message.room_id == room.id))
        ).scalars().all() == []
