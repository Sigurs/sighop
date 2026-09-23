"""End-to-end: the whole store-changes-without-restart lifecycle.

Change `apply-store-changes-without-restart`, tasks 7.1-7.2. A `Runtime.run()`
actually running as a background task, against a real database — not
`reconcile_entities()`/`reconcile_rooms()`/`reconcile_bots()` called directly,
which the rest of the suite already covers exhaustively. This file is the one
place that proves the periodic refresh loop itself notices a change and the
whole pass — identities, then rooms, then bots — runs to completion inside a
live process.

The identity, room and bot writes here are what `sighop keys`, `sighop room`
and `sighop bot` make: a second `Persistence` on the same database stands in
for the separate process the proposal describes, since those commands' own
handlers make exactly these repository calls with nothing of substance
between argv parsing and the write worth re-testing here.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io

from sighop.bots import drivers as bot_drivers
from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import EntityRepository, MemberRecord
from sighop.db.times import ensure_utc
from sighop.net.tx import SystemClock
from sighop.passwords import hash_password
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType
from sighop.runtime import Runtime, RuntimeConfig
from tests.test_runtime import _never_ends, _startup
from tests.test_tx import RecordingLogger

SECRET = base64.b64decode(generate_secret_key())
ADMIN_PASSWORD = "an-admin-password"
REFRESH_SECONDS = 0.2
"""Real, and short — a real `SystemClock` on purpose. `_status_loop`,
`_advert_loop` and `_entity_refresh_loop` all sleep on the clock they are
given; a `ManualClock` that never advances turns every one of those into a
busy-spin with nothing to pace it against the others, which is realistic for
nothing this test needs. Waiting a few real intervals is simpler and it is
what an operator's own "within the refresh interval" actually means."""


def _live_run(database: Database) -> Runtime:
    """A run with nothing stored yet, exactly as one that started before any
    of this test's identities existed — the case this whole change is for."""
    return Runtime(
        source=_never_ends(),
        startup=_startup,
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, entity_refresh_seconds=REFRESH_SECONDS
        ),
        clock=SystemClock(),
        out=io.StringIO(),
        logger=RecordingLogger(),
        persistence=Persistence(database=database),
        webhook_secret=SECRET,
    )


async def _tick() -> None:
    """Wait past a few real refresh intervals, for the loop to notice."""
    await asyncio.sleep(REFRESH_SECONDS * 6)


async def test_creating_an_identity_a_room_and_a_bot_reaches_a_running_process(
    database: Database,
) -> None:
    """7.1: created from the command line against a database this run is
    serving, and within the refresh interval the run adverts for both, serves
    the room and runs the bot — no restart."""
    entities = EntityRepository(database=database)
    room_identity = generate_identity()
    room_host = await entities.store(
        name="[redacted]-host",
        identity=room_identity,
        secret=SECRET,
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(room_host, Succeeded)
    bot_identity = generate_identity()
    bot_host = await entities.store(
        name="dev-greeter-host",
        identity=bot_identity,
        secret=SECRET,
        entity_type="bot",
    )
    assert isinstance(bot_host, Succeeded)

    run = _live_run(database)
    task = asyncio.create_task(run.run())
    try:
        await run._ready.wait()
        assert run.adverts.stubs == [], "the run must start holding neither identity"

        # From the command line: `sighop room create` / `sighop bot create`,
        # both of which are `RoomRepository.create` / `BotRepository.create`.
        room_persistence = Persistence(database=database)
        room = await room_persistence.rooms.create(
            entity_id=room_host.value.id,
            name="[redacted]",
            admin_password_hash=hash_password(ADMIN_PASSWORD),
        )
        assert isinstance(room, Succeeded)
        bot = await room_persistence.bots.create(
            entity_id=bot_host.value.id,
            driver="greeter",
            config=bot_drivers.default_config("greeter"),
            entity_name="dev-greeter-host",
        )
        assert isinstance(bot, Succeeded)

        await _tick()

        held = {stub.identity.public_key for stub in run.adverts.stubs}
        assert room_identity.public_key in held
        assert bot_identity.public_key in held
        assert [server.room.name for server in run.rooms] == ["[redacted]"]
        assert [worker.record.id for worker in run.bots] == [bot.value.id]
    finally:
        run.stop()
        await asyncio.wait_for(task, 5)


async def test_disabling_then_removing_those_identities_withdraws_them_and_keeps_the_store(
    database: Database,
) -> None:
    """7.2: the run withdraws them, stops the room and the bot, and the
    stored rooms, members, messages and bot state survive."""
    entities = EntityRepository(database=database)
    room_identity = generate_identity()
    room_host = await entities.store(
        name="[redacted]-host",
        identity=room_identity,
        secret=SECRET,
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(room_host, Succeeded)
    bot_identity = generate_identity()
    bot_host = await entities.store(
        name="dev-greeter-host",
        identity=bot_identity,
        secret=SECRET,
        entity_type="bot",
    )
    assert isinstance(bot_host, Succeeded)
    persistence = Persistence(database=database)
    room = await persistence.rooms.create(
        entity_id=room_host.value.id,
        name="[redacted]",
        admin_password_hash=hash_password(ADMIN_PASSWORD),
    )
    assert isinstance(room, Succeeded)
    bot = await persistence.bots.create(
        entity_id=bot_host.value.id,
        driver="greeter",
        config=bot_drivers.default_config("greeter"),
        entity_name="dev-greeter-host",
    )
    assert isinstance(bot, Succeeded)
    assert isinstance(await persistence.bot_state.set(bot.value.id, "greeted", True), Succeeded)
    member_key = bytes(range(32))
    now = ensure_utc(dt.datetime.now(dt.UTC), field="room_member.first_login")
    assert isinstance(
        await persistence.members.upsert(
            MemberRecord(
                room_id=room.value.id,
                public_key=member_key,
                node_hash=member_key[0],
                permissions=0,
                sync_since=0,
                last_timestamp=0,
                first_login=now,
                last_activity=now,
            )
        ),
        Succeeded,
    )

    run = _live_run(database)
    task = asyncio.create_task(run.run())
    try:
        await run._ready.wait()
        await _tick()
        assert [server.room.name for server in run.rooms] == ["[redacted]"]
        assert [worker.record.id for worker in run.bots] == [bot.value.id]

        # From the command line: `sighop keys disable` then `sighop keys delete`.
        assert isinstance(await entities.set_enabled(room_identity.public_key, False), Succeeded)
        assert isinstance(await entities.set_enabled(bot_identity.public_key, False), Succeeded)
        await _tick()

        assert run.rooms == [], "the room must stop being served when its identity is disabled"
        assert list(run.bots) == [], "the bot must stop running when its identity is disabled"
        held = {stub.identity.public_key for stub in run.adverts.stubs}
        assert room_identity.public_key not in held
        assert bot_identity.public_key not in held

        # room-server / bot-runtime: disabling is not a deletion. Checked here,
        # right after the disable the run just withdrew — this is the
        # guarantee those specs make, and it is `EntityRepository.remove`
        # below, not this disable, that a bound identity's own docstring
        # calls "deliberately blunt": `ON DELETE CASCADE` takes a bound
        # room's members and history with it, which is exactly why
        # `web-admin`'s removal confirmation refuses a bound identity
        # outright and offers disabling as the reversible action instead.
        stored_rooms = await persistence.rooms.list_all()
        assert isinstance(stored_rooms, Succeeded)
        assert [r.name for r in stored_rooms.value] == ["[redacted]"]
        members = await persistence.members.load_for_room(room.value.id)
        assert isinstance(members, Succeeded)
        assert [m.public_key for m in members.value] == [member_key]
        state = await persistence.bot_state.get(bot.value.id, "greeted")
        assert isinstance(state, Succeeded)
        assert state.value is True

        # From the command line: `sighop room delete` and `sighop bot delete`
        # first — required, since removal refuses a bound identity — then
        # `sighop keys delete` on each now-unbound identity.
        assert isinstance(await persistence.rooms.delete(room.value.id), Succeeded)
        assert isinstance(await persistence.bots.delete(bot.value.id), Succeeded)
        assert isinstance(await entities.remove(room_identity.public_key), Succeeded)
        assert isinstance(await entities.remove(bot_identity.public_key), Succeeded)
        await _tick()

        listed = await persistence.entities.list_all()
        assert isinstance(listed, Succeeded)
        assert listed.value == [], "both identities must be gone from the store"
        held_after_removal = {stub.identity.public_key for stub in run.adverts.stubs}
        assert room_identity.public_key not in held_after_removal
        assert bot_identity.public_key not in held_after_removal
    finally:
        run.stop()
        await asyncio.wait_for(task, 5)
    await persistence.stop()
