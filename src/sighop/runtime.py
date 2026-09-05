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
import datetime as dt
import signal
import sys
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import structlog

from sighop.db.persistence import Persistence
from sighop.db.repositories import LoadedEntity
from sighop.keystore import EntityRegistry, LocalEntity
from sighop.logging import get_logger
from sighop.monitor.render import (
    PERSISTENCE_OFF,
    render_detail_line,
    render_dm_event,
    render_frame_line,
    render_persistence,
    render_run_startup,
    render_status,
    render_stubs,
)
from sighop.net.adverts import AdvertScheduler
from sighop.net.airtime import time_on_air_ms
from sighop.net.bus import IngressPipeline, NetworkBus, Submission, TxOutcome
from sighop.net.contacts import Contact, ContactError, ContactStore
from sighop.net.dedup import DEFAULT_MAX_ENTRIES, DEFAULT_TTL_SECONDS, DedupCache
from sighop.net.dm import DirectMessageError, DirectMessageEvent, DirectMessenger
from sighop.net.paths import PathStore
from sighop.net.rx import RxRecord, decode_event
from sighop.net.tx import (
    DEFAULT_CEILING_FRACTION,
    AirtimeBudget,
    Clock,
    PacketSender,
    SystemClock,
    TxScheduler,
)
from sighop.protocol.payloads import NodeType
from sighop.radio.capture import CaptureWriter
from sighop.radio.modem import ModemEvent, RadioParams
from sighop.radio.probe import ProbeResult

DEFAULT_STATUS_INTERVAL_SECONDS = 60.0
DEFAULT_ADVERT_TICK_SECONDS = 5.0

DEFAULT_PEER_WAIT_SECONDS = 60.0
"""How long a `--send` waits for its peer's advert before saying it is unknown.

The first-transmit runbook starts the run, then asks the peer for a zero-hop
advert, then sends — so the peer is not a contact at the moment the run comes
up, and a send that gave up instantly would be unusable. It gives up loudly
rather than exiting: an unknown peer must not end a receiving session."""

PEER_POLL_SECONDS = 0.25


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
    entity_keyfiles: tuple[Path, ...] = ()
    """Persistent identities, loaded through the registry so two that share a
    node hash fail startup rather than being loaded (milestone 4 design D1)."""

    stored_entities: tuple[LoadedEntity, ...] = ()
    """Identities the entity store held, already decrypted by the caller.

    Registered *before* the keyfiles, so the §3 rule 3 collision check runs
    across both sources and a generated stub avoids every taken hash whichever
    store it came from (design D14)."""

    replay_persists: bool = False
    """Whether a replayed capture writes (design D13). Off by default: replayed
    receptions carry an earlier session's timestamps, and writing them would make
    a week-old contact indistinguishable from one heard this minute."""

    peer: str | None = None
    send_text: str | None = None
    allow_flood: bool = False
    """Inverts the firmware's default (design D4): with no route known, a send
    is refused unless this was asked for explicitly."""

    zero_hop_advert: str | None = None
    """The name of an entity to emit exactly one zero-hop advert for."""

    peer_wait_seconds: float = DEFAULT_PEER_WAIT_SECONDS


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

    persistence: Persistence | None = None
    """The durable backing, already opened and version-checked by the caller.

    None is the whole of "no database configured": the stores get no sink, every
    lookup is answered from memory exactly as before, and the startup line says
    that state will not survive the process. Opening happens outside the runtime
    because a configured database that cannot be reached is a *startup* failure
    (`database` spec) — it must be reported before a pipeline exists, and before
    anything could be transmitted."""

    bus: NetworkBus = field(init=False)
    pipeline: IngressPipeline = field(init=False)
    scheduler: TxScheduler = field(init=False)
    adverts: AdvertScheduler = field(init=False)
    entities: EntityRegistry = field(init=False)
    contacts: ContactStore = field(init=False)
    messenger: DirectMessenger = field(init=False)
    _stop: asyncio.Event = field(init=False)
    _ready: asyncio.Event = field(init=False)
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
            on_transmitted=self._record_transmitted,
            on_resolved=self._record_tx,
            logger=self.logger,
        )
        self.bus = NetworkBus(tx_sink=self.scheduler, logger=self.logger)
        self.pipeline = IngressPipeline(
            bus=self.bus,
            dedup=DedupCache(
                ttl_seconds=self.config.dedup_ttl_seconds,
                max_entries=self.config.dedup_max_entries,
            ),
            paths=PathStore(
                sink=None if self.persistence is None else self.persistence.path_sink()
            ),
            logger=self.logger,
            radio=self.radio,
        )
        self.adverts = AdvertScheduler(
            submit=self.bus.submit, clock=self.clock, logger=self.logger
        )
        # Persistent identities first: a stub's generated key is then made to
        # avoid their node hashes rather than the other way round. Stored
        # entities before keyfiles, so a collision between the two sources is
        # reported the same way as one between two keyfiles (design D14).
        self.entities = EntityRegistry(logger=self.logger)
        for stored in self.config.stored_entities:
            self._adopt_entity(
                self.entities.add_stored(
                    stored.name, stored.identity, node_type=stored.record.node_type
                )
            )
        for path in self.config.entity_keyfiles:
            self._adopt_entity(self.entities.load(path))
        for name in self.config.stub_names:
            self.adverts.add_stub(name)
        self.contacts = ContactStore(
            logger=self.logger,
            sink=None if self.persistence is None else self.persistence.contact_sink(),
        )
        if self.persistence is not None:
            self.persistence.attach_contacts(self.contacts)
        self.messenger = DirectMessenger(
            contacts=self.contacts,
            paths=self.pipeline.paths,
            submit=self.bus.submit,
            entities=self.adverts.stubs,
            clock=self.clock,
            radio=self.radio,
            allow_flood=self.config.allow_flood,
            on_event=self._on_dm_event,
            logger=self.logger,
        )
        self.contacts.subscribe(self.bus)
        self.messenger.subscribe(self.bus)
        if self.config.advert_override_seconds is not None:
            for stub in self.adverts.stubs:
                self.adverts.set_override(
                    stub,
                    self.config.advert_override_seconds,
                    expires_in=self.config.advert_override_expires_in
                    or self.config.advert_override_seconds * 10,
                )
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()

    def _adopt_entity(self, entity: LocalEntity) -> None:
        """Give a local identity to the advert scheduler, whatever it came from."""
        self.adverts.add_identity(
            entity.name,
            entity.identity,
            node_type=(
                entity.node_type if isinstance(entity.node_type, NodeType) else NodeType.CHAT
            ),
            keyfile=entity.source,
        )

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
        self.messenger.set_radio(radio)

    @property
    def _one_shot_requested(self) -> bool:
        return self.config.send_text is not None or self.config.zero_hop_advert is not None

    async def run(self) -> None:
        """Run until the source ends or `stop()` is called, then shut down."""
        # Restoration happens before the source is consumed, so a peer heard on
        # an earlier run is addressable before any traffic arrives rather than
        # racing it (contacts spec).
        await self._restore()
        send = asyncio.create_task(self._send_once(), name="send-once")
        tasks = [
            asyncio.create_task(self._print_startup(), name="runtime-startup"),
            asyncio.create_task(self.scheduler.run(), name="tx-scheduler"),
            asyncio.create_task(self._advert_loop(), name="advert-loop"),
            asyncio.create_task(self._status_loop(), name="status-loop"),
            send,
        ]
        consume = asyncio.create_task(self._consume(), name="rx-consume")
        stopping = asyncio.create_task(self._stop.wait(), name="stop")
        try:
            await asyncio.wait((consume, stopping), return_when=asyncio.FIRST_COMPLETED)
            if self._one_shot_requested and not send.done():
                # A run asked to send one message does not exit before that send
                # has resolved or given up. Without this a replayed source — which
                # ends in milliseconds — would tear the run down before the peer
                # it was told to send to had even been heard. `stop()` still cuts
                # it short, and `_await_peer` watches for that.
                await asyncio.wait((send, stopping), return_when=asyncio.FIRST_COMPLETED)
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
            if self.persistence is not None:
                # After the bus, so nothing is still producing rows, and before
                # the last status line, so its counters are final.
                await self.persistence.stop()
            self._started = True
            self._release()
            self._write(self._status_line())

    async def _restore(self) -> None:
        """Load contacts and paths, then start the writers, pruner and probe."""
        if self.persistence is None:
            return
        await self.persistence.restore(
            self.contacts,
            self.pipeline.paths,
            entities=len(self.config.stored_entities),
        )
        self.persistence.start()

    # --- Loops -------------------------------------------------------------

    async def _consume(self) -> None:
        async for event in self.source:
            if self.capture_writer is not None:
                self.capture_writer.write(event)
            record = decode_event(event)
            self.pipeline.ingest(record)
            if self.persistence is not None:
                # After ingest and after the decision it describes: the feed
                # records what happened, and nothing consults it (§6).
                self.persistence.record_rx(record, airtime_ms=self._airtime_ms(record))
            self._print(render_frame_line(record))
            self._print(render_detail_line(record))

    def _airtime_ms(self, record: RxRecord) -> float | None:
        """What the frame cost the air, when the board's readback is known.

        Absent rather than guessed when it is not — the same rule the budget and
        the RX wide event follow.
        """
        if self.radio is None:
            return None
        return round(time_on_air_ms(record.size_bytes, self.radio), 3)

    async def _advert_loop(self) -> None:
        while True:
            self.adverts.tick()
            await self.clock.sleep(self.config.advert_tick)

    async def _status_loop(self) -> None:
        while True:
            await self.clock.sleep(self.config.status_interval)
            self._print(self._status_line())

    async def _send_once(self) -> None:
        """The `--send` one-shot, and the one-shot zero-hop advert with it.

        Both are deliberate single acts rather than schedules. Neither can end
        the run: a peer that never adverts leaves a receiving session running,
        which is what an operator watching a first transmission needs.

        Both wait for startup, because startup is what adopts the board's radio
        readback — and without one the scheduler refuses to compute airtime and
        drops the packet. Observed doing exactly that on the first attempt at the
        one-shot advert: correct refusal, wrong ordering. A `--send` survived it
        only by accident, having waited for its peer's advert in the meantime.
        """
        await self._ready.wait()
        if self.config.zero_hop_advert is not None:
            await self._request_zero_hop(self.config.zero_hop_advert)
        if self.config.peer is None or self.config.send_text is None:
            return
        contact = await self._await_peer(self.config.peer)
        if contact is None:
            return
        entity = next(iter(self.adverts.stubs), None)
        if entity is None:
            self._print(
                "cannot send: no entity identity is loaded "
                "(use --entity <keyfile>, or --stub for an ephemeral one)"
            )
            return
        try:
            await self.messenger.send(entity, contact, self.config.send_text)
        except DirectMessageError as exc:
            # Refused rather than sent — the outcome is reported and the run
            # keeps receiving, since a refusal is information, not a failure.
            self._print(f"send refused: {exc}")

    async def _request_zero_hop(self, name: str) -> None:
        for stub in self.adverts.stubs:
            if stub.name == name or stub.entity_id == name:
                self.adverts.request_zero_hop(stub)
                self._print(f"zero-hop advert requested for {stub.name!r}")
                return
        self._print(f"no entity named {name!r} to advert; the run continues")

    async def _await_peer(self, reference: str) -> Contact | None:
        """Wait for a peer's advert, then resolve it — or say it is unknown."""
        deadline = self.clock.now() + dt.timedelta(seconds=self.config.peer_wait_seconds)
        while True:
            try:
                contact = self.contacts.resolve(reference)
            except ContactError as exc:
                if self.clock.now() >= deadline or self._stop.is_set():
                    self._print(f"peer {reference!r} is unknown: {exc}")
                    self._print("nothing was queued; the run continues receiving")
                    return None
                await self.clock.sleep(PEER_POLL_SECONDS)
                continue
            return contact

    # --- Capture and events ------------------------------------------------

    def _record_transmitted(self, packet: bytes, outcome: TxOutcome) -> None:
        """Every frame that reached the air, into the capture beside the
        receptions — the first captures that hold both directions."""
        if self.capture_writer is None:
            return
        self.capture_writer.write_transmitted(
            packet,
            at=self.clock.now(),
            packet_id=outcome.packet_id,
            airtime_ms=outcome.airtime_ms,
        )

    def _record_tx(self, submission: Submission, outcome: TxOutcome) -> None:
        """Every resolved submission into the feed — suppressed ones included.

        A gated run resolves everything as `suppressed`, and a feed that showed
        only what reached the air would render that as silence.
        """
        if self.persistence is None:
            return
        self.persistence.record_tx(submission, outcome, at=self.clock.now())

    def _on_dm_event(self, event: DirectMessageEvent) -> None:
        self._print(render_dm_event(event))

    # --- Output ------------------------------------------------------------

    def _status_line(self) -> str:
        persistence = PERSISTENCE_OFF if self.persistence is None else self.persistence.state
        writers = self.persistence
        return render_status(
            self.scheduler.status(),
            dedup=self.pipeline.dedup.stats,
            learned_paths=self.pipeline.paths.destination_count,
            active_overrides=len(self.adverts.active_overrides()),
            contacts=len(self.contacts),
            persistence=persistence,
            packet_log_discarded=0 if writers is None else writers.packet_log_writer.discarded,
            routes_discarded=0 if writers is None else writers.path_writer.discarded,
            awaiting_backfill=self.contacts.awaiting_backfill,
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
                entities=self.adverts.stubs,
            )
        )
        self._write(render_stubs(self.adverts.stubs))
        self._write(self._persistence_line())
        for entity in self.entities.entities:
            for warning in entity.warnings:
                self._write(f"!! {warning}")
        self._release()
        if self.capture_writer is not None:
            probe_result = await self.capture_probe() if self.capture_probe else None
            self.capture_writer.start(probe_result)
        # Only now is the board's readback adopted, so only now may anything be
        # queued: the scheduler drops what it cannot compute airtime for.
        self._ready.set()

    def _persistence_line(self) -> str:
        """What the run's durability is, said once, before any traffic."""
        if self.persistence is None:
            return render_persistence()
        return render_persistence(
            database=self.persistence.database.config.redacted_url,
            schema_version=self.persistence.database.applied_revision,
            entities=self.persistence.restored.entities,
            contacts=self.persistence.restored.contacts,
            paths=self.persistence.restored.paths,
            writing=self.persistence.writes_enabled,
            not_writing_because=(
                "a replay carries an earlier session's timestamps; "
                "--persist-replay writes anyway"
            ),
        )

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
