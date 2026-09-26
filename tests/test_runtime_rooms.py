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
from sighop.passwords import hash_password
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType, Permission
from sighop.runtime import Runtime, RuntimeConfig
from tests.protocol.corpus import AMBIENT, CORPUS_DIR
from tests.test_runtime import _events, _startup, runtime
from tests.test_tx import ManualClock, RecordingLogger

pytestmark = pytest.mark.usefixtures("default_persistence")

SECRET = base64.b64decode(generate_secret_key())
CAPTURE = CORPUS_DIR / AMBIENT

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


def _live_runtime(persistence: Persistence, *, config: RuntimeConfig | None = None) -> Runtime:
    """A run with a real `entity_loader`, for reconcile against the database
    directly rather than a stub — `stored_entities` stays empty on purpose,
    the way a run that started before an identity existed does."""
    return Runtime(
        source=_events(CAPTURE),
        startup=_startup,
        config=config or RuntimeConfig(status_interval=3600, advert_tick=3600),
        clock=ManualClock(),
        out=io.StringIO(),
        logger=RecordingLogger(),
        persistence=persistence,
        webhook_secret=SECRET,
    )


# --- 11.2 What a run says when it serves no room ----------------------------


async def test_a_run_with_a_database_and_no_rooms_says_none_are_configured(
    database: Database,
) -> None:
    run = runtime(_events(CAPTURE), out=io.StringIO(), persistence=Persistence(database=database))
    await run._restore()

    assert run.rooms == []
    assert run._room_lines() == ["rooms: none configured"]


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
    assert entity_id in run.path_bodies._room_entity_ids, (
        "the path-body reader must leave the room server's own PATH returns to it"
    )
    assert any(stub.entity_id == entity_id for stub in run.path_bodies.entities), (
        "claiming an entity for a room must not remove it from the shared "
        "adverts.stubs / path_bodies.entities list — it would stop advertising"
    )

    await run.rooms[0].stop()
    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


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


async def test_a_room_read_for_an_entity_that_has_none_is_none_not_an_error(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    found = await persistence.rooms.get_for_entity(uuid.uuid4())
    assert isinstance(found, Succeeded)
    assert found.value is None


# --- Stopping a deleted room, without a restart ------------------------------


async def test_a_deleted_room_stops_being_served_without_a_restart(
    database: Database,
) -> None:
    """A room is four attachments, not one, and the bus is the one that answers."""
    persistence, room, loaded = await _stored_room_server(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()
    entity_id = run.rooms[0].entity.entity_id
    assert "room:lounge" in [stats.name for stats in run.bus.subscriber_stats]

    assert await run.stop_serving_room(room.id) is True

    assert run.rooms == []
    assert "room:lounge" not in [stats.name for stats in run.bus.subscriber_stats], (
        "the room is off the list but still attached to the bus, so it still answers"
    )
    assert entity_id not in run.messenger._room_entity_ids, (
        "the direct messenger is still standing aside for a room that is gone"
    )
    assert any(stub.entity_id == entity_id for stub in run.path_bodies.entities), (
        "the path-body reader never got the entity back"
    )

    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


async def test_stopping_a_room_this_run_does_not_serve_reports_so(
    database: Database,
) -> None:
    persistence, _room, loaded = await _stored_room_server(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()

    assert await run.stop_serving_room(uuid.uuid4()) is False
    assert [server.room.name for server in run.rooms] == ["lounge"]

    await run.rooms[0].stop()
    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


async def test_stopping_one_room_leaves_the_others_served(database: Database) -> None:
    persistence, room, _loaded = await _stored_room_server(database)
    entities = EntityRepository(database=database)
    second = await entities.store(
        name="rs-2", identity=generate_identity(), secret=SECRET, node_type=NodeType.ROOM_SERVER
    )
    assert isinstance(second, Succeeded)
    await persistence.rooms.create(
        entity_id=second.value.id,
        name="study",
        admin_password_hash=hash_password(ADMIN_PASSWORD),
    )
    both = await entities.load_all(SECRET)
    assert isinstance(both, Succeeded)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, stored_entities=tuple(both.value)
        ),
        persistence=persistence,
    )
    await run._restore()
    assert sorted(server.room.name for server in run.rooms) == ["lounge", "study"]

    await run.stop_serving_room(room.id)

    assert [server.room.name for server in run.rooms] == ["study"]
    assert "room:study" in [stats.name for stats in run.bus.subscriber_stats]

    await run.rooms[0].stop()
    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


# --- 1.1 / 1.2 The alias live adoption depends on (design D5) --------------


async def test_serving_and_stopping_a_room_keeps_the_path_body_alias(
    database: Database,
) -> None:
    """Design D5: `path_bodies.entities` and `adverts.stubs` are the same list
    object, mutated in place — never rebound — so an identity appended to one
    after a room has been served and stopped still reaches the other."""
    persistence, room, loaded = await _stored_room_server(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()
    assert run.path_bodies.entities is run.adverts.stubs, (
        "serving the room rebound path_bodies.entities instead of mutating it in place"
    )

    await run.stop_serving_room(room.id)

    assert run.path_bodies.entities is run.adverts.stubs, (
        "stopping the room rebound path_bodies.entities instead of mutating it in place"
    )

    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


async def test_an_identity_appended_after_a_room_cycle_reaches_the_path_body_reader(
    database: Database,
) -> None:
    """Regression for design D5: before the fix, serving and stopping a room
    rebound `path_bodies.entities`, so an identity added to `adverts.stubs`
    afterwards was invisible to the path-body reader."""
    persistence, room, loaded = await _stored_room_server(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()
    await run.stop_serving_room(room.id)

    run.adverts.add_stub("late-arrival")

    assert any(stub.name == "late-arrival" for stub in run.path_bodies.entities), (
        "adverts.stubs and path_bodies.entities have detached from each other"
    )

    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


# --- 4.1-4.4 `reconcile_rooms` (design D7, room-server) ---------------------


async def test_a_room_on_an_unheld_identity_is_not_served_and_states_why(
    database: Database,
) -> None:
    """4.3: the same reason startup states, for a room whose identity this run
    has never held — reconcile changes nothing about that until it is adopted."""
    persistence, _room, _loaded = await _stored_room_server(database)
    run = _live_runtime(persistence)
    await run._restore()

    assert run.rooms == []
    (line,) = run._room_lines()
    assert "identity was not loaded" in line

    await run.reconcile_rooms()

    assert run.rooms == []

    await persistence.stop()


async def test_a_room_is_served_once_its_identity_is_adopted_mid_run(
    database: Database,
) -> None:
    """4.1: creating an identity and a room in either order reaches the same
    state — here the room existed first, and the identity is adopted after."""
    persistence, _room, _loaded = await _stored_room_server(database)
    run = _live_runtime(persistence)
    await run._restore()
    assert run.rooms == []

    assert await run.reconcile_entities() is True

    assert [server.room.name for server in run.rooms] == ["lounge"]
    assert "room:lounge" in [stats.name for stats in run.bus.subscriber_stats]

    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


async def test_a_first_room_taken_up_mid_run_starts_the_pruner(database: Database) -> None:
    """4.2: constructed and started when the first room is taken up mid-run —
    `_load_rooms` builds one only when startup already finds a room, and this
    run starts with none."""
    persistence, _room, _loaded = await _stored_room_server(database)
    run = _live_runtime(persistence)
    await run._restore()
    assert run.retention is None

    await run.reconcile_entities()

    assert run.retention is not None
    assert run.retention._task is not None

    await run.retention.stop()
    await persistence.stop()


async def test_the_last_room_withdrawn_mid_run_stops_the_pruner(database: Database) -> None:
    """4.2's other half: nothing is left to prune once the last room stops.

    Withdraws the room's identity (rather than calling `stop_serving_room`
    directly) so the room record and its identity both still exist — the
    ordinary way a room stops being served mid-run, and the path that
    exercises `reconcile_rooms`'s own pruner bookkeeping rather than the
    panel's direct deletion call."""
    persistence, _room, loaded = await _stored_room_server(database)
    entities = EntityRepository(database=database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
        webhook_secret=SECRET,
    )
    await run._restore()
    assert run.retention is not None

    disabled = await entities.set_enabled(loaded[0].public_key, False)
    assert isinstance(disabled, Succeeded)
    await run.reconcile_entities()

    assert run.rooms == []
    assert run.retention is None

    await persistence.stop()


async def test_disabling_the_identity_stops_the_room_with_members_and_messages_intact(
    database: Database,
) -> None:
    """4.3: a room that stops being served because its identity was disabled
    is listed as stored but not served, and its members and messages survive."""
    persistence, room, loaded = await _stored_room_server(database)
    entities = EntityRepository(database=database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
        webhook_secret=SECRET,
    )
    await run._restore()
    assert [server.room.name for server in run.rooms] == ["lounge"]

    disabled = await entities.set_enabled(loaded[0].public_key, False)
    assert isinstance(disabled, Succeeded)
    await run.reconcile_entities()

    assert run.rooms == []
    stored_room = await persistence.rooms.list_all()
    assert isinstance(stored_room, Succeeded)
    assert [r.name for r in stored_room.value] == ["lounge"], "the room itself must survive"

    # And enabled again: re-served, with membership and history intact.
    enabled = await entities.set_enabled(loaded[0].public_key, True)
    assert isinstance(enabled, Succeeded)
    await run.reconcile_entities()

    assert [server.room.name for server in run.rooms] == ["lounge"]
    assert run.rooms[0].room.id == room.id

    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


async def test_taking_up_a_room_mid_run_leaves_the_others_undisturbed(
    database: Database,
) -> None:
    """4.4: every already-served room's members, unsynced positions and
    counters are untouched, and nothing is logged out."""
    persistence, _first_room, first_loaded = await _stored_room_server(database, name="rs-1")
    entities = EntityRepository(database=database)
    second_identity = generate_identity()
    second_stored = await entities.store(
        name="rs-2", identity=second_identity, secret=SECRET, node_type=NodeType.ROOM_SERVER
    )
    assert isinstance(second_stored, Succeeded)
    second_room = await persistence.rooms.create(
        entity_id=second_stored.value.id,
        name="study",
        admin_password_hash=hash_password(ADMIN_PASSWORD),
    )
    assert isinstance(second_room, Succeeded)

    lounge = await persistence.rooms.list_all()
    assert isinstance(lounge, Succeeded)
    (lounge_room,) = [r for r in lounge.value if r.name == "lounge"]
    member_key = bytes(range(32))
    now = ensure_utc(dt.datetime.now(dt.UTC), field="room_member.first_login")
    assert isinstance(
        await persistence.members.upsert(
            MemberRecord(
                room_id=lounge_room.id,
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
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, stored_entities=tuple(first_loaded)
        ),
        persistence=persistence,
        webhook_secret=SECRET,
    )
    await run._restore()
    assert [server.room.name for server in run.rooms] == ["lounge"]
    before_members = dict(run.rooms[0].members)
    assert member_key in before_members, "the member fixture did not attach to the right room"

    await run.reconcile_entities()

    assert sorted(server.room.name for server in run.rooms) == ["lounge", "study"]
    assert run.rooms[0].members == before_members, "the already-served room's members changed"

    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


# --- Room settings reach a served room (change push-ack-window-from-transmit)


async def test_changed_room_settings_reach_the_served_room_on_reconcile(
    database: Database,
) -> None:
    """Design D10: access, retention and delivery all without a restart."""
    persistence, room, loaded = await _stored_room_server(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()
    server = run.rooms[0]
    assert server.policy.guest_open is False
    assert server.room.push_ack_window_seconds is None

    assert isinstance(await persistence.rooms.set_passwords(room.id, guest_open=True), Succeeded)
    assert isinstance(
        await persistence.rooms.set_retention(room.id, retention_days=30, retention_messages=None),
        Succeeded,
    )
    assert isinstance(
        await persistence.rooms.set_delivery(
            room.id, push_ack_window_seconds=30, push_recent_days=7
        ),
        Succeeded,
    )
    await run.reconcile_rooms()

    assert run.rooms[0] is server, "the room was restarted rather than refreshed"
    assert server.policy.guest_open is True, "logins still use the old access settings"
    assert server.room.retention_days == 30
    assert server.room.push_ack_window_seconds == 30
    assert server.room.push_recent_days == 7
    applied = run.logger.of("room_settings_applied")
    assert applied
    assert set(applied[-1]["changed"]) == {
        "guest_open",
        "retention_days",
        "push_ack_window_seconds",
        "push_recent_days",
    }
    (line,) = run._room_status_lines()
    assert "not_recent=0" in line
    assert "ack_window=30s" in line
    assert "recent=7d" in line

    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()


async def test_a_member_heard_only_by_advert_is_eligible_for_delivery(
    database: Database,
) -> None:
    """Design D9: the runtime hands the room the contact store's `last_heard`."""
    persistence, room, loaded = await _stored_room_server(database)
    assert isinstance(
        await persistence.rooms.set_delivery(
            room.id, push_ack_window_seconds=None, push_recent_days=7
        ),
        Succeeded,
    )
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    long_ago = ensure_utc(run.clock.now() - dt.timedelta(days=30), field="last_activity")
    member_key = generate_identity().public_key
    assert isinstance(
        await persistence.members.upsert(
            MemberRecord(
                room_id=room.id,
                public_key=member_key,
                node_hash=member_key[0],
                permissions=int(Permission.READ_WRITE),
                sync_since=0,
                last_timestamp=0,
                first_login=long_ago,
                last_activity=long_ago,
            )
        ),
        Succeeded,
    )
    await run._restore()
    server = run.rooms[0]
    assert server.members_not_recent() == 1
    assert "not_recent=1" in run._room_status_lines()[0]

    contact = run.contacts.add_public_key(member_key)
    contact.last_heard = run.clock.now() - dt.timedelta(days=1)

    assert server.members_not_recent() == 0

    if run.retention is not None:
        await run.retention.stop()
    await persistence.stop()
