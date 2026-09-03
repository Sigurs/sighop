import asyncio
import json

from sighop.radio.capture import CaptureRun
from sighop.radio.kiss import KissTransport, encode_frame
from sighop.radio.modem import SUB_OK, SUB_RXMETA, TYPE_DATA, TYPE_SET_HARDWARE, Modem


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


async def test_capture_run_writes_jsonl_end_to_end(tmp_path):
    """Full pipeline: raw KISS bytes -> KissTransport -> Modem -> CaptureRun -> JSONL file."""
    ok = encode_frame(TYPE_SET_HARDWARE, bytes((SUB_OK,)))
    data = encode_frame(TYPE_DATA, bytes((0xAB, 0xCD)))
    rx_meta = encode_frame(
        TYPE_SET_HARDWARE, bytes((SUB_RXMETA, 8 & 0xFF, (-90) & 0xFF))
    )
    malformed = bytes((0xC0, 0x00, 0xDB, 0xC0))  # dangling escape byte at end of frame

    reader = FakeReader([ok + data + rx_meta + malformed])
    writer = FakeWriter()

    async def connect():
        return reader, writer

    transport = KissTransport(connect)
    modem = Modem(transport)
    out_path = tmp_path / "capture.jsonl"
    run = CaptureRun(modem, out_path, heartbeat_interval=1000)

    async def stop_soon():
        await asyncio.sleep(0.05)
        run.stop()

    await asyncio.gather(run.run(), stop_soon())

    lines = out_path.read_text().strip().splitlines()
    assert len(lines) == 2

    rx_record = json.loads(lines[0])
    assert rx_record["kind"] == "rx_frame"
    assert rx_record["raw_hex"] == bytes((0xAB, 0xCD)).hex()
    assert rx_record["rx_meta"] == {"snr_db": 2.0, "rssi_dbm": -90}
    assert "T" in rx_record["ts"] and ("+" in rx_record["ts"] or "Z" in rx_record["ts"])

    unparsed_record = json.loads(lines[1])
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
    modem = Modem(transport)
    out_path = tmp_path / "capture.jsonl"
    run = CaptureRun(modem, out_path, heartbeat_interval=1000)

    async def stop_soon():
        await asyncio.sleep(0.02)
        run.stop()

    await asyncio.gather(run.run(), stop_soon())

    assert out_path.exists()
    assert out_path.read_text() == ""
