"""The runtime and command line under persistence (section 8, and 3.9).

The properties worth proving here are the ones that cross module boundaries:
that a run says which of the two durability modes it is in before any traffic
arrives, that a replay does not write unless asked, that a database at the wrong
revision stops startup rather than being migrated behind the operator's back,
and — the one the whole design is built around — that the reception path does
not slow down when the database stops answering.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import time
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from sighop.config import (
    DATABASE_SCHEMA_VARIABLE,
    Config,
    DatabaseConfig,
    generate_secret_key,
)
from sighop.db import migrations
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import EntityRepository
from sighop.keystore import NodeHashCollisionError, create_keyfile
from sighop.monitor.render import render_persistence, render_status
from sighop.net.bus import IngressPipeline, NetworkBus
from sighop.net.dedup import DedupCache
from sighop.net.paths import PathStore
from sighop.net.rx import RxRecord, decode_event
from sighop.protocol.identity import generate_identity
from sighop.radio.modem import EU868_NARROW, ModemEvent
from sighop.radio.replay import CaptureReplay
from sighop.runtime import Runtime, RuntimeConfig
from tests.dbfixtures import _create_schema, _drop_schema
from tests.protocol.corpus import CAPTURE_FILES, CAPTURES_DIR
from tests.test_render import _dedup_stats, _status
from tests.test_runtime import _events, _startup, runtime
from tests.test_tx import ManualClock

CAPTURE = CAPTURES_DIR / "2026-09-04-03.jsonl"
SECRET = base64.b64decode(generate_secret_key())
URL = "postgresql+asyncpg://role:secret@db.example:5432/sighop"


async def _corpus_events() -> AsyncIterator[ModemEvent]:
    """Every recorded frame. One capture is too quiet to learn a contact from —
    the whole corpus is what has adverts and multi-hop routes in it."""
    for name in CAPTURE_FILES:
        for event in CaptureReplay.open(CAPTURES_DIR / name).read():
            yield event


# --- 8.1 Where the database comes from --------------------------------------


def test_the_environment_supplies_the_database_when_no_override_is_given() -> None:
    config = Config.from_environment({"DATABASE_URL": URL})
    assert config.database is not None
    assert config.database.host == "db.example"


def test_the_command_line_overrides_the_environment_and_names_what_is_in_force() -> None:
    other = "postgresql+asyncpg://role:pw@elsewhere:5432/other"
    config = Config.from_environment({"DATABASE_URL": URL}, database_url=other)
    assert config.database is not None
    assert config.database.host == "elsewhere"
    assert "pw" not in config.database.redacted_url


def test_neither_supplied_is_not_an_error() -> None:
    assert Config.from_environment({}).database is None


# --- 8.3 What startup says --------------------------------------------------


def test_the_startup_line_for_an_in_memory_run_says_state_does_not_survive() -> None:
    assert render_persistence() == (
        "persistence: off — contacts, paths and the packet log are in memory only "
        "and do not survive the process"
    )


def test_the_startup_line_for_a_persistent_run_names_the_database_and_counts() -> None:
    assert render_persistence(
        database="postgresql+asyncpg://role:***@db.example:5432/sighop",
        schema_version="0001",
        entities=1,
        contacts=12,
        paths=7,
    ) == (
        "persistence: on — postgresql+asyncpg://role:***@db.example:5432/sighop  "
        "schema=0001\nrestored: entities=1 contacts=12 paths=7"
    )


def test_the_startup_line_says_when_a_replay_is_not_writing() -> None:
    line = render_persistence(
        database="postgresql+asyncpg://role:***@db.example:5432/sighop",
        schema_version="0001",
        writing=False,
        not_writing_because="a replay carries an earlier session's timestamps",
    )
    assert "not writing" in line
    assert "earlier session's timestamps" in line


def test_no_startup_line_can_carry_a_password() -> None:
    """The renderer is handed an already-redacted URL, so there is no path."""
    redacted = DatabaseConfig(url=URL).redacted_url
    assert "secret" not in render_persistence(database=redacted, schema_version="0001")


async def test_an_in_memory_run_reports_it_before_any_traffic() -> None:
    out = io.StringIO()
    run = runtime(_events(CAPTURE), out=out)
    await run.run()
    assert "persistence: off" in out.getvalue()
    assert "do not survive the process" in out.getvalue()


# --- 8.6 The status line ----------------------------------------------------


@pytest.mark.parametrize("state", ["off", "on", "degraded"])
def test_the_three_persistence_states_are_distinguishable(state: str) -> None:
    line = render_status(_status(), dedup=_dedup_stats(), learned_paths=0, persistence=state)
    assert f"persist={state}" in line
    others = {"off", "on", "degraded"} - {state}
    for other in others:
        assert f"persist={other}" not in line


def test_the_counters_read_zero_rather_than_being_omitted() -> None:
    line = render_status(_status(), dedup=_dedup_stats(), learned_paths=0)
    assert "log_drop=0" in line
    assert "route_drop=0" in line
    assert "backfill=0" in line


def test_the_counters_report_what_was_discarded() -> None:
    line = render_status(
        _status(),
        dedup=_dedup_stats(),
        learned_paths=0,
        persistence="degraded",
        packet_log_discarded=17,
        routes_discarded=4,
        awaiting_backfill=2,
    )
    assert "persist=degraded log_drop=17 route_drop=4 backfill=2" in line


async def test_degraded_clears_with_no_write_once_the_probe_succeeds() -> None:
    """The probe is the recovery trigger, not the next write (design D15)."""

    class _Probeable(Database):
        reachable = False

        async def probe(self) -> bool:
            return self.reachable

    handle = _Probeable(config=DatabaseConfig(url=URL))
    handle.degraded = True
    assert handle.state == "degraded"

    assert await handle.probe_once() is False
    assert handle.state == "degraded"

    handle.reachable = True
    assert await handle.probe_once() is True
    assert handle.state == "on"
    assert handle.stats.operations == 0, "a write was attempted to clear the flag"


# --- 8.4 Entities from both sources -----------------------------------------


@pytest.mark.database
async def test_a_run_loads_entities_from_the_store_and_reports_the_source(
    database: Database,
) -> None:
    store = EntityRepository(database=database)
    identity = generate_identity()
    assert isinstance(
        await store.store(name="from-store", identity=identity, secret=SECRET), Succeeded
    )
    loaded = await store.load_all(SECRET, enabled_only=True)
    assert isinstance(loaded, Succeeded)

    run = runtime(
        _events(CAPTURE),
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, stored_entities=tuple(loaded.value)
        ),
    )
    (entity,) = run.entities.entities
    assert entity.name == "from-store"
    assert entity.source == "the entity store"
    assert entity.public_key == identity.public_key
    assert [stub.identity.public_key for stub in run.adverts.stubs] == [identity.public_key]


@pytest.mark.database
async def test_a_run_loads_both_sources_and_reports_each_with_its_own(
    database: Database, tmp_path
) -> None:
    store = EntityRepository(database=database)
    stored_identity = generate_identity()
    await store.store(name="from-store", identity=stored_identity, secret=SECRET)
    loaded = await store.load_all(SECRET, enabled_only=True)
    assert isinstance(loaded, Succeeded)

    keyfile = create_keyfile(
        tmp_path / "from-file.json",
        "from-file",
        avoid_node_hashes=frozenset({stored_identity.node_hash}),
    )
    run = runtime(
        _events(CAPTURE),
        config=RuntimeConfig(
            status_interval=3600,
            advert_tick=3600,
            stored_entities=tuple(loaded.value),
            entity_keyfiles=(keyfile.path,),
        ),
    )
    sources = {entity.name: entity.source for entity in run.entities.entities}
    assert sources == {"from-store": "the entity store", "from-file": str(keyfile.path)}


def test_a_keyfile_colliding_with_a_stored_entity_stops_the_run(tmp_path) -> None:
    """§3 rule 3 applies across every source in the run (design D14)."""
    import uuid

    from sighop.db.repositories import EntityRecord, LoadedEntity, advert_config_for
    from sighop.protocol.payloads import NodeType

    stored_identity = generate_identity()
    while True:
        colliding = generate_identity()
        if colliding.node_hash == stored_identity.node_hash:
            break
    keyfile = create_keyfile(tmp_path / "collides.json", "from-file", identity=colliding)
    stored = LoadedEntity(
        record=EntityRecord(
            id=uuid.uuid4(),
            type="companion",
            name="from-store",
            public_key=stored_identity.public_key,
            node_hash=stored_identity.node_hash,
            advert_config=advert_config_for(NodeType.CHAT),
            enabled=True,
            created_at=dt.datetime.now(dt.UTC),
        ),
        identity=stored_identity,
    )

    with pytest.raises(NodeHashCollisionError) as excinfo:
        runtime(
            _events(CAPTURE),
            config=RuntimeConfig(
                status_interval=3600,
                advert_tick=3600,
                stored_entities=(stored,),
                entity_keyfiles=(keyfile.path,),
            ),
        )
    message = str(excinfo.value)
    assert "from-store" in message
    assert str(keyfile.path) in message
    assert f"0x{stored_identity.node_hash:02x}" in message


# --- 8.2 / 8.5 A replay against a configured database -----------------------


@pytest.mark.database
async def test_a_replay_run_reaches_steady_state_and_stops_cleanly(
    database: Database,
) -> None:
    persistence = Persistence(database=database)
    run = runtime(_events(CAPTURE), out=io.StringIO(), persistence=persistence)
    await asyncio.wait_for(run.run(), timeout=30)

    assert run.pipeline.delivered > 0
    assert persistence.contact_writer.pending == 0
    assert persistence.path_writer.pending == 0
    assert persistence.packet_log_writer.pending == 0


@pytest.mark.database
async def test_a_replay_writes_nothing_by_default_and_writes_when_asked(
    database: Database,
) -> None:
    async def rows() -> tuple[int, int, int]:
        counts: list[int] = []
        async with database.sessions() as session:
            for table in ("contact", "path", "packet_log"):
                counts.append(
                    (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
                )
        return counts[0], counts[1], counts[2]

    quiet = Persistence(database=database, writes_enabled=False)
    await asyncio.wait_for(
        runtime(_corpus_events(), out=io.StringIO(), persistence=quiet).run(), timeout=60
    )
    assert await rows() == (0, 0, 0), "a replay wrote without being asked"

    writing = Persistence(database=database, writes_enabled=True)
    await asyncio.wait_for(
        runtime(_corpus_events(), out=io.StringIO(), persistence=writing).run(),
        timeout=60,
    )
    contacts, paths, packets = await rows()
    assert (contacts, paths, packets) != (0, 0, 0)
    assert packets > 0, "the opt-in did not write the feed"


@pytest.mark.database
async def test_the_startup_line_of_a_non_writing_replay_says_so(
    database: Database,
) -> None:
    out = io.StringIO()
    persistence = Persistence(database=database, writes_enabled=False)
    await asyncio.wait_for(
        runtime(_events(CAPTURE), out=out, persistence=persistence).run(), timeout=30
    )
    text_out = out.getvalue()
    assert "persistence: on, not writing" in text_out
    assert "--persist-replay" in text_out


@pytest.mark.database
async def test_contacts_and_paths_come_back_on_the_next_run(database: Database) -> None:
    """The whole milestone in one test: run, restart, and find the mesh already
    known before a single frame arrives."""
    first = Persistence(database=database)
    await asyncio.wait_for(
        runtime(_corpus_events(), out=io.StringIO(), persistence=first).run(), timeout=60
    )
    second = Persistence(database=database)
    out = io.StringIO()
    run = runtime(_never_ending(), out=out, persistence=second)
    task = asyncio.create_task(run.run())
    for _ in range(80):
        await asyncio.sleep(0)
    run.stop()
    await asyncio.wait_for(task, timeout=10)

    assert second.restored.contacts > 0, "no contact survived the first run"
    assert second.restored.paths > 0, "no route survived the first run"
    assert f"restored: entities=0 contacts={second.restored.contacts}" in out.getvalue()


async def _never_ending() -> AsyncIterator[ModemEvent]:
    """A source that stays open, so the test decides when the run ends."""
    forever = asyncio.Event()
    await forever.wait()
    yield  # type: ignore[misc]  # pragma: no cover - what makes this a generator


# --- 8.8 A database at the wrong revision stops startup ---------------------


@pytest.mark.database
async def test_a_run_against_an_unmigrated_database_fails_naming_both_revisions(
    database_url: str,
) -> None:
    schema = "sighop_test_unmigrated_run"
    await _drop_schema(database_url, schema)
    await _create_schema(database_url, schema)
    handle = Database(config=DatabaseConfig(url=database_url, schema=schema))
    try:
        from sighop.db.engine import SchemaVersionError

        with pytest.raises(SchemaVersionError) as excinfo:
            await Persistence(database=handle).open()
        message = str(excinfo.value)
        assert migrations.expected_revision() in message
        assert migrations.UPGRADE_COMMAND in message

        # Nothing was applied: the schema is exactly as it was left.
        async with handle.engine.connect() as connection:
            tables = (
                await connection.execute(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname = :s"),
                    {"s": schema},
                )
            ).scalar_one()
        assert tables == 0, "a failed startup applied migrations anyway"
    finally:
        await handle.dispose()
        await _drop_schema(database_url, schema)


# --- 8.7 The db command surface ---------------------------------------------


@pytest.mark.database
async def test_db_current_agrees_with_the_code_against_the_dev_database(
    database_config: DatabaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sighop.cli import main

    assert database_config.schema is not None
    monkeypatch.setenv(DATABASE_SCHEMA_VARIABLE, database_config.schema)
    out = io.StringIO()
    code = await asyncio.to_thread(
        main, ["db", "current", "--database-url", database_config.url], out=out
    )
    printed = out.getvalue()
    assert code == 0
    assert f"applied    {migrations.expected_revision()}" in printed
    assert "agree      yes" in printed
    assert "secret" not in printed


@pytest.mark.database
async def test_db_upgrade_is_idempotent_against_a_database_already_at_head(
    database_config: DatabaseConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sighop.cli import main

    assert database_config.schema is not None
    monkeypatch.setenv(DATABASE_SCHEMA_VARIABLE, database_config.schema)
    out = io.StringIO()
    code = await asyncio.to_thread(
        main, ["db", "upgrade", "--database-url", database_config.url], out=out
    )
    assert code == 0
    assert f"applied    {migrations.expected_revision()}" in out.getvalue()


# --- 3.9 The reception path is unaffected by an unresponsive database -------


def _corpus_records() -> list[RxRecord]:
    records: list[RxRecord] = []
    for name in CAPTURE_FILES:
        records.extend(
            decode_event(event) for event in CaptureReplay.open(CAPTURES_DIR / name).read()
        )
    return records


class _NeverAnswers:
    """A sink that accepts everything and writes nothing, ever.

    Which is what an unreachable database looks like from the reception path:
    `offer` returns, the queue fills, the oldest are dropped and counted, and no
    caller ever waits.
    """

    def __init__(self) -> None:
        self.offered = 0

    def offer(self, item: object) -> bool:
        self.offered += 1
        return True


def test_decode_and_dispatch_timings_match_a_run_with_no_database() -> None:
    records = _corpus_records()
    assert len(records) > 100, "this measurement needs a real capture"

    def ingest_all(sink: object | None) -> tuple[float, list[bool]]:
        bus = NetworkBus()
        pipeline = IngressPipeline(
            bus=bus,
            dedup=DedupCache(),
            paths=PathStore(sink=sink),  # type: ignore[arg-type]
        )
        started = time.perf_counter()
        outcomes = [pipeline.ingest(record) for record in records]
        return time.perf_counter() - started, outcomes

    baseline, plain_outcomes = ingest_all(None)
    sink = _NeverAnswers()
    with_database, database_outcomes = ingest_all(sink)

    # The decisions are identical: the database is not in the decode path at all.
    assert database_outcomes == plain_outcomes
    assert sink.offered > 0, "the unresponsive sink was never even offered a route"
    # And the time is the time of a dict insert, not of a database round trip.
    # A generous bound: the point is orders of magnitude, not microseconds.
    assert with_database < max(baseline * 4, baseline + 0.25), (
        f"an unresponsive database cost the reception path "
        f"{with_database - baseline:.4f}s over {len(records)} frames"
    )


async def test_a_run_with_an_unresponsive_database_still_delivers_every_reception() -> None:
    """End to end: the pipeline's own counts are what they are with no database."""
    plain = runtime(_corpus_events(), out=io.StringIO())
    await asyncio.wait_for(plain.run(), timeout=60)

    stalled = Persistence(
        database=Database(
            config=DatabaseConfig(url=URL, connect_timeout=0.05, statement_timeout=0.05)
        )
    )
    run = runtime(_corpus_events(), out=io.StringIO(), persistence=stalled)
    await asyncio.wait_for(run.run(), timeout=60)

    assert run.pipeline.delivered == plain.pipeline.delivered
    assert run.pipeline.duplicates == plain.pipeline.duplicates
    assert len(run.contacts) == len(plain.contacts)
    assert run.pipeline.paths.destination_count == plain.pipeline.paths.destination_count


async def test_the_runtime_helper_accepts_persistence() -> None:
    """A guard on the test helper itself: a silently ignored keyword would make
    every persistence test above pass for the wrong reason."""
    persistence = Persistence(database=Database(config=DatabaseConfig(url=URL)))
    run = Runtime(
        source=_never_ending(),
        startup=_startup,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600),
        radio=EU868_NARROW,
        clock=ManualClock(),
        out=io.StringIO(),
        persistence=persistence,
    )
    assert run.persistence is persistence
    assert run.contacts._sink is persistence.contact_writer
