"""`sighop capture`: persist modem RX/unparsed events to a JSONL file.

One JSON object per line, appended and flushed immediately so a process
interruption loses at most the in-flight record. See design.md's "Capture
file format" decision for the record shape.

A file this command creates begins with a `capture_meta` provenance record
built from the startup probe (DESIGN.md §12, design D6): the corpus outlives
the session that recorded it, and a fixture whose recording conditions live
only in someone's memory decays into an untrustworthy one. Values are what the
board reported; a value it did not report is written as an explicit null with
its reason, never as the configured value.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import IO, Protocol

import structlog

from sighop.logging import commit_hash, get_logger, package_version
from sighop.radio.modem import ModemEvent, RxEvent
from sighop.radio.probe import ProbeResult

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0


class ModemSource(Protocol):
    """The slice of `Modem` a capture needs: events, and what the board is.

    `CaptureRun` never probes anything itself (design 3.4), so this is also
    the whole of what a test has to stand in for.
    """

    reconnect_count: int
    probe_result: ProbeResult | None
    probe_ready: asyncio.Event

    def events(self) -> AsyncIterator[ModemEvent]: ...

CAPTURE_META_KIND = "capture_meta"


def _sighop_provenance() -> dict[str, str]:
    return {"version": package_version(), "commit_hash": commit_hash()}


def capture_meta_record(probe_result: ProbeResult | None) -> dict:
    """The header record: what the board said it is, plus what recorded it.

    `probe_result` is None only when the link never became ready — recorded as
    such rather than papered over, for the same reason an unanswered
    sub-command is.
    """
    record: dict[str, object] = {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "kind": CAPTURE_META_KIND,
        "sighop": _sighop_provenance(),
    }
    if probe_result is None:
        record["probe"] = None
        record["probe_absent_reason"] = "the link never reached the ready state"
        return record
    record.update(probe_result.as_json())
    return record


def _record_for(event: ModemEvent) -> dict:
    ts = (event.received_at or dt.datetime.now(dt.UTC)).isoformat()
    if isinstance(event, RxEvent):
        rx_meta = None
        if event.rx_meta is not None:
            rx_meta = {"snr_db": event.rx_meta.snr_db, "rssi_dbm": event.rx_meta.rssi_dbm}
        return {"ts": ts, "kind": "rx_frame", "raw_hex": event.packet.hex(), "rx_meta": rx_meta}
    return {"ts": ts, "kind": "unparsed", "raw_hex": event.raw.hex(), "reason": event.reason}


class CaptureWriter:
    """Owns a capture file: one provenance header, then one record per event.

    An event written before the header is available is *held*, not dropped and
    not written ahead of it — a capture file's header is its first line, and a
    frame can easily arrive while the board is still being probed. Holding
    rather than blocking matters: the frame loop must keep running for the
    probe's own responses to be routed at all.

    `sighop monitor --capture` writes through this same writer, so the two
    commands cannot drift into producing two dialects of one format.
    """

    def __init__(self, out_path: Path) -> None:
        self._out_path = out_path
        self._file: IO[str] | None = None
        self._append_only = False
        self._started = False
        self._held: list[ModemEvent] = []
        self.rx_count = 0
        self.unparsed_count = 0

    @property
    def started(self) -> bool:
        return self._started

    def open(self) -> None:
        # A file that already holds records keeps the header it has, if any:
        # a capture file carries at most one, and it is the first line.
        self._append_only = self._out_path.is_file() and self._out_path.stat().st_size > 0
        self._file = self._out_path.open("a", encoding="utf-8")

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def start(self, probe_result: ProbeResult | None) -> None:
        """Write the provenance header and release anything held behind it."""
        if self._started:
            return
        self._started = True
        if not self._append_only:
            self._write(capture_meta_record(probe_result))
        held, self._held = self._held, []
        for event in held:
            self._write_event(event)

    def write(self, event: ModemEvent) -> None:
        if not self._started:
            self._held.append(event)
            return
        self._write_event(event)

    def _write_event(self, event: ModemEvent) -> None:
        self._write(_record_for(event))
        if isinstance(event, RxEvent):
            self.rx_count += 1
        else:
            self.unparsed_count += 1

    def _write(self, record: dict) -> None:
        assert self._file is not None, "call open() before writing"
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()


class CaptureRun:
    """Drives a `Modem` and writes every event it produces to `out_path`,
    until asked to stop (SIGINT/SIGTERM or `stop()`).

    The probe result is taken from the modem once it is ready, or injected
    directly — `CaptureRun` never probes anything itself, which is what keeps
    it testable with no device in sight.
    """

    def __init__(
        self,
        modem: ModemSource,
        out_path: Path,
        *,
        probe_result: ProbeResult | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._modem = modem
        self._injected_probe_result = probe_result
        self._heartbeat_interval = heartbeat_interval
        self._logger = logger or get_logger(component="capture")
        self._writer = CaptureWriter(out_path)
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

    async def run(self) -> None:
        self._writer.open()
        try:
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            header_task = asyncio.create_task(self._start_writer())
            consume_task = asyncio.create_task(self._consume())
            stop_task = asyncio.create_task(self._stop_event.wait())
            try:
                await asyncio.wait(
                    (consume_task, stop_task), return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (consume_task, heartbeat_task, header_task, stop_task):
                    if not task.done():
                        task.cancel()
                for task in (consume_task, heartbeat_task, header_task, stop_task):
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        finally:
            self._writer.close()

    async def _start_writer(self) -> None:
        if self._injected_probe_result is not None:
            self._writer.start(self._injected_probe_result)
            return
        await self._modem.probe_ready.wait()
        self._writer.start(self._modem.probe_result)

    async def _consume(self) -> None:
        async for event in self._modem.events():
            self._writer.write(event)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            self._logger.info(
                "capture_heartbeat",
                rx_count=self._writer.rx_count,
                unparsed_count=self._writer.unparsed_count,
                reconnect_count=self._modem.reconnect_count,
            )
