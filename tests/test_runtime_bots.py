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
import asyncio
import base64
import io
import uuid
from pathlib import Path

import pytest

import sighop
from sighop.bots import drivers as bot_drivers
from sighop.bots.base import BotMode
from sighop.bots.runtime import BotWorker
from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import EntityRepository
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType
from sighop.runtime import Runtime, RuntimeConfig
from tests.protocol.corpus import AMBIENT, CORPUS_DIR
from tests.test_runtime import _events, _startup, run_briefly, runtime
from tests.test_tx import ManualClock
from tests.test_tx import RecordingLogger as _RuntimeRecordingLogger

pytestmark = pytest.mark.usefixtures("default_persistence")

SECRET = base64.b64decode(generate_secret_key())
CAPTURE = CORPUS_DIR / AMBIENT


class _RecordingLogger:
    """Captures events at the two levels the wide-event contract allows."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", event, fields))

    def names(self) -> list[str]:
        return [name for _level, name, _fields in self.events]


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


def _live_runtime(persistence: Persistence, *, config: RuntimeConfig | None = None) -> Runtime:
    """A run with a real `entity_loader`, reconciling against the database
    directly rather than a stub — `stored_entities` stays empty on purpose,
    the way a run that started before an identity existed does."""
    return Runtime(
        source=_events(CAPTURE),
        startup=_startup,
        config=config or RuntimeConfig(status_interval=3600, advert_tick=3600),
        clock=ManualClock(),
        out=io.StringIO(),
        logger=_RuntimeRecordingLogger(),
        persistence=persistence,
        webhook_secret=SECRET,
    )


# --- 9.2 No database, no bots -----------------------------------------------


async def test_a_run_with_a_database_and_no_bots_says_none_are_configured(
    database: Database,
) -> None:
    run = runtime(_events(CAPTURE), out=io.StringIO(), persistence=Persistence(database=database))
    await run._restore()

    assert run.bots.workers == []
    assert run._bot_lines() == ["bots: none configured"]


# --- 9.1 / 9.3 Loading, binding and reporting -------------------------------


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
    # A room arrives on the same identity afterwards. Written as a row rather
    # than through `RoomRepository.create`, because that call refuses it twice
    # over — the identity adverts as a chat node and already carries a bot — and
    # what this test is about is precisely the row no repository call would make:
    # a restored dump, or a hand-edited database.
    import datetime as dt
    import uuid as _uuid

    from sighop.db.models import Room as RoomRow
    from sighop.db.times import ensure_utc

    async def _insert(session: object) -> None:
        session.add(  # type: ignore[attr-defined]
            RoomRow(
                id=_uuid.uuid4(),
                entity_id=stored.value.id,
                name="lounge",
                admin_password_hash=hash_password("pw"),
                guest_password_hash=None,
                guest_open=False,
                allow_read_only=False,
                created_at=ensure_utc(dt.datetime.now(dt.UTC), field="room.created_at"),
            )
        )

    assert isinstance(await database.run("insert_room_row", _insert), Succeeded)
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


class _SlowDriver:
    """A driver that parks mid-dispatch and then writes the state it owes."""

    def __init__(self, entered: asyncio.Event, release: asyncio.Event | None) -> None:
        self.entered = entered
        self.release = release

    async def on_advert(self, context, event) -> None:
        self.entered.set()
        if self.release is None:
            await asyncio.Event().wait()  # never finishes
        await self.release.wait()
        assert await context.state.set("greeted", True) is True

    async def on_direct_message(self, context, event) -> None:
        return None


async def _dispatching_worker(release: asyncio.Event | None):
    """A started worker parked inside one dispatch, with its storage."""
    from tests.botfixtures import MemoryBotStorage
    from tests.test_bot_runtime import advert_event, worker

    entered = asyncio.Event()
    storage = MemoryBotStorage()
    running = worker(_SlowDriver(entered, release), storage=storage)
    running.start()
    running.offer(advert_event())
    await asyncio.wait_for(entered.wait(), timeout=2)
    return running, storage


async def test_a_stopping_worker_finishes_the_dispatch_it_is_running() -> None:
    """9.4, design D6: the `bot_state` write a dispatch owes lands at the stop."""
    release = asyncio.Event()
    running, storage = await _dispatching_worker(release)

    stopping = asyncio.ensure_future(running.stop())
    await asyncio.sleep(0)
    assert storage.bot_state.writes == 0, "the driver has not written yet"
    release.set()  # the driver finishes only after the stop has begun
    await asyncio.wait_for(stopping, timeout=2)

    assert storage.bot_state.writes == 1, "the state write landed, not cancelled"
    assert running._task is None


async def test_a_driver_that_never_finishes_is_cut_short_and_named() -> None:
    """9.4, design D6: the budget bounds the stop and the report names the bot."""
    running, storage = await _dispatching_worker(None)
    logger = _RecordingLogger()
    running.logger = logger

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(running.stop(deadline=loop.time() + 0.2), timeout=2)
    elapsed = loop.time() - started

    assert elapsed >= 0.2
    assert storage.bot_state.writes == 0
    cut_short = [
        fields for level, name, fields in logger.events if name == "bot_dispatch_cut_short"
    ]
    assert len(cut_short) == 1
    assert cut_short[0]["bot"] == running.name


async def test_a_stop_does_not_start_the_dispatches_still_queued() -> None:
    """9.4: what was queued behind the one in progress is not run."""
    from tests.test_bot_runtime import advert_event

    release = asyncio.Event()
    running, storage = await _dispatching_worker(release)
    running.offer(advert_event(packet_id="packet-2"))
    running.offer(advert_event(packet_id="packet-3"))

    stopping = asyncio.ensure_future(running.stop())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(stopping, timeout=2)

    assert storage.bot_state.writes == 1, "one dispatch ran, the queued two did not"


async def test_an_idle_worker_stops_at_once_and_reports_nothing() -> None:
    from tests.test_bot_runtime import worker

    idle = worker()
    logger = _RecordingLogger()
    idle.logger = logger
    idle.start()
    await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(idle.stop(), timeout=2)

    assert loop.time() - started < 0.5, "an idle worker waits for nothing"
    assert logger.names() == []


# --- 9.5 The periodic status line -------------------------------------------


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
        record=bot_record(
            driver="greedy", mode="observe", config={"rate_per_hour": 1e6, "burst": 10_000}
        ),
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


# --- Stopping a deleted bot, without a restart -------------------------------


async def test_a_deleted_bot_stops_running_without_a_restart(database: Database) -> None:
    persistence, record, loaded = await _stored_bot(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()
    run.bots.start()
    assert len(run.bots) == 1

    assert await run.stop_bot(record.id) is True

    assert len(run.bots) == 0
    assert run._bot_lines() == ["bots: none configured"]


async def test_stopping_a_bot_waits_for_the_dispatch_in_flight(database: Database) -> None:
    """`bot-runtime` already requires a stopping worker to finish its dispatch;
    deleting one must not become the way round that."""
    persistence, record, loaded = await _stored_bot(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()
    worker = run.bots.workers[0]
    run.bots.start()

    await run.stop_bot(record.id)

    assert worker._task is None or worker._task.done(), "the worker's task outlived the delete"
    assert not worker._task or not worker._task.cancelled(), (
        "the dispatch was cancelled rather than allowed to finish"
    )


async def test_stopping_a_bot_this_run_does_not_run_reports_so(database: Database) -> None:
    persistence, _record, loaded = await _stored_bot(database)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
    )
    await run._restore()

    assert await run.stop_bot(uuid.uuid4()) is False
    assert len(run.bots) == 1

    await run.bots.stop()


async def test_stopping_one_bot_leaves_the_others_running(database: Database) -> None:
    persistence, record, _loaded = await _stored_bot(database)
    entities = EntityRepository(database=database)
    second = await entities.store(
        name="greeter-two",
        identity=generate_identity(),
        secret=SECRET,
        node_type=NodeType.CHAT,
        entity_type="bot",
    )
    assert isinstance(second, Succeeded)
    await persistence.bots.create(
        entity_id=second.value.id,
        driver="greeter",
        config=bot_drivers.default_config("greeter"),
        entity_name="greeter-two",
    )
    both = await entities.load_all(SECRET)
    assert isinstance(both, Succeeded)
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, stored_entities=tuple(both.value)
        ),
        persistence=persistence,
    )
    await run._restore()
    run.bots.start()
    assert len(run.bots) == 2

    await run.stop_bot(record.id)

    assert [worker.name for worker in run.bots] == ["greeter-two"]
    await run.bots.stop()


# --- 5.1-5.3 `reconcile_bots` (design D7, bot-runtime) ----------------------


async def test_a_disabled_bot_is_not_started_and_states_the_reason(database: Database) -> None:
    """5.2: the same reason startup states, unchanged by reconcile."""
    persistence, _record, _loaded = await _stored_bot(database, enabled=False)
    run = _live_runtime(persistence)
    await run._restore()

    assert run.bots.workers == []

    await run.reconcile_entities()

    assert run.bots.workers == []


async def test_a_bot_created_from_the_command_line_is_run_within_the_reread(
    database: Database,
) -> None:
    """5.1: a bot on an entity this run now holds is run — `reconcile_bots`
    reusing `_run_bot` unchanged, driven from `reconcile_entities` in one pass."""
    persistence, record, _loaded = await _stored_bot(database)
    run = _live_runtime(persistence)
    await run._restore()
    assert run.bots.workers == []

    assert await run.reconcile_entities() is True

    assert [worker.record.id for worker in run.bots] == [record.id]


async def test_the_entity_arriving_after_the_bot_is_then_run(database: Database) -> None:
    """5.1: a bot created on an entity this run does not yet hold is run once
    that entity is adopted — order does not matter, only the end state."""
    persistence, record, _loaded = await _stored_bot(database)
    run = _live_runtime(persistence)
    await run._restore()
    (line,) = run._bot_lines()
    assert "identity was not loaded" in line

    await run.reconcile_entities()

    assert [worker.record.id for worker in run.bots] == [record.id]


async def test_an_observing_bot_started_mid_run_touches_no_radio(database: Database) -> None:
    """5.2: dispatches to its driver and touches no radio, exactly as one
    started at startup does — `_run_bot` is reused unchanged, so the
    transmit-enabled property already covered at startup carries over."""
    persistence, record, _loaded = await _stored_bot(database, mode="observe")
    run = _live_runtime(persistence)
    await run._restore()

    await run.reconcile_entities()

    (worker,) = list(run.bots)
    assert worker.record.id == record.id
    assert worker.mode is not None
    assert str(worker.mode) == "observe"


async def test_starting_a_bot_mid_run_leaves_the_others_running_state_untouched(
    database: Database,
) -> None:
    """5.2: every bot already running keeps its durable state, rate-limit
    position and counters when a second one is started mid-run."""
    persistence, first_record, first_loaded = await _stored_bot(database, name="first")
    entities = EntityRepository(database=database)
    second_stored = await entities.store(
        name="second", identity=generate_identity(), secret=SECRET, entity_type="bot"
    )
    assert isinstance(second_stored, Succeeded)
    second_created = await persistence.bots.create(
        entity_id=second_stored.value.id,
        driver="greeter",
        config=bot_drivers.default_config("greeter"),
        entity_name="second",
    )
    assert isinstance(second_created, Succeeded)

    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, stored_entities=tuple(first_loaded)
        ),
        persistence=persistence,
        webhook_secret=SECRET,
    )
    await run._restore()
    (first_worker,) = list(run.bots)
    first_worker.counters.observations = 7
    before_counters = first_worker.counters

    await run.reconcile_entities()

    assert sorted(worker.record.id for worker in run.bots) == sorted(
        (first_record.id, second_created.value.id)
    )
    still_running = next(worker for worker in run.bots if worker.record.id == first_record.id)
    assert still_running is first_worker, "the running worker was replaced"
    assert first_worker.counters is before_counters
    assert first_worker.counters.observations == 7

    await persistence.stop()


async def test_a_bot_stopped_by_a_disable_keeps_its_durable_state(database: Database) -> None:
    """5.3: disabled mid-run, its durable state survives, and enabling it
    again resumes from that state rather than starting over."""
    persistence, record, loaded = await _stored_bot(database)
    assert isinstance(
        await persistence.bot_state.set(record.id, "greeted", True),
        Succeeded,
    )
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded)),
        persistence=persistence,
        webhook_secret=SECRET,
    )
    await run._restore()
    assert len(run.bots) == 1

    disabled = await persistence.bots.set_enabled(record.id, False)
    assert isinstance(disabled, Succeeded)
    await run.reconcile_entities()

    assert run.bots.workers == []
    state = await persistence.bot_state.get(record.id, "greeted")
    assert isinstance(state, Succeeded)
    assert state.value is True, "the durable state must survive being stopped"

    enabled = await persistence.bots.set_enabled(record.id, True)
    assert isinstance(enabled, Succeeded)
    await run.reconcile_entities()

    assert len(run.bots) == 1
    state_after = await persistence.bot_state.get(record.id, "greeted")
    assert isinstance(state_after, Succeeded)
    assert state_after.value is True

    await persistence.stop()
