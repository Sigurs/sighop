"""KISS framing and escaping over a serial link.

Per DESIGN.md §4.1: this module handles FEND/FESC framing and escaping and
nothing else. Frame/command semantics (Data, RxMeta, SetHardware, ...) live
in `sighop.radio.modem`. Protocol reference:
`related-repos/MeshCore/docs/kiss_modem_protocol.md`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import serial_asyncio

from sighop.logging import Logger, get_logger, wide_event

_FEND = 0xC0
_FESC = 0xDB
_TFEND = 0xDC
_TFESC = 0xDD

DEFAULT_BAUDRATE = 115200


@dataclass(frozen=True, slots=True)
class KissFrame:
    """A decoded KISS frame: the type byte and its (unescaped) data."""

    type_byte: int
    data: bytes


@dataclass(frozen=True, slots=True)
class MalformedFrame:
    """A frame that could not be decoded, with the raw (still-escaped) bytes seen."""

    raw: bytes
    reason: str


@dataclass(frozen=True, slots=True)
class Reconnected:
    """Emitted once the transport re-establishes the link after a disconnect.

    Consumers that keep connection-scoped state (e.g. `Modem`'s startup
    handshake) must treat this as a signal to redo that setup — the device
    is never assumed to have retained its prior configuration.
    """

    attempts: int


FrameEvent = KissFrame | MalformedFrame | Reconnected

Connector = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class Transport(Protocol):
    """The slice of `KissTransport` that `Modem` depends on.

    Narrow on purpose: it is what lets a test drive modem semantics from a
    scripted list of frames, with no serial device and no reconnect loop
    anywhere near it.
    """

    async def open(self) -> None: ...

    def frames(self) -> AsyncIterator[FrameEvent]: ...

    async def send(self, type_byte: int, data: bytes = b"") -> None: ...


class DeviceNotFoundError(RuntimeError):
    """The configured serial device path does not exist."""


class KissDecoder:
    """Stateful KISS frame decoder: feed raw bytes, get back decoded frames.

    Pure and synchronous — no I/O — so it can be driven directly in tests
    with synthetic byte sequences.
    """

    def __init__(self) -> None:
        self._started = False
        self._decoded = bytearray()
        self._raw = bytearray()
        self._escape_pending = False
        self._malformed_reason: str | None = None

    def feed(self, chunk: bytes) -> list[FrameEvent]:
        events: list[FrameEvent] = []
        for byte in chunk:
            event = self._feed_byte(byte)
            if event is not None:
                events.append(event)
        return events

    def _feed_byte(self, byte: int) -> FrameEvent | None:
        if byte == _FEND:
            if not self._started:
                self._started = True
                return None
            event = self._flush()
            self._reset_buffers()
            return event

        if not self._started:
            return None  # noise before the first FEND

        self._raw.append(byte)

        if self._escape_pending:
            self._escape_pending = False
            if byte == _TFEND:
                self._decoded.append(_FEND)
            elif byte == _TFESC:
                self._decoded.append(_FESC)
            else:
                self._malformed_reason = "invalid escape sequence"
            return None

        if byte == _FESC:
            self._escape_pending = True
            return None

        self._decoded.append(byte)
        return None

    def _flush(self) -> FrameEvent | None:
        if self._escape_pending:
            return MalformedFrame(
                raw=bytes(self._raw), reason="dangling escape byte at end of frame"
            )
        if self._malformed_reason is not None:
            return MalformedFrame(raw=bytes(self._raw), reason=self._malformed_reason)
        if not self._decoded:
            return None  # two consecutive FEND bytes: no frame
        return KissFrame(type_byte=self._decoded[0], data=bytes(self._decoded[1:]))

    def _reset_buffers(self) -> None:
        self._decoded = bytearray()
        self._raw = bytearray()
        self._escape_pending = False
        self._malformed_reason = None


def encode_frame(type_byte: int, data: bytes = b"") -> bytes:
    """Encode a type byte + data into a FEND-delimited, FESC-escaped KISS frame."""
    escaped = bytearray()
    for byte in bytes([type_byte]) + data:
        if byte == _FEND:
            escaped += bytes((_FESC, _TFEND))
        elif byte == _FESC:
            escaped += bytes((_FESC, _TFESC))
        else:
            escaped.append(byte)
    return bytes((_FEND,)) + bytes(escaped) + bytes((_FEND,))


def serial_connector(device_path: str, baudrate: int = DEFAULT_BAUDRATE) -> Connector:
    """Build a connector that opens `device_path` over serial.

    **Leaves DTR and RTS at their defaults**, which is what resets this board
    the least. On an ESP32 those lines drive the auto-reset circuit (`EN` and
    `GPIO0`) through a transistor pair that fires on a *difference* between
    them, so what matters is the transition, not the level. Measured on the
    Heltec V3 + CP2102, 2026-09-03: opening with `dtr`/`rts` deasserted reset
    the board 0.52 s after every open, reproducibly; opening with pyserial's
    defaults reset it not at all. Deasserting them looks like the careful
    thing to do and is the opposite — hence this note, so it is not
    "fixed" again.

    Raises `DeviceNotFoundError` if the path doesn't exist at connect time —
    the caller decides whether that's fatal (initial connect) or something
    to retry (reconnect loop); see `KissTransport`.
    """

    async def _connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # A single stat on a device node, deliberately kept synchronous: it is
        # the check that turns "device unplugged" into a named error instead of
        # an opaque one, and moving it to a thread would cost more than it saves.
        if not Path(device_path).exists():  # noqa: ASYNC240
            raise DeviceNotFoundError(f"KISS serial device not found: {device_path}")
        reader, writer = await serial_asyncio.open_serial_connection(
            url=device_path, baudrate=baudrate
        )
        return reader, writer

    return _connect


class KissTransport:
    """Owns the serial link: initial connect (fails fast), then frame I/O
    with an unbounded, backoff reconnect loop on failure.
    """

    def __init__(
        self,
        connect: Connector,
        *,
        backoff_initial: float = 0.5,
        backoff_cap: float = 30.0,
        logger: Logger | None = None,
    ) -> None:
        self._connect = connect
        self._backoff_initial = backoff_initial
        self._backoff_cap = backoff_cap
        self._logger = logger or get_logger(component="kiss_transport")
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        """Initial connect. Raises immediately on failure — no retry."""
        self._reader, self._writer = await self._connect()

    async def frames(self) -> AsyncIterator[FrameEvent]:
        """Yield decoded frame events forever, reconnecting with backoff on
        I/O failure. `open()` must have succeeded first.
        """
        assert self._reader is not None, "call open() before frames()"
        decoder = KissDecoder()
        while True:
            try:
                chunk = await self._reader.read(4096)
                if not chunk:
                    raise ConnectionError("serial link closed (EOF)")
            except (OSError, ConnectionError) as exc:
                attempts = await self._reconnect(exc)
                decoder = KissDecoder()  # a frame torn across the gap is unrecoverable
                yield Reconnected(attempts=attempts)
                continue
            for event in decoder.feed(chunk):
                yield event

    async def _reconnect(self, exc: Exception) -> int:
        self._logger.error("serial_disconnected", error=str(exc))
        if self._writer is not None:
            self._writer.close()
        with wide_event(self._logger, "serial_reconnect") as fields:
            attempts = 0
            backoff = self._backoff_initial
            while True:
                attempts += 1
                # Back off *before* every attempt, not only after a failed
                # one. Reopening a device that is present but immediately
                # reports EOF -- an ESP32-S3 rebooting because opening the
                # CP2102 port asserted DTR -- otherwise succeeds instantly and
                # spins: observed live 2026-09-03, seven reconnects in 60 ms.
                await asyncio.sleep(backoff)
                try:
                    self._reader, self._writer = await self._connect()
                except Exception:
                    backoff = min(backoff * 2, self._backoff_cap)
                    continue
                fields["attempts"] = attempts
                return attempts

    async def send(self, type_byte: int, data: bytes = b"") -> None:
        assert self._writer is not None, "call open() before send()"
        self._writer.write(encode_frame(type_byte, data))
        await self._writer.drain()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
