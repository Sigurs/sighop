import asyncio
import json

import pytest

from sighop.radio.capture import CAPTURE_META_KIND, CaptureRun, capture_meta_record
from sighop.radio.kiss import KissTransport, encode_frame
from sighop.radio.modem import (
    EU868_NARROW,
    SUB_OK,
    SUB_RXMETA,
    TYPE_DATA,
    TYPE_SET_HARDWARE,
    Modem,
    RadioParams,
)
from sighop.radio.probe import AbsenceReason, Absent, FirmwareVersion, ProbeResult

# Short enough that the startup probe's unanswered sub-commands resolve within
# a test, rather than holding frame records behind the header for 14 seconds.
FAST_TIMEOUT = 0.005


class FakeReader:
    """Replays fixed chunks, then blocks forever (simulating an idle link)
    until the awaiting task is cancelled.
    """

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FakeWriter:
    def __init__(self):
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


def probe_result(**overrides) -> ProbeResult:
    fields = {
        "configured_radio": EU868_NARROW,
        "device_name": "Heltec V3",
        "radio": EU868_NARROW,
        "tx_power_dbm": -20,
        "firmware_version": FirmwareVersion(version=1, reserved=0),
        "battery_mv": 4021,
        "mcu_temp_tenths_c": 253,
        "sensors_raw": bytes.fromhex("01670115"),
    }
    return ProbeResult(**{**fields, **overrides})


async def run_until_stopped(run: CaptureRun, delay: float = 0.2) -> None:
    async def stop_soon():
        await asyncio.sleep(delay)
        run.stop()

    await asyncio.gather(run.run(), stop_soon())


def records(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def test_capture_run_writes_jsonl_end_to_end(tmp_path):
    """Full pipeline: raw KISS bytes -> KissTransport -> Modem -> CaptureRun -> JSONL file."""
    ok = encode_frame(TYPE_SET_HARDWARE, bytes((SUB_OK,)))
    data = encode_frame(TYPE_DATA, bytes((0xAB, 0xCD)))
    rx_meta = encode_frame(TYPE_SET_HARDWARE, bytes((SUB_RXMETA, 8 & 0xFF, (-90) & 0xFF)))
    malformed = bytes((0xC0, 0x00, 0xDB, 0xC0))  # dangling escape byte at end of frame

    reader = FakeReader([ok + data + rx_meta + malformed])
    writer = FakeWriter()

    async def connect():
        return reader, writer

    transport = KissTransport(connect)
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    out_path = tmp_path / "capture.jsonl"
    run = CaptureRun(modem, out_path, heartbeat_interval=1000)

    await run_until_stopped(run)

    written = records(out_path)
    assert len(written) == 3
    assert written[0]["kind"] == CAPTURE_META_KIND

    rx_record = written[1]
    assert rx_record["kind"] == "rx_frame"
    assert rx_record["raw_hex"] == bytes((0xAB, 0xCD)).hex()
    assert rx_record["rx_meta"] == {"snr_db": 2.0, "rssi_dbm": -90}
    assert "T" in rx_record["ts"] and ("+" in rx_record["ts"] or "Z" in rx_record["ts"])

    unparsed_record = written[2]
    assert unparsed_record["kind"] == "unparsed"
    assert unparsed_record["reason"] == "dangling escape byte at end of frame"

    # setup frame written to the writer: SetHardware/SetRadio request
    assert bytes((0xC0, TYPE_SET_HARDWARE, 0x09)) in bytes(writer.written)


async def test_capture_run_stops_gracefully_and_flushes(tmp_path):
    ok = encode_frame(TYPE_SET_HARDWARE, bytes((SUB_OK,)))
    reader = FakeReader([ok])
    writer = FakeWriter()

    async def connect():
        return reader, writer

    transport = KissTransport(connect)
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    out_path = tmp_path / "capture.jsonl"
    run = CaptureRun(modem, out_path, heartbeat_interval=1000)

    await run_until_stopped(run)

    # No frames arrived, but the probe answered, so the file holds its header
    # and nothing else.
    written = records(out_path)
    assert [record["kind"] for record in written] == [CAPTURE_META_KIND]


# --- Provenance header -----------------------------------------------------


def test_header_record_carries_the_probed_values_and_the_sighop_build():
    record = capture_meta_record(probe_result())

    assert record["kind"] == CAPTURE_META_KIND
    assert record["device_name"]["value"] == "Heltec V3"
    assert record["radio"]["value"] == EU868_NARROW.as_json()
    assert record["tx_power_dbm"]["value"] == -20
    assert record["firmware_version"]["value"] == {"version": 1, "reserved": 0}
    assert record["battery_mv"]["value"] == 4021
    assert record["mcu_temp_tenths_c"]["value"] == 253
    assert record["sensors_raw_hex"]["value"] == "01670115"
    assert set(record["sighop"]) == {"version", "commit_hash"}


def test_header_record_writes_an_unanswered_field_as_null_with_its_reason():
    record = capture_meta_record(
        probe_result(
            device_name=Absent(reason=AbsenceReason.UNSUPPORTED, error_code=0x05),
            battery_mv=Absent(reason=AbsenceReason.TIMEOUT),
        )
    )

    assert record["device_name"] == {
        "value": None,
        "reason": "unsupported",
        "error_code": 0x05,
        "detail": "",
    }
    assert record["battery_mv"]["value"] is None
    assert record["battery_mv"]["reason"] == "timeout"


def test_header_record_keeps_the_read_back_and_configured_radio_apart():
    """Design D6: a disagreement has to be visible in the file itself."""
    observed = RadioParams(freq_hz=869_525_000, bw_hz=250_000, sf=11, cr=5)
    record = capture_meta_record(probe_result(radio=observed))

    assert record["radio"]["value"] == observed.as_json()
    assert record["configured_radio"] == EU868_NARROW.as_json()
    assert record["radio_matches_configured"] is False


def test_header_record_states_an_absent_probe_rather_than_omitting_it():
    record = capture_meta_record(None)

    assert record["probe"] is None
    assert record["probe_absent_reason"]


class StubModem:
    """A modem that yields a fixed event list — no transport, no device."""

    def __init__(self, events):
        self._events = list(events)
        self.reconnect_count = 0
        self.probe_result = None
        self.probe_ready = asyncio.Event()

    async def events(self):
        for event in self._events:
            yield event
        await asyncio.Event().wait()


async def test_header_precedes_a_frame_that_arrived_during_probing(tmp_path):
    from sighop.radio.modem import RxEvent, RxMeta

    modem = StubModem([RxEvent(packet=b"\x01\x02", rx_meta=RxMeta(snr_db=2.0, rssi_dbm=-90))])
    out_path = tmp_path / "capture.jsonl"
    run = CaptureRun(modem, out_path, probe_result=probe_result(), heartbeat_interval=1000)

    await run_until_stopped(run, delay=0.05)

    kinds = [record["kind"] for record in records(out_path)]
    assert kinds == [CAPTURE_META_KIND, "rx_frame"]


async def test_appending_to_a_non_empty_capture_file_adds_no_second_header(tmp_path):
    from sighop.radio.modem import RxEvent

    out_path = tmp_path / "capture.jsonl"
    out_path.write_text(json.dumps({"ts": "x", "kind": "rx_frame", "raw_hex": "00"}) + "\n")

    modem = StubModem([RxEvent(packet=b"\x03", rx_meta=None)])
    run = CaptureRun(modem, out_path, probe_result=probe_result(), heartbeat_interval=1000)

    await run_until_stopped(run, delay=0.05)

    kinds = [record["kind"] for record in records(out_path)]
    assert kinds == ["rx_frame", "rx_frame"]
    assert CAPTURE_META_KIND not in kinds


@pytest.mark.parametrize("existing", ["", None])
async def test_a_new_or_empty_file_gets_a_header(tmp_path, existing):
    from sighop.radio.modem import RxEvent

    out_path = tmp_path / "capture.jsonl"
    if existing is not None:
        out_path.write_text(existing)

    modem = StubModem([RxEvent(packet=b"\x03", rx_meta=None)])
    run = CaptureRun(modem, out_path, probe_result=probe_result(), heartbeat_interval=1000)

    await run_until_stopped(run, delay=0.05)

    assert records(out_path)[0]["kind"] == CAPTURE_META_KIND
