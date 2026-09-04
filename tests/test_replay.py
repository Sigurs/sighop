"""Capture replay: the inverse of the capture writer (milestone 2, design D1).

The round-trip test is the one that matters — it is what keeps `capture.py`
and `replay.py` from drifting apart, since nothing else forces them to agree.
"""

from __future__ import annotations

import asyncio
import json

from sighop.radio.capture import CAPTURE_META_KIND, CaptureRun
from sighop.radio.modem import RxEvent, RxMeta, UnparsedEvent
from sighop.radio.replay import CaptureReplay
from tests.test_capture import StubModem, probe_result, run_until_stopped


def write(path, *records) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


async def test_a_capture_run_round_trips_through_replay(tmp_path):
    """Write with `CaptureRun`, read back with `CaptureReplay`, compare."""
    written = [
        RxEvent(packet=b"\xab\xcd", rx_meta=RxMeta(snr_db=2.0, rssi_dbm=-90)),
        RxEvent(packet=b"\x01", rx_meta=None),
        UnparsedEvent(raw=b"\xde\xad", reason="dangling escape byte at end of frame"),
    ]
    out_path = tmp_path / "capture.jsonl"
    run = CaptureRun(
        StubModem(written), out_path, probe_result=probe_result(), heartbeat_interval=1000
    )
    await run_until_stopped(run, delay=0.05)

    replay = CaptureReplay.open(out_path)
    replayed = [event async for event in replay.events()]

    assert replay.provenance is not None
    assert replay.provenance["kind"] == CAPTURE_META_KIND
    assert replay.unreadable == []
    assert len(replayed) == len(written)
    for original, event in zip(written, replayed, strict=True):
        assert type(event) is type(original)
        assert event.received_at is not None
        if isinstance(original, RxEvent):
            assert event.packet == original.packet
            assert event.rx_meta == original.rx_meta
        else:
            assert event.raw == original.raw
            assert event.reason == original.reason


async def test_replay_exposes_the_provenance_header_without_decoding_it(tmp_path):
    path = tmp_path / "c.jsonl"
    write(
        path,
        {"ts": "2026-09-03T20:00:00+00:00", "kind": CAPTURE_META_KIND, "sighop": {}},
        {"ts": "2026-09-03T20:00:01+00:00", "kind": "rx_frame", "raw_hex": "0102", "rx_meta": None},
    )

    replay = CaptureReplay.open(path)
    events = [event async for event in replay.events()]

    assert replay.provenance["kind"] == CAPTURE_META_KIND
    assert len(events) == 1
    assert events[0].packet == b"\x01\x02"


async def test_a_headerless_file_replays_with_provenance_reported_absent(tmp_path):
    """The two milestone 0 captures predate the header and are not rewritten."""
    path = tmp_path / "c.jsonl"
    write(
        path,
        {"ts": "2026-09-03T20:00:01+00:00", "kind": "rx_frame", "raw_hex": "0102", "rx_meta": None},
    )

    replay = CaptureReplay.open(path)
    events = [event async for event in replay.events()]

    assert replay.provenance is None
    assert len(events) == 1


async def test_a_null_rx_meta_yields_an_event_with_no_signal_values(tmp_path):
    path = tmp_path / "c.jsonl"
    write(
        path,
        {"ts": "2026-09-03T20:00:01+00:00", "kind": "rx_frame", "raw_hex": "01", "rx_meta": None},
    )

    events = [event async for event in CaptureReplay.open(path).events()]

    assert events[0].rx_meta is None  # not RxMeta(0.0, 0)


async def test_recorded_timestamps_are_carried_onto_the_events(tmp_path):
    path = tmp_path / "c.jsonl"
    write(
        path,
        {"ts": "2026-09-02T21:15:04+00:00", "kind": "rx_frame", "raw_hex": "01", "rx_meta": None},
    )

    events = [event async for event in CaptureReplay.open(path).events()]

    assert events[0].received_at.isoformat() == "2026-09-02T21:15:04+00:00"


async def test_a_truncated_final_line_is_reported_and_the_rest_still_replays(tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text(
        json.dumps(
            {"ts": "2026-09-03T20:00:01+00:00", "kind": "rx_frame", "raw_hex": "01", "rx_meta": None}
        )
        + "\n"
        + '{"ts": "2026-09-03T20:00:02+00:00", "kind": "rx_fra'
    )

    replay = CaptureReplay.open(path)
    events = [event async for event in replay.events()]

    assert len(events) == 1
    assert len(replay.unreadable) == 1
    assert replay.unreadable[0].line_number == 2
    assert "invalid JSON" in replay.unreadable[0].reason


async def test_an_unknown_record_kind_is_reported_not_skipped(tmp_path):
    path = tmp_path / "c.jsonl"
    write(
        path,
        {"ts": "2026-09-03T20:00:01+00:00", "kind": "heartbeat"},
        {"ts": "2026-09-03T20:00:02+00:00", "kind": "rx_frame", "raw_hex": "01", "rx_meta": None},
    )

    replay = CaptureReplay.open(path)
    events = [event async for event in replay.events()]

    assert len(events) == 1
    assert [line.line_number for line in replay.unreadable] == [1]
    assert "heartbeat" in replay.unreadable[0].reason


async def test_a_record_missing_a_required_field_is_reported(tmp_path):
    path = tmp_path / "c.jsonl"
    write(path, {"ts": "2026-09-03T20:00:01+00:00", "kind": "rx_frame"})

    replay = CaptureReplay.open(path)
    events = [event async for event in replay.events()]

    assert events == []
    assert len(replay.unreadable) == 1
    assert "malformed rx_frame record" in replay.unreadable[0].reason


async def test_every_corpus_capture_replays_with_the_provenance_it_has(tmp_path):
    """Both corpus generations, read by the same reader: the milestone 0 files
    have no `capture_meta` line and must replay anyway, the milestone 2 files
    carry one and must have it read back rather than decoded as a frame.
    """
    from tests.protocol.corpus import (
        CAPTURE_FILES,
        CAPTURES_DIR,
        EXPECTED_FRAMES_PER_FILE,
        SIDECAR_PROVENANCE_FILES,
    )

    for name in CAPTURE_FILES:
        replay = CaptureReplay.open(CAPTURES_DIR / name)
        events = [event async for event in replay.events()]

        if name in SIDECAR_PROVENANCE_FILES:
            assert replay.provenance is None, f"{name} unexpectedly carries a header"
        else:
            assert replay.provenance is not None, f"{name} lost its header"
            assert replay.provenance["kind"] == "capture_meta"
        assert replay.unreadable == [], f"{name} has unreadable lines: {replay.unreadable}"
        rx_events = [event for event in events if isinstance(event, RxEvent)]
        assert len(rx_events) == EXPECTED_FRAMES_PER_FILE[name]


async def test_replay_does_not_pace_itself(tmp_path):
    """Hours of recorded gaps, replayed in the time it takes to read them."""
    path = tmp_path / "c.jsonl"
    write(
        path,
        {"ts": "2026-09-02T20:00:00+00:00", "kind": "rx_frame", "raw_hex": "01", "rx_meta": None},
        {"ts": "2026-09-03T05:16:00+00:00", "kind": "rx_frame", "raw_hex": "02", "rx_meta": None},
    )

    loop = asyncio.get_running_loop()
    start = loop.time()
    events = [event async for event in CaptureReplay.open(path).events()]

    assert len(events) == 2
    assert loop.time() - start < 1.0
