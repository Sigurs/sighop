"""`sighop monitor`: argument handling and an end-to-end replay (milestone 2).

The end-to-end test is the one that matters: a capture file goes in at the CLI
boundary and rendered lines come out, through the same decode path a live link
drives.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json

import pytest

from sighop.cli import _run_monitor_live, _run_monitor_replay, build_parser
from sighop.monitor.render import Summary
from sighop.monitor.run import MonitorRun
from sighop.radio.capture import CAPTURE_META_KIND, CaptureWriter
from sighop.radio.modem import RxEvent, RxMeta, UnparsedEvent
from tests.protocol.corpus import CAPTURES_DIR
from tests.test_capture import probe_result

# FLOOD/ACK, no path, a 4-byte checksum: the smallest frame that decodes.
VALID_ACK = bytes.fromhex("0d0001020304")


def parse(argv):
    return build_parser().parse_args(argv)


# --- Argument validation ---------------------------------------------------


def test_monitor_requires_a_source():
    with pytest.raises(SystemExit):
        parse(["monitor"])


def test_monitor_rejects_both_sources():
    with pytest.raises(SystemExit):
        parse(["monitor", "--device", "/dev/x", "--replay", "c.jsonl"])


def test_monitor_accepts_either_source():
    assert parse(["monitor", "--device", "/dev/x"]).device == "/dev/x"
    assert parse(["monitor", "--replay", "c.jsonl"]).replay.name == "c.jsonl"


def test_monitor_takes_a_radio_preset_a_capture_file_and_a_log_file(tmp_path):
    args = parse(
        [
            "monitor",
            "--device",
            "/dev/x",
            "--radio-preset",
            "eu868-narrow",
            "--capture",
            str(tmp_path / "c.jsonl"),
            "--log-file",
            str(tmp_path / "c.log"),
        ]
    )

    assert args.radio_preset == "eu868-narrow"
    assert args.capture.name == "c.jsonl"
    assert args.log_file.name == "c.log"


async def test_a_missing_device_is_reported_without_opening_anything(tmp_path):
    from sighop.radio.kiss import DeviceNotFoundError

    with pytest.raises(DeviceNotFoundError):
        await _run_monitor_live(str(tmp_path / "nope"), "eu868-narrow", None, out=io.StringIO())


# --- End-to-end replay -----------------------------------------------------


def small_capture(tmp_path, advert_hex: str):
    path = tmp_path / "c.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"ts": "2026-09-03T21:15:00+00:00", "kind": CAPTURE_META_KIND, "sighop": {}},
                {
                    "ts": "2026-09-03T21:15:04+00:00",
                    "kind": "rx_frame",
                    "raw_hex": advert_hex,
                    "rx_meta": {"snr_db": 2.0, "rssi_dbm": -90},
                },
                {
                    "ts": "2026-09-03T21:15:06+00:00",
                    "kind": "unparsed",
                    "raw_hex": "dead",
                    "reason": "dangling escape byte at end of frame",
                },
            )
        )
        + "\n"
    )
    return path


def first_advert_hex() -> str:
    from sighop.net.rx import decode_event
    from sighop.protocol.packet import PayloadType

    with (CAPTURES_DIR / "2026-09-02.jsonl").open() as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("kind") != "rx_frame":
                continue
            if decode_event(
                RxEvent(packet=bytes.fromhex(record["raw_hex"]), rx_meta=None)
            ).payload_type is PayloadType.ADVERT:
                return record["raw_hex"]
    raise AssertionError("no advert in the corpus")


async def test_replaying_a_capture_file_renders_every_frame(tmp_path, capsys, monkeypatch):
    path = small_capture(tmp_path, first_advert_hex())
    out = io.StringIO()

    monkeypatch.setattr(
        "sighop.cli.MonitorRun",
        lambda source, **kwargs: MonitorRun(source, **{**kwargs, "out": out}),
    )
    assert await _run_monitor_replay(path) == 0

    text = out.getvalue()
    assert "replaying" in text
    assert "ADVERT" in text
    assert "✓ advert" in text
    assert "unparsed frame" in text
    assert "-- frames=2 failed=1" in text
    assert "adverts=1" in text
    # the recorded timestamp, not the wall clock
    assert "21:15:04" in text


async def test_replay_completes_and_prints_a_final_summary(tmp_path):
    path = small_capture(tmp_path, first_advert_hex())
    from sighop.monitor.render import render_replay_startup
    from sighop.radio.replay import CaptureReplay

    replay = CaptureReplay.open(path)
    out = io.StringIO()
    run = MonitorRun(
        replay.events(),
        startup=lambda: _immediate(render_replay_startup(replay.provenance, str(path))),
        out=out,
    )
    summary = await run.run()

    assert summary == Summary(
        frames=2,
        decode_failures=1,
        adverts_verified=1,
        adverts_failed=0,
        node_hashes=1,
        reconnects=0,
    )
    assert out.getvalue().strip().endswith("reconnects=0 reboots=0")


async def _immediate(value):
    return value


# --- Live-shaped behaviour, without a device -------------------------------


class SlowStartupSource:
    """Yields a frame immediately, then never ends — the live shape."""

    def __init__(self, events):
        self._events = list(events)

    async def __aiter__(self):
        for event in self._events:
            yield event
        await asyncio.Event().wait()


async def test_frames_decoded_before_the_startup_line_are_held_behind_it():
    """The startup line describes the link, so it cannot arrive second."""
    ready = asyncio.Event()

    async def startup() -> str:
        await ready.wait()
        return "modem: Heltec V3"

    out = io.StringIO()
    run = MonitorRun(
        SlowStartupSource([RxEvent(packet=VALID_ACK, rx_meta=None)]).__aiter__(),
        startup=startup,
        out=out,
    )
    task = asyncio.create_task(run.run())
    await asyncio.sleep(0.02)
    assert out.getvalue() == ""  # held, not printed ahead of the startup line

    ready.set()
    await asyncio.sleep(0.02)
    run.stop()
    await task

    lines = out.getvalue().splitlines()
    assert lines[0] == "modem: Heltec V3"
    assert "ACK" in lines[1]


async def test_stopping_prints_a_final_summary_and_returns_it():
    run = MonitorRun(
        SlowStartupSource([RxEvent(packet=VALID_ACK, rx_meta=None)]).__aiter__(),
        startup=lambda: _immediate("modem: x"),
        out=(out := io.StringIO()),
    )
    task = asyncio.create_task(run.run())
    await asyncio.sleep(0.02)
    run.stop()
    summary = await task

    assert summary.frames == 1
    assert out.getvalue().splitlines()[-1].startswith("-- frames=1")


async def test_a_periodic_summary_is_printed_while_running():
    run = MonitorRun(
        SlowStartupSource([]).__aiter__(),
        startup=lambda: _immediate("modem: x"),
        out=(out := io.StringIO()),
        summary_interval=0.01,
    )
    task = asyncio.create_task(run.run())
    await asyncio.sleep(0.05)
    run.stop()
    await task

    assert out.getvalue().count("-- frames=") >= 2


async def test_monitoring_while_capturing_writes_the_same_format(tmp_path):
    out_path = tmp_path / "c.jsonl"
    writer = CaptureWriter(out_path)
    writer.open()
    events = [
        RxEvent(packet=VALID_ACK, rx_meta=RxMeta(snr_db=2.0, rssi_dbm=-90)),
        UnparsedEvent(raw=b"\xde\xad", reason="dangling escape byte at end of frame"),
    ]
    run = MonitorRun(
        SlowStartupSource(events).__aiter__(),
        startup=lambda: _immediate("modem: x"),
        out=io.StringIO(),
        capture_writer=writer,
        capture_probe=lambda: _immediate(probe_result()),
    )
    task = asyncio.create_task(run.run())
    await asyncio.sleep(0.05)
    run.stop()
    await task
    writer.close()

    records = [json.loads(line) for line in out_path.read_text().splitlines()]
    assert [record["kind"] for record in records] == [
        CAPTURE_META_KIND,
        "rx_frame",
        "unparsed",
    ]
    assert records[0]["device_name"]["value"] == "Heltec V3"
    assert records[1]["rx_meta"] == {"snr_db": 2.0, "rssi_dbm": -90}


async def test_wide_events_go_to_the_log_stream_not_the_rendered_output():
    class RecordingLogger:
        def __init__(self):
            self.events = []

        def info(self, event, **fields):
            self.events.append((event, fields))

        error = info

    logger = RecordingLogger()
    out = io.StringIO()
    run = MonitorRun(
        SlowStartupSource([RxEvent(packet=VALID_ACK, rx_meta=None)]).__aiter__(),
        startup=lambda: _immediate("modem: x"),
        out=out,
        # A recorder standing in for the wide-event logger: the pipeline only
        # ever calls .info/.error on it.
        logger=logger,
    )
    task = asyncio.create_task(run.run())
    await asyncio.sleep(0.02)
    run.stop()
    await task

    assert [name for name, _ in logger.events] == ["packet_rx"]
    assert "packet_rx" not in out.getvalue()


async def test_signal_handlers_install_without_a_device():
    run = MonitorRun(
        SlowStartupSource([]).__aiter__(), startup=lambda: _immediate("x"), out=io.StringIO()
    )
    run.install_signal_handlers()
    task = asyncio.create_task(run.run())
    await asyncio.sleep(0.01)
    run.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await task
