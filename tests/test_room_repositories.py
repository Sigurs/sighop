"""`RoomRepository`'s name rules and its deletion (change web-delete-and-rename).

Two things are asserted here that the schema cannot say for itself:

* **a room's name is validated wherever it is set.** `room.name` is a plain
  `Text` column with no constraint, so "a room cannot be called nothing" is a
  rule the repository holds or nobody does — and it has to hold it in the same
  words for the command line and for the panel, which is why it lives here and
  not in either surface.
* **deleting a room takes its members and messages and leaves its identity.**
  Both halves are the foreign keys' doing rather than the repository's, and a
  cascade is silent: nothing in the call says how much it removed, which is
  exactly why the confirmation has to count first.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid

import pytest

from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    MAX_ROOM_NAME_LENGTH,
    EntityRepository,
    MemberRecord,
    RoomNameError,
    RoomNameTakenError,
    RoomRepository,
    parse_room_name,
)
from sighop.passwords import hash_password
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType, Permission

NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
SECRET = base64.b64decode(generate_secret_key())


async def _room_server(database: Database, name: str) -> uuid.UUID:
    outcome = await EntityRepository(database=database).store(
        name=name,
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(outcome, Succeeded)
    return outcome.value.id


# --- A room's name is validated wherever it is set ---------------------------


def test_an_empty_room_name_is_refused() -> None:
    for empty in ("", "   ", "\t\n"):
        with pytest.raises(RoomNameError) as excinfo:
            parse_room_name(empty)
        assert "cannot be empty" in str(excinfo.value)


def test_a_room_name_is_stripped_of_surrounding_whitespace() -> None:
    assert parse_room_name("  Lobby  ") == "Lobby"


def test_an_over_long_room_name_is_refused() -> None:
    assert parse_room_name("x" * MAX_ROOM_NAME_LENGTH)
    with pytest.raises(RoomNameError) as excinfo:
        parse_room_name("x" * (MAX_ROOM_NAME_LENGTH + 1))
    assert str(MAX_ROOM_NAME_LENGTH) in str(excinfo.value)


def test_a_room_name_with_a_control_character_is_refused() -> None:
    with pytest.raises(RoomNameError) as excinfo:
        parse_room_name("Lo\x00bby")
    assert "U+0000" in str(excinfo.value)


def test_a_room_name_may_hold_the_channel_separator() -> None:
    """A room's name is never advertised and never posted, so the rule
    `parse_entity_name` carries for the mesh does not apply to it."""
    assert parse_room_name("Lobby: the second") == "Lobby: the second"


async def test_creating_a_room_applies_the_name_rules(database: Database) -> None:
    entity_id = await _room_server(database, "roomy")
    rooms = RoomRepository(database=database)
    with pytest.raises(RoomNameError):
        await rooms.create(entity_id=entity_id, name="  ", admin_password_hash=hash_password("pw"))
    listed = await rooms.list_all()
    assert isinstance(listed, Succeeded)
    assert listed.value == [], "a refused name stored a room"


async def test_a_created_room_is_stored_under_the_stripped_name(database: Database) -> None:
    entity_id = await _room_server(database, "roomy")
    created = await RoomRepository(database=database).create(
        entity_id=entity_id, name="  Lobby  ", admin_password_hash=hash_password("pw")
    )
    assert isinstance(created, Succeeded)
    assert created.value.name == "Lobby"


# --- Renaming a room ---------------------------------------------------------


async def test_a_room_rename_changes_the_name_and_nothing_else(database: Database) -> None:
    entity_id = await _room_server(database, "roomy")
    rooms = RoomRepository(database=database)
    created = await rooms.create(
        entity_id=entity_id,
        name="the-room",
        admin_password_hash=hash_password("pw"),
        guest_open=True,
        allow_read_only=True,
    )
    assert isinstance(created, Succeeded)

    renamed = await rooms.rename(created.value.id, "the-lobby")

    assert isinstance(renamed, Succeeded)
    assert renamed.value == "the-room", "the previous name was not reported"
    after = await rooms.get_for_entity(entity_id)
    assert isinstance(after, Succeeded)
    assert after.value is not None
    assert after.value.name == "the-lobby"
    assert after.value.id == created.value.id
    assert after.value.entity_id == entity_id
    assert after.value.admin_password_hash == created.value.admin_password_hash
    assert after.value.guest_open is True
    assert after.value.allow_read_only is True
    assert after.value.created_at == created.value.created_at


async def test_a_room_rename_applies_the_name_rules(database: Database) -> None:
    entity_id = await _room_server(database, "roomy")
    rooms = RoomRepository(database=database)
    created = await rooms.create(
        entity_id=entity_id, name="the-room", admin_password_hash=hash_password("pw")
    )
    assert isinstance(created, Succeeded)

    for refused in ("  ", "x" * (MAX_ROOM_NAME_LENGTH + 1), "a\x01b"):
        with pytest.raises(RoomNameError):
            await rooms.rename(created.value.id, refused)

    listed = await rooms.list_all()
    assert [record.name for record in listed.value] == ["the-room"]


async def test_a_room_rename_onto_another_rooms_name_is_refused(database: Database) -> None:
    rooms = RoomRepository(database=database)
    first = await rooms.create(
        entity_id=await _room_server(database, "one"),
        name="the-room",
        admin_password_hash=hash_password("pw"),
    )
    await rooms.create(
        entity_id=await _room_server(database, "two"),
        name="the-lobby",
        admin_password_hash=hash_password("pw"),
    )
    assert isinstance(first, Succeeded)

    with pytest.raises(RoomNameTakenError) as excinfo:
        await rooms.rename(first.value.id, "the-lobby")

    assert "the-lobby" in str(excinfo.value)
    assert "nothing was renamed" in str(excinfo.value)
    listed = await rooms.list_all()
    assert sorted(record.name for record in listed.value) == ["the-lobby", "the-room"]


async def test_renaming_a_room_to_the_name_it_holds_is_accepted(database: Database) -> None:
    rooms = RoomRepository(database=database)
    created = await rooms.create(
        entity_id=await _room_server(database, "roomy"),
        name="the-room",
        admin_password_hash=hash_password("pw"),
    )
    assert isinstance(created, Succeeded)

    renamed = await rooms.rename(created.value.id, "  the-room  ")

    assert isinstance(renamed, Succeeded)
    listed = await rooms.list_all()
    assert [record.name for record in listed.value] == ["the-room"]


async def test_renaming_a_room_that_does_not_exist_reports_so(database: Database) -> None:
    renamed = await RoomRepository(database=database).rename(uuid.uuid4(), "nobody")
    assert isinstance(renamed, Succeeded)
    assert renamed.value is None


# --- Deleting a room ---------------------------------------------------------


async def test_deleting_a_room_takes_its_members_and_messages(database: Database) -> None:
    """The cascade is silent, which is why every caller counts before asking."""
    persistence = Persistence(database=database)
    entity_id = await _room_server(database, "roomy")
    created = await persistence.rooms.create(
        entity_id=entity_id, name="the-room", admin_password_hash=hash_password("pw")
    )
    assert isinstance(created, Succeeded)
    room_id = created.value.id
    member = generate_identity()
    await persistence.members.upsert(
        MemberRecord(
            room_id=room_id,
            public_key=member.public_key,
            node_hash=member.node_hash,
            permissions=int(Permission.READ_WRITE),
            sync_since=0,
            last_timestamp=0,
            first_login=NOW,
            last_activity=NOW,
        )
    )
    await persistence.messages.store(
        room_id=room_id, author_public_key=member.public_key, text=b"hello"
    )
    assert (await persistence.messages.count(room_id)).value == 1

    deleted = await persistence.rooms.delete(room_id)

    assert isinstance(deleted, Succeeded)
    assert deleted.value is True
    assert (await persistence.rooms.list_all()).value == []
    assert (await persistence.members.load_for_room(room_id)).value == []
    assert (await persistence.messages.count(room_id)).value == 0


async def test_deleting_a_room_leaves_its_identity_stored_and_unbound(
    database: Database,
) -> None:
    """room.entity_id cascades *from* the entity, never towards it."""
    persistence = Persistence(database=database)
    entity_id = await _room_server(database, "roomy")
    created = await persistence.rooms.create(
        entity_id=entity_id, name="the-room", admin_password_hash=hash_password("pw")
    )
    assert isinstance(created, Succeeded)

    await persistence.rooms.delete(created.value.id)

    listed = await persistence.entities.list_all()
    assert [record.name for record in listed.value] == ["roomy"]
    assert listed.value[0].node_type is NodeType.ROOM_SERVER
    bound = await persistence.rooms.get_for_entity(entity_id)
    assert isinstance(bound, Succeeded)
    assert bound.value is None, "the identity is still carrying a room"
    assert (await persistence.entities.bound_to(entity_id)).value == []


async def test_an_identity_can_carry_a_new_room_after_the_old_one_is_deleted(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    entity_id = await _room_server(database, "roomy")
    created = await persistence.rooms.create(
        entity_id=entity_id, name="the-room", admin_password_hash=hash_password("pw")
    )
    assert isinstance(created, Succeeded)
    await persistence.rooms.delete(created.value.id)

    again = await persistence.rooms.create(
        entity_id=entity_id, name="the-second-room", admin_password_hash=hash_password("pw")
    )

    assert isinstance(again, Succeeded)
    assert again.value.name == "the-second-room"


async def test_deleting_a_room_that_does_not_exist_reports_so(database: Database) -> None:
    deleted = await RoomRepository(database=database).delete(uuid.uuid4())
    assert isinstance(deleted, Succeeded)
    assert deleted.value is False


# --- A room's delivery settings (change push-ack-window-from-transmit) -------


async def test_a_created_room_has_no_delivery_settings_and_says_so(database: Database) -> None:
    entity_id = await _room_server(database, "roomy")
    created = await RoomRepository(database=database).create(
        entity_id=entity_id, name="Lobby", admin_password_hash=hash_password("pw")
    )
    assert isinstance(created, Succeeded)
    room = created.value
    assert room.push_ack_window_seconds is None
    assert room.push_recent_days is None
    assert room.push_ack_window == "firmware"
    assert room.push_recency == "all members"
    assert room.as_json()["push_ack_window_seconds"] is None
    assert room.as_json()["push_recent_days"] is None


async def test_delivery_settings_are_set_read_back_and_cleared(database: Database) -> None:
    entity_id = await _room_server(database, "roomy")
    rooms = RoomRepository(database=database)
    created = await rooms.create(
        entity_id=entity_id, name="Lobby", admin_password_hash=hash_password("pw")
    )
    assert isinstance(created, Succeeded)

    stored = await rooms.set_delivery(
        created.value.id, push_ack_window_seconds=30, push_recent_days=7
    )
    assert isinstance(stored, Succeeded)
    assert stored.value is True
    after = await rooms.get_for_entity(entity_id)
    assert isinstance(after, Succeeded)
    assert after.value is not None
    assert after.value.push_ack_window_seconds == 30
    assert after.value.push_recent_days == 7
    assert after.value.push_ack_window == "30 s"
    assert after.value.push_recency == "heard ≤ 7 d"
    assert after.value.as_json()["push_ack_window_seconds"] == 30
    assert after.value.as_json()["push_recent_days"] == 7
    # Nothing else moved.
    assert after.value.retention == created.value.retention
    assert after.value.name == created.value.name

    cleared = await rooms.set_delivery(
        created.value.id, push_ack_window_seconds=None, push_recent_days=None
    )
    assert isinstance(cleared, Succeeded)
    after = await rooms.get_for_entity(entity_id)
    assert isinstance(after, Succeeded)
    assert after.value is not None
    assert after.value.push_ack_window_seconds is None
    assert after.value.push_recent_days is None


async def test_setting_delivery_on_a_room_that_does_not_exist_reports_so(
    database: Database,
) -> None:
    outcome = await RoomRepository(database=database).set_delivery(
        uuid.uuid4(), push_ack_window_seconds=30, push_recent_days=None
    )
    assert isinstance(outcome, Succeeded)
    assert outcome.value is False
