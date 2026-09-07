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

from sighop.bots import drivers as bot_drivers
from sighop.bots.base import BotRuntimeEvent, UnknownDriverError
from sighop.bots.runtime import BotHost, BotWorker
from sighop.db.engine import Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import BotRecord, LoadedEntity, RoomRecord
from sighop.keystore import EntityRegistry, LocalEntity
from sighop.logging import Logger, get_logger
from sighop.monitor.render import (
    BOTS_OFF,
    PERSISTENCE_OFF,
    ROOMS_OFF,
    render_bot_event,
    render_bot_startup,
    render_bot_status,
    render_detail_line,
    render_dm_event,
    render_frame_line,
    render_persistence,
    render_room_event,
    render_room_startup,
    render_room_status,
    render_run_startup,
    render_status,
    render_stubs,
)
from sighop.net.acks import AckDispatcher, AckRegistry
from sighop.net.adverts import AdvertScheduler, EntityStub
from sighop.net.airtime import time_on_air_ms
from sighop.net.bus import IngressPipeline, NetworkBus, Submission, TxOutcome
from sighop.net.contacts import Contact, ContactError, ContactStore
from sighop.net.dedup import DEFAULT_MAX_ENTRIES, DEFAULT_TTL_SECONDS, DedupCache
from sighop.net.dm import (
    DirectMessageError,
    DirectMessageEvent,
    DirectMessenger,
    MessageReceived,
    NoRouteError,
    SendOutcome,
    choose_route,
)
from sighop.net.pathbodies import PathBodyReader
from sighop.net.paths import PathStore
from sighop.net.room import RoomEvent, RoomRetentionPruner, RoomServer
from sighop.net.rx import RxRecord, decode_event
from sighop.net.tx import (
    DEFAULT_CEILING_FRACTION,
    AirtimeBudget,
    Clock,
    PacketSender,
    SystemClock,
    TxScheduler,
)
from sighop.protocol.payloads import (
    NodeType,
    ServerStats,
    TelemetryEntry,
    temperature_entry,
    voltage_entry,
)
from sighop.radio.capture import CaptureWriter
from sighop.radio.modem import ModemEvent, RadioParams
from sighop.radio.probe import Absent, ProbeResult

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
    logger: Logger | None = None
    capture_writer: CaptureWriter | None = None
    """Records the frames as they arrive, through the same writer `capture` and
    `monitor --capture` use — one implementation of the format, so an overnight
    `run` produces a corpus-grade file rather than a third dialect (§12)."""

    capture_probe: Callable[[], Awaitable[ProbeResult | None]] | None = None
    """Supplies the provenance header. Events are held until it resolves."""

    probe_result: ProbeResult | None = None
    """The board's own readback, when one was taken. Also what a room server's
    telemetry answer is built from — a value the board did not give is absent
    there rather than defaulted (§4.1)."""

    persistence: Persistence | None = None
    """The durable backing, already opened and version-checked by the caller.

    None is the whole of "no database configured": the stores get no sink, every
    lookup is answered from memory exactly as before, and the startup line says
    that state will not survive the process. Opening happens outside the runtime
    because a configured database that cannot be reached is a *startup* failure
    (`database` spec) — it must be reported before a pipeline exists, and before
    anything could be transmitted."""

    services: tuple[Callable[[], Awaitable[None]], ...] = ()
    """Long-running work this run should carry that is not the radio's.

    Each is started alongside the runtime's own tasks and cancelled when the run
    stops (milestone 8 design D2). The web interface is the first and, for now,
    the only one — and it arrives as an opaque callable precisely so that
    `runtime.py` imports nothing from `web/` and `web/` imports nothing from
    here. `cli.py` is the single module that knows both.

    A service that raises is reported and does not take the run with it: the
    radio is the run, and an interface failing is a reason to lose the interface,
    not the node.
    """

    bus: NetworkBus = field(init=False)
    pipeline: IngressPipeline = field(init=False)
    scheduler: TxScheduler = field(init=False)
    adverts: AdvertScheduler = field(init=False)
    entities: EntityRegistry = field(init=False)
    contacts: ContactStore = field(init=False)
    messenger: DirectMessenger = field(init=False)
    acks: AckRegistry = field(init=False)
    path_bodies: PathBodyReader = field(init=False)
    rooms: list[RoomServer] = field(init=False, default_factory=list)
    _room_messages: dict[str, int] = field(init=False, default_factory=dict)
    retention: RoomRetentionPruner | None = field(init=False, default=None)
    _unserved_rooms: list[str] = field(init=False, default_factory=list)
    bots: BotHost = field(init=False)
    _unrun_bots: list[str] = field(init=False, default_factory=list)
    _tx_watcher: Callable[[Submission, TxOutcome, dt.datetime], None] | None = field(
        init=False, default=None
    )
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
        # Design D11: one expectation table, shared by everything that waits on
        # an acknowledgement, and one subscriber that matches them — so
        # "unmatched" keeps meaning nobody in this process was waiting.
        self.acks = AckRegistry(logger=self.logger)
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
            acks=self.acks,
            # Where sent and received messages go to be made durable. None with
            # no database and on a replay, which is the whole of "this run is
            # not recording conversations" (design D8, D13).
            records=None if self.persistence is None else self.persistence.dm_sink(),
        )
        self.path_bodies = PathBodyReader(
            paths=self.pipeline.paths,
            contacts=self.contacts,
            entities=self.adverts.stubs,
            acks=self.acks,
            logger=self.logger,
        )
        # Design D1/D2: bots ride on the messenger's reports and the contact
        # store's observations. The host exists before `_restore` so the
        # listener can be wired before the first frame is handled; it holds no
        # workers until `_load_bots` puts some in it.
        self.bots = BotHost(logger=self.logger)
        self.contacts.set_observation_listener(self.bots.on_observation)
        self.contacts.subscribe(self.bus)
        self.messenger.subscribe(self.bus)
        self.path_bodies.subscribe(self.bus)
        AckDispatcher(registry=self.acks).subscribe(self.bus)
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

    def watch_traffic(
        self,
        *,
        on_reception: Callable[[RxRecord, bool], None] | None = None,
        on_transmission: Callable[[Submission, TxOutcome, dt.datetime], None] | None = None,
    ) -> None:
        """Let something watch the traffic this run handles (milestone 8).

        Two plain callables, both on the contact sink's contract — neither may
        await and neither may raise. `on_reception` is told about duplicates as
        well, because a display that showed only what survived deduplication
        would disagree with the deduplication counters beside it.

        Callables rather than an object, for design D2's reason: the only thing
        that watches traffic today is the web panel, and `runtime.py` must not
        learn that `web/` exists.
        """
        if on_reception is not None:
            self.pipeline.observer = on_reception
        if on_transmission is not None:
            self._tx_watcher = on_transmission

    def watch_messages(self, sink: object) -> None:
        """Attach another consumer of what the messenger sends and receives.

        The durable one is wired at construction; this is for a *display*, which
        wants the same records and must not displace the recording of them. Same
        contract: never awaits, never raises.
        """
        self.messenger.add_record_sink(sink)  # type: ignore[arg-type]

    def set_radio(self, radio: RadioParams | None) -> None:
        """Adopt a readback — at startup, and again after every reconnect."""
        self.radio = radio
        self.scheduler.set_radio(radio)
        self.pipeline.radio = radio
        self.messenger.set_radio(radio)
        for room in self.rooms:
            room.radio = radio
            room.telemetry = self._telemetry

    @property
    def _one_shot_requested(self) -> bool:
        return self.config.send_text is not None or self.config.zero_hop_advert is not None

    async def run(self) -> None:
        """Run until the source ends or `stop()` is called, then shut down."""
        # Restoration happens before the source is consumed, so a peer heard on
        # an earlier run is addressable before any traffic arrives rather than
        # racing it (contacts spec).
        await self._restore()
        # Outside `_restore`, so the host's lifecycle is one thing rather than
        # two: with no database it holds no workers and starting it is a no-op,
        # and every worker it does hold is started and stopped by this method.
        self.bots.start()
        send = asyncio.create_task(self._send_once(), name="send-once")
        tasks = [
            asyncio.create_task(self._print_startup(), name="runtime-startup"),
            asyncio.create_task(self.scheduler.run(), name="tx-scheduler"),
            asyncio.create_task(self._advert_loop(), name="advert-loop"),
            asyncio.create_task(self._status_loop(), name="status-loop"),
            send,
        ]
        tasks.extend(
            asyncio.create_task(self._run_service(index, service), name=f"service-{index}")
            for index, service in enumerate(self.services)
        )
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
            # Before the bus closes, so an outstanding delivery is resolved and
            # logged rather than abandoned.
            for room in self.rooms:
                await room.stop()
            # Before the bus too: a bot mid-dispatch may be awaiting a send, and
            # cancelling it here resolves that send rather than abandoning it.
            await self.bots.stop()
            if self.retention is not None:
                await self.retention.stop()
            await self.bus.aclose()
            if self.persistence is not None:
                # After the bus, so nothing is still producing rows, and before
                # the last status line, so its counters are final.
                await self.persistence.stop()
            self._started = True
            self._release()
            self._write(self._status_line())

    async def _run_service(
        self, index: int, service: Callable[[], Awaitable[None]]
    ) -> None:
        """Run one attached service, containing its failure (design D2).

        Cancellation is re-raised — that is the run shutting the service down,
        and swallowing it would leave the task looking like it had finished on
        its own. Anything else is reported and ends here: the run continues
        without the service rather than the service ending the run.
        """
        assert self.logger is not None
        try:
            await service()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(
                "service_failed",
                outcome="error",
                service=index,
                error=repr(exc),
                detail="the run continues without it; the radio is unaffected",
            )

    async def _restore(self) -> None:
        """Load contacts, paths and rooms, then start the writers and probe."""
        if self.persistence is None:
            return
        await self.persistence.restore(
            self.contacts,
            self.pipeline.paths,
            entities=len(self.config.stored_entities),
        )
        await self._load_rooms()
        # After the rooms, because a bot may not run on an entity a room is
        # bound to and this is where that is known (bot-runtime spec).
        await self._load_bots()
        self.persistence.start()
        for room in self.rooms:
            room.start()
        if self.retention is not None:
            self.retention.start()

    async def _load_rooms(self) -> None:
        """Bind each stored room to its entity, and say why any is not served.

        Design D5: rooms require a database, and the two ways a room can fail to
        be served — no database at all, and an entity that is not enabled — are
        both *stated*. A run that silently served no rooms would be
        indistinguishable from one whose rooms failed to load.
        """
        assert self.persistence is not None
        rooms = await self.persistence.rooms.list_all()
        if not isinstance(rooms, Succeeded):
            self._unserved_rooms.append(
                f"rooms could not be read: {rooms.error}; none is served"
            )
            return

        by_id = {stored.record.id: stored for stored in self.config.stored_entities}
        for record in rooms.value:
            stored = by_id.get(record.entity_id)
            if stored is None or not stored.record.enabled:
                reason = (
                    "its identity is not enabled"
                    if stored is not None
                    else "its identity was not loaded"
                )
                self._unserved_rooms.append(
                    render_room_startup(
                        name=record.name,
                        entity_name="?" if stored is None else stored.name,
                        node_hash=0 if stored is None else stored.node_hash,
                        members=0,
                        messages=0,
                        guest_access=record.guest_access,
                        retention=record.retention,
                        served=False,
                        not_served_because=reason,
                    )
                )
                continue
            await self._serve_room(record, stored)
        if self.rooms:
            self.retention = RoomRetentionPruner(rooms=self.rooms, logger=self.logger)

    async def _load_bots(self) -> None:
        """Bind each stored bot to its entity, and say why any is not run.

        Design D5: bots require a database, and every way a bot can fail to run
        — no database at all, a disabled bot, a disabled or unloaded identity, an
        identity a room already holds, a driver this build does not have — is
        *stated*. A run that silently ran no bots would be indistinguishable
        from one whose bots failed to load, and for a component whose correct
        behaviour is usually to stay quiet that distinction is the whole of the
        operator's view.
        """
        assert self.persistence is not None
        bots = await self.persistence.bots.list_all()
        if not isinstance(bots, Succeeded):
            self._unrun_bots.append(f"bots could not be read: {bots.error}; none is run")
            return

        by_id = {stored.record.id: stored for stored in self.config.stored_entities}
        room_entities = {room.entity.identity.public_key for room in self.rooms}
        for record in bots.value:
            stored = by_id.get(record.entity_id)
            reason = ""
            if stored is None:
                reason = "its identity was not loaded"
            elif not stored.record.enabled:
                reason = "its identity is not enabled"
            elif not record.enabled:
                reason = "the bot is disabled"
            elif stored.public_key in room_entities:
                reason = "its identity serves a room, and an identity has one role"
            if reason:
                self._not_run(record, stored, reason)
                continue
            assert stored is not None
            await self._run_bot(record, stored)

    def _not_run(self, record: BotRecord, stored: LoadedEntity | None, reason: str) -> None:
        self._unrun_bots.append(
            render_bot_startup(
                name=record.entity_name or ("?" if stored is None else stored.name),
                driver=record.driver,
                node_hash=0 if stored is None else stored.node_hash,
                mode=record.mode,
                limits={},
                served=False,
                not_served_because=reason,
            )
        )

    async def _run_bot(self, record: BotRecord, stored: LoadedEntity) -> None:
        """Give one stored bot its driver, its entity and its worker."""
        entity = next(
            (stub for stub in self.adverts.stubs if stub.identity.public_key == stored.public_key),
            None,
        )
        if entity is None:  # pragma: no cover - a stored entity is always adopted
            return
        try:
            driver = bot_drivers.build(record.driver, record.config)
        except UnknownDriverError as exc:
            # A row naming a driver this build does not have. Reported rather
            # than crashing the run: the other bots are fine and the operator
            # needs the name to fix it.
            self._not_run(record, stored, str(exc))
            return

        assert self.persistence is not None
        worker = BotWorker(
            record=record,
            driver=driver,
            storage=self.persistence,
            send_message=self._sender_for(entity),
            announce_advert=self._announcer_for(entity),
            # Never floods, and never asks to (design D10): a greeting is
            # unsolicited traffic to a peer that has never contacted us, and
            # shouting one across the whole mesh imposes its cost on everybody.
            route_known=self._route_known,
            lookup=self.contacts.get,
            entity=entity,
            clock=self.clock,
            on_event=self._on_bot_event,
            logger=self.logger,
        )
        self.bots.add(worker)

    def _sender_for(
        self, entity: EntityStub
    ) -> Callable[[Contact, str, float], Awaitable[SendOutcome]]:
        """A bot's send, bound to the identity it speaks as.

        The ordinary outbound path and nothing else (`greeter-bot` spec): the
        same composition, routing, retry, acknowledgement and timeout an
        operator's `--send` uses, at `PriorityClass.MESSAGE` — §4.3's class for
        originated traffic, which already names bot DMs. No retry policy is
        introduced here and none may be.

        The grace window a driver may ask for is not a retry policy: it adds
        listening after the last attempt, never a packet.
        """

        async def send(
            contact: Contact, text: str, ack_grace_seconds: float = 0.0
        ) -> SendOutcome:
            return await self.messenger.send(
                entity,
                contact,
                text,
                allow_flood=False,
                ack_grace_ms=ack_grace_seconds * 1000.0,
            )

        return send

    def _announcer_for(self, entity: EntityStub) -> Callable[[bool], Awaitable[bool]]:
        """A bot's advert, bound to the identity it speaks as.

        The live exercise established why a bot needs one at all: a direct
        message is decrypted with a secret derived from the *sender's* public
        key, so a peer that has never heard our advert cannot read a word of it
        and cannot acknowledge. The greeting looked identical to a peer out of
        range.

        Awaited, and that is the load-bearing part: an advert is
        `PriorityClass.ADVERT` (3) and a message is `MESSAGE` (2), so a send
        queued without waiting would be transmitted *first* and arrive at a peer
        that still could not read it.
        """

        async def announce(flood: bool) -> bool:
            handle = (
                self.adverts.request_flood(entity)
                if flood
                else self.adverts.request_zero_hop(entity)
            )
            outcome = await handle
            return bool(outcome.sent)

        return announce

    def _route_known(self, contact: Contact) -> bool:
        try:
            choose_route(self.pipeline.paths, contact, allow_flood=False)
        except NoRouteError:
            return False
        return True

    async def _serve_room(self, record: RoomRecord, stored: LoadedEntity) -> None:
        assert self.persistence is not None
        entity = next(
            (stub for stub in self.adverts.stubs if stub.identity.public_key == stored.public_key),
            None,
        )
        if entity is None:  # pragma: no cover - a stored entity is always adopted
            return
        members = await self.persistence.members.load_for_room(record.id)
        server = RoomServer(
            entity=entity,
            room=record,
            storage=self.persistence,
            paths=self.pipeline.paths,
            submit=self.bus.submit,
            acks=self.acks,
            members=members.value if isinstance(members, Succeeded) else [],
            clock=self.clock,
            radio=self.radio,
            telemetry=self._telemetry,
            runtime_stats=self._server_stats,
            on_event=self._on_room_event,
            logger=self.logger,
        )
        counted = await self.persistence.messages.count(record.id)
        self._room_messages[record.name] = (
            counted.value if isinstance(counted, Succeeded) else 0
        )
        server.subscribe(self.bus)
        # Design D10: this entity's packets are the room server's, so the direct
        # messenger and the shared path-body reader both leave it alone. Applied
        # here, at wiring time, which is when the ambiguity is resolvable.
        self.messenger.claim_for_room(entity.entity_id)
        self.path_bodies.entities = [
            stub for stub in self.path_bodies.entities if stub is not entity
        ]
        self.rooms.append(server)

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
            for line in self._room_status_lines():
                self._print(line)
            for line in self._bot_status_lines():
                self._print(line)

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
        at = self.clock.now()
        if self._tx_watcher is not None:
            # Before the durable write and outside it: a watcher is a display,
            # and a run with no database still has transmissions to show.
            try:
                self._tx_watcher(submission, outcome, at)
            except Exception as exc:  # pragma: no cover - a broken watcher
                assert self.logger is not None
                self.logger.error(
                    "tx_watcher_error",
                    outcome="error",
                    packet_id=outcome.packet_id,
                    error=repr(exc),
                )
        if self.persistence is None:
            return
        self.persistence.record_tx(submission, outcome, at=at)

    def _on_dm_event(self, event: DirectMessageEvent) -> None:
        self._print(render_dm_event(event))
        if isinstance(event, MessageReceived):
            # Design D1: the messenger owns decryption and acknowledgement, and
            # this report is delivered *after* the acknowledgement was submitted.
            # Offering here rather than subscribing separately is what keeps one
            # decryption and one acknowledgement per packet true.
            self.bots.on_message(event)

    def _on_room_event(self, event: RoomEvent) -> None:
        self._print(render_room_event(event))

    def _on_bot_event(self, event: BotRuntimeEvent) -> None:
        self._print(render_bot_event(event))

    @property
    def _telemetry(self) -> list[TelemetryEntry]:
        """What the board reported about itself, and nothing else (§4.1).

        Built from the §4.1 probe readback, whose whole design is that an
        unanswered query is `Absent` rather than a default. So a frame omitting
        temperature means the board did not answer `GetMCUTemp`, and never that
        it answered zero — which is what makes the telemetry answer honest by
        construction rather than by care (design D14).
        """
        probe = self.probe_result
        if probe is None:
            return []
        entries: list[TelemetryEntry] = []
        if not isinstance(probe.battery_mv, Absent):
            entries.append(voltage_entry(probe.battery_mv / 1000.0))
        if not isinstance(probe.mcu_temp_tenths_c, Absent):
            entries.append(temperature_entry(probe.mcu_temp_tenths_c / 10.0))
        return entries

    def _server_stats(self) -> ServerStats:
        """The shared-radio counters a status request is answered with (D13).

        Runtime-wide, because one modem serves every entity in the process and
        there is no per-entity radio to report. The room's own posted and pushed
        counts are added by the room server itself.
        """
        status = self.scheduler.status()
        dedup = self.pipeline.dedup.stats
        return ServerStats(
            curr_tx_queue_len=min(sum(status.queue_depths.values()), 0xFFFF),
            # `noise_floor` has no equivalent on a KISS modem and is left at
            # zero, which is what this field's absence looks like on the wire.
            n_packets_sent=status.stats.transmitted,
            total_air_time_secs=int(status.duty_cycle_used_ms / 1000),
            n_direct_dups=min(dedup.duplicates, 0xFFFF),
        )

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
        for line in self._room_lines():
            self._write(line)
        for line in self._bot_lines():
            self._write(line)
        for entity in self.entities.entities:
            for warning in entity.warnings:
                self._write(f"!! {warning}")
        self._release()
        if self.capture_writer is not None:
            probe_result = await self.capture_probe() if self.capture_probe else None
            if probe_result is not None:
                self.probe_result = probe_result
            self.capture_writer.start(probe_result)
        # Only now is the board's readback adopted, so only now may anything be
        # queued: the scheduler drops what it cannot compute airtime for.
        self._ready.set()

    def _room_lines(self) -> list[str]:
        """What rooms this run serves, said before any traffic (11.2, 11.3)."""
        if self.persistence is None:
            return [ROOMS_OFF]
        lines = [
            render_room_startup(
                name=room.room.name,
                entity_name=room.entity.name,
                node_hash=room.entity.node_hash,
                members=len(room.members),
                messages=self._room_messages.get(room.room.name, 0),
                guest_access=room.room.guest_access,
                retention=room.room.retention,
            )
            for room in self.rooms
        ]
        lines.extend(self._unserved_rooms)
        return lines or ["rooms: none configured"]

    def _bot_lines(self) -> list[str]:
        """What bots this run is running, said before any traffic (design D5)."""
        if self.persistence is None:
            return [BOTS_OFF]
        lines = [
            render_bot_startup(
                name=worker.name,
                driver=worker.driver_name,
                node_hash=0 if worker.entity is None else worker.entity.node_hash,
                mode=str(worker.mode),
                limits=worker.limits(),
            )
            for worker in self.bots.workers
        ]
        lines.extend(self._unrun_bots)
        return lines or ["bots: none configured"]

    def _bot_status_lines(self) -> list[str]:
        return [
            render_bot_status(
                name=worker.name,
                driver=worker.driver_name,
                mode=str(worker.mode),
                actions=worker.counters.actions,
                observations=worker.counters.observations,
                suppressions=worker.counters.suppressions,
                dropped=worker.counters.dropped,
                failures=worker.counters.failures,
                pending=worker.pending,
                announces=worker.counters.announces,
            )
            for worker in self.bots.workers
        ]

    def _room_status_lines(self) -> list[str]:
        return [
            render_room_status(
                name=room.room.name,
                members=len(room.members),
                messages_stored=room.posts_stored,
                deliveries_outstanding=room.deliveries_outstanding,
                members_behind=room.members_behind(),
                accepting_posts=room.accepting_posts,
                refusals=room.throttle.refusals,
                pruned=room.storage.messages.pruned,
                pruned_unsynced=room.storage.messages.pruned_unsynced,
            )
            for room in self.rooms
        ]

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
            conversations=self.persistence.restored.conversations,
            direct_messages=self.persistence.restored.direct_messages,
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
