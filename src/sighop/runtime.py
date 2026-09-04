"""The sighop runtime: the first time the pieces run as one system.

Source (live modem or capture replay) → decode → dedup → path learning →
bus fan-out, with the transmit scheduler and its advert stubs on the other
side. Milestone 3's `sighop run` is this class with a command line attached.

The gate stays closed unless an operator explicitly opens it, and the runtime
says so at startup and in every status line. Everything else here is wiring: no
policy lives in this module, so there is exactly one place to look for each rule
— `net/tx.py` for the budget, `net/adverts.py` for advert timing, `net/dedup.py`
for duplicates.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from typing import IO

import structlog

from sighop.logging import get_logger
from sighop.monitor.render import (
    render_detail_line,
    render_frame_line,
    render_run_startup,
    render_status,
    render_stubs,
)
from sighop.net.adverts import AdvertScheduler
from sighop.net.bus import IngressPipeline, NetworkBus
from sighop.net.dedup import DEFAULT_MAX_ENTRIES, DEFAULT_TTL_SECONDS, DedupCache
from sighop.net.paths import PathStore
from sighop.net.rx import decode_event
from sighop.net.tx import (
    DEFAULT_CEILING_FRACTION,
    AirtimeBudget,
    Clock,
    PacketSender,
    SystemClock,
    TxScheduler,
)
from sighop.radio.capture import CaptureWriter
from sighop.radio.modem import ModemEvent, RadioParams
from sighop.radio.probe import ProbeResult

DEFAULT_STATUS_INTERVAL_SECONDS = 60.0
DEFAULT_ADVERT_TICK_SECONDS = 5.0


@dataclass(slots=True)
class RuntimeConfig:
    """Everything an operator can turn. Defaults are the safe ones."""

    transmit_enabled: bool = False
    status_interval: float = DEFAULT_STATUS_INTERVAL_SECONDS
    advert_tick: float = DEFAULT_ADVERT_TICK_SECONDS
    dedup_ttl_seconds: float = DEFAULT_TTL_SECONDS
    dedup_max_entries: int = DEFAULT_MAX_ENTRIES
    ceiling_fraction: float = DEFAULT_CEILING_FRACTION
    stub_names: tuple[str, ...] = ()
    advert_override_seconds: float | None = None
    advert_override_expires_in: float | None = None


@dataclass(slots=True)
class Runtime:
    """Composes the pipeline and owns its shutdown."""

    source: AsyncIterable[ModemEvent]
    startup: Callable[[], Awaitable[str]]
    config: RuntimeConfig = field(default_factory=RuntimeConfig)
    sender: PacketSender | None = None
    radio: RadioParams | None = None
    clock: Clock = field(default_factory=SystemClock)
    out: IO[str] | None = None
    logger: structlog.stdlib.BoundLogger | None = None
    capture_writer: CaptureWriter | None = None
    """Records the frames as they arrive, through the same writer `capture` and
    `monitor --capture` use — one implementation of the format, so an overnight
    `run` produces a corpus-grade file rather than a third dialect (§12)."""

    capture_probe: Callable[[], Awaitable[ProbeResult | None]] | None = None
    """Supplies the provenance header. Events are held until it resolves."""

    bus: NetworkBus = field(init=False)
    pipeline: IngressPipeline = field(init=False)
    scheduler: TxScheduler = field(init=False)
    adverts: AdvertScheduler = field(init=False)
    _stop: asyncio.Event = field(init=False)
    _started: bool = field(init=False, default=False)
    _held: list[str] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.logger = self.logger or get_logger(component="runtime")
        self.out = self.out if self.out is not None else sys.stdout
        self.scheduler = TxScheduler(
            sender=self.sender,
            radio=self.radio,
            clock=self.clock,
            budget=AirtimeBudget(ceiling_fraction=self.config.ceiling_fraction),
            transmit_enabled=self.config.transmit_enabled,
            logger=self.logger,
        )
        self.bus = NetworkBus(tx_sink=self.scheduler, logger=self.logger)
        self.pipeline = IngressPipeline(
            bus=self.bus,
            dedup=DedupCache(
                ttl_seconds=self.config.dedup_ttl_seconds,
                max_entries=self.config.dedup_max_entries,
            ),
            paths=PathStore(),
            logger=self.logger,
            radio=self.radio,
        )
        self.adverts = AdvertScheduler(
            submit=self.bus.submit, clock=self.clock, logger=self.logger
        )
        for name in self.config.stub_names:
            self.adverts.add_stub(name)
        if self.config.advert_override_seconds is not None:
            for stub in self.adverts.stubs:
                self.adverts.set_override(
                    stub,
                    self.config.advert_override_seconds,
                    expires_in=self.config.advert_override_expires_in
                    or self.config.advert_override_seconds * 10,
                )
        self._stop = asyncio.Event()

    # --- Lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop)

    def set_radio(self, radio: RadioParams | None) -> None:
        """Adopt a readback — at startup, and again after every reconnect."""
        self.radio = radio
        self.scheduler.set_radio(radio)
        self.pipeline.radio = radio

    async def run(self) -> None:
        """Run until the source ends or `stop()` is called, then shut down."""
        tasks = [
            asyncio.create_task(self._print_startup(), name="runtime-startup"),
            asyncio.create_task(self.scheduler.run(), name="tx-scheduler"),
            asyncio.create_task(self._advert_loop(), name="advert-loop"),
            asyncio.create_task(self._status_loop(), name="status-loop"),
        ]
        consume = asyncio.create_task(self._consume(), name="rx-consume")
        stopping = asyncio.create_task(self._stop.wait(), name="stop")
        try:
            await asyncio.wait((consume, stopping), return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Order matters: stop scheduling before tearing down the loop, so
            # every queued packet is resolved and logged rather than abandoned.
            await self.scheduler.stop()
            for task in (consume, stopping, *tasks):
                if not task.done():
                    task.cancel()
            for task in (consume, stopping, *tasks):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.bus.aclose()
            self._started = True
            self._release()
            self._write(self._status_line())

    # --- Loops -------------------------------------------------------------

    async def _consume(self) -> None:
        async for event in self.source:
            if self.capture_writer is not None:
                self.capture_writer.write(event)
            record = decode_event(event)
            self.pipeline.ingest(record)
            self._print(render_frame_line(record))
            self._print(render_detail_line(record))

    async def _advert_loop(self) -> None:
        while True:
            self.adverts.tick()
            await self.clock.sleep(self.config.advert_tick)

    async def _status_loop(self) -> None:
        while True:
            await self.clock.sleep(self.config.status_interval)
            self._print(self._status_line())

    # --- Output ------------------------------------------------------------

    def _status_line(self) -> str:
        return render_status(
            self.scheduler.status(),
            dedup=self.pipeline.dedup.stats,
            learned_paths=self.pipeline.paths.destination_count,
            active_overrides=len(self.adverts.active_overrides()),
        )

    async def _print_startup(self) -> None:
        text = await self.startup()
        self._started = True
        self._write(
            render_run_startup(
                text,
                transmit_enabled=self.scheduler.transmit_enabled,
                ceiling_pct=self.scheduler.budget.ceiling_fraction * 100,
                above_regulatory_default=self.scheduler.budget.above_regulatory_default,
            )
        )
        self._write(render_stubs(self.adverts.stubs))
        self._release()
        if self.capture_writer is not None:
            probe_result = await self.capture_probe() if self.capture_probe else None
            self.capture_writer.start(probe_result)

    def _print(self, text: str) -> None:
        if not self._started:
            self._held.append(text)
            return
        self._write(text)

    def _release(self) -> None:
        if not self._started:
            return
        held, self._held = self._held, []
        for line in held:
            self._write(line)

    def _write(self, text: str) -> None:
        assert self.out is not None
        self.out.write(text + "\n")
        self.out.flush()
