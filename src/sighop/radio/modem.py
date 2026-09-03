"""Modem-level receive semantics on top of `KissTransport`.

Owns: decoding `Data` frames, correlating `RxMeta` with the `Data` frame it
follows, and the startup `SetHardware`/`SetRadio` handshake needed to bring
the link up. No TX path — that's a later milestone.

Protocol reference: `related-repos/MeshCore/docs/kiss_modem_protocol.md`.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator, AsyncGenerator
from dataclasses import dataclass

import structlog

from sighop.logging import get_logger
from sighop.radio.kiss import (
    FrameEvent,
    KissFrame,
    KissTransport,
    MalformedFrame,
    Reconnected,
)

TYPE_DATA = 0x00
TYPE_SET_HARDWARE = 0x06

SUB_SET_RADIO = 0x09
SUB_OK = 0xF0
SUB_ERROR = 0xF1
SUB_RXMETA = 0xF9


@dataclass(frozen=True, slots=True)
class RadioParams:
    """SetRadio parameters. Wire format is little-endian: freq(4) bw(4) sf(1) cr(1)."""

    freq_hz: int
    bw_hz: int
    sf: int
    cr: int

    def to_bytes(self) -> bytes:
        return struct.pack("<IIBB", self.freq_hz, self.bw_hz, self.sf, self.cr)


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


@dataclass(frozen=True, slots=True)
class UnparsedEvent:
    raw: bytes
    reason: str


ModemEvent = RxEvent | UnparsedEvent


class ModemError(RuntimeError):
    """The modem rejected a SetHardware request (Error 0xF1 response)."""


def _parse_rx_meta(data: bytes) -> RxMeta:
    snr_raw, rssi_raw = struct.unpack("<bb", data[:2])
    return RxMeta(snr_db=snr_raw * 0.25, rssi_dbm=rssi_raw)


class Modem:
    """Owns the startup handshake and RX-side frame interpretation."""

    def __init__(
        self,
        transport: KissTransport,
        radio_params: RadioParams = EU868_NARROW,
        *,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._transport = transport
        self._radio_params = radio_params
        self._logger = logger or get_logger(component="modem")
        self.reconnect_count = 0

    async def events(self) -> AsyncIterator[ModemEvent]:
        """Open the link, run the startup handshake, then yield RX/unparsed
        events forever -- redoing the handshake after every reconnect.
        """
        await self._transport.open()
        frame_iter = self._transport.frames()
        await self._handshake(frame_iter)

        pending: KissFrame | None = None
        async for frame in frame_iter:
            if isinstance(frame, Reconnected):
                self.reconnect_count += 1
                if pending is not None:
                    yield RxEvent(packet=pending.data, rx_meta=None)
                    pending = None
                await self._handshake(frame_iter)
                continue

            if isinstance(frame, MalformedFrame):
                if pending is not None:
                    yield RxEvent(packet=pending.data, rx_meta=None)
                    pending = None
                yield UnparsedEvent(raw=frame.raw, reason=frame.reason)
                continue

            # frame: KissFrame
            if frame.type_byte == TYPE_DATA:
                if pending is not None:
                    self._logger.error(
                        "rx_meta_correlation_anomaly",
                        reason="next Data frame arrived before RxMeta for the previous one",
                    )
                    yield RxEvent(packet=pending.data, rx_meta=None)
                pending = frame
                continue

            if frame.type_byte == TYPE_SET_HARDWARE and frame.data:
                sub_command, sub_data = frame.data[0], frame.data[1:]
                if sub_command == SUB_RXMETA:
                    rx_meta = _parse_rx_meta(sub_data)
                    if pending is not None:
                        yield RxEvent(packet=pending.data, rx_meta=rx_meta)
                        pending = None
                    else:
                        yield UnparsedEvent(
                            raw=bytes((frame.type_byte, *frame.data)),
                            reason="RxMeta with no preceding Data frame",
                        )
                    continue

            if pending is not None:
                yield RxEvent(packet=pending.data, rx_meta=None)
                pending = None
            yield UnparsedEvent(
                raw=bytes((frame.type_byte, *frame.data)),
                reason="unrecognized frame",
            )

        if pending is not None:
            yield RxEvent(packet=pending.data, rx_meta=None)

    async def _handshake(self, frame_iter: AsyncGenerator[FrameEvent, None]) -> None:
        """Send SetRadio and consume frames from the shared iterator until
        its OK/Error response. Must read from the same iterator `events()`
        uses for the main loop -- a second call to `transport.frames()`
        would open an independent generator racing it for the same bytes.
        """
        await self._transport.send(
            TYPE_SET_HARDWARE, bytes((SUB_SET_RADIO,)) + self._radio_params.to_bytes()
        )
        async for frame in frame_iter:
            if isinstance(frame, KissFrame) and frame.type_byte == TYPE_SET_HARDWARE and frame.data:
                sub_command = frame.data[0]
                if sub_command == SUB_OK:
                    self._logger.info("modem_ready", radio_params=self._radio_params)
                    return
                if sub_command == SUB_ERROR:
                    error_code = frame.data[1] if len(frame.data) > 1 else None
                    raise ModemError(f"SetRadio rejected: error code {error_code}")
            # anything else seen while waiting for the handshake response is
            # unexpected this early in the connection and is dropped
