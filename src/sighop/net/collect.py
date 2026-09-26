"""Repeater collection: sighop as a guest client of the repeaters around it.

Everything else in `net/` answers. This logs in to operator-selected repeaters
with a blank guest password, asks each for its status and its neighbour list,
and records what came back (repeater-metrics D1). Stock firmware admits a blank
guest login while the repeater's guest password is unset, and answers
`GET_STATUS` and `GET_NEIGHBOURS` to a guest
(`simple_repeater/MyMesh.cpp:90-143`, `:216`, `:276`).

**One exchange at a time** (D2). Repeaters are polled one after another and
each step waits for its answer before the next is sent, so at most one request
is outstanding and an answer is matched against exactly that one: the repeater
we asked, the identity we asked from, and — for status and neighbours — the
timestamp the firmware echoes as a tag. A login answer carries the repeater's
clock rather than an echo, so it is matched by MAC and shape alone.

**A lost packet costs an attempt, not the poll** (repeater-poll-retries). An
unanswered step is resent with a fresh timestamp, up to three attempts, and a
late answer to any attempt of the step completes it. A repeater that answered a
login within the recency window gets two direct logins and then a flooded one,
whose path-return answer replaces a route gone stale; one that has not answered
recently — a guest password, or down — gets a single login, as before.

**The only decrypter of `RESPONSE`.** Nothing else in the process opens one; a
response bundled in a returned path reaches here from `PathBodyReader`, after
the route it declares has been adopted, so the next request goes direct.

**Settings are read from the database at every tick** (D7), with the last good
read kept when one fails, so a change made on the system page applies at the
next tick with no reconcile call. Collection yields to all other traffic
(`ADVERT` priority) and never submits while transmission is disabled (D6).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from sighop.config import DEFAULT_PATH_HASH_SIZE
from sighop.db.engine import Outcome, Succeeded
from sighop.db.repositories import (
    DEFAULT_COLLECTION_RECENT_DAYS,
    CollectionSettings,
    PollOutcome,
    PollRecord,
)
from sighop.logging import Logger, get_logger
from sighop.net.airtime import NoRadioReadback, require_params, time_on_air_ms
from sighop.net.bus import NetworkBus, PriorityClass, Submission, Subscription, TxHandle
from sighop.net.contacts import Contact, ContactStore
from sighop.net.dm import Route, ack_timeout_ms, choose_route
from sighop.net.paths import PathStore
from sighop.net.readback import wait_for_readback
from sighop.net.rx import Payload, RxRecord
from sighop.net.tx import Clock, SystemClock
from sighop.protocol.crypto import SharedSecretCache, encrypt_then_mac, mac_then_decrypt
from sighop.protocol.identity import LocalIdentity
from sighop.protocol.packet import (
    PAYLOAD_VERSION_1,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
)
from sighop.protocol.packet import encode as encode_packet
from sighop.protocol.payloads import (
    RESP_SERVER_LOGIN_OK,
    AnonRequestEnvelope,
    DirectEnvelope,
    NeighbourEntry,
    NeighboursBody,
    NeighboursRequest,
    NodeType,
    RepeaterLoginBody,
    RepeaterStatusBody,
    RequestBody,
    RequestType,
    ReturnedPathBody,
    build_anon_request,
    build_direct_envelope,
    build_repeater_login_body,
    build_request_body,
    build_returned_path_body,
    parse_login_answer_body,
    parse_neighbours_body,
    parse_repeater_status_body,
)
from sighop.protocol.result import DecodeFailure
from sighop.radio.modem import RadioParams

TICK_SECONDS = 60.0
"""How often the loop reads the settings and decides whether a cycle is due."""

PRUNE_INTERVAL = dt.timedelta(days=1)
"""Pruning runs at the end of every cycle and at least this often besides,
also while collection is disabled (D11)."""

SERVER_RESPONSE_DELAY_MS = 300.0
"""`simple_repeater/MyMesh.cpp:37`: the repeater waits this long before replying."""

NEIGHBOUR_PREFIX_LENGTH = 6
NEIGHBOUR_PAGE_SIZE = 11
"""Entries of `6 + 4 + 1` bytes into the firmware's 130-byte buffer (`:342`)."""

MAX_NEIGHBOUR_PAGES = 8

MAX_STEP_ATTEMPTS = 3
"""Attempts at one step before the poll ends there (repeater-poll-retries D2)."""

RESPONSE_POLL_SECONDS = 0.05

FLOOD_SETTLE_FLOOR_MS = 3000.0
"""Stock clients wait this long before a path return (`handleReturnPathRetry`)."""

FLOOD_SETTLE_WAVES = 2
"""Re-flood waves waited out after a flooded answer: each repeater that hears
it resends after up to five half-airtimes (`Mesh::getRetransmitDelay`), then
spends one airtime sending."""

PATH_RETURN_DEADLINE_SECONDS = 30.0

DIRECT_RELAY_DELAY_FACTOR = 0.3
"""`simple_repeater`'s default `direct_tx_delay_factor` (`MyMesh.cpp:893`): a
relay forwards a direct packet after up to five times this many airtimes
(`getDirectRetransmitDelay`)."""

RELAY_CLEAR_MARGIN_MS = 250.0
"""Per relay, for its dispatch and channel-activity check before it sends."""

_FLOOD_ROUTES = frozenset({RouteType.FLOOD, RouteType.TRANSPORT_FLOOD})


def flood_settle_ms(size_bytes: int, radio: RadioParams) -> float:
    """How long to stay quiet after a flooded packet of this size was heard."""
    airtime = time_on_air_ms(size_bytes, radio)
    wave = airtime * 1.04 / 2 * 5 + airtime
    return max(FLOOD_SETTLE_FLOOR_MS, FLOOD_SETTLE_WAVES * wave)


def relay_clear_ms(size_bytes: int, hops: int, radio: RadioParams) -> float:
    """How long after sending a direct packet until every relay has forwarded it.

    A relay that is still sending cannot hear the next packet, and one that
    gets it mid-send drops it: the request that followed a path return by under
    a second went unanswered while the relay was still forwarding the return.
    """
    airtime = time_on_air_ms(size_bytes, radio)
    per_hop = airtime * DIRECT_RELAY_DELAY_FACTOR * 5 + airtime + RELAY_CLEAR_MARGIN_MS
    return hops * per_hop


# Reply sizes, for the reply's share of a step's timeout: a direct datagram's
# header, path allowance, hashes and MAC, plus the body padded to the block.
_REPLY_OVERHEAD = 2 + 64 + 2 + 2
_LOGIN_REPLY_BODY = 16
_STATUS_REPLY_BODY = 64
_NEIGHBOURS_REPLY_BODY = 144


class CollectionSettingsSource(Protocol):
    """`RepeaterCollectionRepository` satisfies it."""

    async def get(self) -> Outcome[CollectionSettings]: ...

    async def record_cycle(
        self,
        *,
        started_at: dt.datetime,
        finished_at: dt.datetime | None,
        polled: int | None,
        succeeded: int | None,
        note: str | None = ...,
    ) -> Outcome[bool]: ...


class TargetSource(Protocol):
    """`RepeaterTargetRepository` satisfies it."""

    async def list_keys(self) -> Outcome[frozenset[bytes]]: ...


class PollSink(Protocol):
    """`RepeaterPollRepository` satisfies it."""

    async def record(self, poll: PollRecord) -> Outcome[int]: ...

    async def prune_older_than(self, cutoff: dt.datetime) -> Outcome[int]: ...

    async def last_answered(self) -> Outcome[dict[bytes, dt.datetime]]: ...


class CollectorEntity(Protocol):
    """The slice of a local identity this needs. `EntityStub` satisfies it."""

    entity_id: str
    name: str
    identity: LocalIdentity

    @property
    def node_hash(self) -> int: ...


def is_eligible(
    contact: Contact | None,
    *,
    selected: frozenset[bytes],
    recent_days: int,
    now: dt.datetime,
) -> bool:
    """Polled only when a repeater, selected, and heard within the window."""
    if contact is None or contact.public_key not in selected:
        return False
    if contact.node_type != NodeType.REPEATER:
        return False
    if contact.last_heard is None:
        return False
    return now - contact.last_heard <= dt.timedelta(days=recent_days)


class Step(StrEnum):
    LOGIN = "login"
    STATUS = "status"
    NEIGHBOURS = "neighbours"


@dataclass(slots=True)
class _Outstanding:
    entity: CollectorEntity
    contact: Contact
    step: Step
    answer: asyncio.Future[bytes]
    tags: set[int] = field(default_factory=set)
    """Echo tags of every attempt at this step: a late answer to an earlier
    attempt still completes it. Empty for a login, which echoes nothing."""

    heard: RxRecord | None = None
    """The reception that carried the accepted answer."""


@dataclass(frozen=True, slots=True)
class _NotSent:
    reason: str


type _StepResult = bytes | _NotSent | None
"""The answer's plaintext, a submission that never reached the air, or None
for silence through every attempt's timeout."""

_DIRECT_ATTEMPTS: tuple[bool, ...] = (False,) * MAX_STEP_ATTEMPTS
"""A step's attempts, each True when it is flooded whatever route is known."""

_ONE_ATTEMPT: tuple[bool, ...] = (False,)

_FLOOD_FALLBACK: tuple[bool, ...] = (False,) * (MAX_STEP_ATTEMPTS - 1) + (True,)


@dataclass(slots=True)
class _PollProgress:
    routes: list[str] = field(default_factory=list)
    """Every route label used, in order, a change of route appending one."""
    resends: int = 0


@dataclass(slots=True)
class RepeaterCollector:
    contacts: ContactStore
    paths: PathStore
    submit: Callable[[Submission], TxHandle]
    settings: CollectionSettingsSource
    targets: TargetSource
    polls: PollSink
    transmit_enabled: Callable[[], bool]
    entities: Sequence[CollectorEntity] = ()
    """The runtime's live stub list, the same object `adverts.stubs` is."""

    secrets: SharedSecretCache | None = None
    clock: Clock = field(default_factory=SystemClock)
    radio: RadioParams | None = None
    radio_ready: asyncio.Event | None = None
    path_hash_size: int = DEFAULT_PATH_HASH_SIZE
    tick_seconds: float = TICK_SECONDS
    logger: Logger | None = None

    polls_recorded: int = field(default=0, init=False)
    responses_matched: int = field(default=0, init=False)
    responses_unmatched: int = field(default=0, init=False)
    path_returns: int = field(default=0, init=False)
    retries: int = field(default=0, init=False)
    login_floods: int = field(default=0, init=False)
    cycles: int = field(default=0, init=False)
    _outstanding: _Outstanding | None = field(default=None, init=False)
    _last_sent: dict[bytes, int] = field(default_factory=dict, init=False)
    _last_good: CollectionSettings | None = field(default=None, init=False)
    _last_started: dt.datetime | None = field(default=None, init=False)
    _last_pruned: dt.datetime | None = field(default=None, init=False)
    _room_entity_ids: set[str] = field(default_factory=set, init=False)
    _answered: dict[bytes, dt.datetime] = field(default_factory=dict, init=False)
    """When each repeater last answered a login (D3)."""
    _answered_seeded: bool = field(default=False, init=False)
    _stopped: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        self.secrets = self.secrets or SharedSecretCache()
        self.logger = self.logger or get_logger(component="collect")

    # --- Wiring -------------------------------------------------------------

    def subscribe(self, bus: NetworkBus, *, name: str = "repeater-collection") -> Subscription:
        return bus.subscribe(name, handler=self.handle)

    def set_radio(self, radio: RadioParams | None) -> None:
        self.radio = radio

    def claim_for_room(self, entity_id: str) -> None:
        """An identity serving a room is never used to log in (its traffic is the room's)."""
        self._room_entity_ids.add(entity_id)

    def release_from_room(self, entity_id: str) -> None:
        self._room_entity_ids.discard(entity_id)

    def stop(self) -> None:
        self._stopped.set()

    # --- The loop (D7) ------------------------------------------------------

    async def run(self) -> None:
        """Tick until stopped. Cycles run inside the tick, so they never overlap."""
        while not self._stopped.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # the loop must survive a bad tick
                assert self.logger is not None
                self.logger.error("repeater_collection_tick_failed", error=repr(error))
            # On the clock's own time, so a test clock that does not move on
            # sleep never turns this into a database read per loop turn.
            due = self.clock.now() + dt.timedelta(seconds=self.tick_seconds)
            while (
                not self._stopped.is_set()
                and (remaining := (due - self.clock.now()).total_seconds()) > 0
            ):
                await self.clock.sleep(remaining)

    async def tick(self) -> None:
        settings = await self._read_settings()
        if settings is None:
            return
        now = self.clock.now()
        if self._prune_due(now):
            await self._prune(settings, now)
        if not settings.enabled or not self._cycle_due(settings, now):
            return
        await self.run_cycle(settings)

    def _cycle_due(self, settings: CollectionSettings, now: dt.datetime) -> bool:
        started = [
            at for at in (settings.last_cycle_started_at, self._last_started) if at is not None
        ]
        if not started:
            return True
        return max(started) + dt.timedelta(minutes=settings.interval_minutes) <= now

    async def _read_settings(self) -> CollectionSettings | None:
        outcome = await self.settings.get()
        if isinstance(outcome, Succeeded):
            self._last_good = outcome.value
        else:
            assert self.logger is not None
            self.logger.error(
                "repeater_collection_settings_unreadable",
                operation=outcome.operation,
                using_last_good=self._last_good is not None,
            )
        return self._last_good

    async def run_cycle(self, settings: CollectionSettings) -> None:
        """One cycle: every eligible repeater, one at a time, then a summary."""
        assert self.logger is not None
        started = self.clock.now()
        self._last_started = started

        if not self.transmit_enabled():
            await self._skip(started, "transmission is disabled; no poll was sent")
            return
        entity, refusal = self._login_identity(settings)
        if entity is None:
            await self._skip(started, refusal)
            return
        selected = await self.targets.list_keys()
        if not isinstance(selected, Succeeded):
            await self._skip(started, "the selected repeaters could not be read")
            return
        due = sorted(
            (
                contact
                for key in selected.value
                if is_eligible(
                    contact := self.contacts.get(key),
                    selected=selected.value,
                    recent_days=settings.recent_days,
                    now=started,
                )
                and contact is not None
            ),
            key=lambda contact: contact.public_key,
        )
        await self._seed_answered()
        await self.settings.record_cycle(
            started_at=started, finished_at=None, polled=None, succeeded=None
        )
        self.cycles += 1
        self.logger.info(
            "repeater_collection_cycle_started",
            entity_id=entity.entity_id,
            selected=len(selected.value),
            eligible=len(due),
        )

        polled = succeeded = 0
        for contact in due:
            current = await self._read_settings()
            if current is None or not current.enabled:
                self.logger.info("repeater_collection_disabled_mid_cycle", polled=polled)
                break
            if not self.transmit_enabled():
                self.logger.info("repeater_collection_transmit_disabled_mid_cycle", polled=polled)
                break
            poll = await self.poll(entity, contact, recent_days=current.recent_days)
            polled += 1
            succeeded += poll.outcome is PollOutcome.SUCCEEDED
            await self._record(poll)

        finished = self.clock.now()
        await self.settings.record_cycle(
            started_at=started, finished_at=finished, polled=polled, succeeded=succeeded
        )
        self.logger.info(
            "repeater_collection_cycle_finished",
            polled=polled,
            succeeded=succeeded,
            duration_s=round((finished - started).total_seconds(), 1),
        )
        await self._prune(settings, finished)

    async def _skip(self, started: dt.datetime, note: str) -> None:
        assert self.logger is not None
        self.logger.info("repeater_collection_cycle_skipped", reason=note)
        await self.settings.record_cycle(
            started_at=started, finished_at=started, polled=0, succeeded=0, note=note
        )

    def _login_identity(
        self, settings: CollectionSettings
    ) -> tuple[CollectorEntity, str] | tuple[None, str]:
        if settings.entity_id is None:
            return None, "no login identity is set; choose one on the system page"
        wanted = str(settings.entity_id)
        entity = next((e for e in self.entities if e.entity_id == wanted), None)
        if entity is None:
            return None, "the login identity is not loaded in this run"
        if wanted in self._room_entity_ids:
            return None, "the login identity is serving a room; choose another"
        return entity, ""

    async def _seed_answered(self) -> None:
        """Load when each repeater last answered, once; a failed read is retried
        next cycle, and until then no login falls back to a flood (D3)."""
        if self._answered_seeded:
            return
        outcome = await self.polls.last_answered()
        if not isinstance(outcome, Succeeded):
            assert self.logger is not None
            self.logger.error("repeater_last_answered_unreadable", operation=outcome.operation)
            return
        for key, at in outcome.value.items():
            if key not in self._answered or self._answered[key] < at:
                self._answered[key] = at
        self._answered_seeded = True

    def _answered_recently(self, public_key: bytes, recent_days: int, now: dt.datetime) -> bool:
        at = self._answered.get(public_key)
        return at is not None and now - at <= dt.timedelta(days=recent_days)

    def _login_attempts(
        self, contact: Contact, recent_days: int, now: dt.datetime
    ) -> tuple[bool, ...]:
        """Direct, direct, then a flood, for a repeater that answers and has a
        route; one attempt for one that has not answered recently, and one
        flood when no route is known (D2)."""
        if self._route(contact).flood:
            return _ONE_ATTEMPT
        if not self._answered_recently(contact.public_key, recent_days, now):
            return _ONE_ATTEMPT
        return _FLOOD_FALLBACK

    async def _record(self, poll: PollRecord) -> None:
        outcome = await self.polls.record(poll)
        assert self.logger is not None
        if isinstance(outcome, Succeeded):
            self.polls_recorded += 1
        else:
            self.logger.error("repeater_poll_not_recorded", operation=outcome.operation)

    # --- Retention (D11) ----------------------------------------------------

    def _prune_due(self, now: dt.datetime) -> bool:
        return self._last_pruned is None or now - self._last_pruned >= PRUNE_INTERVAL

    async def _prune(self, settings: CollectionSettings, now: dt.datetime) -> None:
        self._last_pruned = now
        if settings.retention_days is None:
            return  # kept forever: nothing is ever deleted
        outcome = await self.polls.prune_older_than(
            now - dt.timedelta(days=settings.retention_days)
        )
        assert self.logger is not None
        if isinstance(outcome, Succeeded):
            if outcome.value:
                self.logger.info("repeater_polls_pruned", deleted=outcome.value)
        else:
            self.logger.error("repeater_polls_not_pruned", operation=outcome.operation)

    # --- One poll (D3, D4, D5, D9) ------------------------------------------

    async def poll(
        self,
        entity: CollectorEntity,
        contact: Contact,
        *,
        recent_days: int = DEFAULT_COLLECTION_RECENT_DAYS,
    ) -> PollRecord:
        """Log in, ask for status, page through neighbours. Always returns a record."""
        started = self.clock.now()
        progress = _PollProgress()

        def record(outcome: PollOutcome, reason: str | None = None, **kwargs: object) -> PollRecord:
            return PollRecord(
                public_key=contact.public_key,
                entity_id=_uuid_or_none(entity.entity_id),
                started_at=started,
                outcome=outcome,
                reason=reason,
                route=" → ".join(progress.routes),
                retries=progress.resends,
                **kwargs,  # type: ignore[arg-type]
            )

        login = await self._step(
            entity,
            contact,
            Step.LOGIN,
            progress,
            attempts=self._login_attempts(contact, recent_days, started),
        )
        if isinstance(login, _NotSent):
            return record(PollOutcome.NOT_SENT, login.reason)
        if login is None:
            return record(
                PollOutcome.LOGIN_UNANSWERED,
                "no answer: out of range, or a guest password is set",
            )
        self._answered[contact.public_key] = self.clock.now()

        status_answer = await self._step(entity, contact, Step.STATUS, progress)
        if isinstance(status_answer, _NotSent):
            return record(PollOutcome.NOT_SENT, f"status request: {status_answer.reason}")
        if status_answer is None:
            return record(PollOutcome.STATUS_UNANSWERED, "the status request was not answered")
        status = parse_repeater_status_body(status_answer)
        if isinstance(status, DecodeFailure):
            return record(PollOutcome.STATUS_UNANSWERED, f"unparsable: {status.detail}")
        assert isinstance(status, RepeaterStatusBody)

        entries: list[NeighbourEntry] = []
        total: int | None = None
        for _ in range(MAX_NEIGHBOUR_PAGES):
            answer = await self._step(
                entity, contact, Step.NEIGHBOURS, progress, offset=len(entries)
            )
            reason: str | None = None
            if isinstance(answer, _NotSent):
                reason = f"neighbour request: {answer.reason}"
            elif answer is None:
                reason = f"no answer to the neighbour request at offset {len(entries)}"
            else:
                page = parse_neighbours_body(answer, NEIGHBOUR_PREFIX_LENGTH)
                if isinstance(page, DecodeFailure):
                    reason = f"unparsable neighbour page: {page.detail}"
                else:
                    assert isinstance(page, NeighboursBody)
                    total = page.total
                    entries.extend(page.entries)
                    if not page.entries or len(entries) >= page.total:
                        break
                    continue
            return record(
                PollOutcome.NEIGHBOURS_INCOMPLETE,
                reason,
                stats=status.stats,
                neighbours_total=total,
                neighbours=tuple(entries),
            )
        return record(
            PollOutcome.SUCCEEDED,
            stats=status.stats,
            neighbours_total=total,
            neighbours=tuple(entries),
        )

    def _route(self, contact: Contact) -> Route:
        """A route keyed by the repeater's public key, else a flood (D5).

        A node-hash route is ignored: a direct packet down another repeater's
        path is simply lost, where a flood reaches it and teaches us the route.
        """
        route = choose_route(
            self.paths, contact, path_hash_size=self.path_hash_size, allow_flood=True
        )
        if route.ambiguous:
            return Route(flood=True, hash_size=self.path_hash_size)
        return route

    def _timestamp(self, public_key: bytes) -> int:
        """Strictly increasing per repeater (D3): firmware drops a repeat."""
        value = max(int(self.clock.now().timestamp()), self._last_sent.get(public_key, 0) + 1)
        self._last_sent[public_key] = value
        return value

    async def _step(
        self,
        entity: CollectorEntity,
        contact: Contact,
        step: Step,
        progress: _PollProgress,
        *,
        offset: int = 0,
        attempts: Sequence[bool] = _DIRECT_ATTEMPTS,
    ) -> _StepResult:
        """One step, resent until answered or out of attempts (D1).

        One `_Outstanding` covers every attempt, so a late answer to an earlier
        attempt completes the step while a later one is waiting.
        """
        assert self.logger is not None
        outstanding = _Outstanding(
            entity=entity,
            contact=contact,
            step=step,
            answer=asyncio.get_running_loop().create_future(),
        )
        self._outstanding = outstanding
        answer: bytes | None = None
        try:
            for attempt, flood in enumerate(attempts, start=1):
                if outstanding.answer.done():
                    break
                if attempt > 1:
                    progress.resends += 1
                    self.retries += 1
                if flood:
                    self.login_floods += 1
                    self.logger.info(
                        "repeater_login_flood_fallback",
                        repeater=contact.public_key.hex()[:12],
                        step_attempt=attempt,
                    )
                result = await self._attempt(
                    outstanding, progress, offset=offset, flood=flood, attempt=attempt
                )
                if isinstance(result, _NotSent):
                    return result
                if result is not None:
                    answer = result
                    break
            if answer is None and outstanding.answer.done():
                answer = outstanding.answer.result()
        finally:
            self._outstanding = None
        if answer is not None and outstanding.heard is not None:
            await self._after_flooded_answer(entity, contact, outstanding.heard)
        return answer

    async def _attempt(
        self,
        outstanding: _Outstanding,
        progress: _PollProgress,
        *,
        offset: int,
        flood: bool,
        attempt: int,
    ) -> _StepResult:
        """Send one attempt at the outstanding step and wait out its timeout."""
        assert self.secrets is not None and self.logger is not None
        entity, contact, step = outstanding.entity, outstanding.contact, outstanding.step
        route = Route(flood=True, hash_size=self.path_hash_size) if flood else self._route(contact)
        if not progress.routes or progress.routes[-1] != route.label:
            progress.routes.append(route.label)
        secret = self.secrets.get(entity.identity, contact.public_key)
        # A fresh timestamp every attempt: firmware drops a repeated one (D3).
        timestamp = self._timestamp(contact.public_key)
        if step is Step.LOGIN:
            packet = _anon_request_packet(
                entity.identity,
                contact,
                secret,
                build_repeater_login_body(RepeaterLoginBody(timestamp=timestamp)),
                route,
            )
            reply_body = _LOGIN_REPLY_BODY
        else:
            outstanding.tags.add(timestamp)
            if step is Step.STATUS:
                request = RequestBody(timestamp=timestamp, request_type=RequestType.GET_STATUS)
                reply_body = _STATUS_REPLY_BODY
            else:
                arguments = NeighboursRequest(
                    count=NEIGHBOUR_PAGE_SIZE,
                    offset=offset,
                    prefix_length=NEIGHBOUR_PREFIX_LENGTH,
                    blob=os.urandom(4),
                ).arguments()
                request = RequestBody(
                    timestamp=timestamp,
                    request_type=RequestType.GET_NEIGHBOURS,
                    arguments=arguments,
                )
                reply_body = _NEIGHBOURS_REPLY_BODY
            packet = _request_packet(
                entity.identity, contact, secret, build_request_body(request), route
            )

        try:
            await wait_for_readback(self.radio_ready)
            radio = require_params(self.radio)
        except NoRadioReadback as error:
            return _NotSent(str(error))
        airtime = max(
            time_on_air_ms(len(packet), radio),
            time_on_air_ms(_REPLY_OVERHEAD + reply_body, radio),
        )
        timeout_ms = ack_timeout_ms(airtime, route) + SERVER_RESPONSE_DELAY_MS

        now = self.clock.now()
        handle = self.submit(
            Submission(
                packet=packet,
                priority=PriorityClass.ADVERT,
                entity_id=entity.entity_id,
                entity_name=entity.name,
                entity_type="entity",
                # A request that cannot go inside its own answer window is
                # better dropped than sent into a window already closed.
                deadline=now + dt.timedelta(milliseconds=timeout_ms),
                origin=f"repeater_{step}",
            )
        )
        sent = await handle
        self.logger.info(
            "repeater_request_sent",
            step=str(step),
            step_attempt=attempt,
            repeater=contact.public_key.hex()[:12],
            route=route.label,
            timeout_ms=round(timeout_ms, 1),
            **sent.as_json(),
        )
        if not sent.sent:
            return _NotSent(sent.reason or str(sent.result))
        return await self._await_answer(outstanding, timeout_ms)

    async def _after_flooded_answer(
        self, entity: CollectorEntity, contact: Contact, heard: RxRecord
    ) -> None:
        """Let a flooded answer's re-floods die down, then tell the repeater our route.

        A repeater with no route to us answers a direct request by flood
        (`simple_repeater/MyMesh.cpp:692-696`), and every repeater in range
        re-floods it for seconds afterwards; a request sent into that is lost.
        Stock clients answer a flooded response with a direct path return
        carrying the route the flood took (`BaseChatMesh::handleReturnPathRetry`),
        which the repeater stores (`onPeerPathRecv`) and answers direct from
        then on — in later polls too, while its guest entry lasts.
        """
        if heard.route_type not in _FLOOD_ROUTES:
            return
        assert self.logger is not None
        try:
            radio = require_params(self.radio)
        except NoRadioReadback:
            return
        await self.clock.sleep(flood_settle_ms(heard.size_bytes, radio) / 1000)
        route = self._route(contact)
        if route.flood or heard.packet is None:
            return  # no route to send it along; the next answer is a path return anyway
        assert self.secrets is not None
        body = (
            build_returned_path_body(
                ReturnedPathBody(
                    hop_count=heard.packet.hop_count,
                    hash_size=heard.packet.hash_size,
                    path=heard.packet.path,
                )
            )
            # No extra: the firmware's dummy type and four random bytes, so
            # the packet hash is unique (`Mesh::createPathReturn`).
            + b"\xff"
            + os.urandom(4)
        )
        packet = _datagram_packet(
            PayloadType.PATH,
            entity.identity,
            contact,
            self.secrets.get(entity.identity, contact.public_key),
            body,
            route,
        )
        handle = self.submit(
            Submission(
                packet=packet,
                priority=PriorityClass.ADVERT,
                entity_id=entity.entity_id,
                entity_name=entity.name,
                entity_type="entity",
                deadline=self.clock.now() + dt.timedelta(seconds=PATH_RETURN_DEADLINE_SECONDS),
                origin="repeater_path_return",
            )
        )
        sent = await handle
        self.path_returns += 1
        self.logger.info(
            "repeater_path_returned",
            repeater=contact.public_key.hex()[:12],
            route=route.label,
            returned_hops=heard.packet.hop_count,
            **sent.as_json(),
        )
        if sent.sent:
            await self.clock.sleep(relay_clear_ms(len(packet), route.hop_count, radio) / 1000)

    async def _await_answer(self, outstanding: _Outstanding, timeout_ms: float) -> bytes | None:
        deadline = self.clock.now() + dt.timedelta(milliseconds=timeout_ms)
        while True:
            if outstanding.answer.done():
                return outstanding.answer.result()
            if self.clock.now() >= deadline:
                return None
            await self.clock.sleep(RESPONSE_POLL_SECONDS)

    # --- Inbound (D2) -------------------------------------------------------

    async def handle(self, record: RxRecord) -> None:
        match record.outcome:
            case Payload(payload=DirectEnvelope() as envelope) if (
                envelope.payload_type is PayloadType.RESPONSE
            ):
                self._handle_direct(envelope, record)
            case _:
                return

    def _addressed_to_us(self, dest_hash: int) -> bool:
        return any(
            entity.node_hash == dest_hash and entity.entity_id not in self._room_entity_ids
            for entity in self.entities
        )

    def _handle_direct(self, envelope: DirectEnvelope, record: RxRecord) -> None:
        outstanding = self._outstanding
        if (
            outstanding is None
            or envelope.dest_hash != outstanding.entity.node_hash
            or envelope.src_hash != outstanding.contact.node_hash
        ):
            if self._addressed_to_us(envelope.dest_hash):
                self._unmatched("nothing outstanding from that repeater")
            return
        assert self.secrets is not None
        secret = self.secrets.get(outstanding.entity.identity, outstanding.contact.public_key)
        candidate, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
        if not candidate.matched or plaintext is None:
            self._unmatched("does not decrypt under the outstanding repeater's secret")
            return
        self._accept(outstanding, plaintext, record)

    def on_bundled_response(
        self, entity: CollectorEntity, contact: Contact, payload: bytes, record: RxRecord
    ) -> None:
        """`PathBodyReader`'s callback: a response returned with its route."""
        outstanding = self._outstanding
        if (
            outstanding is None
            or entity.entity_id != outstanding.entity.entity_id
            or contact.public_key != outstanding.contact.public_key
        ):
            self._unmatched("bundled response with nothing outstanding from that repeater")
            return
        self._accept(outstanding, payload, record)

    def _accept(self, outstanding: _Outstanding, plaintext: bytes, record: RxRecord) -> None:
        if outstanding.answer.done():
            self._unmatched("answer already received for this step")
            return
        if outstanding.step is Step.LOGIN:
            answer = parse_login_answer_body(plaintext)
            if isinstance(answer, DecodeFailure) or answer.result != RESP_SERVER_LOGIN_OK:
                self._unmatched("not a successful login answer")
                return
        elif len(plaintext) < 4 or int.from_bytes(plaintext[:4], "little") not in outstanding.tags:
            self._unmatched("echoed timestamp is not one of the outstanding step's")
            return
        self.responses_matched += 1
        outstanding.heard = record
        outstanding.answer.set_result(plaintext)

    def _unmatched(self, why: str) -> None:
        self.responses_unmatched += 1
        assert self.logger is not None
        self.logger.info("repeater_response_unmatched", reason=why)

    def as_json(self) -> dict[str, object]:
        return {
            "repeater_cycles": self.cycles,
            "repeater_polls_recorded": self.polls_recorded,
            "repeater_responses_matched": self.responses_matched,
            "repeater_responses_unmatched": self.responses_unmatched,
            "repeater_path_returns": self.path_returns,
            "repeater_retries": self.retries,
            "repeater_login_floods": self.login_floods,
        }


def _uuid_or_none(value: str) -> uuid.UUID | None:
    """Stored identities carry their row id; a keyfile or stub identity has none."""
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _packet(route: Route, payload_type: PayloadType, payload: bytes) -> bytes:
    return encode_packet(
        Packet(
            header=PacketHeader(
                route_type=route.route_type,
                payload_type=payload_type,
                payload_version=PAYLOAD_VERSION_1,
            ),
            transport_codes=None,
            hop_count=route.hop_count,
            hash_size=route.hash_size,
            path=route.path,
            payload=payload,
        )
    )


def _anon_request_packet(
    sender: LocalIdentity, contact: Contact, secret: bytes, body: bytes, route: Route
) -> bytes:
    mac, ciphertext = encrypt_then_mac(secret, body)
    envelope = AnonRequestEnvelope(
        dest_hash=contact.node_hash,
        sender_public_key=sender.public_key,
        mac=mac,
        ciphertext=ciphertext,
    )
    return _packet(route, PayloadType.ANON_REQ, build_anon_request(envelope))


def _request_packet(
    sender: LocalIdentity, contact: Contact, secret: bytes, body: bytes, route: Route
) -> bytes:
    return _datagram_packet(PayloadType.REQ, sender, contact, secret, body, route)


def _datagram_packet(
    payload_type: PayloadType,
    sender: LocalIdentity,
    contact: Contact,
    secret: bytes,
    body: bytes,
    route: Route,
) -> bytes:
    mac, ciphertext = encrypt_then_mac(secret, body)
    envelope = DirectEnvelope(
        payload_type=payload_type,
        dest_hash=contact.node_hash,
        src_hash=sender.node_hash,
        mac=mac,
        ciphertext=ciphertext,
    )
    return _packet(route, payload_type, build_direct_envelope(envelope))
