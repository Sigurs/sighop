"""The `SetHardware` request/response exchange (milestone 2, design D2/D3).

The correlation invariants milestone 0 established live in `test_modem.py` and
are deliberately untouched; what is tested here is the new plumbing laid over
them — and, most importantly, that routing a response through the frame loop
does not cost a pending `Data` frame its `RxMeta`.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from sighop.radio.kiss import FrameEvent, KissFrame
from sighop.radio.modem import (
    SUB_ERROR,
    SUB_GET_DEVICE_NAME,
    SUB_GET_VERSION,
    SUB_OK,
    SUB_RESP_DEVICE_NAME,
    SUB_RESP_VERSION,
    SUB_RXMETA,
    SUB_SET_RADIO,
    TYPE_DATA,
    TYPE_SET_HARDWARE,
    ConcurrentRequestError,
    Modem,
    ModemEvent,
    RequestOk,
    RequestRejected,
    RequestTimedOut,
    RxEvent,
    UnparsedEvent,
    boot_banner_reason,
)
from tests.test_modem import set_radio_sends

FAST_TIMEOUT = 0.02


class QueueTransport:
    """A transport whose frames are pushed by the test, so a response can be
    placed at an exact point in the frame stream.
    """

    def __init__(self, *, answer_set_radio: bool = True) -> None:
        self.sent: list[tuple[int, bytes]] = []
        self.opened = False
        self._queue: asyncio.Queue[FrameEvent | None] = asyncio.Queue()
        self._answer_set_radio = answer_set_radio

    async def open(self) -> None:
        self.opened = True

    async def frames(self):
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame

    async def send(self, type_byte: int, data: bytes = b"") -> None:
        self.sent.append((type_byte, data))
        if (
            self._answer_set_radio
            and type_byte == TYPE_SET_HARDWARE
            and data[:1] == bytes((SUB_SET_RADIO,))
        ):
            self.push(sub_frame(SUB_OK))

    def push(self, *frames: FrameEvent) -> None:
        for frame in frames:
            self._queue.put_nowait(frame)


def sub_frame(sub_command: int, data: bytes = b"") -> KissFrame:
    return KissFrame(type_byte=TYPE_SET_HARDWARE, data=bytes((sub_command,)) + data)


def data_frame(payload: bytes) -> KissFrame:
    return KissFrame(type_byte=TYPE_DATA, data=payload)


def rx_meta_frame(snr_raw: int, rssi_raw: int) -> KissFrame:
    return sub_frame(SUB_RXMETA, bytes((snr_raw & 0xFF, rssi_raw & 0xFF)))


@contextlib.asynccontextmanager
async def running(modem: Modem):
    """Drive `events()` in the background, exposing the events it yields."""
    events: list[ModemEvent] = []

    async def consume() -> None:
        async for event in modem.events():
            events.append(event)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(modem.probe_ready.wait(), 2)
        yield events
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def settle() -> None:
    """Give the consumer loop a turn to drain what the test just pushed."""
    for _ in range(10):
        await asyncio.sleep(0)


async def test_request_resolves_with_its_matching_response():
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        pending = asyncio.create_task(modem.request(SUB_GET_VERSION))
        await settle()
        transport.push(sub_frame(SUB_RESP_VERSION, b"\x01\x00"))
        result = await pending

    assert result == RequestOk(
        sub_command=SUB_GET_VERSION, response_code=SUB_RESP_VERSION, data=b"\x01\x00"
    )


async def test_request_rejected_carries_the_modem_error_code():
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        pending = asyncio.create_task(modem.request(SUB_GET_VERSION))
        await settle()
        transport.push(sub_frame(SUB_ERROR, b"\x05"))
        result = await pending

    assert result == RequestRejected(sub_command=SUB_GET_VERSION, error_code=0x05)


async def test_unanswered_request_times_out_and_the_loop_keeps_running():
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem) as events:
        result = await modem.request(SUB_GET_VERSION)
        transport.push(data_frame(b"\x0a"), rx_meta_frame(snr_raw=8, rssi_raw=-90))
        await settle()

    assert result == RequestTimedOut(sub_command=SUB_GET_VERSION)
    assert len(events) == 1
    assert events[0].packet == b"\x0a"
    assert events[0].rx_meta is not None


async def test_a_second_concurrent_request_raises():
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        pending = asyncio.create_task(modem.request(SUB_GET_VERSION, timeout=5))
        await settle()
        with pytest.raises(ConcurrentRequestError):
            await modem.request(SUB_GET_DEVICE_NAME)
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending


async def test_unsolicited_response_is_reported_unparsed():
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem) as events:
        transport.push(sub_frame(SUB_RESP_DEVICE_NAME, b"Heltec V3"))
        await settle()

    assert len(events) == 1
    assert isinstance(events[0], UnparsedEvent)
    assert events[0].reason == "unrecognized frame"


async def test_response_between_data_and_rxmeta_keeps_the_signal_metadata():
    """Design D3: the sharp edge of the request API.

    A probe response landing between a `Data` frame and its `RxMeta` must not
    flush the pending frame — that would silently cost the packet its SNR and
    RSSI, with no error anywhere.
    """
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem) as events:
        pending = asyncio.create_task(modem.request(SUB_GET_VERSION, timeout=5))
        await settle()
        transport.push(
            data_frame(b"\xab\xcd"),
            sub_frame(SUB_RESP_VERSION, b"\x01\x00"),
            rx_meta_frame(snr_raw=8, rssi_raw=-90),
        )
        result = await pending
        await settle()

    assert isinstance(result, RequestOk)
    assert events == [
        RxEvent(packet=b"\xab\xcd", rx_meta=events[0].rx_meta),
    ]
    assert events[0].rx_meta is not None
    assert events[0].rx_meta.snr_db == pytest.approx(2.0)
    assert events[0].rx_meta.rssi_dbm == -90


# --- Device reboot (found live, 2026-09-03) --------------------------------

# The first 96 bytes of a real ESP32-S3 ROM banner, taken verbatim from
# `captures/2026-09-03-2.jsonl`, where it arrived seven times in nine minutes.
BOOT_BANNER = (
    b"ESP-ROM:esp32s3-20210327\r\nBuild:Mar 27 2021\r\n"
    b"rst:0x1 (POWERON),boot:0x29 (SPI_FAST_FLASH_BOOT)\r\nSPIWP:0xee\r\n"
)


def banner_frame() -> KissFrame:
    return KissFrame(type_byte=BOOT_BANNER[0], data=BOOT_BANNER[1:])


def test_boot_banner_is_recognized_with_its_reset_reason():
    assert boot_banner_reason(BOOT_BANNER) == "POWERON"
    assert boot_banner_reason(b"\x06\xf0") is None
    assert boot_banner_reason(b"ESP-ROM:esp32s3 no reason here") == "unknown"


async def test_a_boot_banner_redoes_the_handshake():
    """The board came back on the firmware's build defaults, not on our
    SetRadio -- and the USB bridge never dropped, so nothing else says so.
    """
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem) as events:
        before = len(set_radio_sends(transport))
        transport.push(banner_frame())
        await asyncio.wait_for(_wait_for_reboot(modem), 2)
        await settle()

        assert modem.reboot_count == 1
        assert len(set_radio_sends(transport)) == before + 1

    assert any(
        isinstance(event, UnparsedEvent) and event.reason.startswith("device rebooted")
        for event in events
    ), events


async def test_a_reboot_banner_does_not_discard_a_pending_data_frame():
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem) as events:
        transport.push(data_frame(b"\xab\xcd"), banner_frame())
        await asyncio.wait_for(_wait_for_reboot(modem), 2)
        await settle()

    assert events[0] == RxEvent(packet=b"\xab\xcd", rx_meta=None)


async def _wait_for_reboot(modem: Modem) -> None:
    while modem.reboot_count == 0:
        await asyncio.sleep(0)
