"""Capture replay: the inverse of the capture writer (milestone 2, design D1).

The round-trip test is the one that matters — it is what keeps `capture.py`
and `replay.py` from drifting apart, since nothing else forces them to agree.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from sighop.radio.capture import CAPTURE_META_KIND
from sighop.radio.modem import RxEvent, RxMeta, UnparsedEvent
from sighop.radio.replay import CaptureReplay
from tests.test_capture import StubModem, capture, probe_result


def write(path, *records) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


async def test_a_capture_round_trips_through_replay(tmp_path):
    """Write with `CaptureWriter`, read back with `CaptureReplay`, compare."""
    written = [
        RxEvent(packet=b"\xab\xcd", rx_meta=RxMeta(snr_db=2.0, rssi_dbm=-90)),
        RxEvent(packet=b"\x01", rx_meta=None),
        UnparsedEvent(raw=b"\xde\xad", reason="dangling escape byte at end of frame"),
    ]
    out_path = tmp_path / "capture.jsonl"
    await capture(StubModem(written), out_path, seconds=0.05, probe=probe_result())

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
            {
                "ts": "2026-09-03T20:00:01+00:00",
                "kind": "rx_frame",
                "raw_hex": "01",
                "rx_meta": None,
            }
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
    """Every corpus file carries a synthetic `capture_meta` header, which must be
    read back as provenance rather than decoded as a frame.
    """
    from tests.protocol.corpus import CORPUS_DIR, CORPUS_FILES, EXPECTED_FRAMES_PER_FILE

    for name in CORPUS_FILES:
        replay = CaptureReplay.open(CORPUS_DIR / name)
        events = [event async for event in replay.events()]

        assert replay.provenance is not None, f"{name} lost its header"
        assert replay.provenance["kind"] == "capture_meta"
        assert replay.provenance["synthetic"] is True
        assert replay.unreadable == [], f"{name} has unreadable lines: {replay.unreadable}"
        rx_events = [event for event in events if isinstance(event, RxEvent)]
        # A file's frame records are its receptions plus, for a capture from a
        # transmitting run, the frames sighop sent. Only the receptions are
        # replayed; the rest are counted as passed over, never as unreadable.
        assert len(rx_events) + replay.transmitted_skipped == EXPECTED_FRAMES_PER_FILE[name], name


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


# --- The parity harness: `python -m sighop.replay` (`capture-replay`) ---------


def test_the_harness_renders_a_committed_capture_and_exits_zero(capsys) -> None:
    from sighop.replay import main
    from tests.protocol.corpus import AMBIENT, CORPUS_DIR

    assert main([str(CORPUS_DIR / AMBIENT)]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("replaying ")
    assert "\n-- frames=" in printed


@pytest.mark.parametrize("argv", [[], ["one.jsonl", "two.jsonl"]])
def test_the_harness_takes_exactly_one_capture_path(argv: list[str], capsys) -> None:
    from sighop.replay import main

    assert main(argv) == 2
    captured = capsys.readouterr()
    assert "exactly one capture path" in captured.err
    assert captured.out == ""


def test_a_malformed_line_is_reported_and_the_rest_still_renders(tmp_path, capsys) -> None:
    from sighop.replay import main

    path = tmp_path / "capture.jsonl"
    write(
        path,
        {"ts": "2026-09-04T12:00:00+00:00", "kind": "rx_frame", "raw_hex": "1200", "rx_meta": None},
    )
    with path.open("a") as file:
        file.write("{not json\n")

    assert main([str(path)]) == 1
    captured = capsys.readouterr()
    assert "unreadable capture line 2: invalid JSON" in captured.err
    assert "-- frames=1 " in captured.out


def test_the_harness_needs_no_database(monkeypatch, capsys) -> None:
    """It is not a node: with no database anywhere, it still renders."""
    from sighop.replay import main
    from tests.protocol.corpus import AMBIENT, CORPUS_DIR

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SIGHOP_TEST_DATABASE_URL", raising=False)
    assert main([str(CORPUS_DIR / AMBIENT)]) == 0


def test_the_harness_reads_the_radio_from_the_capture_header() -> None:
    from sighop.replay import replay_radio
    from tests.protocol.corpus import AMBIENT, CORPUS_DIR

    replay = CaptureReplay.open(CORPUS_DIR / AMBIENT)
    radio = replay_radio(replay.provenance)

    assert radio is not None
    assert radio.sf == 8
    assert radio.bw_hz == 62_500


def test_a_headerless_capture_reports_no_radio_rather_than_a_guess(tmp_path) -> None:
    from sighop.replay import replay_radio

    path = tmp_path / "capture.jsonl"
    write(path, {"ts": "2026-09-04T12:00:00+00:00", "kind": "rx_frame", "raw_hex": "1200"})
    assert replay_radio(CaptureReplay.open(path).provenance) is None
