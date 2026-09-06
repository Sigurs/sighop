"""Bots in the runtime (`bot-runtime`, `runtime-cli`, groups 9 and 12).

These are the only bot tests that need Postgres, and they need it for the right
reason: what is under test is that a bot *bound to a stored identity* is loaded,
reported and run, and both halves of that sentence are database facts. Everything
about dispatch, limits, modes and the greeter's gates runs against the in-memory
fixtures in `tests/botfixtures.py`.

Group 12's regressions are here too, and they are the ones that would be
embarrassing to discover later: bots must not change the reception path, must
not need Postgres to be tested, and — in observe mode — must produce zero
submissions across a whole corpus replay, whatever the driver decides.
"""

from __future__ import annotations

import ast
import base64
import io
from pathlib import Path

import pytest

import sighop
from sighop.bots.base import BotMode
from sighop.bots.runtime import BotWorker
from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import EntityRepository
from sighop.monitor.render import BOTS_OFF
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType
from sighop.runtime import RuntimeConfig
from tests.protocol.corpus import CAPTURE_FILES, CAPTURES_DIR
from tests.test_runtime import _events, run_briefly, runtime

SECRET = base64.b64decode(generate_secret_key())
CAPTURE = CAPTURES_DIR / CAPTURE_FILES[0]


async def _stored_bot(
    database: Database,
    *,
    name: str = "greeter-bot",
    enabled: bool = True,
    entity_enabled: bool = True,
    mode: str = "observe",
    driver: str = "greeter",
):
    """A stored bot identity with a bot bound to it."""
    entities = EntityRepository(database=database)
    stored = await entities.store(
        name=name,
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.CHAT,
        entity_type="bot",
        enabled=entity_enabled,
    )
    assert isinstance(stored, Succeeded)

    persistence = Persistence(database=database)
    from sighop.bots import drivers as bot_drivers

    created = await persistence.bots.create(
        entity_id=stored.value.id,
        driver=driver,
        config=bot_drivers.default_config("greeter"),
        mode=mode,
        enabled=enabled,
        entity_name=name,
    )
    assert isinstance(created, Succeeded)
    loaded = await entities.load_all(SECRET, enabled_only=entity_enabled)
    assert isinstance(loaded, Succeeded)
    return persistence, created.value, loaded.value


# --- 9.2 No database, no bots -----------------------------------------------


async def test_a_run_with_no_database_says_bots_require_durable_storage() -> None:
    """9.2, design D5: §7 rule 1 is a statement about a table that does not
    exist without one, so a DB-less greeter would greet the whole neighbourhood
    after every restart. Stated rather than omitted."""
    run = runtime(_events(CAPTURE), out=io.StringIO())

    assert len(run.bots) == 0
    assert run._bot_lines() == [BOTS_OFF]


async def test_a_replay_run_with_no_database_wires_no_bots_at_all() -> None:
    """9.2: not merely "runs none" — the host has no workers to offer to."""
    out = io.StringIO()
    run = runtime(_events(CAPTURE), out=out)
    await run_briefly(run)

    assert run.bots.workers == []
    assert BOTS_OFF in out.getvalue()


@pytest.mark.database
async def test_a_run_with_a_database_and_no_bots_says_none_are_configured(
    database: Database,
) -> None:
    run = runtime(_events(CAPTURE), out=io.StringIO(), persistence=Persistence(database=database))
    await run._restore()

    assert run.bots.workers == []
    assert run._bot_lines() == ["bots: none configured"]


# --- 9.1 / 9.3 Loading, binding and reporting -------------------------------


@pytest.mark.database
async def test_a_bot_is_run_and_reported_before_any_traffic(database: Database) -> None:
    """9.3: the identity, the driver, the mode and the limits, at startup."""
    persistence, _record, loaded = await _stored_bot(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()

    assert len(run.bots) == 1
    worker = run.bots.workers[0]
    assert worker.name == "greeter-bot"
    assert worker.mode is BotMode.OBSERVE
    assert worker.entity is not None

    (line,) = run._bot_lines()
    assert "bot 'greeter-bot'" in line
    assert "driver=greeter" in line
    assert "mode=observe" in line
    assert "max_hops=1" in line
    assert "rate_per_hour=" in line


@pytest.mark.database
async def test_a_disabled_bot_is_reported_as_not_running_and_receives_nothing(
    database: Database,
) -> None:
    """9.3: silent-because-disabled must not look like silent-by-decision."""
    persistence, _record, loaded = await _stored_bot(database, enabled=False)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()

    assert run.bots.workers == []
    (line,) = run._bot_lines()
    assert "NOT RUNNING" in line
    assert "the bot is disabled" in line


@pytest.mark.database
async def test_a_bot_on_a_disabled_identity_is_reported_with_that_reason(
    database: Database,
) -> None:
    persistence, _record, _loaded = await _stored_bot(database, entity_enabled=False)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600),
        persistence=persistence,
    )
    await run._restore()

    assert run.bots.workers == []
    (line,) = run._bot_lines()
    assert "NOT RUNNING" in line
    assert "identity was not loaded" in line


@pytest.mark.database
async def test_a_bot_whose_identity_serves_a_room_is_refused_with_that_reason(
    database: Database,
) -> None:
    """9.1: an identity has one role, and the run says which one it already has.

    The repository refuses this at creation; the runtime refuses it again at
    load, because a row can be made another way — a restored dump, a hand-edited
    database — and the run must not decrypt one packet twice for two components.
    """
    from sighop.passwords import hash_password

    entities = EntityRepository(database=database)
    identity = generate_identity()
    stored = await entities.store(
        name="dual", identity=identity, secret=SECRET, node_type=NodeType.CHAT, entity_type="bot"
    )
    assert isinstance(stored, Succeeded)
    persistence = Persistence(database=database)
    from sighop.bots import drivers as bot_drivers

    assert isinstance(
        await persistence.bots.create(
            entity_id=stored.value.id,
            driver="greeter",
            config=bot_drivers.default_config("greeter"),
            entity_name="dual",
        ),
        Succeeded,
    )
    # A room arrives on the same identity afterwards, which `bot create` would
    # have refused and a restored dump would not.
    assert isinstance(
        await persistence.rooms.create(
            entity_id=stored.value.id, name="lounge", admin_password_hash=hash_password("pw")
        ),
        Succeeded,
    )
    loaded = await entities.load_all(SECRET, enabled_only=True)
    assert isinstance(loaded, Succeeded)

    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded.value)
        ),
        persistence=persistence,
    )
    await run._restore()

    assert run.bots.workers == []
    line = next(item for item in run._bot_lines() if "NOT RUNNING" in item)
    assert "serves a room" in line
    assert "one role" in line


@pytest.mark.database
async def test_a_bot_naming_a_driver_this_build_lacks_is_reported_not_fatal(
    database: Database,
) -> None:
    """9.1: the other bots are fine, and the operator gets the name to fix."""
    persistence, _record, loaded = await _stored_bot(database, driver="weather")
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()

    assert run.bots.workers == []
    (line,) = run._bot_lines()
    assert "weather" in line
    assert "greeter" in line, "and what this build does have"


# --- 9.4 Lifecycle ----------------------------------------------------------


@pytest.mark.database
async def test_bot_workers_start_and_stop_with_the_run(database: Database) -> None:
    """9.4: no pending task survives a stop, and a shutdown does not raise."""
    persistence, _record, loaded = await _stored_bot(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )

    await run_briefly(run)

    assert len(run.bots) == 1
    assert run.bots.workers[0]._task is None, "the worker task was stopped, not abandoned"


async def test_stopping_a_host_with_no_bots_is_not_an_error() -> None:
    from sighop.bots.runtime import BotHost

    host = BotHost(workers=[])
    host.start()
    await host.stop()


# --- 9.5 The periodic status line -------------------------------------------


@pytest.mark.database
async def test_the_status_report_includes_every_per_bot_counter(
    database: Database,
) -> None:
    """9.5: actions, observations, suppressions by reason, drops and failures."""
    from sighop.bots.base import SuppressionReason

    persistence, _record, loaded = await _stored_bot(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()
    worker = run.bots.workers[0]
    worker.counters.actions = 2
    worker.counters.observations = 3
    worker.counters.dropped = 1
    worker.counters.failures = 1
    worker.counters.suppress(SuppressionReason.TOO_MANY_HOPS)

    (line,) = run._bot_status_lines()

    assert "acted=2" in line
    assert "observed=3" in line
    assert "dropped=1" in line
    assert "failures=1" in line
    assert "too_many_hops=1" in line


# --- 12.1 / 12.3 Boundaries -------------------------------------------------


def test_the_protocol_package_gained_nothing_this_milestone() -> None:
    """12.1: a greeting is a `TXT_MSG` composed by code that existed in
    milestone 4, so `protocol/` puts no new byte on the wire.

    `tests/protocol/test_import_boundary.py` keeps `protocol/` from importing
    outwards; this asserts the other direction — that nothing in it grew a
    dependency on bots.
    """
    package = Path(sighop.__file__).parent / "protocol"
    offenders = []
    for path in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any("bots" in name for name in names):
                offenders.append(f"{path.name}:{getattr(node, 'lineno', 0)}")

    assert not offenders


def test_no_bot_module_imports_sqlalchemy() -> None:
    """12.3: the storage seam is protocols, which is what keeps every dispatch,
    limit, mode and gate test runnable with no database configured."""
    package = Path(sighop.__file__).parent / "bots"
    offenders = []
    for path in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(name.startswith("sqlalchemy") for name in names):
                offenders.append(f"{path.name}:{getattr(node, 'lineno', 0)}")

    assert not offenders


# --- 12.2 / 12.4 The reception path, with and without a bot -----------------


def _pipeline_counts(run) -> dict[str, int]:
    dedup = run.pipeline.dedup.stats
    return {
        "considered": dedup.considered,
        "passed_through": dedup.passed_through,
        "duplicates": dedup.duplicates,
        "contacts": len(run.contacts),
        "paths": run.pipeline.paths.destination_count,
    }


class GreedyDriver:
    """A driver that tries to send for every advert it is handed.

    Deliberately worse-behaved than the greeter: 12.4 is about what the
    *runtime* guarantees in observe mode, "whatever its driver decides", so the
    driver under it should decide to act as often as it possibly can.
    """

    driver_name = "greedy"

    def __init__(self) -> None:
        self.decisions = 0

    @staticmethod
    def default_config() -> dict[str, object]:
        return {}

    @staticmethod
    def validate_config(key: str, value: str) -> object:
        return value

    def limits(self) -> dict[str, object]:
        return {}

    def counters(self) -> dict[str, int]:
        return {"decisions": self.decisions}

    async def on_advert(self, context, event) -> None:
        self.decisions += 1
        await context.send(event.contact, "hello")

    async def on_direct_message(self, context, event) -> None:
        return


async def test_a_wired_bot_changes_no_reception_count() -> None:
    """12.2: the same corpus, with and without a bot, delivers the same counts.

    The bot rides on the contact store's listener and the messenger's reports,
    so if it changed a count it would mean driver work had leaked onto the
    reception path.
    """
    from tests.botfixtures import MemoryBotStorage, RecordingSender, bot_record

    plain = runtime(_events(CAPTURE), out=io.StringIO())
    await run_briefly(plain)

    wired = runtime(_events(CAPTURE), out=io.StringIO())
    wired.bots.add(
        BotWorker(
            record=bot_record(driver="greedy", mode="observe", config={}),
            driver=GreedyDriver(),
            storage=MemoryBotStorage(),
            send_message=RecordingSender(),
            route_known=lambda contact: True,
            lookup=wired.contacts.get,
            capacity=4096,
        )
    )
    await run_briefly(wired)

    assert _pipeline_counts(plain) == _pipeline_counts(wired)


async def test_an_observe_mode_bot_submits_nothing_across_a_whole_replay() -> None:
    """12.4: zero submissions, whatever the driver decides.

    Counted at the messenger's own seam rather than by reading the mode back:
    the claim is about the scheduler, so it is asserted where a submission would
    have reached it.
    """
    from tests.botfixtures import MemoryBotStorage, RecordingSender, bot_record

    sender = RecordingSender()
    driver = GreedyDriver()
    run = runtime(_events(CAPTURE), out=io.StringIO())
    worker = BotWorker(
        record=bot_record(driver="greedy", mode="observe", config={"rate_per_hour": 1e6, "burst": 10_000}),
        driver=driver,
        storage=MemoryBotStorage(),
        send_message=sender,
        route_known=lambda contact: True,
        lookup=run.contacts.get,
        capacity=4096,
    )
    run.bots.add(worker)

    await run_briefly(run, turns=400)

    assert driver.decisions > 0, "the driver did decide to act"
    assert sender.sent == [], "and nothing reached the scheduler"
    assert worker.counters.actions == 0
    assert worker.counters.observations == driver.decisions
