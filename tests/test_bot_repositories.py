"""`BotRepository` and `BotStateRepository` (tasks 2.1, 2.2, design D3/D16).

Two things are asserted here that the schema cannot say for itself:

* **a refusal names what already holds the entity.** The unique constraint stops
  a second bot; it does not tell an operator that the identity is a room
  server's, and "which role does this identity already play" is the whole
  question `sighop bot create` is being asked.
* **a failed state write reports failure.** Design D6 has the greeter write its
  greeting record *before* it transmits, so a write that quietly reported
  success would produce exactly the duplicate greeting the record exists to
  prevent. That one is asserted with no database at all, because the interesting
  case is a database that is not answering.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.repositories import (
    BotExistsError,
    BotRepository,
    BotStateRepository,
    EntityHasRoleError,
    EntityRepository,
    RoomRepository,
)
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType
from tests.botfixtures import MemoryBotState, bot_record

SECRET = base64.b64decode(generate_secret_key())


async def _entity(database: Database, name: str, *, node_type: NodeType = NodeType.CHAT) -> uuid.UUID:
    outcome = await EntityRepository(database=database).store(
        name=name,
        identity=generate_identity(),
        secret=SECRET,
        node_type=node_type,
        entity_type="bot" if node_type is NodeType.CHAT else None,
    )
    assert isinstance(outcome, Succeeded)
    return outcome.value.id


# --- 2.1 Creation, and the two refusals -------------------------------------


@pytest.mark.database
async def test_a_bot_is_created_enabled_and_observing_and_names_its_identity(
    database: Database,
) -> None:
    bots = BotRepository(database=database)
    entity_id = await _entity(database, "greeter-bot")

    created = await bots.create(
        entity_id=entity_id, driver="greeter", config={"greeting": "hi"}, entity_name="greeter-bot"
    )

    assert isinstance(created, Succeeded)
    assert created.value.enabled is True
    assert created.value.mode == "observe"
    assert created.value.active is False

    listed = await bots.list_all()
    assert isinstance(listed, Succeeded)
    # The name comes off `entity` on the same query: a bot has no name of its
    # own, and every place that shows one shows whose identity it speaks as.
    assert [record.entity_name for record in listed.value] == ["greeter-bot"]


@pytest.mark.database
async def test_a_second_bot_on_one_identity_is_refused_naming_what_holds_it(
    database: Database,
) -> None:
    bots = BotRepository(database=database)
    entity_id = await _entity(database, "greeter-bot")
    await bots.create(entity_id=entity_id, driver="greeter", config={})

    with pytest.raises(BotExistsError) as excinfo:
        await bots.create(entity_id=entity_id, driver="greeter", config={})

    assert "greeter" in str(excinfo.value), "the refusal names the driver that holds it"
    assert "one role" in str(excinfo.value)


@pytest.mark.database
async def test_a_bot_on_a_room_servers_identity_is_refused_naming_the_room(
    database: Database,
) -> None:
    """2.1: the identity already has a role, and the refusal says which one."""
    entity_id = await _entity(database, "the-room", node_type=NodeType.ROOM_SERVER)
    room = await RoomRepository(database=database).create(
        entity_id=entity_id, name="lounge", admin_password_hash="$argon2id$x"
    )
    assert isinstance(room, Succeeded)

    with pytest.raises(EntityHasRoleError) as excinfo:
        await BotRepository(database=database).create(
            entity_id=entity_id, driver="greeter", config={}
        )

    assert "lounge" in str(excinfo.value)
    assert "one role" in str(excinfo.value)


@pytest.mark.database
async def test_enablement_mode_and_configuration_are_each_settable(
    database: Database,
) -> None:
    bots = BotRepository(database=database)
    entity_id = await _entity(database, "greeter-bot")
    created = await bots.create(
        entity_id=entity_id, driver="greeter", config={"max_hops": 1}, entity_name="greeter-bot"
    )
    assert isinstance(created, Succeeded)
    record = created.value

    assert isinstance(await bots.set_enabled(record.id, False), Succeeded)
    assert isinstance(await bots.set_mode(record.id, "active"), Succeeded)
    assert isinstance(await bots.set_config(record.id, {"max_hops": 3}), Succeeded)

    found = await bots.get_by_name("greeter-bot")
    assert isinstance(found, Succeeded) and found.value is not None
    assert found.value.enabled is False
    assert found.value.mode == "active"
    assert found.value.active is True
    assert found.value.config == {"max_hops": 3}


@pytest.mark.database
async def test_an_unknown_name_is_absence_rather_than_an_error(database: Database) -> None:
    found = await BotRepository(database=database).get_by_name("nobody")
    assert isinstance(found, Succeeded)
    assert found.value is None


# --- 2.2 State, written straight through ------------------------------------


@pytest.mark.database
async def test_state_is_written_read_listed_and_deleted_per_bot(database: Database) -> None:
    bots = BotRepository(database=database)
    state = BotStateRepository(database=database)
    first = await bots.create(entity_id=await _entity(database, "a"), driver="greeter", config={})
    second = await bots.create(entity_id=await _entity(database, "b"), driver="greeter", config={})
    assert isinstance(first, Succeeded) and isinstance(second, Succeeded)

    assert isinstance(await state.set(first.value.id, "greeted:aa", {"outcome": "sent"}), Succeeded)
    assert isinstance(await state.set(second.value.id, "greeted:aa", {"outcome": "observed"}), Succeeded)
    # Upserted: writing a key twice is ordinary, not a constraint violation.
    assert isinstance(await state.set(first.value.id, "greeted:aa", {"outcome": "acked"}), Succeeded)

    read = await state.get(first.value.id, "greeted:aa")
    assert isinstance(read, Succeeded) and read.value == {"outcome": "acked"}

    listed = await state.list(second.value.id)
    assert isinstance(listed, Succeeded)
    assert listed.value == {"greeted:aa": {"outcome": "observed"}}

    assert isinstance(await state.delete(first.value.id, "greeted:aa"), Succeeded)
    gone = await state.get(first.value.id, "greeted:aa")
    assert isinstance(gone, Succeeded) and gone.value is None
    # The other bot's identically named key is untouched.
    still = await state.get(second.value.id, "greeted:aa")
    assert isinstance(still, Succeeded) and still.value == {"outcome": "observed"}


@pytest.mark.database
async def test_clearing_state_reports_how_many_keys_went(database: Database) -> None:
    bots = BotRepository(database=database)
    state = BotStateRepository(database=database)
    created = await bots.create(entity_id=await _entity(database, "a"), driver="greeter", config={})
    assert isinstance(created, Succeeded)
    await state.set(created.value.id, "greeted:aa", {})
    await state.set(created.value.id, "greeted:bb", {})

    cleared = await state.clear(created.value.id)
    assert isinstance(cleared, Succeeded) and cleared.value == 2


async def test_a_failed_write_reports_failure_rather_than_silently_succeeding() -> None:
    """2.2: the property design D6 turns on, asserted with no database at all.

    A state handle that reported success for a write that did not land would
    hand the greeter permission to transmit on the strength of a record nobody
    holds, and the next restart would greet that stranger a second time.
    """
    from sighop.bots.base import BotStateHandle

    store = MemoryBotState(fail_writes=True)
    handle = BotStateHandle(bot_id=bot_record().id, store=store)

    assert await handle.set("greeted:aa", {"outcome": "sent"}) is False
    assert handle.write_failures == 1
    assert store.rows == {}, "nothing was written, and the caller was told so"


async def test_a_read_that_failed_is_not_reported_as_absence_in_the_counters() -> None:
    """2.2: an empty read and a read that did not happen are different facts.

    The value still comes back as the default — there is nothing else to return
    — but the failure is counted, and the runtime's own `degraded` flag is what a
    driver consults before it acts on an absence.
    """
    from sighop.bots.base import BotStateHandle

    handle = BotStateHandle(bot_id=bot_record().id, store=MemoryBotState(fail_reads=True))

    assert await handle.get("greeted:aa") is None
    assert handle.write_failures == 1
