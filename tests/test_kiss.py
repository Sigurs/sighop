
import pytest

from sighop.radio.kiss import (
    DeviceNotFoundError,
    KissDecoder,
    KissFrame,
    KissTransport,
    MalformedFrame,
    Reconnected,
    encode_frame,
    serial_connector,
)

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD


def test_well_formed_frame():
    decoder = KissDecoder()
    events = decoder.feed(bytes((FEND, 0x00, 0x01, 0x02, 0x03, FEND)))
    assert events == [KissFrame(type_byte=0x00, data=bytes((0x01, 0x02, 0x03)))]


def test_escaped_fend_byte():
    decoder = KissDecoder()
    events = decoder.feed(bytes((FEND, 0x00, FESC, TFEND, 0x02, FEND)))
    assert events == [KissFrame(type_byte=0x00, data=bytes((FEND, 0x02)))]


def test_escaped_fesc_byte():
    decoder = KissDecoder()
    events = decoder.feed(bytes((FEND, 0x00, FESC, TFESC, 0x02, FEND)))
    assert events == [KissFrame(type_byte=0x00, data=bytes((FESC, 0x02)))]


def test_empty_frame_between_consecutive_fend():
    decoder = KissDecoder()
    events = decoder.feed(bytes((FEND, 0x00, 0x01, FEND, FEND, 0x06, 0xF9, FEND)))
    assert events == [
        KissFrame(type_byte=0x00, data=bytes((0x01,))),
        KissFrame(type_byte=0x06, data=bytes((0xF9,))),
    ]


def test_dangling_escape_byte_at_end_of_frame():
    decoder = KissDecoder()
    events = decoder.feed(bytes((FEND, 0x00, 0x01, FESC, FEND)))
    assert len(events) == 1
    assert isinstance(events[0], MalformedFrame)
    assert events[0].reason == "dangling escape byte at end of frame"
    assert events[0].raw == bytes((0x00, 0x01, FESC))


def test_invalid_escape_target_reported_malformed():
    decoder = KissDecoder()
    events = decoder.feed(bytes((FEND, 0x00, FESC, 0x41, 0x02, FEND)))
    assert len(events) == 1
    assert isinstance(events[0], MalformedFrame)
    assert events[0].reason == "invalid escape sequence"


def test_decoder_recovers_after_malformed_frame():
    decoder = KissDecoder()
    events = decoder.feed(
        bytes((FEND, 0x00, 0x01, FESC, FEND, 0x00, 0x02, 0x03, FEND))
    )
    assert isinstance(events[0], MalformedFrame)
    assert events[1] == KissFrame(type_byte=0x00, data=bytes((0x02, 0x03)))


def test_decoder_feed_across_multiple_chunks():
    decoder = KissDecoder()
    assert decoder.feed(bytes((FEND, 0x00, 0x01))) == []
    events = decoder.feed(bytes((0x02, FEND)))
    assert events == [KissFrame(type_byte=0x00, data=bytes((0x01, 0x02)))]


def test_encode_round_trip_plain():
    encoded = encode_frame(0x00, bytes((0x01, 0x02, 0x03)))
    decoder = KissDecoder()
    events = decoder.feed(encoded)
    assert events == [KissFrame(type_byte=0x00, data=bytes((0x01, 0x02, 0x03)))]


def test_encode_escapes_fend_and_fesc():
    encoded = encode_frame(0x00, bytes((FEND, FESC)))
    assert encoded == bytes((FEND, 0x00, FESC, TFEND, FESC, TFESC, FEND))
    decoder = KissDecoder()
    events = decoder.feed(encoded)
    assert events == [KissFrame(type_byte=0x00, data=bytes((FEND, FESC)))]


def test_encode_empty_data():
    encoded = encode_frame(0x06)
    decoder = KissDecoder()
    events = decoder.feed(encoded)
    assert events == [KissFrame(type_byte=0x06, data=b"")]


async def test_serial_connector_raises_on_missing_device(tmp_path):
    missing = tmp_path / "does-not-exist"
    connector = serial_connector(str(missing))
    with pytest.raises(DeviceNotFoundError):
        await connector()


async def test_transport_open_fails_fast_on_missing_device(tmp_path):
    missing = tmp_path / "does-not-exist"
    transport = KissTransport(serial_connector(str(missing)))
    with pytest.raises(DeviceNotFoundError):
        await transport.open()


async def test_transport_reconnects_after_disconnect():
    """Drive KissTransport against injected StreamReader/Writer pairs: the
    first connection EOFs immediately, the second yields one frame.
    """
    calls = 0

    class FakeReader:
        def __init__(self, chunks: list[bytes]):
            self._chunks = list(chunks)

        async def read(self, _n: int) -> bytes:
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

    class FakeWriter:
        def close(self) -> None:
            pass

    async def connect():
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeReader([]), FakeWriter()  # EOF immediately -> triggers reconnect
        return FakeReader([bytes((FEND, 0x00, 0xAA, FEND))]), FakeWriter()

    transport = KissTransport(connect, backoff_initial=0.001, backoff_cap=0.01)
    await transport.open()

    events = []
    async for event in transport.frames():
        events.append(event)
        if len(events) == 2:
            break

    assert calls == 2
    assert events == [
        Reconnected(attempts=1),
        KissFrame(type_byte=0x00, data=bytes((0xAA,))),
    ]
