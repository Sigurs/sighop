"""What a driver is, what it is handed, and what it is allowed to do.

DESIGN.md §7 states the plugin seam and this module is it. One rule shapes
everything here: **a driver's only capability is what its context grants.** It
is not given the transmit scheduler, the network bus, the modem or a database
session, so the rate limit, the observe/active mode and the never-flood rule
cannot be bypassed by a driver that forgets to check — there is no path along
which a driver could reach the radio to bypass them with.

**Why there is no `on_channel_message`.** §7's sketch has three handlers. Two
are here. The third is not, and its absence is intent rather than oversight: no
channel key store exists and nothing in `net/` decrypts `GRP_TXT`, so a hook
declared here could never fire. A protocol member that is structurally
unreachable is a promise the runtime cannot keep, and a driver author would
write against it. It arrives with channels.

**Why an advert event and not an advert.** `AdvertEvent` is built from a
`ContactObservation` and the `RxRecord` that produced it, and `ContactStore`
produces an observation only from a `VerifiedAdvert` (`net/contacts.py`). So a
driver cannot be invoked for an advert whose signature did not verify — the type
system carries that, not a check somebody has to remember. An unverified
advert's name, node type and location are attacker-chosen, and the trigger for a
transmission may never rest on them.

Nothing in this module imports `monitor/`, `db/` beyond the outcome type, or any
driver. The direction is one way: drivers import this, this imports nothing of
theirs.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from sighop.db.engine import Outcome
from sighop.net.contacts import Contact
from sighop.protocol.payloads import NodeType, WireText

DEFAULT_RATE_PER_HOUR = 6.0
"""The sustained rate a bot's outbound actions refill at, per hour.

Small on purpose and not derived from the airtime ceiling: the ceiling bounds
how much air *sighop* may take and this bounds how much of it one automated
decision path may claim on its own initiative. §7's warned-about case is a burst
of adverts after an outage, and six an hour is a neighbourhood's worth of new
nodes rather than a mesh's."""

DEFAULT_BURST = 3
"""How many actions may happen back to back before the sustained rate binds."""

RESERVED_CONFIG_KEYS = frozenset({"rate_per_hour", "burst"})
"""Configuration keys the runtime owns rather than the driver.

Driver configuration and the two limit values share one JSONB column because
they are set, listed and validated together (`sighop bot set`). These two are
the runtime's; a driver's validator is never asked about them and may not claim
them."""


class BotMode(StrEnum):
    """Whether this bot may touch the radio at all (design D4).

    Stored on the row rather than passed per run, so a bot cannot become active
    because an operator forgot which flags the last run had. `--enable-transmit`
    is the *other* gate and is unchanged: the mode says this bot is meant to
    act, the flag says this run is allowed to transmit, and they answer to
    different people.
    """

    OBSERVE = "observe"
    ACTIVE = "active"


class SuppressionReason(StrEnum):
    """Why a driver's action produced no transmission. Every one is counted.

    A closed set for the reason `net/room.py`'s `RefusalReason` is one: a reason
    reported as free text is a reason nobody can count, and a greeter that is
    greeting nobody has to be distinguishable from a mesh that has gone quiet
    and from a greeter that is broken.

    The first four belong to the runtime and are spent by any driver; the rest
    are the greeter's gates and are counted through the same channel so that one
    status line shows all of them.
    """

    RATE_LIMITED = "rate_limited"
    NO_ROUTE = "no_route"
    STORAGE_DEGRADED = "storage_degraded"
    STATE_WRITE_FAILED = "state_write_failed"

    ALREADY_GREETED = "already_greeted"
    GREETING_COOLDOWN = "greeting_cooldown"
    """A greeting went unanswered and the retry is not due yet."""

    GREETING_EXHAUSTED = "greeting_exhausted"
    """Every attempt this contact was allowed has gone unanswered."""

    TOO_MANY_HOPS = "too_many_hops"
    NODE_TYPE = "node_type"
    LOW_SNR = "low_snr"


class BotSendResult(StrEnum):
    """How one driver-initiated send ended."""

    ACKNOWLEDGED = "acknowledged"
    UNACKNOWLEDGED = "unacknowledged"
    """Transmitted to the end of the outbound path's attempts, unanswered."""

    OBSERVED = "observed"
    """The decision was made in full and nothing was queued (design D4)."""

    REFUSED = "refused"
    """Never composed: the rate limit, no known route, or the send was dropped
    before the air. Carries the reason."""


@dataclass(frozen=True, slots=True)
class BotSendOutcome:
    """What a driver gets back from `BotContext.send`. Never an exception.

    A driver that raises is contained (the runtime counts it and continues), but
    a refusal is not a fault — it is the limits doing their job — so it is a
    value the driver can act on rather than an exception it has to catch.
    """

    result: BotSendResult
    contact: Contact
    text: str
    reason: SuppressionReason | None = None
    attempts: int = 0
    route: str = ""

    @property
    def transmitted(self) -> bool:
        return self.result in (BotSendResult.ACKNOWLEDGED, BotSendResult.UNACKNOWLEDGED)

    @property
    def acknowledged(self) -> bool:
        return self.result is BotSendResult.ACKNOWLEDGED


# --- What a driver is handed ------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdvertEvent:
    """One verified advert observation, as a driver sees it.

    Carries what the *reception* established rather than what the advert
    claimed: `created` is the contact store's own answer to "had we ever heard
    this key", `hop_count` is the packet's path length and `snr_db` is the
    board's readback. A driver deciding to transmit rests on these.
    """

    contact: Contact
    created: bool
    hop_count: int | None
    snr_db: float | None
    packet_id: str
    received_at: dt.datetime
    name_changed: tuple[str, str] | None = None

    @property
    def node_type(self) -> NodeType | int | None:
        """The type the verified advert declared, as the store recorded it."""
        return self.contact.node_type

    def as_json(self) -> dict[str, object]:
        return {
            "packet_id": self.packet_id,
            "public_key": self.contact.public_key.hex(),
            "created": self.created,
            "hop_count": self.hop_count,
            "snr_db": self.snr_db,
        }


@dataclass(frozen=True, slots=True)
class DirectMessageEvent:
    """One decrypted direct message addressed to this bot's own entity.

    `contact` is a **claimed** sender and never a proven one — a MAC match
    selects a key (design D7 of milestone 4) — and a driver replying is replying
    to a claim. The acknowledgement has already been submitted by the time this
    exists (`direct-messaging` spec), so a driver can neither delay nor prevent
    it.
    """

    contact: Contact
    text: WireText
    timestamp: int
    packet_id: str

    def as_json(self) -> dict[str, object]:
        return {
            "packet_id": self.packet_id,
            "claimed_sender": self.contact.public_key.hex(),
            "text_bytes": len(self.text.raw),
        }


type BotEvent = AdvertEvent | DirectMessageEvent


# --- Durable state (design D16) ---------------------------------------------


class BotStateStore(Protocol):
    """The `bot_state` table's operations. `BotStateRepository` satisfies it.

    A protocol rather than the repository, for the reason `RoomStorage` is one:
    every bot-runtime and greeter test runs against in-memory storage with no
    database at all, so the database-marked tests are the ones actually about
    storage.
    """

    async def get(self, bot_id: uuid.UUID, key: str) -> Outcome[object | None]: ...

    async def set(
        self, bot_id: uuid.UUID, key: str, value: object, *, at: dt.datetime | None = ...
    ) -> Outcome[int]: ...

    async def set_many(
        self, bot_id: uuid.UUID, entries: dict[str, object], *, at: dt.datetime | None = ...
    ) -> Outcome[int]: ...

    async def delete(self, bot_id: uuid.UUID, key: str) -> Outcome[bool]: ...

    async def list(self, bot_id: uuid.UUID) -> Outcome[dict[str, object]]: ...

    async def clear(self, bot_id: uuid.UUID) -> Outcome[int]: ...


class BotStorage(Protocol):
    """The slice of `db/persistence.py` the bot runtime uses.

    Read-only properties rather than plain attributes, because a protocol's
    mutable attribute is invariant: declared as attributes, only something
    holding *exactly* a `BotStateRepository` would satisfy this, which is the
    opposite of the point. The real `Persistence` object satisfies it by having
    the right attributes, with no adapter in between.
    """

    @property
    def bot_state(self) -> BotStateStore: ...

    @property
    def degraded(self) -> bool: ...


@dataclass(slots=True)
class BotStateHandle:
    """One bot's slice of `bot_state`, and the only persistence a driver has.

    Namespaced by construction: the bot id is held here and every call carries
    it, so a driver cannot name another bot's key even by accident. Writes go
    straight through (design D16) — a driver that is told a write succeeded may
    rely on the row being there, which is what the greeter's write-then-transmit
    ordering rests on.
    """

    bot_id: uuid.UUID
    store: BotStateStore
    reads: int = 0
    writes: int = 0
    write_failures: int = 0

    async def get(self, key: str, default: object | None = None) -> object | None:
        from sighop.db.engine import Succeeded

        self.reads += 1
        outcome = await self.store.get(self.bot_id, key)
        if isinstance(outcome, Succeeded):
            return default if outcome.value is None else outcome.value
        # A read that failed is not "absent": reporting it as absent is how a
        # greeter greets somebody it already greeted. The caller distinguishes
        # the two by asking `degraded` before it acts.
        self.write_failures += 1
        return default

    async def set(self, key: str, value: object) -> bool:
        """Write one key. **False means it did not land**, never "probably"."""
        from sighop.db.engine import Succeeded

        outcome = await self.store.set(self.bot_id, key, value)
        if isinstance(outcome, Succeeded):
            self.writes += 1
            return True
        self.write_failures += 1
        return False

    async def delete(self, key: str) -> bool:
        from sighop.db.engine import Succeeded

        outcome = await self.store.delete(self.bot_id, key)
        return isinstance(outcome, Succeeded) and bool(outcome.value)

    async def all(self) -> dict[str, object]:
        from sighop.db.engine import Succeeded

        outcome = await self.store.list(self.bot_id)
        return outcome.value if isinstance(outcome, Succeeded) else {}


# --- The context ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BotContext:
    """A driver's whole world: send, look a contact up, persist, report.

    Deliberately a handful of callables and one state handle rather than a
    reference to the runtime. Nothing reachable from here is the scheduler, the
    bus, the modem or a database session, so a driver has no route to the radio
    that the mode and the rate limit are not in front of.

    `can_send` is the non-consuming half of the limit, split for the reason
    `net/room.py`'s throttle splits `check` from `spend`: the greeter must write
    its greeting record *before* it transmits (design D6), and a limit that were
    only discoverable by attempting the send would burn that record on an action
    the limit was going to refuse.
    """

    bot_name: str
    mode: BotMode
    state: BotStateHandle
    _send: Callable[[Contact, str, float], Awaitable[BotSendOutcome]]
    _can_send: Callable[[Contact], SuppressionReason | None]
    _lookup: Callable[[bytes], Contact | None]
    _suppress: Callable[[SuppressionReason, Contact | None, str], None]
    _degraded: Callable[[], bool]
    _announce: Callable[[int], Awaitable[bool]]
    _now: Callable[[], dt.datetime]

    @property
    def observing(self) -> bool:
        """True when nothing this driver decides will reach the air (design D4)."""
        return self.mode is BotMode.OBSERVE

    @property
    def storage_degraded(self) -> bool:
        """Whether durable storage is currently unavailable.

        A driver whose decisions depend on state it cannot record must not act
        while this is true — the `bot-runtime` spec says so, and the greeter
        suppresses with `storage_degraded` rather than greeting blind.
        """
        return self._degraded()

    def can_send(self, contact: Contact) -> SuppressionReason | None:
        """Whether a send to this contact could happen, or why it could not.

        Consumes nothing. Answers `no_route` before `rate_limited`, because a
        contact we have no route to would not have cost a token anyway.
        """
        return self._can_send(contact)

    async def send(
        self, contact: Contact, text: str, *, ack_grace_seconds: float = 0.0
    ) -> BotSendOutcome:
        """Transmit one direct message as this bot's identity — or record that
        it would have, in observe mode. Spends from the rate limit either way,
        so an observed run's counters are what an active run would have done.

        `ack_grace_seconds` buys extra *listening* after the message path has
        spent its attempts, and never an extra transmission. A driver whose next
        move on silence is expensive — the greeter's is a flood advert — should
        ask for it, because an acknowledgement that came back over a longer path
        than the one we sent on is late rather than missing, and acting on the
        difference costs the whole mesh.
        """
        return await self._send(contact, text, ack_grace_seconds)

    async def announce(self, hops: int) -> bool:
        """Make this bot's identity known to a peer `hops` away, and wait.

        **A direct message is only decryptable by a node that already holds the
        sender's public key** — the shared secret is derived from it, so a peer
        that has never heard our advert cannot read a word we send, and cannot
        acknowledge. That is not a failure mode a driver can detect: it looks
        exactly like a peer that is out of range. So a driver that is about to
        message a stranger says how far away it is and the runtime introduces us
        at that distance.

        The mechanism is the runtime's, not the driver's: `hops == 0` is a
        zero-hop advert that stops at direct neighbours, and anything further
        needs a flood, which every repeater in the mesh repeats. **Awaited**,
        because an advert is class 3 and a message is class 2, so a send issued
        without waiting would overtake the very advert that makes it readable.

        *When* to spend that is the driver's call, not this method's: a peer may
        already hold our key from an earlier advert, in which case introducing
        ourselves again buys nothing. A driver is free to try the cheap thing
        first and announce only once silence has proved it necessary.

        Returns whether the advert reached the air. Observe mode transmits
        nothing and answers False.
        """
        return await self._announce(hops)

    def lookup(self, public_key: bytes) -> Contact | None:
        """One contact by its public key. The store stays the authority."""
        return self._lookup(public_key)

    def now(self) -> dt.datetime:
        """The runtime's clock, so a driver measuring an interval is testable.

        The same injected clock the rate limit refills against — a driver that
        called `datetime.now` directly would be one whose cooldown could only be
        tested by waiting it out.
        """
        return self._now()

    def suppress(
        self,
        reason: SuppressionReason,
        contact: Contact | None = None,
        detail: str = "",
    ) -> None:
        """Report and count a decision not to act. A greeter that is greeting
        nobody must be distinguishable from one that is not being asked to."""
        self._suppress(reason, contact, detail)


# --- The driver -------------------------------------------------------------


@runtime_checkable
class Bot(Protocol):
    """What the runtime requires of a driver.

    Two handlers, both optional in effect — a driver that cares about only one
    kind of event implements the other as a no-op — and three pieces of
    configuration behaviour, because `sighop bot set` hands each value to the
    driver that owns it rather than to a schema in the CLI (design D14).

    There is no `on_channel_message`. See the module docstring: nothing decrypts
    `GRP_TXT` yet, and a hook that could never fire is a promise the runtime
    cannot keep.
    """

    driver_name: str

    @staticmethod
    def default_config() -> dict[str, object]:
        """What a newly created bot of this driver is configured with."""
        ...

    @staticmethod
    def validate_config(key: str, value: str) -> object:
        """Parse and check one configured value, or raise `BotConfigError`.

        Runs at configuration time so that "greeting text too long for one
        direct message" is refused when it is typed rather than discovered at
        3am when the first new node adverts.
        """
        ...

    def limits(self) -> dict[str, object]:
        """The driver's own bounds, for the startup line and `bot show`."""
        ...

    def counters(self) -> dict[str, int]:
        """Whatever this driver counts, added to the bot's own counters."""
        ...

    async def on_advert(self, context: BotContext, event: AdvertEvent) -> None: ...

    async def on_direct_message(
        self, context: BotContext, event: DirectMessageEvent
    ) -> None: ...


class BotConfigError(ValueError):
    """A configured value a driver refuses. The message is what the CLI prints."""


class UnknownDriverError(ValueError):
    """A driver name no registry entry matches. Lists the ones that exist."""


@dataclass(slots=True)
class BotCounters:
    """What one bot did, for the periodic status report and `sighop bot show`.

    Every field is reported even at zero, for the reason the persistence
    counters are: a field that disappears when it is zero cannot be told from a
    field nobody wrote.
    """

    actions: int = 0
    """Transmissions this bot put on the air (or tried to)."""

    announces: int = 0
    """Adverts this bot asked for so a peer could read what it sent next.

    Counted apart from `actions` because they are a different cost: a zero-hop
    one is local, and a flood one is repeated by every repeater in the mesh."""

    observations: int = 0
    """Actions taken in full and deliberately not transmitted (design D4)."""

    suppressions: dict[str, int] = field(default_factory=dict)
    dropped: int = 0
    """Dispatches discarded because this bot's queue was full (design D12)."""

    failures: int = 0
    """Times this bot's driver raised. A bot that keeps failing must be
    identifiable as failing rather than as idle."""

    def suppress(self, reason: SuppressionReason) -> None:
        self.suppressions[str(reason)] = self.suppressions.get(str(reason), 0) + 1

    def as_json(self) -> dict[str, object]:
        return {
            "bot_actions": self.actions,
            "bot_announces": self.announces,
            "bot_observations": self.observations,
            "bot_suppressions": dict(sorted(self.suppressions.items())),
            "bot_dispatches_dropped": self.dropped,
            "bot_driver_failures": self.failures,
        }


# --- Events the renderer formats --------------------------------------------
#
# Typed values, never strings: `bots/` never imports `monitor/`, exactly as
# `net/room.py` does not (milestone 6 design D17).


@dataclass(frozen=True, slots=True)
class BotActed:
    """A bot transmitted. Reported whatever the acknowledgement said."""

    bot_name: str
    driver: str
    contact: Contact
    text: str
    result: BotSendResult
    attempts: int
    route: str


@dataclass(frozen=True, slots=True)
class BotWouldAct:
    """An observe-mode decision. Rendered as an observation that transmitted
    nothing, and never formatted like a transmission (design D4)."""

    bot_name: str
    driver: str
    contact: Contact
    text: str


@dataclass(frozen=True, slots=True)
class BotSuppressed:
    bot_name: str
    driver: str
    reason: SuppressionReason
    contact: Contact | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BotDispatchDropped:
    """The oldest pending dispatch, discarded because the queue was full.

    Oldest rather than newest, matching `net/bus.py`'s subscription semantics
    rather than inventing a second policy: for a greeter, an advert queued long
    enough to be dropped has almost certainly been superseded (design D12).
    """

    bot_name: str
    driver: str
    dropped: int


@dataclass(frozen=True, slots=True)
class BotFailed:
    """A driver handler raised. Counted, reported, and otherwise ignored."""

    bot_name: str
    driver: str
    event: str
    error: str
    failures: int


type BotRuntimeEvent = BotActed | BotWouldAct | BotSuppressed | BotDispatchDropped | BotFailed
