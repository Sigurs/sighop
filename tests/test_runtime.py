"""The runtime and `sighop run` (milestone 3, `runtime-cli`, design D18).

The end-to-end test replays a real capture through the whole pipeline —
decode, dedup, path learning, fan-out, scheduler — which is the first time in
this project those run as one system. The rest are about the gate and about
shutdown: a run that cannot say what happened to a queued packet is exactly the
silent-drop failure DESIGN.md §4.3 rules out.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import io
import json
import uuid
from collections.abc import AsyncIterator
from typing import cast

import pytest

from sighop.config import DatabaseConfig
from sighop.db.engine import Database
from sighop.db.persistence import Persistence
from sighop.db.repositories import EntityRecord, LoadedEntity, OpenedEntities, advert_config_for
from sighop.net.bus import PriorityClass, Submission, TxResult
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType
from sighop.radio.modem import EU868_NARROW, ModemEvent, TransmitDone
from sighop.radio.replay import CaptureReplay
from sighop.runtime import Runtime, RuntimeConfig
from tests.dbfixtures import the_default_persistence
from tests.protocol.corpus import AMBIENT, CORPUS_DIR
from tests.test_tx import ManualClock, RecordingLogger, RecordingSender

pytestmark = pytest.mark.usefixtures("default_persistence")

CAPTURE = CORPUS_DIR / AMBIENT


async def _events(path=CAPTURE) -> AsyncIterator[ModemEvent]:
    for event in CaptureReplay.open(path).read():
        yield event


async def _startup() -> str:
    return "replaying a capture"


async def _never_ends() -> AsyncIterator[ModemEvent]:
    """A source that stays open, so a test controls when the run ends."""
    forever = asyncio.Event()
    await forever.wait()
    yield cast(ModemEvent, None)  # pragma: no cover - unreachable, and what makes this a generator


async def run_briefly(run: Runtime, *, turns: int = 60) -> None:
    """Run, give the loops some turns, then stop the way an operator would."""
    task = asyncio.create_task(run.run())
    for _ in range(turns):
        await asyncio.sleep(0)
    run.stop()
    await asyncio.wait_for(task, 2)


def runtime(
    source: AsyncIterator[ModemEvent],
    *,
    config: RuntimeConfig | None = None,
    sender: RecordingSender | None = None,
    clock: ManualClock | None = None,
    out: io.StringIO | None = None,
    persistence=None,
    entity_loader=None,
    webhook_secret: bytes | None = None,
) -> Runtime:
    return Runtime(
        source=source,
        startup=_startup,
        config=config or RuntimeConfig(status_interval=3600, advert_tick=3600),
        sender=sender,
        radio=EU868_NARROW,
        clock=clock or ManualClock(),
        out=out or io.StringIO(),
        logger=RecordingLogger(),
        # A database is required, so a test that brings no rows of its own
        # runs against the emptied one its module's `default_persistence` gives.
        persistence=persistence if persistence is not None else the_default_persistence(),
        entity_loader=entity_loader,
        webhook_secret=webhook_secret,
    )


def _loaded_entity(
    name: str = "skogen",
    *,
    identity=None,
    node_type: NodeType = NodeType.CHAT,
    enabled: bool = True,
    flood_interval_seconds: float | None = None,
    zero_hop_interval_seconds: float = 0.0,
):
    """A `LoadedEntity` with no database behind it, for a stub `entity_loader`."""
    identity = identity or generate_identity()
    record = EntityRecord(
        id=uuid.uuid4(),
        type="entity",
        name=name,
        public_key=identity.public_key,
        node_hash=identity.node_hash,
        advert_config=advert_config_for(
            node_type,
            flood_interval_seconds=flood_interval_seconds,
            zero_hop_interval_seconds=zero_hop_interval_seconds,
        ),
        enabled=enabled,
        created_at=dt.datetime.now(dt.UTC),
    )
    return LoadedEntity(record=record, identity=identity)


def _stub_loader(entities=(), *, stranded=(), error: Exception | None = None):
    """An `entity_loader` that answers from a fixed list rather than a database."""

    async def load() -> OpenedEntities:
        if error is not None:
            raise error
        return OpenedEntities(opened=tuple(entities), stranded=tuple(stranded))

    return load


# --- End to end ------------------------------------------------------------


async def test_a_capture_replays_through_the_whole_pipeline() -> None:
    out = io.StringIO()
    run = runtime(_events(), out=out)

    await run.run()

    text = out.getvalue()
    assert "replaying a capture" in text
    assert "transmit disabled" in text
    assert run.pipeline.delivered > 0
    assert run.pipeline.duplicates > 0, "the capture holds repeats; dedup saw none"
    assert run.pipeline.paths.destination_count > 0
    # The status line is printed once more on the way out, whatever else happened.
    assert text.strip().splitlines()[-1].startswith("== TX=disabled")


async def test_receptions_reach_a_subscriber() -> None:
    out = io.StringIO()
    run = runtime(_events(), out=out)
    seen: list[str] = []

    async def handler(record) -> None:
        seen.append(record.packet_id)

    run.bus.subscribe("entity", handler=handler, queue_size=1024)
    await run.run()
    await asyncio.sleep(0)

    assert len(seen) == run.pipeline.delivered


async def test_duplicates_are_counted_but_not_delivered() -> None:
    run = runtime(_events())
    await run.run()

    assert run.bus.published == run.pipeline.delivered
    assert run.pipeline.duplicates > 0


# --- The gate --------------------------------------------------------------


async def test_a_run_without_the_flag_sends_nothing() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    run = runtime(
        _never_ends(),
        config=RuntimeConfig(status_interval=3600, advert_tick=0.001, stub_names=("skogen",)),
        sender=sender,
        clock=clock,
    )
    # Force the stub due immediately, so the advert path is genuinely exercised.
    run.adverts.stubs[0].next_flood_at = clock.now()

    await run_briefly(run)

    assert sender.sent == [], "transmit was disabled and something still went out"
    assert run.scheduler.stats.suppressed > 0, "no advert was scheduled; the test is vacuous"


async def test_enabling_transmit_lets_an_advert_through() -> None:
    clock = ManualClock()
    sender = RecordingSender()
    run = runtime(
        _never_ends(),
        config=RuntimeConfig(
            transmit_enabled=True,
            status_interval=3600,
            advert_tick=0.001,
            stub_names=("skogen",),
        ),
        sender=sender,
        clock=clock,
    )
    run.adverts.stubs[0].next_flood_at = clock.now()

    await run_briefly(run)

    assert sender.sent, "the gate was open and nothing was transmitted"
    assert run.scheduler.stats.transmitted > 0


async def test_the_startup_banner_names_the_gate_and_the_stubs() -> None:
    out = io.StringIO()
    run = runtime(
        _events(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stub_names=("skogen",)),
        out=out,
    )

    await run.run()

    text = out.getvalue()
    assert "transmit disabled" in text
    assert "skogen" in text
    assert "ephemeral" in text


# --- Shutdown --------------------------------------------------------------


async def test_stopping_drains_queued_packets_and_says_why() -> None:
    clock = ManualClock()

    run = runtime(_never_ends(), clock=clock)
    # A packet the budget cannot admit, so it is still queued at shutdown —
    # which is the case where a silent drop would otherwise be possible.
    run.scheduler.budget.charge(run.scheduler.budget.ceiling_ms, clock.now())
    handle = run.bus.submit(
        Submission(
            packet=b"\x04" * 32,
            priority=PriorityClass.MESSAGE,
            entity_id="stub-1",
            deadline=clock.now() + dt.timedelta(seconds=600),
        )
    )
    task = asyncio.create_task(run.run())
    for _ in range(5):
        await asyncio.sleep(0)
    run.stop()
    await asyncio.wait_for(task, 2)

    outcome = await handle
    assert outcome.result is TxResult.DROPPED
    assert outcome.reason == "scheduler_stopped"


async def test_the_final_status_line_is_printed_on_the_way_out() -> None:
    out = io.StringIO()
    run = runtime(_events(), out=out)

    await run.run()

    assert out.getvalue().strip().splitlines()[-1].startswith("== TX=")


# --- Radio readback --------------------------------------------------------


async def test_the_readback_reaches_both_the_scheduler_and_the_rx_event() -> None:
    run = runtime(_events())

    run.set_radio(None)
    assert run.scheduler._radio is None
    assert run.pipeline.radio is None

    run.set_radio(EU868_NARROW)
    assert run.pipeline.radio == EU868_NARROW


async def test_without_a_readback_nothing_is_admitted_even_with_transmit_on() -> None:
    clock = ManualClock()
    sender = RecordingSender(TransmitDone(success=True))
    run = runtime(
        _never_ends(),
        config=RuntimeConfig(
            transmit_enabled=True,
            status_interval=3600,
            advert_tick=0.001,
            stub_names=("skogen",),
        ),
        sender=sender,
        clock=clock,
    )
    run.set_radio(None)
    run.adverts.stubs[0].next_flood_at = clock.now()

    await run_briefly(run)

    assert sender.sent == []
    assert run.scheduler.stats.dropped > 0


# --- The CLI ---------------------------------------------------------------


# --- Capture ---------------------------------------------------------------


async def test_the_runtime_writes_a_capture_with_its_provenance_header(tmp_path) -> None:
    """An overnight `run` must produce a corpus-grade file, not a third dialect."""
    from sighop.radio.capture import CaptureWriter
    from sighop.radio.modem import EU868_NARROW as PRESET
    from sighop.radio.probe import FirmwareVersion, ProbeResult

    out_path = tmp_path / "session.jsonl"
    writer = CaptureWriter(out_path)
    writer.open()

    async def probe() -> ProbeResult:
        return ProbeResult(
            configured_radio=PRESET,
            device_name="Heltec V4 OLED",
            radio=PRESET,
            tx_power_dbm=0,
            firmware_version=FirmwareVersion(version=1, reserved=0),
            battery_mv=4279,
            mcu_temp_tenths_c=320,
            sensors_raw=b"",
        )

    run = Runtime(
        source=_events(),
        startup=_startup,
        persistence=the_default_persistence(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600),
        radio=PRESET,
        clock=ManualClock(),
        out=io.StringIO(),
        logger=RecordingLogger(),
        capture_writer=writer,
        capture_probe=probe,
    )
    await run.run()
    writer.close()

    lines = [json.loads(line) for line in out_path.read_text().splitlines()]
    assert lines[0]["kind"] == "capture_meta"
    assert lines[0]["device_name"]["value"] == "Heltec V4 OLED"
    assert lines[0]["radio"]["value"]["sf"] == 8
    assert len(lines) - 1 == writer.rx_count + writer.unparsed_count
    assert writer.rx_count > 0


async def test_a_run_reports_the_path_hash_size_from_the_environment() -> None:
    from sighop.boot import runtime_config
    from sighop.config import Config, DatabaseConfig

    config = Config(
        database=DatabaseConfig(url="postgresql+asyncpg://r:p@h/d"), path_hash_size_raw="1"
    )
    out = io.StringIO()
    run = runtime(
        _events(),
        config=dataclasses.replace(runtime_config(config), status_interval=3600, advert_tick=3600),
        out=out,
    )
    await run.run()

    assert "path hash size 1 byte (SIGHOP_PATH_HASH_SIZE)" in out.getvalue()


# --- The radio-readiness signal (design D1) --------------------------------


async def test_the_radio_signal_follows_the_readback_across_a_reconnect() -> None:
    """Set when parameters are adopted, cleared when they are lost.

    `set_radio` is the one funnel every readback passes through, startup and
    reconnect alike, which is why the signal lives on it rather than beside it.
    """
    run = runtime(_never_ends())
    assert run._radio_ready.is_set(), "constructed with parameters and still not ready"

    run.set_radio(None)  # the reconnect that loses the board
    assert not run._radio_ready.is_set()

    run.set_radio(EU868_NARROW)  # and the one that finds it again
    assert run._radio_ready.is_set()


async def test_the_radio_signal_is_not_the_startup_signal() -> None:
    """Why D1 refuses to reuse `_ready`.

    A run that has finished starting up and has since lost its board has
    `_ready` set and no radio. A reply that waited on `_ready` would sail
    straight through into the refusal this change exists to remove.
    """
    run = Runtime(
        source=_never_ends(),
        startup=_startup,
        persistence=the_default_persistence(),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600),
        sender=RecordingSender(),
        radio=None,
        clock=ManualClock(),
        out=io.StringIO(),
        logger=RecordingLogger(),
    )
    assert not run._radio_ready.is_set(), "no readback was ever adopted"

    run._ready.set()  # startup finished
    run.set_radio(EU868_NARROW)
    run.set_radio(None)  # and the board went away afterwards

    assert run._ready.is_set()
    assert not run._radio_ready.is_set(), "the two signals moved together"


# --- Renaming a loaded identity reaches the run ------------------------------


async def test_renaming_an_identity_reaches_every_consumer_at_once() -> None:
    """The messengers, the rooms and the bots were handed `adverts.stubs`, so
    they hold these very objects: one in-place rename reaches all of them
    (change web-delete-and-rename, design D1)."""
    run = runtime(_events(CAPTURE), config=RuntimeConfig(stub_names=("skogen",)))
    await run._restore()
    stub = run.adverts.stubs[0]
    key = stub.identity.public_key
    assert run.messenger.entities[0] is stub, "the messenger was handed a copy"

    assert run.rename_entity(key, "skogen-2") is True

    assert stub.name == "skogen-2"
    assert run.adverts.stubs[0] is stub, "the stub was replaced rather than renamed"
    assert run.messenger.entities[0].name == "skogen-2"
    assert run.channels.entities[0].name == "skogen-2"


async def test_renaming_replaces_the_frozen_registry_entry() -> None:
    """`keystore.LocalEntity` is frozen, so the registry entry is replaced —
    safe because nothing holds one for behaviour (design D1)."""
    run = runtime(_events(CAPTURE), config=RuntimeConfig(stub_names=("skogen",)))
    await run._restore()
    identity = generate_identity()
    run.entities.add_stored("stored-one", identity)
    assert [entity.name for entity in run.entities.entities] == ["stored-one"]

    assert run.entities.rename(identity.public_key, "stored-two") is True

    assert [entity.name for entity in run.entities.entities] == ["stored-two"]
    assert run.entities.entities[0].identity.public_key == identity.public_key


async def test_renaming_an_identity_this_run_does_not_hold_reports_so() -> None:
    run = runtime(_events(CAPTURE), config=RuntimeConfig(stub_names=("skogen",)))
    await run._restore()

    assert run.rename_entity(generate_identity().public_key, "nobody") is False
    assert run.adverts.stubs[0].name == "skogen"


# --- 3.1 `entity_loader`, mirroring `channel_loader` (design D2) -----------


async def test_a_run_with_a_database_but_no_secret_gets_no_entity_loader() -> None:
    """A missing `SIGHOP_SECRET_KEY` means there is nothing to open; defaulting
    anyway would give reconcile a loader that only ever fails. Never opened —
    only `__post_init__`'s wiring is under test, no connection is made."""
    run = Runtime(
        source=_events(CAPTURE),
        startup=_startup,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600),
        clock=ManualClock(),
        out=io.StringIO(),
        logger=RecordingLogger(),
        persistence=Persistence(
            database=Database(config=DatabaseConfig(url="postgresql+asyncpg://unused/unused"))
        ),
    )

    assert run.entity_loader is None


async def test_a_run_with_a_database_and_a_secret_gets_an_entity_loader(
    database: Database,
) -> None:
    run = Runtime(
        source=_events(CAPTURE),
        startup=_startup,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600),
        clock=ManualClock(),
        out=io.StringIO(),
        logger=RecordingLogger(),
        persistence=Persistence(database=database),
        webhook_secret=b"0" * 32,
    )

    assert run.entity_loader is not None
    opened = await run.entity_loader()
    assert opened.opened == ()
    assert opened.stranded == ()


def _identity_with_node_hash(node_hash: int):
    while True:
        candidate = generate_identity()
        if candidate.node_hash == node_hash:
            return candidate


# --- 3.2 `reconcile_entities` diffs by public key (design D1, D3, D6) ------


async def test_reconcile_adopts_a_newly_openable_identity() -> None:
    stored = _loaded_entity("newcomer")
    run = runtime(_events(CAPTURE), entity_loader=_stub_loader([stored]))

    assert await run.reconcile_entities() is True

    (stub,) = run.adverts.stubs
    assert stub.identity.public_key == stored.public_key
    assert stub.name == "newcomer"
    assert stub.persistent is True


async def test_adopting_an_identity_leaves_the_others_schedules_untouched() -> None:
    """advert-policy: "The other identities' schedules are untouched" — the
    adoption side. An identity adopted mid-run must not itself advert
    (`_stagger` only sets fields on the new stub) and must not touch any
    already-loaded identity's schedule, count or override."""
    stub_names = ("skogen",)
    run = runtime(
        _events(CAPTURE),
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, stub_names=stub_names),
    )
    existing = run.adverts.stubs[0]
    run.adverts.set_override(existing, interval_seconds=3600)
    run.adverts.request_flood(existing)
    before = (
        existing.next_flood_at,
        existing.last_flood_at,
        existing.adverts_sent,
        existing.override,
    )

    stored = _loaded_entity("newcomer")
    run.entity_loader = _stub_loader([stored])
    assert await run.reconcile_entities() is True

    assert (
        existing.next_flood_at,
        existing.last_flood_at,
        existing.adverts_sent,
        existing.override,
    ) == before
    (newcomer,) = [stub for stub in run.adverts.stubs if stub.name == "newcomer"]
    assert newcomer.adverts_sent == 0, "an adopted identity must not itself advert on adoption"


async def test_reconcile_withdraws_an_identity_no_longer_openable() -> None:
    stored = _loaded_entity("leaving")
    run = runtime(_events(CAPTURE), entity_loader=_stub_loader([stored]))
    await run.reconcile_entities()
    assert len(run.adverts.stubs) == 1

    run.entity_loader = _stub_loader([])
    await run.reconcile_entities()

    assert run.adverts.stubs == []
    assert run.entities.entities == ()


async def test_reconcile_never_withdraws_a_keyfile_identity(tmp_path) -> None:
    from sighop.keystore import create_keyfile

    keyfile = create_keyfile(tmp_path / "one.json", "keyfile-one")
    run = runtime(
        _events(CAPTURE),
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, entity_keyfiles=(keyfile.path,)
        ),
        entity_loader=_stub_loader([]),
    )
    await run._restore()
    assert len(run.adverts.stubs) == 1

    await run.reconcile_entities()

    assert len(run.adverts.stubs) == 1, "a keyfile-sourced stub must never be withdrawn"
    assert run.adverts.stubs[0].name == "keyfile-one"


async def test_a_read_failure_keeps_the_loaded_set_and_reports_it() -> None:
    stored = _loaded_entity("steady")
    run = runtime(_events(CAPTURE), out=io.StringIO(), entity_loader=_stub_loader([stored]))
    run._started = True
    await run.reconcile_entities()

    run.entity_loader = _stub_loader(error=RuntimeError("database is degraded"))
    assert await run.reconcile_entities() is False

    assert len(run.adverts.stubs) == 1
    assert run.adverts.stubs[0].name == "steady"
    assert "database is degraded" in run.out.getvalue()
    assert "keeping" in run.out.getvalue()


# --- 3.3 Withdrawal leaves the schedule and refuses further adverts --------


async def test_a_withdrawn_identity_originates_no_further_advert() -> None:
    stored = _loaded_entity("leaving")
    run = runtime(_events(CAPTURE), entity_loader=_stub_loader([stored]))
    await run.reconcile_entities()

    run.entity_loader = _stub_loader([])
    await run.reconcile_entities()

    assert run.adverts.stubs == []
    assert run.adverts.tick() == [], "nothing is due for an identity that no longer exists"


async def test_a_zero_hop_request_for_a_withdrawn_identity_is_refused() -> None:
    stored = _loaded_entity("leaving")
    sender = RecordingSender()
    run = runtime(
        _events(CAPTURE), out=io.StringIO(), sender=sender, entity_loader=_stub_loader([stored])
    )
    run._started = True
    await run.reconcile_entities()

    run.entity_loader = _stub_loader([])
    await run.reconcile_entities()

    await run._request_zero_hop("leaving")

    assert "no entity named 'leaving'" in run.out.getvalue()
    assert sender.sent == [], "nothing must be transmitted for a withdrawn identity"


# --- 3.4 A refused adoption is remembered, not re-reported (design D4) -----


async def test_a_collision_with_a_keyfile_is_refused_naming_both(tmp_path) -> None:
    from sighop.keystore import create_keyfile

    keyfile_identity = generate_identity()
    keyfile = create_keyfile(tmp_path / "one.json", "keyfile-one", identity=keyfile_identity)
    colliding_stored = _loaded_entity(
        "newcomer", identity=_identity_with_node_hash(keyfile_identity.node_hash)
    )
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, entity_keyfiles=(keyfile.path,)
        ),
        entity_loader=_stub_loader([colliding_stored]),
    )
    await run._restore()
    run._started = True

    assert await run.reconcile_entities() is True

    assert len(run.adverts.stubs) == 1, "the run must keep the identities it had"
    assert run.adverts.stubs[0].name == "keyfile-one"
    text = run.out.getvalue()
    assert "keyfile-one" in text
    assert "newcomer" in text


async def test_the_refusal_is_not_repeated_on_a_later_reread(tmp_path) -> None:
    from sighop.keystore import create_keyfile

    keyfile_identity = generate_identity()
    keyfile = create_keyfile(tmp_path / "one.json", "keyfile-one", identity=keyfile_identity)
    colliding_stored = _loaded_entity(
        "newcomer", identity=_identity_with_node_hash(keyfile_identity.node_hash)
    )
    run = runtime(
        _events(CAPTURE),
        out=io.StringIO(),
        config=RuntimeConfig(
            status_interval=3600, advert_tick=3600, entity_keyfiles=(keyfile.path,)
        ),
        entity_loader=_stub_loader([colliding_stored]),
    )
    await run._restore()
    run._started = True
    await run.reconcile_entities()
    before = run.out.getvalue()

    await run.reconcile_entities()

    assert run.out.getvalue() == before, "a standing collision must not be reported again"


async def test_a_collision_resolved_by_withdrawal_is_then_adopted_and_reported() -> None:
    first_identity = generate_identity()
    colliding_identity = _identity_with_node_hash(first_identity.node_hash)
    first = _loaded_entity("first", identity=first_identity)
    colliding = _loaded_entity("second", identity=colliding_identity)
    run = runtime(_events(CAPTURE), out=io.StringIO(), entity_loader=_stub_loader([first]))
    run._started = True
    await run.reconcile_entities()
    assert len(run.adverts.stubs) == 1

    run.entity_loader = _stub_loader([first, colliding])
    await run.reconcile_entities()
    assert len(run.adverts.stubs) == 1, "the collision must still be refused"

    run.entity_loader = _stub_loader([colliding])
    await run.reconcile_entities()

    assert [stub.name for stub in run.adverts.stubs] == ["second"]
    assert "+second" in run.out.getvalue()


# --- 3.5 Adoption/withdrawal is reported, silence otherwise (design D9) ----


async def test_a_reread_that_changes_nothing_is_silent() -> None:
    stored = _loaded_entity("steady")
    run = runtime(_events(CAPTURE), out=io.StringIO(), entity_loader=_stub_loader([stored]))
    run._started = True
    await run.reconcile_entities()
    before = run.out.getvalue()

    await run.reconcile_entities()

    assert run.out.getvalue() == before


async def test_an_adoption_and_a_withdrawal_are_named_with_no_key_material() -> None:
    staying = _loaded_entity("staying")
    leaving = _loaded_entity("leaving")
    run = runtime(
        _events(CAPTURE), out=io.StringIO(), entity_loader=_stub_loader([staying, leaving])
    )
    run._started = True
    await run.reconcile_entities()

    arriving = _loaded_entity("arriving")
    run.entity_loader = _stub_loader([staying, arriving])
    await run.reconcile_entities()

    text = run.out.getvalue()
    assert "identities changed" in text
    assert "+arriving" in text
    assert "-leaving" in text
    assert arriving.identity.private_key.hex() not in text
    assert staying.identity.private_key.hex() not in text
    assert leaving.identity.private_key.hex() not in text


# --- 3.6 The periodic refresh loop (design D8) ------------------------------


async def test_entity_refresh_seconds_defaults_to_sixty() -> None:
    assert RuntimeConfig().entity_refresh_seconds == 60.0


async def test_the_refresh_loop_reads_nothing_before_the_interval_elapses() -> None:
    clock = ManualClock()
    calls = 0

    async def _load() -> OpenedEntities:
        nonlocal calls
        calls += 1
        return OpenedEntities()

    run = runtime(
        _events(CAPTURE),
        clock=clock,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, entity_refresh_seconds=60),
        entity_loader=_load,
    )
    task = asyncio.create_task(run._entity_refresh_loop())
    try:
        for _ in range(50):
            await asyncio.sleep(0)
        assert calls == 0, "a manual clock that never advances must never turn this into a read"

        clock.advance(60)
        for _ in range(50):
            await asyncio.sleep(0)
        assert calls == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_change_behind_the_loader_is_adopted_within_the_refresh_interval() -> None:
    clock = ManualClock()
    stored = _loaded_entity("late-arrival")
    run = runtime(
        _events(CAPTURE),
        clock=clock,
        config=RuntimeConfig(status_interval=3600, advert_tick=3600, entity_refresh_seconds=60),
        entity_loader=_stub_loader([stored]),
    )
    task = asyncio.create_task(run._entity_refresh_loop())
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert run.adverts.stubs == []

        clock.advance(60)
        for _ in range(50):
            await asyncio.sleep(0)
        assert [stub.name for stub in run.adverts.stubs] == ["late-arrival"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
