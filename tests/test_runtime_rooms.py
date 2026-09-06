"""Rooms in the runtime (milestone 6, tasks 11.1 - 11.2).

These are the only room tests that need Postgres, and they need it for the right
reason: what is under test is that a room *bound to a stored identity* comes back
after a restart, and both halves of that sentence are database facts. Everything
about login, posting, syncing and retention runs against the in-memory fixtures
in `tests/roomfixtures.py`.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import uuid

import pytest

from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import EntityRepository, MemberRecord
from sighop.db.times import ensure_utc
from sighop.monitor.render import ROOMS_OFF
from sighop.passwords import hash_password
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType, Permission
from sighop.runtime import RuntimeConfig
from tests.protocol.corpus import CAPTURE_FILES, CAPTURES_DIR
from tests.test_runtime import _events, runtime

SECRET = base64.b64decode(generate_secret_key())
CAPTURE = CAPTURES_DIR / CAPTURE_FILES[0]

ADMIN_PASSWORD = "an-admin-password"


async def _stored_room_server(database: Database, *, name: str = "rs-1", enabled: bool = True):
    """A stored room-server identity with a room bound to it."""
    entities = EntityRepository(database=database)
    identity = generate_identity()
    stored = await entities.store(
        name=name,
        identity=identity,
        secret=SECRET,
        node_type=NodeType.ROOM_SERVER,
        enabled=enabled,
    )
    assert isinstance(stored, Succeeded)

    persistence = Persistence(database=database)
    room = await persistence.rooms.create(
        entity_id=stored.value.id,
        name="lounge",
        admin_password_hash=hash_password(ADMIN_PASSWORD),
    )
    assert isinstance(room, Succeeded)
    loaded = await entities.load_all(SECRET, enabled_only=enabled)
    assert isinstance(loaded, Succeeded)
    return persistence, room.value, loaded.value


# --- 11.2 What a run says when it serves no room ----------------------------


async def test_a_run_with_no_database_says_rooms_require_durable_storage() -> None:
    """11.2, design D5: no rooms, said rather than omitted. No database needed."""
    out = io.StringIO()
    run = runtime(_events(CAPTURE), out=out)

    assert run.rooms == []
    assert run._room_lines() == [ROOMS_OFF]


@pytest.mark.database
async def test_a_run_with_a_database_and_no_rooms_says_none_are_configured(
    database: Database,
) -> None:
    run = runtime(_events(CAPTURE), out=io.StringIO(), persistence=Persistence(database=database))
    await run._restore()

    assert run.rooms == []
    assert run._room_lines() == ["rooms: none configured"]


@pytest.mark.database
async def test_a_room_on_a_disabled_identity_is_reported_as_not_served(
    database: Database,
) -> None:
    """11.2: not served, with the reason, rather than silently absent."""
    persistence, _room, _loaded = await _stored_room_server(database, enabled=False)
    # The runtime is given no stored entities, exactly as a disabled identity
    # reaches it: `load_all(enabled_only=True)` did not return one.
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600),
        persistence=persistence,
    )
    await run._restore()

    assert run.rooms == []
    (line,) = run._room_lines()
    assert "NOT SERVED" in line
    assert "identity was not loaded" in line


# --- 11.1 A room that is served ---------------------------------------------


@pytest.mark.database
async def test_a_room_is_served_and_reported_before_any_traffic(
    database: Database,
) -> None:
    persistence, room, loaded = await _stored_room_server(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()

    assert [server.room.name for server in run.rooms] == ["lounge"]
    (line,) = run._room_lines()
    assert "'lounge'" in line
    assert "members=0" in line
    assert "messages=0" in line
    assert "retention=unlimited" in line
    assert "guest=refused", "a new room refuses guests until one is configured"
    # No hash reaches the output, ever (task 11.3).
    assert room.admin_password_hash not in line
    assert ADMIN_PASSWORD not in line

    await run.rooms[0].stop()
    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


@pytest.mark.database
async def test_the_room_server_is_a_bus_subscriber_and_owns_its_entity(
    database: Database,
) -> None:
    """11.1 with design D10: exactly one component decrypts this entity's packets."""
    persistence, _room, loaded = await _stored_room_server(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()

    names = [stats.name for stats in run.bus.subscriber_stats]
    assert "room:lounge" in names
    assert "direct-messages" in names
    assert "acks" in names, "one subscriber matches acknowledgements (design D11)"

    entity_id = run.rooms[0].entity.entity_id
    assert entity_id in run.messenger._room_entity_ids
    assert all(stub.entity_id != entity_id for stub in run.path_bodies.entities), (
        "the path-body reader still holds the room server's entity"
    )

    await run.rooms[0].stop()
    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


@pytest.mark.database
async def test_the_push_loop_and_the_pruner_start_and_shut_down_cleanly(
    database: Database,
) -> None:
    """11.1: started with the run, stopped in the existing shutdown order."""
    persistence, _room, loaded = await _stored_room_server(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )

    # The whole run, start to finish, over a replayed capture.
    await asyncio.wait_for(run.run(), timeout=30)

    assert len(run.rooms) == 1
    assert run.rooms[0]._push_task is None, "the push loop outlived the run"
    assert run.retention is not None
    assert run.retention._task is None, "the pruner outlived the run"
    # Nothing was abandoned: every delivery this run started is resolved.
    assert run.rooms[0].deliveries_outstanding == 0


@pytest.mark.database
async def test_a_member_who_logged_in_before_a_restart_comes_back_with_the_room(
    database: Database,
) -> None:
    """11.1 / 6.8: the ACL is restored with the room, not re-authenticated."""
    persistence, room, loaded = await _stored_room_server(database)
    now = ensure_utc(dt.datetime.now(dt.UTC), field="room_member.first_login")
    member_key = bytes(range(32))
    assert isinstance(
        await persistence.members.upsert(
            MemberRecord(
                room_id=room.id,
                public_key=member_key,
                node_hash=member_key[0],
                permissions=int(Permission.ADMIN),
                sync_since=1_700_000_000,
                last_timestamp=1_700_000_500,
                first_login=now,
                last_activity=now,
            )
        ),
        Succeeded,
    )

    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()

    server = run.rooms[0]
    assert member_key in server.members
    restored = server.members[member_key]
    assert restored.permission is Permission.ADMIN
    assert restored.sync_since == 1_700_000_000
    assert restored.last_timestamp == 1_700_000_500, (
        "the replay guard was reset by the restart (design D9)"
    )
    assert "members=1" in run._room_lines()[0]

    await server.stop()
    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


@pytest.mark.database
async def test_only_one_room_may_be_bound_to_an_identity(database: Database) -> None:
    """12.1: refused with a message naming the existing room, and nothing changes."""
    from sighop.db.repositories import RoomExistsError

    persistence, room, _loaded = await _stored_room_server(database)

    with pytest.raises(RoomExistsError) as excinfo:
        await persistence.rooms.create(
            entity_id=room.entity_id,
            name="second",
            admin_password_hash=hash_password("x"),
        )
    assert "'lounge'" in str(excinfo.value), "the refusal must name the existing room"

    rooms = await persistence.rooms.list_all()
    assert isinstance(rooms, Succeeded)
    assert [existing.name for existing in rooms.value] == ["lounge"]


@pytest.mark.database
async def test_retention_can_be_set_and_cleared_back_to_unlimited(
    database: Database,
) -> None:
    """12.5's storage half: clearing returns the room to unlimited."""
    persistence, room, _loaded = await _stored_room_server(database)

    assert isinstance(
        await persistence.rooms.set_retention(room.id, retention_days=30, retention_messages=500),
        Succeeded,
    )
    reloaded = await persistence.rooms.get_for_entity(room.entity_id)
    assert isinstance(reloaded, Succeeded) and reloaded.value is not None
    assert reloaded.value.retention == "30 days and 500 messages"

    assert isinstance(
        await persistence.rooms.set_retention(
            room.id, retention_days=None, retention_messages=None
        ),
        Succeeded,
    )
    cleared = await persistence.rooms.get_for_entity(room.entity_id)
    assert isinstance(cleared, Succeeded) and cleared.value is not None
    assert cleared.value.retention == "unlimited"


@pytest.mark.database
async def test_rotating_a_password_leaves_the_membership_alone(
    database: Database,
) -> None:
    """§7: rotation evicts nobody, because membership is keyed on the public key."""
    persistence, room, _loaded = await _stored_room_server(database)
    now = ensure_utc(dt.datetime.now(dt.UTC), field="room_member.first_login")
    await persistence.members.upsert(
        MemberRecord(
            room_id=room.id,
            public_key=bytes(range(32)),
            node_hash=0,
            permissions=int(Permission.READ_WRITE),
            sync_since=0,
            last_timestamp=0,
            first_login=now,
            last_activity=now,
        )
    )

    assert isinstance(
        await persistence.rooms.set_passwords(
            room.id, admin_password_hash=hash_password("a-new-password")
        ),
        Succeeded,
    )

    members = await persistence.members.load_for_room(room.id)
    assert isinstance(members, Succeeded)
    assert len(members.value) == 1, "rotating a password evicted a member"


@pytest.mark.database
async def test_revoking_removes_the_cursor_and_the_replay_guard_with_the_member(
    database: Database,
) -> None:
    """6.9: all four live in one row, so there is no partial revocation."""
    persistence, room, _loaded = await _stored_room_server(database)
    now = ensure_utc(dt.datetime.now(dt.UTC), field="room_member.first_login")
    key = bytes(range(32))
    await persistence.members.upsert(
        MemberRecord(
            room_id=room.id,
            public_key=key,
            node_hash=0,
            permissions=int(Permission.ADMIN),
            sync_since=99,
            last_timestamp=1234,
            first_login=now,
            last_activity=now,
        )
    )

    removed = await persistence.members.delete(room.id, key)
    assert isinstance(removed, Succeeded) and removed.value is True

    members = await persistence.members.load_for_room(room.id)
    assert isinstance(members, Succeeded)
    assert members.value == []


@pytest.mark.database
async def test_history_is_stored_ordered_and_read_back_byte_for_byte(
    database: Database,
) -> None:
    """1.3 / 7.3 against the real table: `max(now, last + 1)` and `bytea`."""
    persistence, room, _loaded = await _stored_room_server(database)
    author = bytes(range(32))

    stamps = []
    for text in (b"first", b"caf\xe9 \xff", b"third"):
        stored = await persistence.messages.store(
            room_id=room.id,
            author_public_key=author,
            text=text,
            sender_timestamp=1,
            now=1_700_000_000,
        )
        assert isinstance(stored, Succeeded)
        stamps.append(stored.value.post_timestamp)

    assert stamps == [1_700_000_000, 1_700_000_001, 1_700_000_002], (
        "two posts in one second must not share an ordering value (design D3)"
    )

    history = await persistence.messages.history(room.id)
    assert isinstance(history, Succeeded)
    assert [post.text for post in history.value] == [b"first", b"caf\xe9 \xff", b"third"]
    assert history.value[1].rendered.is_valid_utf8 is False


@pytest.mark.database
async def test_the_pruner_applies_a_policy_against_the_real_table(
    database: Database,
) -> None:
    persistence, room, _loaded = await _stored_room_server(database)
    author = bytes(range(32))
    for index in range(5):
        await persistence.messages.store(
            room_id=room.id,
            author_public_key=author,
            text=b"x",
            now=1_700_000_000 + index,
        )

    pruned = await persistence.messages.prune(room.id, retention_days=None, retention_messages=2)
    assert isinstance(pruned, Succeeded)
    assert pruned.value[0] == 3

    remaining = await persistence.messages.count(room.id)
    assert isinstance(remaining, Succeeded)
    assert remaining.value == 2


@pytest.mark.database
async def test_a_room_read_for_an_entity_that_has_none_is_none_not_an_error(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    found = await persistence.rooms.get_for_entity(uuid.uuid4())
    assert isinstance(found, Succeeded)
    assert found.value is None
