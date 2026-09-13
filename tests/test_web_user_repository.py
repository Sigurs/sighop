"""`WebUserRepository` (tasks 2.1-2.3, milestone 9 design D1).

Each operation against a real server, plus the one property that needs none:
no rendering of an account carries its hash.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from sighop.db.engine import Database, Failed, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    UsernameError,
    WebUserExistsError,
    WebUserRecord,
    WebUserRepository,
)

HASH = "$argon2id$v=19$m=65536,t=2,p=1$c2FsdHNhbHQ$dGFndGFndGFn"
OTHER_HASH = "$argon2id$v=19$m=65536,t=2,p=1$b3RoZXJzYWx0$b3RoZXJ0YWc"


def _value[T](outcome: Succeeded[T] | Failed) -> T:
    assert isinstance(outcome, Succeeded), outcome
    return outcome.value


# --- 2.1 Each operation -----------------------------------------------------


@pytest.mark.database
async def test_add_stores_an_enabled_account_under_the_normalised_name(
    database: Database,
) -> None:
    users = WebUserRepository(database=database)
    record = _value(await users.add("Dev-Operator", password_hash=HASH))
    assert record.username == "dev-operator"
    assert record.enabled
    assert record.created_at == record.password_set_at
    assert record.created_at.tzinfo is not None

    stored = _value(await users.get("DEV-OPERATOR"))
    assert stored is not None
    assert stored.id == record.id
    assert stored.password_hash == HASH


@pytest.mark.database
async def test_a_username_differing_only_in_case_is_refused_naming_the_existing_one(
    database: Database,
) -> None:
    users = WebUserRepository(database=database)
    first = _value(await users.add("dev-operator", password_hash=HASH))
    with pytest.raises(WebUserExistsError) as excinfo:
        await users.add("DEV-Operator", password_hash=OTHER_HASH)
    message = str(excinfo.value)
    assert "'dev-operator'" in message
    assert str(first.id) in message
    assert HASH not in message and OTHER_HASH not in message
    stored = _value(await users.get("dev-operator"))
    assert stored is not None and stored.password_hash == HASH, "the stored row is unchanged"


@pytest.mark.database
async def test_get_answers_none_for_an_unknown_account(database: Database) -> None:
    assert _value(await WebUserRepository(database=database).get("nobody")) is None


@pytest.mark.database
async def test_list_returns_every_account_oldest_first(database: Database) -> None:
    users = WebUserRepository(database=database)
    earlier = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    later = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)
    _value(await users.add("dev-second", password_hash=HASH, created_at=later))
    _value(await users.add("dev-first", password_hash=HASH, created_at=earlier))
    listed = _value(await users.list())
    assert [record.username for record in listed] == ["dev-first", "dev-second"]


@pytest.mark.database
async def test_set_password_replaces_the_hash_and_moves_the_epoch(database: Database) -> None:
    users = WebUserRepository(database=database)
    created = _value(
        await users.add(
            "dev-operator", password_hash=HASH, created_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
        )
    )
    changed_at = dt.datetime(2026, 9, 12, 12, tzinfo=dt.UTC)
    assert _value(await users.set_password("Dev-Operator", password_hash=OTHER_HASH, at=changed_at))
    stored = _value(await users.get("dev-operator"))
    assert stored is not None
    assert stored.password_hash == OTHER_HASH
    assert stored.password_set_at == changed_at
    assert stored.created_at == created.created_at
    assert not _value(await users.set_password("nobody", password_hash=HASH))


@pytest.mark.database
async def test_set_enabled_disables_and_enables(database: Database) -> None:
    users = WebUserRepository(database=database)
    _value(await users.add("dev-operator", password_hash=HASH))
    assert _value(await users.set_enabled("dev-operator", False))
    stored = _value(await users.get("dev-operator"))
    assert stored is not None and not stored.enabled
    assert _value(await users.set_enabled("DEV-OPERATOR", True))
    stored = _value(await users.get("dev-operator"))
    assert stored is not None and stored.enabled
    assert not _value(await users.set_enabled("nobody", True))


@pytest.mark.database
async def test_remove_deletes_the_row(database: Database) -> None:
    users = WebUserRepository(database=database)
    _value(await users.add("dev-operator", password_hash=HASH))
    assert _value(await users.remove("Dev-Operator"))
    assert _value(await users.get("dev-operator")) is None
    assert not _value(await users.remove("dev-operator"))


@pytest.mark.database
async def test_count_enabled_counts_only_enabled_accounts(database: Database) -> None:
    users = WebUserRepository(database=database)
    assert _value(await users.count_enabled()) == 0
    _value(await users.add("dev-one", password_hash=HASH))
    _value(await users.add("dev-two", password_hash=HASH))
    _value(await users.add("dev-three", password_hash=HASH, enabled=False))
    assert _value(await users.count_enabled()) == 2


@pytest.mark.database
async def test_persistence_exposes_the_repository(database: Database) -> None:
    persistence = Persistence(database=database)
    _value(await persistence.web_users.add("dev-operator", password_hash=HASH))
    assert _value(await persistence.web_users.count_enabled()) == 1


async def test_an_unusable_username_is_refused_before_the_database_is_touched() -> None:
    class Untouchable:
        async def run(self, *_: object) -> object:  # pragma: no cover - must not run
            raise AssertionError("the database was reached")

    users = WebUserRepository(database=Untouchable())  # type: ignore[arg-type]
    with pytest.raises(UsernameError):
        await users.get("two words")


# --- 2.3 No rendering carries the hash --------------------------------------


def test_no_rendering_of_an_account_carries_its_hash() -> None:
    now = dt.datetime.now(dt.UTC)
    record = WebUserRecord(
        id=uuid.uuid4(),
        username="dev-operator",
        password_hash=HASH,
        enabled=True,
        created_at=now,
        password_set_at=now,
    )
    for rendering in (repr(record), str(record), str(record.as_json()), f"{record!r}"):
        assert HASH not in rendering
        assert "$argon2id$" not in rendering
    assert "dev-operator" in repr(record)
    assert set(record.as_json()) == {
        "web_user_id",
        "username",
        "enabled",
        "created_at",
        "password_set_at",
    }
