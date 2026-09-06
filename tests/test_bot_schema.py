"""The two tables milestone 7 owns, and the constraints that carry design.

Each of these is a shape design D3 argued for rather than a column that happened
to be convenient, so each is asserted rather than assumed:

* one bot per identity (`uq_bot_entity_id`), the same rule `room` has;
* a new bot is enabled and in **observe** mode, which is the whole safety
  posture of this milestone expressed as two defaults;
* state is keyed `(bot_id, key)`, so two bots may hold one key and one bot may
  not hold it twice;
* and dropping a bot drops its state with it.

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

from sighop.config import DatabaseConfig, generate_secret_key
from sighop.db import migrations
from sighop.db.engine import Database, SchemaVersionError, Succeeded
from sighop.db.models import Bot, BotState
from sighop.db.repositories import EntityRepository
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType
from tests.dbfixtures import _create_schema, _drop_schema

SECRET = base64.b64decode(generate_secret_key())


# --- 1.1 / 1.2 What the models declare, with no database --------------------


def test_one_bot_per_identity_is_a_constraint_not_a_convention() -> None:
    """1.1: an identity plays one role, expressed as uniqueness (design D3)."""
    assert inspect(Bot).columns["entity_id"].unique is True


def test_a_new_bot_is_enabled_and_observing() -> None:
    """1.1, design D4: transmitting is the opt-in, and it is a stored decision.

    Asserted at the model *and* the migration, because these two defaults are
    the whole of the safety posture: a bot that came up active would transmit at
    a stranger on the strength of a row nobody looked at twice.
    """
    columns = inspect(Bot).columns
    assert columns["enabled"].default.arg is True
    assert columns["mode"].default.arg == "observe"

    source = (migrations.migrations_dir() / "versions" / "0003_bots.py").read_text()
    assert "'observe'" in source, "the schema itself must default to observe"


def test_bot_state_is_keyed_by_the_bot_and_the_key() -> None:
    """1.2: isolation between bots is the schema's, not the runtime's memory."""
    primary = {column.name for column in inspect(BotState).primary_key}
    assert primary == {"bot_id", "key"}


def test_every_bot_timestamp_carries_a_time_zone() -> None:
    """1.1 / 1.2, design D7: nothing here is wire seconds, so all are instants."""
    from sqlalchemy import DateTime

    for model, names in ((Bot, ("created_at",)), (BotState, ("updated_at",))):
        columns = inspect(model).columns  # type: ignore[attr-defined]
        for name in names:
            kind = columns[name].type
            assert isinstance(kind, DateTime) and kind.timezone, (
                f"{model.__tablename__}.{name} must carry a time zone"
            )


# --- 1.4 What the migration records -----------------------------------------


def test_the_bot_migration_records_the_last_absent_table_and_what_a_downgrade_costs() -> None:
    """1.4: §6's eighth table arrives, a ninth joins it, and both are explained.

    Reviewed by assertion rather than by hope: `0002` said `bot_state` was
    deliberately absent, so `0003` has to say it is no longer, why `bot` came
    with it, and what unwinding this revision actually loses.
    """
    source = (migrations.migrations_dir() / "versions" / "0003_bots.py").read_text()
    assert "``bot_state``" in source, "the table §6 named must be named as arriving"
    assert "``bot``" in source, "the table §6 did not sketch must be explained"
    assert "design D3" in source, "why there are two tables rather than one"
    assert "greeting record" in source, "a downgrade's cost is stated, not discovered"
    assert "greeted again" in source


# --- 1.1 / 1.2 / 1.3 The same shapes against a real server ------------------


async def _entity(database: Database, name: str = "greeter-bot") -> uuid.UUID:
    outcome = await EntityRepository(database=database).store(
        name=name,
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.CHAT,
        entity_type="bot",
    )
    assert isinstance(outcome, Succeeded)
    return outcome.value.id


def _bot(entity_id: uuid.UUID, *, driver: str = "greeter") -> Bot:
    return Bot(
        id=uuid.uuid4(),
        entity_id=entity_id,
        driver=driver,
        config={},
        created_at=dt.datetime.now(dt.UTC),
    )


@pytest.mark.database
async def test_an_identity_may_carry_only_one_bot(database: Database) -> None:
    """1.1: a second bot on one identity is refused by the schema itself."""
    entity_id = await _entity(database)
    async with database.sessions() as session:
        session.add(_bot(entity_id))
        await session.commit()

    with pytest.raises(IntegrityError):
        async with database.sessions() as session:
            session.add(_bot(entity_id, driver="greeter"))
            await session.commit()


@pytest.mark.database
async def test_a_stored_bot_comes_back_enabled_and_observing(database: Database) -> None:
    """1.1: the two defaults survive the round trip, which is where they matter."""
    entity_id = await _entity(database)
    bot = _bot(entity_id)
    async with database.sessions() as session:
        session.add(bot)
        await session.commit()

    async with database.sessions() as session:
        stored = (await session.execute(select(Bot).where(Bot.id == bot.id))).scalar_one()
        assert stored.enabled is True
        assert stored.mode == "observe"


@pytest.mark.database
async def test_two_bots_may_hold_one_key_and_one_bot_may_not_hold_it_twice(
    database: Database,
) -> None:
    """1.2: what makes one bot's greeting records invisible to another."""
    first = _bot(await _entity(database, "first"))
    second = _bot(await _entity(database, "second"))
    now = dt.datetime.now(dt.UTC)
    async with database.sessions() as session:
        session.add(first)
        session.add(second)
        await session.commit()

    async with database.sessions() as session:
        session.add(BotState(bot_id=first.id, key="greeted:aa", value={"n": 1}, updated_at=now))
        session.add(BotState(bot_id=second.id, key="greeted:aa", value={"n": 2}, updated_at=now))
        await session.commit()

    async with database.sessions() as session:
        rows = (
            (await session.execute(select(BotState).where(BotState.key == "greeted:aa")))
            .scalars()
            .all()
        )
        assert {row.value["n"] for row in rows} == {1, 2}

    with pytest.raises(IntegrityError):
        async with database.sessions() as session:
            session.add(
                BotState(bot_id=first.id, key="greeted:aa", value={"n": 3}, updated_at=now)
            )
            await session.commit()


@pytest.mark.database
async def test_removing_a_bot_removes_its_state(database: Database) -> None:
    """1.3: `ON DELETE CASCADE`, so no greeting record outlives its bot."""
    bot = _bot(await _entity(database))
    async with database.sessions() as session:
        session.add(bot)
        await session.commit()
    async with database.sessions() as session:
        session.add(
            BotState(
                bot_id=bot.id, key="greeted:aa", value={}, updated_at=dt.datetime.now(dt.UTC)
            )
        )
        await session.commit()

    async with database.sessions() as session:
        stored = (await session.execute(select(Bot).where(Bot.id == bot.id))).scalar_one()
        await session.delete(stored)
        await session.commit()

    async with database.sessions() as session:
        assert (
            await session.execute(select(BotState).where(BotState.bot_id == bot.id))
        ).scalars().all() == []


# --- 1.5 A database one revision behind -------------------------------------


@pytest.mark.database
async def test_a_database_at_0002_is_refused_naming_both_revisions_and_the_command(
    database_url: str,
) -> None:
    """1.5: a milestone-6 deployment meeting a milestone-7 binary fails at
    startup, naming where the database is, where the code expects it, and the
    one command that reconciles them — not at the first advert."""
    schema = "sighop_test_bots_behind"
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    config = DatabaseConfig(url=database_url, schema=schema)
    await migrations.upgrade_async(config, revision="0002")

    handle = Database(config=config)
    try:
        with pytest.raises(SchemaVersionError) as excinfo:
            await handle.open()
        message = str(excinfo.value)
        assert "0002" in message, "the message must name where the database is"
        assert "0003" in message, "and where the code expects it to be"
        assert migrations.UPGRADE_COMMAND in message
    finally:
        await handle.dispose()
        await _drop_schema(database_url, schema)
