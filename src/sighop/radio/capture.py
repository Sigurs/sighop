"""`sighop capture`: persist modem RX/unparsed events to a JSONL file.

One JSON object per line, appended and flushed immediately so a process
interruption loses at most the in-flight record. See design.md's "Capture
file format" decision for the record shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import signal
from pathlib import Path
from typing import IO

import structlog

from sighop.logging import get_logger
from sighop.radio.modem import Modem, ModemEvent, RxEvent, UnparsedEvent

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0


def _record_for(event: ModemEvent) -> dict:
    ts = dt.datetime.now(dt.UTC).isoformat()
    if isinstance(event, RxEvent):
        rx_meta = None
        if event.rx_meta is not None:
            rx_meta = {"snr_db": event.rx_meta.snr_db, "rssi_dbm": event.rx_meta.rssi_dbm}
        return {"ts": ts, "kind": "rx_frame", "raw_hex": event.packet.hex(), "rx_meta": rx_meta}
    return {"ts": ts, "kind": "unparsed", "raw_hex": event.raw.hex(), "reason": event.reason}


class CaptureRun:
    """Drives a `Modem` and writes every event it produces to `out_path`,
    until asked to stop (SIGINT/SIGTERM or `stop()`).
    """

    def __init__(
        self,
        modem: Modem,
        out_path: Path,
        *,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        logger: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._modem = modem
        self._out_path = out_path
        self._heartbeat_interval = heartbeat_interval
        self._logger = logger or get_logger(component="capture")
        self._rx_count = 0
        self._unparsed_count = 0
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

    async def run(self) -> None:
        with self._out_path.open("a", encoding="utf-8") as out_file:
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            consume_task = asyncio.create_task(self._consume(out_file))
            stop_task = asyncio.create_task(self._stop_event.wait())
            try:
                await asyncio.wait(
                    (consume_task, stop_task), return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (consume_task, heartbeat_task, stop_task):
                    if not task.done():
                        task.cancel()
                for task in (consume_task, heartbeat_task, stop_task):
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def _consume(self, out_file: IO[str]) -> None:
        async for event in self._modem.events():
            record = _record_for(event)
            out_file.write(json.dumps(record) + "\n")
            out_file.flush()
            if isinstance(event, RxEvent):
                self._rx_count += 1
            else:
                self._unparsed_count += 1

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            self._logger.info(
                "capture_heartbeat",
                rx_count=self._rx_count,
                unparsed_count=self._unparsed_count,
                reconnect_count=self._modem.reconnect_count,
            )
