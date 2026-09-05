"""`MonitorRun`: drive a source through the decode pipeline and render it.

Orchestration only — every string it prints comes from `render.py`. The source
is an async iterable of modem events and nothing here knows whether it is a
live link or a capture file (design D1), which is what lets the same run be
exercised offline.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from typing import IO

from sighop.logging import Logger, get_logger
from sighop.monitor.render import (
    Summary,
    render_detail_line,
    render_frame_line,
    render_summary,
)
from sighop.net.rx import AdvertOutcome, RxRecord, decode_stream
from sighop.protocol.crypto import VerifiedAdvert
from sighop.radio.capture import CaptureWriter
from sighop.radio.modem import ModemEvent
from sighop.radio.probe import ProbeResult

DEFAULT_SUMMARY_INTERVAL_SECONDS = 300.0


class MonitorRun:
    """Renders every frame a source produces, until the source ends or the
    operator stops it.

    `startup` is awaited before the first frame is printed and returns the
    line describing where these frames came from — for a live link that means
    waiting for the startup probe, so frames decoded in the meantime are held
    and released behind it rather than printed first.
    """

    def __init__(
        self,
        source: AsyncIterable[ModemEvent],
        *,
        startup: Callable[[], Awaitable[str]],
        out: IO[str] | None = None,
        capture_writer: CaptureWriter | None = None,
        capture_probe: Callable[[], Awaitable[ProbeResult | None]] | None = None,
        summary_interval: float = DEFAULT_SUMMARY_INTERVAL_SECONDS,
        reconnects: Callable[[], int] = lambda: 0,
        reboots: Callable[[], int] = lambda: 0,
        logger: Logger | None = None,
    ) -> None:
        self._source = source
        self._startup = startup
        self._out = out if out is not None else sys.stdout
        self._capture_writer = capture_writer
        self._capture_probe = capture_probe
        self._summary_interval = summary_interval
        self._reconnects = reconnects
        self._reboots = reboots
        self._logger = logger or get_logger(component="monitor")
        self._stop_event = asyncio.Event()
        self._started = False
        self._held: list[str] = []
        self._frames = 0
        self._decode_failures = 0
        self._adverts_verified = 0
        self._adverts_failed = 0
        self._node_hashes: set[int] = set()

    # --- Lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        self._stop_event.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

    async def run(self) -> Summary:
        """Render until the source ends or `stop()` is called, then summarize."""
        startup_task = asyncio.create_task(self._print_startup())
        summary_task = asyncio.create_task(self._summary_loop())
        consume_task = asyncio.create_task(self._consume())
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait(
                (consume_task, stop_task), return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (consume_task, summary_task, startup_task, stop_task):
                if not task.done():
                    task.cancel()
            for task in (consume_task, summary_task, startup_task, stop_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Stopped before the startup line resolved (a Ctrl-C during the
            # probe, say): print what was held rather than losing it.
            self._started = True
            self._release()
            self._write(render_summary(self.summary()))
        return self.summary()

    # --- Counters ----------------------------------------------------------

    def summary(self) -> Summary:
        return Summary(
            frames=self._frames,
            decode_failures=self._decode_failures,
            adverts_verified=self._adverts_verified,
            adverts_failed=self._adverts_failed,
            node_hashes=len(self._node_hashes),
            reconnects=self._reconnects(),
            reboots=self._reboots(),
        )

    def _count(self, record: RxRecord) -> None:
        self._frames += 1
        if record.failed:
            self._decode_failures += 1
        if isinstance(record.outcome, AdvertOutcome):
            if isinstance(record.outcome.verification, VerifiedAdvert):
                self._adverts_verified += 1
            else:
                self._adverts_failed += 1
        if record.src_hash is not None:
            self._node_hashes.add(record.src_hash)

    # --- Output ------------------------------------------------------------

    async def _print_startup(self) -> None:
        text = await self._startup()
        self._started = True
        self._write(text)
        self._release()
        if self._capture_writer is not None:
            probe_result = await self._capture_probe() if self._capture_probe else None
            self._capture_writer.start(probe_result)

    def _release(self) -> None:
        """Print anything decoded before the startup line was ready."""
        if not self._started:
            return
        held, self._held = self._held, []
        for line in held:
            self._write(line)

    def _print(self, text: str) -> None:
        if not self._started:
            self._held.append(text)
            return
        self._write(text)

    def _write(self, text: str) -> None:
        self._out.write(text + "\n")
        self._out.flush()

    async def _consume(self) -> None:
        async for record in decode_stream(self._tee(), logger=self._logger):
            self._count(record)
            self._print(render_frame_line(record))
            self._print(render_detail_line(record))

    async def _tee(self) -> AsyncIterator[ModemEvent]:
        """Pass events through, copying them to the capture file when asked."""
        async for event in self._source:
            if self._capture_writer is not None:
                self._capture_writer.write(event)
            yield event

    async def _summary_loop(self) -> None:
        while True:
            await asyncio.sleep(self._summary_interval)
            self._print(render_summary(self.summary()))
