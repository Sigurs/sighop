"""`WebUserRepository` (tasks 2.1-2.3, milestone 9 design D1).

Each operation against a real server, plus the one property that needs none:
no rendering of an account carries its hash.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sighop.config import generate_secret_key
from sighop.db.engine import Database, Failed, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    EntityRepository,
    UnknownIdentityError,
    UsernameError,
    WebUserExistsError,
    WebUserRecord,
    WebUserRepository,
)
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType

SECRET = base64.b64decode(generate_secret_key())
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
async def test_count_counts_every_account_enabled_or_not(database: Database) -> None:
    users = WebUserRepository(database=database)
    assert _value(await users.count()) == 0
    _value(await users.add("dev-one", password_hash=HASH))
    _value(await users.add("dev-two", password_hash=HASH, enabled=False))
    assert _value(await users.count()) == 2


# --- web-first-run-setup 1.2/1.3 `add_first` (design D6) ---------------------


@dataclass
class _GatedDatabase:
    """A `Database` whose unit of work pauses, lock held, before it commits.

    Only `operation` is gated; anything else the repository runs first (`add`
    reads before it writes) passes straight through. The work runs inside the
    real transaction; `holding` is set once it has returned (its locks taken,
    its row flushed) and the commit waits for `release`. That is the window a
    concurrent writer has to be seen waiting in.
    """

    inner: Database
    operation: str
    holding: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def run[T](
        self, operation: str, work: Callable[[AsyncSession], Awaitable[T]]
    ) -> Succeeded[T] | Failed:
        if operation != self.operation:
            return await self.inner.run(operation, work)

        async def gated(session: AsyncSession) -> T:
            result = await work(session)
            await session.flush()
            self.holding.set()
            await self.release.wait()
            return result

        return await self.inner.run(operation, gated)


async def _still_waiting(task: asyncio.Task[object], seconds: float = 0.5) -> bool:
    done, _ = await asyncio.wait({task}, timeout=seconds)
    return not done


@pytest.mark.database
async def test_add_first_inserts_an_enabled_account_into_an_empty_table(
    database: Database,
) -> None:
    users = WebUserRepository(database=database)
    record = _value(await users.add_first("Dev-Operator", password_hash=HASH))
    assert record is not None
    assert record.username == "dev-operator" and record.enabled
    stored = _value(await users.get("dev-operator"))
    assert stored is not None and stored.id == record.id and stored.password_hash == HASH


@pytest.mark.database
@pytest.mark.parametrize("enabled", [True, False])
async def test_add_first_inserts_nothing_beside_any_existing_account(
    database: Database, enabled: bool
) -> None:
    users = WebUserRepository(database=database)
    _value(await users.add("dev-existing", password_hash=HASH, enabled=enabled))
    assert _value(await users.add_first("dev-setup", password_hash=OTHER_HASH)) is None
    assert [r.username for r in _value(await users.list())] == ["dev-existing"]


@pytest.mark.database
async def test_two_concurrent_add_first_calls_leave_exactly_one_row(database: Database) -> None:
    gated = _GatedDatabase(inner=database, operation="add_first_web_user")
    first = asyncio.create_task(
        WebUserRepository(database=gated).add_first("dev-first", password_hash=HASH)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(gated.holding.wait(), 5)
    second = asyncio.create_task(
        WebUserRepository(database=database).add_first("dev-second", password_hash=HASH)
    )
    assert await _still_waiting(second), "the second setup waits on the first's table lock"
    gated.release.set()
    first_record = _value(await first)
    assert first_record is not None and first_record.username == "dev-first"
    assert _value(await second) is None
    users = WebUserRepository(database=database)
    assert [r.username for r in _value(await users.list())] == ["dev-first"]


@pytest.mark.database
async def test_a_plain_add_waits_for_a_setup_holding_its_lock(database: Database) -> None:
    gated = _GatedDatabase(inner=database, operation="add_first_web_user")
    setup = asyncio.create_task(
        WebUserRepository(database=gated).add_first("dev-setup", password_hash=HASH)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(gated.holding.wait(), 5)
    terminal = asyncio.create_task(
        WebUserRepository(database=database).add("dev-terminal", password_hash=OTHER_HASH)
    )
    assert await _still_waiting(terminal), "the insert waits for setup to commit"
    gated.release.set()
    assert _value(await setup) is not None
    _value(await terminal)
    listed = _value(await WebUserRepository(database=database).list())
    assert [r.username for r in listed] == ["dev-setup", "dev-terminal"]


@pytest.mark.database
async def test_a_setup_waits_for_a_plain_add_holding_its_insert(database: Database) -> None:
    gated = _GatedDatabase(inner=database, operation="add_web_user")
    terminal = asyncio.create_task(
        WebUserRepository(database=gated).add("dev-terminal", password_hash=OTHER_HASH)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(gated.holding.wait(), 5)
    setup = asyncio.create_task(
        WebUserRepository(database=database).add_first("dev-setup", password_hash=HASH)
    )
    assert await _still_waiting(setup), "the table lock waits for the uncommitted insert"
    gated.release.set()
    _value(await terminal)
    assert _value(await setup) is None
    listed = _value(await WebUserRepository(database=database).list())
    assert [r.username for r in listed] == ["dev-terminal"]


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
        "default_entity_id",
    }


# --- 2.1 The default chat identity ------------------------------------------


@pytest.mark.database
async def test_the_default_identity_is_set_cleared_and_read_back(
    database: Database,
) -> None:
    users = WebUserRepository(database=database)
    _value(await users.add("dev-operator", password_hash=HASH))
    entity = _value(
        await EntityRepository(database=database).store(
            name="dev-companion",
            identity=generate_identity(),
            secret=SECRET,
            node_type=NodeType.CHAT,
        )
    )

    assert _value(await users.get("dev-operator")).default_entity_id is None
    assert _value(await users.set_default_identity("Dev-Operator", entity.id))
    assert _value(await users.get("dev-operator")).default_entity_id == entity.id

    assert _value(await users.set_default_identity("dev-operator", None))
    assert _value(await users.get("dev-operator")).default_entity_id is None


@pytest.mark.database
async def test_setting_a_default_for_an_account_that_is_not_there_reports_false(
    database: Database,
) -> None:
    users = WebUserRepository(database=database)
    assert not _value(await users.set_default_identity("nobody", None))


@pytest.mark.database
async def test_an_identity_no_row_holds_is_refused_naming_it(database: Database) -> None:
    """The refusal is the repository's, not the foreign key's: the caller is a
    person choosing from a list, and an integrity error is not an answer."""
    users = WebUserRepository(database=database)
    _value(await users.add("dev-operator", password_hash=HASH))
    unknown = uuid.uuid4()

    with pytest.raises(UnknownIdentityError) as excinfo:
        await users.set_default_identity("dev-operator", unknown)

    assert str(unknown) in str(excinfo.value)
    assert _value(await users.get("dev-operator")).default_entity_id is None
