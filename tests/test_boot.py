"""The entry point (`node-boot`, design D1-D3).

sighop has no command line. These tests pin what that means from the outside:
an argument is refused rather than interpreted, an incomplete environment is
reported whole and starts nothing, and a complete one reaches the node — with
the migrations applied on the way, every time, and never a second time.
"""

from __future__ import annotations

import io

import pytest

from sighop import boot
from sighop.config import (
    RADIO_PRESET_NAMES,
    SECRET_KEY_COMMAND,
    Config,
    DatabaseConfig,
    generate_secret_key,
)
from sighop.db import migrations
from sighop.db.engine import Database, SchemaVersionError
from tests.dbfixtures import _create_schema, _drop_schema

VARIABLES = (
    "DATABASE_URL",
    "SIGHOP_SECRET_KEY",
    "SIGHOP_MODEM",
    "SIGHOP_STATUS_INTERVAL",
    "SIGHOP_PATH_HASH_SIZE",
)


@pytest.fixture
def bare_environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No sighop variable set, whatever the developer's shell holds."""
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[Config]:
    """Replace the node with a recorder: these tests are about getting there."""
    reached: list[Config] = []

    async def run(config: Config, out: object = None) -> int:
        reached.append(config)
        return 0

    monkeypatch.setattr(boot, "run", run)
    return reached


# --- Arguments are refused ---------------------------------------------------


@pytest.mark.parametrize("argv", [["run"], ["--help"], ["run", "--device", "/dev/modem"]])
def test_any_argument_is_refused_and_nothing_starts(
    argv: list[str], ran: list[Config], capsys: pytest.CaptureFixture[str]
) -> None:
    assert boot.main(argv) == 2
    assert ran == []
    assert "takes no arguments" in capsys.readouterr().err


# --- An incomplete environment ------------------------------------------------


def test_an_empty_environment_reports_every_problem_once_and_starts_nothing(
    bare_environment: pytest.MonkeyPatch, ran: list[Config], capsys: pytest.CaptureFixture[str]
) -> None:
    assert boot.main([]) == 2
    assert ran == []
    err = capsys.readouterr().err
    for name in ("DATABASE_URL", "SIGHOP_SECRET_KEY", "SIGHOP_MODEM"):
        assert err.count(f"{name} is not set") == 1, name


def test_a_missing_secret_prints_the_command_that_generates_one(
    bare_environment: pytest.MonkeyPatch,
    ran: list[Config],
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bare_environment.setenv("DATABASE_URL", database_url)
    bare_environment.setenv("SIGHOP_MODEM", "/dev/serial/by-id/usb-modem")

    assert boot.main([]) == 2
    assert ran == []
    err = capsys.readouterr().err
    assert SECRET_KEY_COMMAND in err
    assert "DATABASE_URL" not in err, "a configured database is not a problem"


def test_a_malformed_setting_names_itself_and_starts_nothing(
    bare_environment: pytest.MonkeyPatch,
    ran: list[Config],
    database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bare_environment.setenv("DATABASE_URL", database_url)
    bare_environment.setenv("SIGHOP_SECRET_KEY", generate_secret_key())
    bare_environment.setenv("SIGHOP_MODEM", "/dev/serial/by-id/usb-modem")
    bare_environment.setenv("SIGHOP_STATUS_INTERVAL", "soon")

    assert boot.main([]) == 2
    assert ran == []
    assert "SIGHOP_STATUS_INTERVAL is 'soon'" in capsys.readouterr().err


# --- A complete environment ---------------------------------------------------


def test_a_complete_environment_reaches_the_node(
    bare_environment: pytest.MonkeyPatch, ran: list[Config], database_url: str
) -> None:
    bare_environment.setenv("DATABASE_URL", database_url)
    bare_environment.setenv("SIGHOP_SECRET_KEY", generate_secret_key())
    bare_environment.setenv("SIGHOP_MODEM", "/dev/serial/by-id/usb-modem")

    assert boot.main([], out=io.StringIO()) == 0
    (config,) = ran
    assert config.modem == "/dev/serial/by-id/usb-modem"
    assert config.transmit_enabled is False


def test_every_accepted_preset_resolves_to_radio_parameters() -> None:
    """`config.py` validates the name and `boot.py` resolves it; they must agree,
    or a name the environment accepts would fail when the modem is opened."""
    assert set(RADIO_PRESET_NAMES) == set(boot.RADIO_PRESETS)


# --- Migrations are applied by starting (node-boot, database) -----------------


async def test_starting_brings_an_empty_database_to_head_and_a_second_start_applies_nothing(
    database_url: str,
) -> None:
    """Starting is the deploy: there is no flag to pass and no command to run."""
    schema = "sighop_test_boot_migrate"
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    config = Config(
        database=DatabaseConfig(url=database_url, schema=schema),
        secret_key=generate_secret_key(),
    )
    handle = Database(config=config.database)
    try:
        for _ in range(2):
            await boot.migrate_on_start(config)
            assert await handle.read_applied_revision() == migrations.expected_revision()
            persistence, stored = await boot.open_persistence(config)
            assert stored == ()
            await persistence.stop()
    finally:
        await handle.dispose()
        await _drop_schema(database_url, schema)


async def test_a_database_ahead_of_the_code_is_left_alone_and_refused(database_url: str) -> None:
    """Only *behind* is fixed by starting. Nothing in this build describes a
    newer schema, so it is neither migrated nor operated against."""
    schema = "sighop_test_boot_ahead"
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    config = Config(
        database=DatabaseConfig(url=database_url, schema=schema),
        secret_key=generate_secret_key(),
    )
    handle = Database(config=config.database)
    try:
        await boot.migrate_on_start(config)
        async with handle.engine.begin() as connection:
            from sqlalchemy import text

            await connection.execute(
                text("UPDATE alembic_version SET version_num = 'ffffffffffff'")
            )

        await boot.migrate_on_start(config)  # leaves it alone
        assert await handle.read_applied_revision() == "ffffffffffff"
        with pytest.raises(SchemaVersionError, match="run a build that knows it"):
            await boot.open_persistence(config)
    finally:
        await handle.dispose()
        await _drop_schema(database_url, schema)


@pytest.mark.parametrize("value", ["4", "three"])
def test_an_invalid_path_hash_size_refuses_the_start_and_starts_nothing(
    bare_environment: pytest.MonkeyPatch,
    ran: list[Config],
    database_url: str,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    """Before the pipeline exists, so nothing could go out at a width not asked for."""
    bare_environment.setenv("DATABASE_URL", database_url)
    bare_environment.setenv("SIGHOP_SECRET_KEY", generate_secret_key())
    bare_environment.setenv("SIGHOP_MODEM", "/dev/serial/by-id/usb-modem")
    bare_environment.setenv("SIGHOP_PATH_HASH_SIZE", value)

    assert boot.main([]) == 2
    assert ran == []
    err = capsys.readouterr().err
    assert "SIGHOP_PATH_HASH_SIZE" in err
    assert repr(value) in err
    assert "1, 2, 3" in err
