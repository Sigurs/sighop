"""The stored preferred first hop (preferred-first-hop 3.2, design D4)."""

from __future__ import annotations

import pytest

from sighop.db.engine import Database
from sighop.db.repositories import RoutePreferenceError, RoutePreferenceRepository
from tests.test_repeater_repositories import NOW, REPEATER, _value


async def test_an_absent_row_reads_as_none(database: Database) -> None:
    assert _value(await RoutePreferenceRepository(database=database).get()) is None


async def test_a_preference_is_stored_cleared_and_read_back(database: Database) -> None:
    repository = RoutePreferenceRepository(database=database)
    assert _value(await repository.save(REPEATER, at=NOW)) == REPEATER
    assert _value(await repository.get()) == REPEATER
    assert _value(await repository.save(None, at=NOW)) is None
    assert _value(await repository.get()) is None


async def test_a_key_that_is_not_32_bytes_is_refused_and_nothing_stored(
    database: Database,
) -> None:
    repository = RoutePreferenceRepository(database=database)
    _value(await repository.save(REPEATER, at=NOW))
    with pytest.raises(RoutePreferenceError, match="32 bytes"):
        await repository.save(b"\x42", at=NOW)
    assert _value(await repository.get()) == REPEATER
