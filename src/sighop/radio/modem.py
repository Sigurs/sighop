"""Modem-level receive semantics on top of `KissTransport`.

Owns: decoding `Data` frames, correlating `RxMeta` with the `Data` frame it
follows, the `SetHardware` request/response exchange, and the startup
handshake (`SetRadio` plus the capability probe) needed to bring the link up.
No TX path — that's a later milestone; nothing here sends `Data`,
`SetTxPower` or `Reboot`.

Protocol reference: `related-repos/MeshCore/docs/kiss_modem_protocol.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import re
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from sighop.logging import get_logger
from sighop.radio.kiss import (
    FrameEvent,
    KissFrame,
    MalformedFrame,
    Reconnected,
    Transport,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle broken at runtime, see _run_probe
    from sighop.radio.probe import ProbeResult

TYPE_DATA = 0x00
TYPE_SET_HARDWARE = 0x06

# Request sub-commands (host -> TNC), `kiss_modem_protocol.md` "Request
# Sub-commands". Only queries and `SetRadio` appear here: this milestone is
# receive-only by construction, so `SetTxPower` (0x0A), `Data` and `Reboot`
# (0x18) are deliberately absent, and `tests/test_receive_only.py` checks that
# no code in `radio/` sends them.
SUB_SET_RADIO = 0x09
SUB_GET_RADIO = 0x0B
SUB_GET_TX_POWER = 0x0C
SUB_GET_VERSION = 0x11
SUB_GET_BATTERY = 0x13
SUB_GET_MCU_TEMP = 0x14
SUB_GET_SENSORS = 0x15
SUB_GET_DEVICE_NAME = 0x16

# Response sub-commands (TNC -> host). `response = request | 0x80`, except the
# generic and unsolicited codes in the 0xF0+ range.
RESPONSE_BIT = 0x80
SUB_RESP_RADIO = 0x8B
SUB_RESP_TX_POWER = 0x8C
SUB_RESP_VERSION = 0x91
SUB_RESP_BATTERY = 0x93
SUB_RESP_MCU_TEMP = 0x94
SUB_RESP_SENSORS = 0x95
SUB_RESP_DEVICE_NAME = 0x96

SUB_OK = 0xF0
SUB_ERROR = 0xF1
SUB_RXMETA = 0xF9

# Error codes, `kiss_modem_protocol.md` "Error Codes". The two that mean "this
# board does not do that" rather than "you asked wrongly".
ERROR_NO_CALLBACK = 0x03
ERROR_UNKNOWN_CMD = 0x05

# GetSensors takes a permissions byte: base | location | environment.
SENSOR_PERMISSIONS_ALL = 0x07

DEFAULT_REQUEST_TIMEOUT_SECONDS = 2.0

# The handshake gets its own, longer budget and a retry, because the first
# `SetRadio` can be sent into a board that is still booting and is simply lost.
# Observed live on the Heltec V3, 2026-09-03: the modem answered nothing for
# ~2 s after open and then answered every probe within 19 ms. (The board was
# resetting on a fault of its own; see `serial_connector` for why the tempting
# DTR/RTS explanation is wrong.)
HANDSHAKE_TIMEOUT_SECONDS = 5.0
HANDSHAKE_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class RadioParams:
    """SetRadio parameters. Wire format is little-endian: freq(4) bw(4) sf(1) cr(1)."""

    freq_hz: int
    bw_hz: int
    sf: int
    cr: int

    def to_bytes(self) -> bytes:
        return struct.pack("<IIBB", self.freq_hz, self.bw_hz, self.sf, self.cr)

    @classmethod
    def from_bytes(cls, data: bytes) -> RadioParams:
        """Parse a `Radio` (0x8B) response body. Raises `ValueError` if short."""
        if len(data) < 10:
            raise ValueError(f"radio parameters need 10 bytes, got {len(data)}")
        freq_hz, bw_hz, sf, cr = struct.unpack("<IIBB", data[:10])
        return cls(freq_hz=freq_hz, bw_hz=bw_hz, sf=sf, cr=cr)

    def as_json(self) -> dict[str, int]:
        return {"freq_hz": self.freq_hz, "bw_hz": self.bw_hz, "sf": self.sf, "cr": self.cr}


# EU/UK 868 "narrow": 869.618 MHz, BW 62.5 kHz, SF8, CR8 -- confirmed against
# https://www.meshcore.ch/settings/ ("the default setting for pretty much all
# of Europe and the UK"), 2026-09-02. NOT 869.525 MHz: that frequency is the
# firmware's *legacy* default (paired with the old 250 kHz/SF11 preset,
# per docs/cli_commands.md's `869.525,250,11,5`), not the narrow preset's
# center frequency -- an earlier version of this default used 869.525/SF7/CR5
# by mistake, guessed from the general "narrow" trend without checking the
# EU-specific numbers, and it received nothing against the live mesh.
EU868_NARROW = RadioParams(freq_hz=869_618_000, bw_hz=62_500, sf=8, cr=8)


@dataclass(frozen=True, slots=True)
class RxMeta:
    snr_db: float
    rssi_dbm: int


@dataclass(frozen=True, slots=True)
class RxEvent:
    packet: bytes
    rx_meta: RxMeta | None
    received_at: dt.datetime | None = None
    """When the frame was received, for sources that know it.

    The live modem leaves this unset -- it has no clock of its own worth
    trusting over the consumer's -- and the RX pipeline stamps ingress time.
    `radio/replay.py` fills it with the timestamp recorded in the capture file
    so a replayed frame carries its original time (design D10).
    """


@dataclass(frozen=True, slots=True)
class UnparsedEvent:
    raw: bytes
    reason: str
    received_at: dt.datetime | None = None


ModemEvent = RxEvent | UnparsedEvent


@dataclass(frozen=True, slots=True)
class RequestOk:
    """A `SetHardware` request answered by its matching response (or `OK`)."""

    sub_command: int
    response_code: int
    data: bytes


@dataclass(frozen=True, slots=True)
class RequestRejected:
    """The modem answered `Error` (0xF1). `error_code` is its code byte."""

    sub_command: int
    error_code: int | None


@dataclass(frozen=True, slots=True)
class RequestTimedOut:
    """No matching response or `Error` arrived before the request timeout."""

    sub_command: int


RequestResult = RequestOk | RequestRejected | RequestTimedOut


class ModemError(RuntimeError):
    """The modem rejected a SetHardware request (Error 0xF1 response)."""


class ConcurrentRequestError(RuntimeError):
    """A second request was issued while one was still outstanding.

    The modem answers serially and correlation is by response code alone, so a
    concurrent request is a programming error, not a queueing problem
    (design D2).
    """


@dataclass(slots=True)
class _PendingRequest:
    sub_command: int
    response_code: int
    future: asyncio.Future[RequestResult]


class _StreamEnded:
    """Sentinel: the transport's frame iterator is exhausted."""


_STREAM_ENDED = _StreamEnded()


DEVICE_REBOOT_REASON = "device rebooted"

_BOOT_BANNER_MARKERS = (b"ESP-ROM:", b"rst:0x")
_RESET_REASON_PATTERN = re.compile(rb"rst:0x[0-9a-fA-F]+ \(([A-Z0-9_]+)\)")


def boot_banner_reason(raw: bytes) -> str | None:
    """The reset reason if `raw` is an ESP32 ROM boot banner, else None.

    The banner arrives interleaved in the KISS stream as unframed bytes —
    DESIGN.md §4.1 already anticipated ESP32 output surfacing that way. It is
    the only signal that the board restarted, so it is worth reading rather
    than filing under "unrecognized".
    """
    if not any(marker in raw for marker in _BOOT_BANNER_MARKERS):
        return None
    match = _RESET_REASON_PATTERN.search(raw)
    return match.group(1).decode("ascii") if match else "unknown"


def _parse_rx_meta(data: bytes) -> RxMeta:
    snr_raw, rssi_raw = struct.unpack("<bb", data[:2])
    return RxMeta(snr_db=snr_raw * 0.25, rssi_dbm=rssi_raw)


class Modem:
    """Owns the startup handshake, RX-side frame interpretation, and the
    `SetHardware` request/response exchange.

    Everything reads from a single frame iterator: a second call to
    `transport.frames()` would open an independent generator racing this one
    for the same bytes. Requests therefore resolve from inside that one loop
    (design D2) -- pumped by the handshake before the loop starts, and by the
    `events()` consumer afterwards.
    """

    def __init__(
        self,
        transport: Transport,
        radio_params: RadioParams = EU868_NARROW,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        handshake_timeout: float | None = None,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._transport = transport
        self._radio_params = radio_params
        self._request_timeout = request_timeout
        self._handshake_timeout = (
            HANDSHAKE_TIMEOUT_SECONDS if handshake_timeout is None else handshake_timeout
        )
        self._logger = logger or get_logger(component="modem")
        self.reconnect_count = 0
        self.probe_result: ProbeResult | None = None
        self.probe_ready = asyncio.Event()
        self._frame_iter: AsyncIterator[FrameEvent] | None = None
        self._next_frame_task: asyncio.Future[FrameEvent] | None = None
        self._pending_data: KissFrame | None = None
        self._pending_request: _PendingRequest | None = None
        self._buffered: list[ModemEvent] = []
        self._consumer_driving = False
        self._probe_task: asyncio.Task[None] | None = None
        self._device_restarted = False
        self.reboot_count = 0

    @property
    def radio_params(self) -> RadioParams:
        """The parameters applied via `SetRadio` — configured, not read back."""
        return self._radio_params

    # --- Public API --------------------------------------------------------

    async def events(self) -> AsyncIterator[ModemEvent]:
        """Open the link, run the startup handshake and probe, then yield
        RX/unparsed events forever -- redoing both after every reconnect.
        """
        await self._transport.open()
        self._frame_iter = self._transport.frames()
        try:
            while True:
                await self._start_link()
                for event in self._drain_buffered():
                    yield event

                restart = False
                while True:
                    frame = await self._next_frame(None)
                    if isinstance(frame, _StreamEnded):
                        break
                    assert frame is not None  # only a timeout yields None
                    if isinstance(frame, Reconnected):
                        self.reconnect_count += 1
                        await self._cancel_probe()
                        for event in self._flush_pending_data():
                            yield event
                        restart = True
                        break
                    for event in self._handle_frame(frame):
                        yield event
                    if self._device_restarted:
                        # The board rebooted underneath us without the USB
                        # bridge ever dropping, so nothing else would have
                        # told us. Redo the handshake: it came back on the
                        # firmware's build defaults, not on our SetRadio.
                        await self._cancel_probe()
                        restart = True
                        break

                if not restart:
                    break

            for event in self._flush_pending_data():
                yield event
        finally:
            await self._cancel_probe()
            self._cancel_next_frame()

    async def request(
        self,
        sub_command: int,
        data: bytes = b"",
        *,
        timeout: float | None = None,
    ) -> RequestResult:
        """Send a `SetHardware` sub-command and resolve with its response.

        Correlation is by the `response = request | 0x80` convention, with
        `OK` (0xF0) accepted as the answer to the commands that have no
        dedicated response code, and `Error` (0xF1) resolving as a rejection.
        Never raises on a modem-side failure: a rejection and a timeout are
        both values, so the frame loop is never aborted by one.
        """
        if self._pending_request is not None:
            raise ConcurrentRequestError(
                f"request 0x{sub_command:02x} issued while 0x"
                f"{self._pending_request.sub_command:02x} is still outstanding"
            )
        wait = self._request_timeout if timeout is None else timeout
        future: asyncio.Future[RequestResult] = asyncio.get_running_loop().create_future()
        self._pending_request = _PendingRequest(
            sub_command=sub_command,
            response_code=sub_command | RESPONSE_BIT,
            future=future,
        )
        try:
            await self._transport.send(
                TYPE_SET_HARDWARE, bytes((sub_command,)) + data
            )
            if self._consumer_driving:
                try:
                    return await asyncio.wait_for(future, wait)
                except TimeoutError:
                    return RequestTimedOut(sub_command=sub_command)
            resolved = await self._pump_until_resolved(future, wait)
            return resolved if resolved is not None else RequestTimedOut(sub_command)
        finally:
            self._pending_request = None

    # --- Link startup ------------------------------------------------------

    async def _start_link(self) -> None:
        """Handshake, then start the capability probe alongside the RX loop.

        The probe runs as a task rather than inline so frames keep flowing
        while the board is being questioned: its requests resolve from the
        same loop that yields RX events.
        """
        self.probe_ready.clear()
        self._device_restarted = False
        await self._handshake()
        self._consumer_driving = True
        self._probe_task = asyncio.create_task(self._run_probe())

    async def _handshake(self) -> None:
        """Apply the configured radio parameters and confirm the response.

        Runs before the `events()` loop starts, so it pumps the shared frame
        iterator itself; frames decoded while it waits are buffered and
        yielded once the link is up.
        """
        self._consumer_driving = False
        loop = asyncio.get_running_loop()
        # A budget rather than a count of tries: a reconnect resolves an
        # in-flight request instantly, so counting attempts would spend the
        # whole allowance in microseconds without ever having waited.
        deadline = loop.time() + self._handshake_timeout * HANDSHAKE_ATTEMPTS
        attempt = 0
        while True:
            attempt += 1
            result = await self.request(
                SUB_SET_RADIO,
                self._radio_params.to_bytes(),
                timeout=min(self._handshake_timeout, max(deadline - loop.time(), 0.0)),
            )
            match result:
                case RequestRejected(error_code=code):
                    raise ModemError(f"SetRadio rejected: error code {code}")
                case RequestOk():
                    self._logger.info(
                        "modem_ready",
                        radio_params=self._radio_params.as_json(),
                        attempts=attempt,
                    )
                    return
                case RequestTimedOut():
                    if loop.time() >= deadline:
                        break
        # Not fatal: milestone 0 tolerated a silent modem here, and the probe
        # that follows will say plainly whether the board is answering at all.
        # It is still an error event -- this layer is the only observability
        # the radio has (§4.1), and an unapplied SetRadio means the board is
        # running whatever it was running before.
        self._logger.error(
            "modem_handshake_unanswered",
            radio_params=self._radio_params.as_json(),
            timeout_seconds=self._handshake_timeout,
            attempts=attempt,
        )

    async def _run_probe(self) -> None:
        # Imported here, not at module scope: `probe.py` needs this module's
        # sub-command constants and request types, so the dependency has to
        # run one way at import time.
        from sighop.radio.probe import run_probe

        try:
            self.probe_result = await run_probe(
                self, self._radio_params, logger=self._logger
            )
        finally:
            self.probe_ready.set()

    async def _cancel_probe(self) -> None:
        task, self._probe_task = self._probe_task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # --- Frame handling ----------------------------------------------------

    def _handle_frame(self, frame: KissFrame | MalformedFrame) -> list[ModemEvent]:
        """Interpret one frame, returning the events it produces.

        Pure with respect to I/O so both the handshake pump and the `events()`
        loop can share it, and so correlation state lives in exactly one place.
        """
        if isinstance(frame, MalformedFrame):
            flushed = self._flush_pending_data()
            # A boot banner can land here too: it is unframed text, so whether
            # it decodes as a frame or as garbage depends on which bytes it
            # happens to contain.
            if (reset_reason := boot_banner_reason(frame.raw)) is not None:
                return [*flushed, *self._device_rebooted(frame.raw, reset_reason)]
            return [*flushed, UnparsedEvent(raw=frame.raw, reason=frame.reason)]

        if frame.type_byte == TYPE_DATA:
            events: list[ModemEvent] = []
            if self._pending_data is not None:
                self._logger.error(
                    "rx_meta_correlation_anomaly",
                    reason="next Data frame arrived before RxMeta for the previous one",
                )
                events += self._flush_pending_data()
            self._pending_data = frame
            return events

        if frame.type_byte == TYPE_SET_HARDWARE and frame.data:
            sub_command, sub_data = frame.data[0], frame.data[1:]
            if sub_command == SUB_RXMETA:
                return self._handle_rx_meta(frame, sub_data)
            if self._resolve_pending_request(sub_command, sub_data):
                # Design D3: a routed response must not disturb correlation.
                # Flushing here would silently cost the pending Data frame its
                # SNR and RSSI whenever a probe response lands mid-packet.
                return []

        raw = bytes((frame.type_byte, *frame.data))
        if (reset_reason := boot_banner_reason(raw)) is not None:
            return [*self._flush_pending_data(), *self._device_rebooted(raw, reset_reason)]
        return [
            *self._flush_pending_data(),
            UnparsedEvent(raw=raw, reason="unrecognized frame"),
        ]

    def _device_rebooted(self, raw: bytes, reset_reason: str) -> list[ModemEvent]:
        """The ESP32 ROM bootloader's banner arrived on the KISS stream.

        The board restarted. The USB bridge is a separate chip and stays
        enumerated across an ESP32 reset, so no disconnect is reported and
        nothing else in the stack would notice -- but the firmware persists no
        radio configuration, so it is now running its build defaults rather
        than what `SetRadio` applied. Observed live 2026-09-03: the Heltec V3
        power-cycled every 30-75 s on its own, which is a hardware fault, but
        the re-handshake is owed regardless of what causes the restart
        (DESIGN.md §4.1: never assume the device came back configured).
        """
        self.reboot_count += 1
        self._device_restarted = True
        self._logger.error(
            "modem_rebooted",
            reset_reason=reset_reason,
            reboot_count=self.reboot_count,
            detail=(
                "the board restarted and lost its radio configuration; "
                "re-applying SetRadio"
            ),
        )
        return [UnparsedEvent(raw=raw, reason=f"{DEVICE_REBOOT_REASON}: {reset_reason}")]

    def _handle_rx_meta(self, frame: KissFrame, sub_data: bytes) -> list[ModemEvent]:
        raw = bytes((frame.type_byte, *frame.data))
        if len(sub_data) < 2:
            return [
                *self._flush_pending_data(),
                UnparsedEvent(raw=raw, reason="RxMeta shorter than SNR + RSSI"),
            ]
        if self._pending_data is None:
            return [UnparsedEvent(raw=raw, reason="RxMeta with no preceding Data frame")]
        packet = self._pending_data.data
        self._pending_data = None
        return [RxEvent(packet=packet, rx_meta=_parse_rx_meta(sub_data))]

    def _resolve_pending_request(self, sub_command: int, sub_data: bytes) -> bool:
        """Route a `SetHardware` response to the outstanding request, if any.

        Returns False for a response matching nothing outstanding, which the
        caller reports as an unparsed frame.
        """
        pending = self._pending_request
        if pending is None or pending.future.done():
            return False
        if sub_command == pending.response_code or sub_command == SUB_OK:
            pending.future.set_result(
                RequestOk(
                    sub_command=pending.sub_command,
                    response_code=sub_command,
                    data=sub_data,
                )
            )
            return True
        if sub_command == SUB_ERROR:
            pending.future.set_result(
                RequestRejected(
                    sub_command=pending.sub_command,
                    error_code=sub_data[0] if sub_data else None,
                )
            )
            return True
        return False

    def _flush_pending_data(self) -> list[ModemEvent]:
        if self._pending_data is None:
            return []
        packet, self._pending_data = self._pending_data.data, None
        return [RxEvent(packet=packet, rx_meta=None)]

    def _drain_buffered(self) -> list[ModemEvent]:
        buffered, self._buffered = self._buffered, []
        return buffered

    # --- Frame pumping -----------------------------------------------------

    async def _pump_until_resolved(
        self, future: asyncio.Future[RequestResult], timeout: float
    ) -> RequestResult | None:
        """Drive the frame iterator until `future` resolves or time runs out.

        Used only before the `events()` loop is running. Events decoded on the
        way are buffered, never dropped.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not future.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            frame = await self._next_frame(remaining)
            if frame is None or isinstance(frame, _StreamEnded):
                break
            if isinstance(frame, Reconnected):
                # The link dropped mid-request. Give up on the response; the
                # `events()` loop redoes the handshake when it sees this.
                self.reconnect_count += 1
                self._buffered.extend(self._flush_pending_data())
                break
            self._buffered.extend(self._handle_frame(frame))
        return future.result() if future.done() else None

    async def _next_frame(
        self, timeout: float | None
    ) -> FrameEvent | _StreamEnded | None:
        """Await the next frame, returning None if `timeout` elapses first.

        The in-flight `__anext__` is kept as a task across a timeout rather
        than cancelled: cancelling it would tear down the transport's frame
        generator and silently end the stream.
        """
        assert self._frame_iter is not None, "call events() before _next_frame()"
        if self._next_frame_task is None:
            self._next_frame_task = asyncio.ensure_future(anext(self._frame_iter))
        task = self._next_frame_task
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            return None
        self._next_frame_task = None
        try:
            return task.result()
        except StopAsyncIteration:
            return _STREAM_ENDED

    def _cancel_next_frame(self) -> None:
        task, self._next_frame_task = self._next_frame_task, None
        if task is not None and not task.done():
            task.cancel()
