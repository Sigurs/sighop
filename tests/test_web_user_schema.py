"""The eleventh table, its migration, and the one username rule (tasks 1.1-1.4).

Metadata and normalisation assertions run with no database. The constraint,
cycle and version-check assertions need a real server, for
`test_bot_schema.py`'s reason: a constraint SQLAlchemy declares and Postgres
does not enforce is a constraint that does not exist.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid

import pytest
from sqlalchemy import DateTime, Table, UniqueConstraint, inspect, select
from sqlalchemy.exc import IntegrityError

from sighop.config import DatabaseConfig, generate_secret_key
from sighop.db import migrations
from sighop.db.engine import Database, SchemaVersionError, Succeeded
from sighop.db.models import WebUser
from sighop.db.repositories import (
    MAX_USERNAME_LENGTH,
    EntityRepository,
    UsernameError,
    normalise_username,
)
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType
from tests.dbfixtures import SCHEMA_PREFIX, _connect, _create_schema, _drop_schema

MIGRATION = "0005_web_users.py"
SECRET = base64.b64decode(generate_secret_key())


def _full_width(text: str) -> str:
    """ASCII letters as their full-width compatibility forms (U+FF21...).

    Built rather than written literally, so the source carries no character a
    reader could mistake for the letter it stands for.
    """
    return "".join(chr(ord(character) + 0xFEE0) for character in text)


TABLE: Table = WebUser.__table__  # type: ignore[assignment]


def _row(**overrides: object) -> WebUser:
    now = dt.datetime.now(dt.UTC)
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "username": "dev-operator",
        "password_hash": "$argon2id$v=19$m=65536,t=2,p=1$c2FsdA$dGFn",
        "enabled": True,
        "created_at": now,
        "password_set_at": now,
    }
    fields.update(overrides)
    return WebUser(**fields)


# --- 1.1 What the model declares --------------------------------------------


def test_the_username_is_unique() -> None:
    unique = {
        constraint.name: {column.name for column in constraint.columns}
        for constraint in TABLE.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique["uq_web_user_username"] == {"username"}


def test_both_timestamps_are_instants_with_a_zone() -> None:
    columns = inspect(WebUser).columns
    for name in ("created_at", "password_set_at"):
        kind = columns[name].type
        assert isinstance(kind, DateTime) and kind.timezone, f"{name} must be TIMESTAMPTZ"
        assert columns[name].nullable is False
    assert columns["password_hash"].nullable is False
    assert columns["enabled"].nullable is False


# --- 1.2 What the migration records -----------------------------------------


def test_the_migration_states_what_a_downgrade_costs() -> None:
    source = (migrations.migrations_dir() / "versions" / MIGRATION).read_text()
    assert "``web_user``" in source
    assert "eleventh" in source
    assert "deletes every" in source and "account" in source
    assert "``--web``" in source and "cannot start" in source


# --- 1.4 One normalisation, everywhere --------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("operator", "operator"),
        ("Operator", "operator"),
        ("OPERATOR", "operator"),
        ("dev-Operator_2", "dev-operator_2"),
        # Full-width compatibility forms are the letters they stand for.
        (_full_width("Operator"), "operator"),
        # casefold, not lower: German sharp s folds to "ss".
        ("Stra" + chr(0xDF) + "e", "strasse"),
        # A ligature is NFKC-decomposed.
        (chr(0xFB01) + "le", "file"),
    ],
)
def test_usernames_normalise_to_one_form(given: str, expected: str) -> None:
    assert normalise_username(given) == expected


def test_a_mixed_case_and_a_compatibility_form_name_the_same_account() -> None:
    assert normalise_username("Operator") == normalise_username(_full_width("oPERATOR"))


@pytest.mark.parametrize(
    ("given", "reason"),
    [
        ("", "empty"),
        (" ", "whitespace"),
        ("two words", "whitespace"),
        ("tab\there", "whitespace"),
        ("nul\x00", "control"),
        ("bell\x07", "control"),
        ("zero" + chr(0x200B) + "width", "control"),
        ("x" * (MAX_USERNAME_LENGTH + 1), "at most"),
    ],
)
def test_unusable_usernames_are_refused_saying_why(given: str, reason: str) -> None:
    with pytest.raises(UsernameError) as excinfo:
        normalise_username(given)
    assert reason in str(excinfo.value)


def test_the_cap_is_exactly_the_documented_length() -> None:
    assert normalise_username("x" * MAX_USERNAME_LENGTH) == "x" * MAX_USERNAME_LENGTH


# --- 1.1 Against a real server ----------------------------------------------


async def test_the_server_refuses_a_second_row_with_the_same_username(
    database: Database,
) -> None:
    async with database.sessions() as session:
        session.add(_row())
        await session.commit()
    with pytest.raises(IntegrityError):
        async with database.sessions() as session:
            session.add(_row())
            await session.commit()


async def test_the_server_columns_are_timestamptz(database: Database, test_schema: str) -> None:
    from sqlalchemy import text

    async with database.sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'web_user'"
                ),
                {"schema": test_schema},
            )
        ).tuples()
        kinds = {str(name): str(kind) for name, kind in rows}
    assert kinds["created_at"] == "timestamp with time zone"
    assert kinds["password_set_at"] == "timestamp with time zone"


# --- 1.2 The cycle, in a throwaway schema -----------------------------------


async def test_0005_upgrade_downgrade_upgrade_leaves_no_leftover_objects(
    database_url: str,
) -> None:
    schema = f"{SCHEMA_PREFIX}webuser_cycle"
    config = DatabaseConfig(url=database_url, schema=schema)
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    try:
        await migrations.upgrade_async(config)
        assert await _objects_named_web_user(database_url, schema)

        await migrations.downgrade_async(config, revision="0004")
        assert await _objects_named_web_user(database_url, schema) == set(), (
            "a downgrade to 0004 must leave no table, index or constraint behind"
        )

        await migrations.upgrade_async(config)
        handle = Database(config=config)
        try:
            assert await handle.read_applied_revision() == migrations.expected_revision()
        finally:
            await handle.dispose()
    finally:
        await _drop_schema(database_url, schema)


async def _objects_named_web_user(url: str, schema: str) -> set[str]:
    connection = await _connect(url)
    try:
        rows = await connection.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname LIKE '%web_user%'",
            schema,
        )
        constraints = await connection.fetch(
            "SELECT conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = $1 AND conname LIKE '%web_user%'",
            schema,
        )
        return {row[0] for row in rows} | {row[0] for row in constraints}
    finally:
        await connection.close()


# --- 1.3 A database one revision behind -------------------------------------


async def test_a_database_at_0004_is_refused_naming_both_revisions_and_the_command(
    database_url: str,
) -> None:
    schema = f"{SCHEMA_PREFIX}webuser_behind"
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    config = DatabaseConfig(url=database_url, schema=schema)
    await migrations.upgrade_async(config, revision="0004")
    handle = Database(config=config)
    try:
        with pytest.raises(SchemaVersionError) as excinfo:
            await handle.open()
        message = str(excinfo.value)
        assert "0004" in message
        assert migrations.expected_revision() in message
        assert migrations.RESTART_TO_MIGRATE in message
    finally:
        await handle.dispose()
        await _drop_schema(database_url, schema)


# --- 1.4 The default chat identity the account row carries -------------------


async def test_removing_an_identity_clears_every_default_naming_it(
    database: Database,
) -> None:
    """`ON DELETE SET NULL` is the cleanup (migration 0009).

    A default that outlived the identity it names would be a composer quietly
    posting as something else, so the constraint is asserted against a real
    server rather than trusted from the model: a cascade SQLAlchemy declares and
    Postgres does not enforce is a cascade that does not exist.
    """
    entities = EntityRepository(database=database)
    stored = await entities.store(
        name="dev-companion",
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.CHAT,
    )
    assert isinstance(stored, Succeeded)
    entity = stored.value

    async with database.sessions() as session:
        session.add(_row(default_entity_id=entity.id))
        await session.commit()

    removed = await entities.remove(entity.public_key)
    assert isinstance(removed, Succeeded)
    assert removed.value

    async with database.sessions() as session:
        held = (
            await session.execute(select(WebUser).where(WebUser.username == "dev-operator"))
        ).scalar_one()
        assert held.default_entity_id is None, (
            "removing an identity must clear the default of every account naming it"
        )
