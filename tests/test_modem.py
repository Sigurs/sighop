import pytest

from sighop.radio.kiss import KissFrame, MalformedFrame, Reconnected
from sighop.radio.modem import (
    EU868_NARROW,
    SUB_ERROR,
    SUB_OK,
    SUB_RXMETA,
    TYPE_DATA,
    TYPE_SET_HARDWARE,
    Modem,
    ModemError,
    RxEvent,
    UnparsedEvent,
)


class FakeTransport:
    """Replays a fixed sequence of frame events; records sent bytes.

    Mirrors just the `KissTransport` surface `Modem` uses, so modem-rx
    behavior is testable without any real (or fake) serial I/O.
    """

    def __init__(self, events):
        self._events = list(events)
        self.sent: list[tuple[int, bytes]] = []
        self.opened = False

    async def open(self) -> None:
        self.opened = True

    async def frames(self):
        for event in self._events:
            yield event

    async def send(self, type_byte: int, data: bytes = b"") -> None:
        self.sent.append((type_byte, data))


def ok_frame() -> KissFrame:
    return KissFrame(type_byte=TYPE_SET_HARDWARE, data=bytes((SUB_OK,)))


def error_frame(code: int = 0x02) -> KissFrame:
    return KissFrame(type_byte=TYPE_SET_HARDWARE, data=bytes((SUB_ERROR, code)))


def data_frame(payload: bytes) -> KissFrame:
    return KissFrame(type_byte=TYPE_DATA, data=payload)


def rx_meta_frame(snr_raw: int, rssi_raw: int) -> KissFrame:
    return KissFrame(
        type_byte=TYPE_SET_HARDWARE,
        data=bytes((SUB_RXMETA, snr_raw & 0xFF, rssi_raw & 0xFF)),
    )


async def collect(modem: Modem) -> list:
    return [event async for event in modem.events()]


async def test_handshake_sends_set_radio_and_confirms_ok():
    transport = FakeTransport([ok_frame()])
    modem = Modem(transport, radio_params=EU868_NARROW)
    events = await collect(modem)
    assert events == []
    assert transport.opened is True
    assert transport.sent == [
        (TYPE_SET_HARDWARE, bytes((0x09,)) + EU868_NARROW.to_bytes())
    ]


async def test_handshake_error_response_raises():
    transport = FakeTransport([error_frame(code=0x02)])
    modem = Modem(transport)
    with pytest.raises(ModemError):
        await collect(modem)


async def test_data_then_rxmeta_correlate():
    transport = FakeTransport(
        [ok_frame(), data_frame(bytes((0x01, 0x02))), rx_meta_frame(snr_raw=8, rssi_raw=-90)]
    )
    modem = Modem(transport)
    events = await collect(modem)
    assert len(events) == 1
    assert isinstance(events[0], RxEvent)
    assert events[0].packet == bytes((0x01, 0x02))
    assert events[0].rx_meta.snr_db == pytest.approx(2.0)
    assert events[0].rx_meta.rssi_dbm == -90


async def test_second_data_before_rxmeta_flushes_first_without_meta():
    transport = FakeTransport(
        [
            ok_frame(),
            data_frame(bytes((0x01,))),
            data_frame(bytes((0x02,))),
            rx_meta_frame(snr_raw=4, rssi_raw=-80),
        ]
    )
    modem = Modem(transport)
    events = await collect(modem)
    assert len(events) == 2
    assert events[0] == RxEvent(packet=bytes((0x01,)), rx_meta=None)
    assert events[1].packet == bytes((0x02,))
    assert events[1].rx_meta.rssi_dbm == -80


async def test_data_frame_pending_at_end_of_run_is_flushed():
    transport = FakeTransport([ok_frame(), data_frame(bytes((0x09,)))])
    modem = Modem(transport)
    events = await collect(modem)
    assert events == [RxEvent(packet=bytes((0x09,)), rx_meta=None)]


async def test_negative_snr_and_rssi_parsed_correctly():
    transport = FakeTransport(
        [ok_frame(), data_frame(b"\x00"), rx_meta_frame(snr_raw=-8, rssi_raw=-120)]
    )
    modem = Modem(transport)
    events = await collect(modem)
    assert events[0].rx_meta.snr_db == pytest.approx(-2.0)
    assert events[0].rx_meta.rssi_dbm == -120


async def test_unrecognized_command_byte_reported_unparsed():
    transport = FakeTransport([ok_frame(), KissFrame(type_byte=0x0F, data=b"\x01")])
    modem = Modem(transport)
    events = await collect(modem)
    assert len(events) == 1
    assert isinstance(events[0], UnparsedEvent)
    assert events[0].reason == "unrecognized frame"


async def test_transport_malformed_frame_forwarded_as_unparsed():
    transport = FakeTransport(
        [ok_frame(), MalformedFrame(raw=b"\xde\xad", reason="dangling escape byte at end of frame")]
    )
    modem = Modem(transport)
    events = await collect(modem)
    assert events == [UnparsedEvent(raw=b"\xde\xad", reason="dangling escape byte at end of frame")]


async def test_reconnect_redoes_handshake_and_flushes_pending():
    transport = FakeTransport(
        [
            ok_frame(),
            data_frame(bytes((0x01,))),
            Reconnected(attempts=3),
            ok_frame(),
            data_frame(bytes((0x02,))),
            rx_meta_frame(snr_raw=0, rssi_raw=-70),
        ]
    )
    modem = Modem(transport)
    events = await collect(modem)
    assert events[0] == RxEvent(packet=bytes((0x01,)), rx_meta=None)
    assert events[1].packet == bytes((0x02,))
    assert events[1].rx_meta.rssi_dbm == -70
    # handshake sent once initially, once after reconnect
    assert len(transport.sent) == 2
