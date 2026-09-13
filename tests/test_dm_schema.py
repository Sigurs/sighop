"""§6's tenth table, and the three shapes design D7 argued for.

Each of these is a decision rather than a column that happened to be convenient,
so each is asserted:

* one message is one row — `UNIQUE (entity_public_key, ref)` is what makes
  "written at submission, updated when it resolves" true rather than hopeful;
* `wire_timestamp` is `BIGINT`, because it is the peer's clock as it travels and
  not an instant, and the ordering column beside it is `TIMESTAMPTZ`;
* the migration says out loud what a downgrade costs and that stored message
  text is not encrypted at rest.

The metadata assertions run with no database. The constraint assertions need a
real server, for `test_bot_schema.py`'s reason: a constraint SQLAlchemy declares
and Postgres does not enforce is a constraint that does not exist.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import BigInteger, DateTime, Table, UniqueConstraint, inspect, select, text
from sqlalchemy.exc import IntegrityError

from sighop.config import DatabaseConfig
from sighop.db import migrations
from sighop.db.engine import Database, SchemaVersionError
from sighop.db.models import DirectMessage
from tests.dbfixtures import _create_schema, _drop_schema

ENTITY_KEY = bytes(range(32))
PEER_KEY = bytes(range(32, 64))

MIGRATION = "0004_direct_messages.py"


def _message(**overrides: object) -> DirectMessage:
    fields: dict[str, object] = {
        "entity_public_key": ENTITY_KEY,
        "peer_public_key": PEER_KEY,
        "direction": "out",
        "text": b"hello",
        "wire_timestamp": 1_757_000_000,
        "handled_at": dt.datetime.now(dt.UTC),
        "ref": "0123456789abcdef",
        "packet_ids": ["aa11"],
        "attempts": 1,
        "route_flood": False,
        "route_path": b"",
        "outcome": "in_flight",
        "ack_latency_ms": None,
    }
    fields.update(overrides)
    return DirectMessage(**fields)


# --- 2.1 What the model declares, with no database --------------------------


TABLE: Table = DirectMessage.__table__  # type: ignore[assignment]


def test_one_message_is_one_row_per_identity_and_ref() -> None:
    """2.1: the uniqueness that makes an update in place possible at all."""
    unique = {
        constraint.name: {column.name for column in constraint.columns}
        for constraint in TABLE.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique["uq_direct_message_entity_public_key_ref"] == {
        "entity_public_key",
        "ref",
    }


def test_the_conversation_read_is_indexed_by_handled_at_not_the_wire_clock() -> None:
    """2.1: the index carries the ordering rule design D7 states."""
    indexes = {
        index.name: [column.name for column in index.columns] for index in TABLE.indexes
    }
    assert indexes["ix_direct_message_entity_public_key_peer_public_key_handled_at"] == [
        "entity_public_key",
        "peer_public_key",
        "handled_at",
    ]


def test_the_wire_clock_is_an_integer_and_ours_is_an_instant() -> None:
    """2.1, §6's rule: the two representations never share a column's meaning.

    `wire_timestamp` is MeshCore's unsigned 32-bit epoch seconds as they travel —
    a peer's claim — and `room_member.sync_since` already establishes that such a
    value is `BIGINT`. `handled_at` is ours, so it is an instant with a zone.
    """
    columns = inspect(DirectMessage).columns
    assert isinstance(columns["wire_timestamp"].type, BigInteger), (
        "wire_timestamp must be BIGINT: it is the peer's clock as it appears on "
        "the wire, not an instant"
    )
    handled = columns["handled_at"].type
    assert isinstance(handled, DateTime) and handled.timezone


def test_an_unresolved_send_has_an_outcome_rather_than_a_null() -> None:
    """2.1: a restart mid-send leaves `in_flight`, never a claim of delivery."""
    columns = inspect(DirectMessage).columns
    assert columns["outcome"].nullable is False
    assert columns["text"].nullable is False
    assert columns["packet_ids"].nullable is False


# --- 2.3 What the migration records -----------------------------------------


def test_the_migration_records_the_tenth_table_its_cost_and_its_exposure() -> None:
    """2.3: reviewed by assertion — the three facts a reader has to be told."""
    source = (migrations.migrations_dir() / "versions" / MIGRATION).read_text()
    assert "``direct_message``" in source, "the table itself must be named"
    assert "tenth" in source, "§6's count must be stated, as `0003` stated the eighth"
    assert "design D7" in source
    assert "every recorded conversation" in source, "a downgrade's cost is stated"
    assert "unencrypted" in source, "the at-rest exposure is stated, not inferred"
    assert "dump" in source, "and what it means for a database dump"


# --- 2.1 The same shapes against a real server ------------------------------


@pytest.mark.database
async def test_one_identity_may_not_hold_one_ref_twice(database: Database) -> None:
    """2.1: the schema refuses the second row, so the repository must upsert."""
    async with database.sessions() as session:
        session.add(_message())
        await session.commit()

    with pytest.raises(IntegrityError):
        async with database.sessions() as session:
            session.add(_message(text=b"again"))
            await session.commit()


@pytest.mark.database
async def test_two_identities_may_hold_the_same_ref(database: Database) -> None:
    """2.1: `ref` is scoped to the identity, because a reception's `packet_id`
    can legitimately be told to two identities in one process."""
    other = bytes(range(64, 96))
    async with database.sessions() as session:
        session.add(_message(direction="in", ref="pkt-1"))
        session.add(_message(direction="in", ref="pkt-1", entity_public_key=other))
        await session.commit()

    async with database.sessions() as session:
        rows = (
            (await session.execute(select(DirectMessage).where(DirectMessage.ref == "pkt-1")))
            .scalars()
            .all()
        )
        assert {row.entity_public_key for row in rows} == {ENTITY_KEY, other}


@pytest.mark.database
async def test_message_text_survives_bytes_that_are_not_valid_text(database: Database) -> None:
    """`dm-history`: stored as the bytes that were on the wire, untranscoded."""
    raw = b"\xff\xfe not utf-8 \x00 at all"
    async with database.sessions() as session:
        session.add(_message(direction="in", ref="pkt-bytes", text=raw))
        await session.commit()

    async with database.sessions() as session:
        stored = (
            await session.execute(
                select(DirectMessage).where(DirectMessage.ref == "pkt-bytes")
            )
        ).scalar_one()
        assert stored.text == raw


@pytest.mark.database
async def test_the_wire_clock_column_holds_a_value_no_timestamp_would(
    database: Database, test_schema: str
) -> None:
    """2.1: asserted against the server, not only against the model.

    A far-future wire timestamp is exactly what a peer with a wrong clock sends,
    and it has to round-trip as the integer it is rather than being rejected or
    reinterpreted.
    """
    async with database.sessions() as session:
        kind = (
            await session.execute(
                text(
                    "SELECT data_type FROM information_schema.columns WHERE "
                    "table_schema = :schema AND table_name = 'direct_message' "
                    "AND column_name = 'wire_timestamp'"
                ),
                {"schema": test_schema},
            )
        ).scalar_one()
        assert kind == "bigint"

        session.add(_message(direction="in", ref="pkt-clock", wire_timestamp=4_294_967_295))
        await session.commit()

    async with database.sessions() as session:
        stored = (
            await session.execute(
                select(DirectMessage).where(DirectMessage.ref == "pkt-clock")
            )
        ).scalar_one()
        assert stored.wire_timestamp == 4_294_967_295


# --- 2.4 A database one revision behind -------------------------------------


@pytest.mark.database
async def test_a_database_at_0003_is_refused_naming_both_revisions_and_the_command(
    database_url: str,
) -> None:
    """2.4: a milestone-7 deployment meeting a milestone-8 binary fails at
    startup, naming where the database is, where the code expects it, and the
    one command that reconciles them."""
    schema = "sighop_test_dm_behind"
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    config = DatabaseConfig(url=database_url, schema=schema)
    await migrations.upgrade_async(config, revision="0003")

    handle = Database(config=config)
    try:
        with pytest.raises(SchemaVersionError) as excinfo:
            await handle.open()
        message = str(excinfo.value)
        assert "0003" in message, "the message must name where the database is"
        # The head moves on (0005 is milestone 9's); where the code expects
        # the database is whatever this checkout's head is.
        assert migrations.expected_revision() in message, "and where the code expects it"
        assert migrations.UPGRADE_COMMAND in message
    finally:
        await handle.dispose()
        await _drop_schema(database_url, schema)
