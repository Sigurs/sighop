"""The runtime and `sighop run` (milestone 3, `runtime-cli`, design D18).

The end-to-end test replays a real capture through the whole pipeline —
decode, dedup, path learning, fan-out, scheduler — which is the first time in
this project those run as one system. The rest are about the gate and about
shutdown: a run that cannot say what happened to a queued packet is exactly the
silent-drop failure DESIGN.md §4.3 rules out.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
from collections.abc import AsyncIterator
from typing import cast

import pytest

from sighop.net.bus import PriorityClass, Submission, TxResult
from sighop.protocol.identity import generate_identity
from sighop.radio.modem import EU868_NARROW, ModemEvent, TransmitDone
from sighop.radio.replay import CaptureReplay
from sighop.runtime import Runtime, RuntimeConfig
from tests.protocol.corpus import CAPTURES_DIR
from tests.test_tx import ManualClock, RecordingLogger, RecordingSender

CAPTURE = CAPTURES_DIR / "2026-09-04-03.jsonl"


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
        # None is the milestone-4 shape and stays the default: every existing
        # test here runs with no database, which is the property the proposal
        # asked for.
        persistence=persistence,
    )


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


def test_run_requires_a_source() -> None:
    from sighop.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["run"])


def test_transmit_is_off_unless_the_flag_is_given() -> None:
    from sighop.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["run", "--replay", str(CAPTURE)]).enable_transmit is False
    assert (
        parser.parse_args(["run", "--replay", str(CAPTURE), "--enable-transmit"]).enable_transmit
        is True
    )


def test_the_default_ceiling_is_ten_percent() -> None:
    from sighop.cli import build_parser

    args = build_parser().parse_args(["run", "--replay", str(CAPTURE)])
    assert args.duty_cycle_ceiling == 0.10


def test_the_replay_run_reads_the_radio_from_the_capture_header() -> None:
    from sighop.cli import _replay_radio

    replay = CaptureReplay.open(CAPTURE)
    radio = _replay_radio(replay.provenance)

    assert radio is not None
    assert radio.sf == 8
    assert radio.bw_hz == 62_500


def test_a_headerless_capture_reports_no_radio() -> None:
    from sighop.cli import _replay_radio

    assert _replay_radio(None) is None
    assert _replay_radio({}) is None


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


def test_capture_is_refused_for_a_replay_source(capsys) -> None:
    """A capture is evidence of a session on the air, not a copy of a file."""
    from sighop.cli import main

    code = main(["run", "--replay", str(CAPTURE), "--capture", "/tmp/nope.jsonl"])

    assert code == 2
    assert "cannot be combined with --replay" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["4", "three"])
def test_an_invalid_path_hash_size_fails_the_run_before_the_pipeline_starts(
    capsys, monkeypatch, value: str
) -> None:
    from sighop.cli import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SIGHOP_PATH_HASH_SIZE", value)

    def _refuse(self: Runtime) -> None:
        raise AssertionError("the pipeline started with an invalid path hash size")

    monkeypatch.setattr(Runtime, "run", _refuse)
    code = main(["run", "--replay", str(CAPTURE), "--status-interval", "3600"])

    assert code == 2
    captured = capsys.readouterr()
    assert "SIGHOP_PATH_HASH_SIZE" in captured.err
    assert repr(value) in captured.err
    assert "1, 2, 3" in captured.err
    assert captured.out == ""


def test_a_run_reports_the_path_hash_size_from_the_environment(capsys, monkeypatch) -> None:
    from sighop.cli import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SIGHOP_PATH_HASH_SIZE", "1")

    code = main(["run", "--replay", str(CAPTURE), "--status-interval", "3600"])

    assert code == 0
    assert "path hash size 1 byte (SIGHOP_PATH_HASH_SIZE)" in capsys.readouterr().out


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
