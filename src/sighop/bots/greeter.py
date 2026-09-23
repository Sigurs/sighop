"""The greeter: one welcome message to a node the platform has never heard.

DESIGN.md §7 gives three rules for this driver — never re-greet across a
restart, rate-limit globally, greet only after signature verification — and this
milestone adds a fourth: **greet only what is nearby.** All four are properties
of the code rather than of this driver's good intentions:

* *verified only* — an `AdvertEvent` exists only for a `VerifiedAdvert`
  (`bots/base.py`), so there is no path along which an unverified advert reaches
  a handler;
* *rate-limited* — the token bucket is spent by `BotContext.send`, which is the
  only way out (`bots/runtime.py`);
* *never twice* — the greeting record, written **before** the transmission
  (design D6/D7);
* *nearby only* — the reception's own hop count against `max_hops`.

**What the hop gate does not do, stated rather than implied.** `hop_count` is
the number of repeaters an advert crossed, so `max_hops` bounds *network*
distance: the default of 1 greets nodes we hear directly and nodes one repeater
away, which is the neighbourhood a greeting is for. It does **not** bound radio
distance. A tropospheric or ducted path delivers a zero-hop advert from a node
hundreds of kilometres away, and no hop count will ever separate that from a
neighbour across the street. `min_snr_db` is offered for the operator who wants
to try and is null by default, because a strong ducted signal defeats it too.
The honest position (design D8): `max_hops` bounds how much of the mesh can
trigger us, the once-ever rule bounds the damage when it lets one through, and
neither claims to identify a duct.

**Known is not greeted (design D7, revised).** The gate is the greeting record
and nothing else: whether the triggering advert *created* the contact does not
enter into it. Those are two different facts — "we have never heard this key" is
the platform's memory of the mesh, "we have never said anything to this node" is
this bot's — and conflating them refused every peer heard before the greeter
existed, along with every peer a rate limit or a degraded database made us skip
once. A contact created before the greeter existed can never be created again.

What keeps that from meaning *the whole contact table at once* is that a new
greeter is **seeded**: creating one records every contact already present
as greeted, marked `seeded` rather than sent. The debt is data an operator reads
and edits on the bot's greeted page, one contact at a time, rather than a rule
that also refuses what the rule was never meant to refuse.

**Why the record goes first.** A crash between writing the greeting record and
transmitting costs one un-sent greeting, which is invisible to its recipient.
The reverse order costs a duplicate unsolicited message to a stranger after
every crash, which is the failure §7 rule 1 exists to prevent. That is milestone
6's "acknowledge only once the row has landed" pointed the other way: there the
row was the promise, here the record is the guard. Its consequence is stated
rather than hidden — while the database is degraded the greeter greets nobody,
and says so.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field

from sighop.bots.base import (
    AdvertEvent,
    BotConfigError,
    BotContext,
    BotSendOutcome,
    BotSendResult,
    DirectMessageEvent,
    SuppressionReason,
)
from sighop.net.contacts import Contact
from sighop.net.dm import MAX_TEXT_LEN
from sighop.protocol.payloads import NodeType

DEFAULT_GREETING = "Hi — you are new to me. I am a sighop node listening on this mesh."
"""Short, factual and unsolicited-friendly. Configurable, and bounded at
configuration time by what one direct message carries (design D14)."""

DEFAULT_MAX_HOPS = 1
"""Nodes we hear directly, and nodes one repeater away. See the module docstring
for what this bounds and what it does not."""

DEFAULT_NODE_TYPES: tuple[int, ...] = (int(NodeType.CHAT),)
"""Chat nodes only (design D9). Greeting a repeater or another room server puts
a message on the air that no human will read, and its airtime cost is paid by
everyone on the channel. Configurable, because someone may want to greet room
servers; default-restrictive, because the alternative spends other people's
airtime."""

GREETED_PREFIX = "greeted:"
"""One key per contact greeted, `greeted:<hex public key>`. Per bot, because
`bot_state` is — so two greeters on one node each decide for themselves — and
durable, because it is the only thing that makes "greeted at most once" survive a
restart, a contact deletion and a bot re-creation (design D7)."""

DEFAULT_RETRY_AFTER_MINUTES = 15
"""How long an unanswered greeting waits before it may be tried again.

Long enough that a node adverting every few minutes does not turn one silence
into a burst, short enough that a peer which was briefly unreachable is caught
on the same visit rather than the next day."""

DEFAULT_GREETING_ATTEMPTS = 3
"""How many greetings one contact may be sent before the greeter gives up.

Distinct from `net/dm.py`'s `MAX_ATTEMPT`, which counts *packets* inside one
send. This counts greetings: each is a fresh decision, minutes apart, with an
advert possibly in front of it."""

DEFAULT_ACK_GRACE_SECONDS = 30
"""How long a greeting keeps listening after the message path has spent its
attempts, before silence is believed.

The live exercise's `[redacted]` acknowledgement would have had to return over a
repeater we did not send through, and an acknowledgement that took a longer way
home is late rather than absent. Believing it absent costs a flood advert and a
second greeting, so a few seconds of listening is much the cheaper mistake. No
packet is transmitted during the window."""

PENDING = "pending"
"""Written before the transmission and replaced by its outcome. Surviving a
crash as `pending` costs one attempt, which is the right side of design D6's
trade — an attempt over-counted is a greeting not sent, and an attempt
under-counted is a stranger messaged twice."""

SEEDED = "seeded"
"""A record written when a bot is created, for a contact that already existed.

Distinct from a sent greeting on purpose: it is the difference between "we said
hello" and "we decided not to owe this one a hello", and an operator reading
the greeted list needs to be able to tell them apart before releasing one."""

OPERATOR = "operator"
"""A record an operator set by hand, to excuse a contact from being greeted."""

SETTLED = frozenset({"acknowledged", SEEDED, OPERATOR, "would-have-greeted"})
"""Record outcomes that end the matter, whatever else happens.

Everything else — `pending`, `unacknowledged`, `refused` — describes a greeting
that may never have been *read*, and is retried under the cooldown. The
distinction is the whole of finding 1 from the live exercise: a greeting nobody
acknowledged is not a greeting delivered, and recording it as one was wrong."""


def greeted_key(public_key: bytes) -> str:
    return f"{GREETED_PREFIX}{public_key.hex()}"


def greeted_public_key(key: str) -> bytes | None:
    """The contact a `greeted:` key names, or None for any other key."""
    if not key.startswith(GREETED_PREFIX):
        return None
    try:
        return bytes.fromhex(key.removeprefix(GREETED_PREFIX))
    except ValueError:  # pragma: no cover - only a hand-written row reaches this
        return None


def seeded_entries(contacts: Iterable[Contact], *, at: str) -> dict[str, object]:
    """Every contact already known, as a record saying nothing is owed to it.

    The debt a new greeter would otherwise start with (design D7): without this,
    creating a greeter on a node that already knows fifty peers is a decision to
    message fifty strangers. Which contacts, which key and which record are all
    one decision and it lives here, beside the key convention, because the
    panel's create form calls this with the durable contact table, so every
    greeter incurs the same debt however it came to exist.
    """
    return {
        greeted_key(contact.public_key): {
            "outcome": SEEDED,
            "at": at,
            "name": contact.display_name,
        }
        for contact in contacts
    }


def operator_entry(contact: Contact, *, at: str) -> dict[str, object]:
    """A record an operator set by hand, to excuse a contact from being greeted."""
    return {"outcome": OPERATOR, "at": at, "name": contact.display_name}


@dataclass(slots=True)
class GreeterBot:
    """The driver. Holds counters and configuration, and no state of its own —
    everything that must survive a restart is in `bot_state`."""

    config: dict = field(default_factory=dict)

    greetings_sent: int = 0
    greetings_acknowledged: int = 0
    greetings_observed: int = 0
    greetings_retried: int = 0

    driver_name = "greeter"

    # --- Configuration ------------------------------------------------------

    @staticmethod
    def default_config() -> dict[str, object]:
        return {
            "greeting": DEFAULT_GREETING,
            "max_hops": DEFAULT_MAX_HOPS,
            "node_types": list(DEFAULT_NODE_TYPES),
            "min_snr_db": None,
            "retry_after_minutes": DEFAULT_RETRY_AFTER_MINUTES,
            "greeting_attempts": DEFAULT_GREETING_ATTEMPTS,
            "ack_grace_seconds": DEFAULT_ACK_GRACE_SECONDS,
        }

    @staticmethod
    def _refuse_unknown(key: str) -> None:
        raise BotConfigError(
            f"the greeter has no setting {key!r}; it takes greeting, max_hops, "
            "node_types, min_snr_db, retry_after_minutes, greeting_attempts and "
            "ack_grace_seconds"
        )

    @staticmethod
    def validate_config(key: str, value: str) -> object:
        """Parse and check one value, refusing with what an operator can act on.

        Refused here rather than at send time, which is design D14's whole
        point: a greeting too long for one direct message is a typo, and finding
        it out at 3am when the first new node adverts is finding it out from the
        wrong place. Nothing is ever truncated at send time.
        """
        match key:
            case "greeting":
                raw = value.encode("utf-8")
                if len(raw) > MAX_TEXT_LEN:
                    raise BotConfigError(
                        f"a greeting is one direct message: the limit is "
                        f"{MAX_TEXT_LEN} bytes and this is {len(raw)}. A greeting "
                        "is never truncated or split at send time"
                    )
                if not raw:
                    raise BotConfigError(
                        "an empty greeting would put a message on the air that "
                        "says nothing; set a greeting or disable the bot"
                    )
                return value
            case "max_hops":
                try:
                    hops = int(value)
                except ValueError as exc:
                    raise BotConfigError(
                        f"max_hops is a number of repeaters crossed; {value!r} is not"
                    ) from exc
                if hops < 0:
                    raise BotConfigError("max_hops cannot be negative")
                return hops
            case "node_types":
                return _parse_node_types(value)
            case "retry_after_minutes":
                try:
                    minutes = int(value)
                except ValueError as exc:
                    raise BotConfigError(
                        f"retry_after_minutes is a whole number of minutes; {value!r} is not"
                    ) from exc
                if minutes < 1:
                    raise BotConfigError(
                        "a cooldown below one minute would let a node adverting "
                        "every few seconds turn one silence into a burst"
                    )
                return minutes
            case "greeting_attempts":
                try:
                    attempts = int(value)
                except ValueError as exc:
                    raise BotConfigError(
                        f"greeting_attempts is a whole number of greetings; {value!r} is not"
                    ) from exc
                if attempts < 1:
                    raise BotConfigError(
                        "fewer than one attempt would greet nobody, which is what "
                        "disabling the bot is for"
                    )
                return attempts
            case "ack_grace_seconds":
                try:
                    seconds = int(value)
                except ValueError as exc:
                    raise BotConfigError(
                        f"ack_grace_seconds is a whole number of seconds; {value!r} is not"
                    ) from exc
                if seconds < 0:
                    raise BotConfigError("ack_grace_seconds cannot be negative")
                return seconds
            case "min_snr_db":
                if value.strip().lower() in ("", "none", "null"):
                    return None
                try:
                    return float(value)
                except ValueError as exc:
                    raise BotConfigError(
                        f"min_snr_db is a signal-to-noise floor in dB; {value!r} is not. "
                        "Note that it does not identify a ducted long-haul path either"
                    ) from exc
            case _:
                GreeterBot._refuse_unknown(key)
                raise AssertionError  # pragma: no cover - `_refuse_unknown` raises

    @property
    def greeting(self) -> str:
        return str(self.config.get("greeting", DEFAULT_GREETING))

    @property
    def max_hops(self) -> int:
        try:
            return int(self.config.get("max_hops", DEFAULT_MAX_HOPS))
        except (TypeError, ValueError):
            return DEFAULT_MAX_HOPS

    @property
    def node_types(self) -> tuple[int, ...]:
        raw = self.config.get("node_types", DEFAULT_NODE_TYPES)
        if not isinstance(raw, list | tuple):
            return DEFAULT_NODE_TYPES
        try:
            return tuple(int(value) for value in raw)
        except (TypeError, ValueError):
            return DEFAULT_NODE_TYPES

    @property
    def min_snr_db(self) -> float | None:
        raw = self.config.get("min_snr_db")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @property
    def retry_after(self) -> dt.timedelta:
        try:
            minutes = int(self.config.get("retry_after_minutes", DEFAULT_RETRY_AFTER_MINUTES))
        except (TypeError, ValueError):
            minutes = DEFAULT_RETRY_AFTER_MINUTES
        return dt.timedelta(minutes=max(minutes, 0))

    @property
    def greeting_attempts(self) -> int:
        try:
            return max(int(self.config.get("greeting_attempts", DEFAULT_GREETING_ATTEMPTS)), 1)
        except (TypeError, ValueError):
            return DEFAULT_GREETING_ATTEMPTS

    @property
    def ack_grace_seconds(self) -> float:
        try:
            return max(float(self.config.get("ack_grace_seconds", DEFAULT_ACK_GRACE_SECONDS)), 0.0)
        except (TypeError, ValueError):
            return float(DEFAULT_ACK_GRACE_SECONDS)

    def limits(self) -> dict[str, object]:
        return {
            "max_hops": self.max_hops,
            "node_types": ",".join(_node_type_name(value) for value in self.node_types),
            "min_snr_db": self.min_snr_db,
            "greeting_bytes": len(self.greeting.encode("utf-8")),
            "greeting_attempts": self.greeting_attempts,
            "retry_after_minutes": int(self.retry_after.total_seconds() // 60),
            "ack_grace_seconds": int(self.ack_grace_seconds),
        }

    def counters(self) -> dict[str, int]:
        return {
            "greetings_sent": self.greetings_sent,
            "greetings_acknowledged": self.greetings_acknowledged,
            "greetings_observed": self.greetings_observed,
            "greetings_retried": self.greetings_retried,
        }

    # --- The gates ----------------------------------------------------------

    async def on_advert(self, context: BotContext, event: AdvertEvent) -> None:
        """Every gate must pass, and each is separately observable.

        Ordered cheapest first, with one exception that is not about cost: the
        storage check comes before the greeting-record read, because a read that
        failed while the database was degraded would come back empty and an
        empty read is indistinguishable from "never greeted". Greeting on the
        strength of a read that did not happen is exactly the duplicate this
        driver exists to avoid.

        `event.created` is deliberately not consulted (design D7, revised). The
        in-memory gates run first, so the record read — the one database round
        trip on this path — happens only for an advert that is otherwise worth
        greeting: nearby, the right node type, above any signal floor. Adverts
        are hours apart per node (92 in the 1003-record corpus), so that is a
        read per *interesting* advert rather than per reception.
        """
        contact = event.contact
        if event.hop_count is not None and event.hop_count > self.max_hops:
            context.suppress(
                SuppressionReason.TOO_MANY_HOPS,
                contact,
                detail=f"{event.hop_count} hops, limit {self.max_hops}",
            )
            return

        node_type = event.node_type
        if node_type is None or int(node_type) not in self.node_types:
            context.suppress(
                SuppressionReason.NODE_TYPE,
                contact,
                detail=_node_type_name(node_type),
            )
            return

        floor = self.min_snr_db
        if floor is not None and (event.snr_db is None or event.snr_db < floor):
            context.suppress(
                SuppressionReason.LOW_SNR,
                contact,
                detail=f"{event.snr_db} dB, floor {floor} dB",
            )
            return

        if context.storage_degraded:
            # Design D6's stated consequence: while storage is degraded the
            # greeter greets nobody, because it cannot record that it did.
            context.suppress(
                SuppressionReason.STORAGE_DEGRADED,
                contact,
                detail="a greeting is recorded before it is sent, and nothing can be recorded",
            )
            return

        key = greeted_key(contact.public_key)
        record = await context.state.get(key)
        attempted = _attempts(record)
        now = context.now()
        refusal = self._record_gate(record, attempted, now)
        if refusal is not None:
            context.suppress(refusal, contact, detail=self._record_detail(record, attempted, now))
            return

        attempt = attempted
        while True:
            attempt += 1
            refusal = context.can_send(contact)
            if refusal is not None:
                # Checked before the record is written, so a greeting the rate
                # limit was going to refuse does not burn one of this contact's
                # attempts.
                context.suppress(refusal, contact)
                return

            # The record goes first, carrying the attempt this is about to be.
            # See the module docstring: a crash between here and the send costs
            # one attempt, and the alternative costs a stranger a duplicate.
            if not await context.state.set(
                key,
                _record(PENDING, contact, attempt, acknowledged=False, at=context.now()),
            ):
                context.suppress(
                    SuppressionReason.STATE_WRITE_FAILED,
                    contact,
                    detail="the greeting record did not land, so nothing was transmitted",
                )
                return
            if attempt > 1:
                self.greetings_retried += 1

            # Introduce ourselves, if this attempt needs it. A direct message is
            # only readable by a node holding our public key, and the two
            # distances cost very differently — see `_announce_hops`.
            hops = self._announce_hops(event, attempt)
            if hops is not None:
                await context.announce(hops)

            outcome = await context.send(
                contact, self.greeting, ack_grace_seconds=self.ack_grace_seconds
            )
            if outcome.result is BotSendResult.OBSERVED:
                self.greetings_observed += 1
            elif outcome.transmitted:
                self.greetings_sent += 1
                if outcome.acknowledged:
                    self.greetings_acknowledged += 1

            await context.state.set(
                key,
                {
                    **_record(
                        _outcome_name(outcome.result),
                        contact,
                        attempt,
                        acknowledged=outcome.acknowledged,
                        at=context.now(),
                    ),
                    "route": outcome.route,
                    "packets": outcome.attempts,
                },
            )

            if not self._escalates(event, outcome, attempt):
                return

    def _escalates(self, event: AdvertEvent, outcome: BotSendOutcome, attempt: int) -> bool:
        """Whether to introduce ourselves and greet again without waiting.

        The distant case, and only it. A first greeting to a contact heard over
        a repeater goes out bare, betting the peer already holds our key; when
        that bet loses there is nothing to learn from waiting fifteen minutes
        and hearing the same silence, because the reason is not that the peer
        was busy — it is that it cannot read us. So the flood advert and the
        second greeting follow immediately, while the peer is demonstrably
        awake and adverting.

        Once only, and then the cooldown takes over. A second silence means
        something the escalation cannot fix — out of range, asleep, ignoring
        us — and the answer to that is to wait, not to keep transmitting.

        A direct neighbour never comes here: it was introduced to by a zero-hop
        advert *before* its first greeting, so silence from it is already the
        second kind, and repeating the introduction would buy nothing.
        """
        if attempt != 1:
            return False
        if attempt >= self.greeting_attempts:
            return False
        if (event.hop_count or 0) == 0:
            return False
        return outcome.result is BotSendResult.UNACKNOWLEDGED

    def _record_gate(
        self, record: object, attempted: int, now: dt.datetime
    ) -> SuppressionReason | None:
        """What an existing greeting record says about greeting again.

        Finding 1 from the live exercise: an unacknowledged greeting is **not**
        a greeting delivered. The peer may never have been able to read it — as
        happened, because it did not hold our key — so recording it as settled
        made one silent failure permanent. Now only an outcome in `SETTLED`
        closes the matter; everything else is retried, bounded by the attempt
        count and spaced by the cooldown so a node adverting every few minutes
        cannot turn one silence into a burst.
        """
        if record is None:
            return None
        outcome = _outcome(record)
        if outcome in SETTLED:
            return SuppressionReason.ALREADY_GREETED
        if attempted >= self.greeting_attempts:
            return SuppressionReason.GREETING_EXHAUSTED
        last = _attempted_at(record)
        if last is not None and now - last < self.retry_after:
            return SuppressionReason.GREETING_COOLDOWN
        return None

    def _record_detail(self, record: object, attempted: int, now: dt.datetime) -> str:
        if record is None:
            return ""
        outcome = _outcome(record)
        if outcome in SETTLED:
            return outcome
        if attempted >= self.greeting_attempts:
            return f"{attempted} attempts, none acknowledged"
        last = _attempted_at(record)
        if last is None:
            return ""
        waited = now - last
        remaining = self.retry_after - waited
        minutes = max(int(remaining.total_seconds() // 60), 0)
        return f"attempt {attempted} unanswered, retry in {minutes}m"

    def _announce_hops(self, event: AdvertEvent, attempt: int) -> int | None:
        """Whether to advert before this greeting, and how far it must reach.

        Finding 2 from the live exercise: a direct message is only decryptable
        by a node that already holds the sender's public key, so a peer that has
        never heard our advert cannot read a greeting and cannot acknowledge
        one. The two distances cost very differently, and that asymmetry is the
        whole of this policy:

        * **A direct neighbour** is introduced to by a zero-hop advert, which
          stops at direct neighbours and costs the mesh nothing. Cheap enough to
          spend up front, on every attempt.
        * **Anything further** needs a flood, which every repeater in the mesh
          repeats. Too expensive to spend on a guess — so the first greeting to
          such a peer goes out bare, on the chance it already holds our key from
          an earlier advert of ours, and the flood is paid for only once silence
          has proved it necessary. That proof arrives inside the same reaction
          (`_escalates`), not fifteen minutes later: the second attempt is the
          flooded one.

        `None` means send without adverting first.
        """
        hops = event.hop_count or 0
        if hops == 0:
            return 0
        return hops if attempt > 1 else None

    async def on_direct_message(self, context: BotContext, event: DirectMessageEvent) -> None:
        """The greeter says hello once and then listens.

        Implemented as a deliberate no-op rather than omitted: a driver answering
        a reply would turn one unsolicited message into a conversation nobody
        asked for, and the acknowledgement the sender needs has already been
        submitted by the messenger before this is called.
        """
        return


def _record(
    outcome: str,
    contact: Contact,
    attempts: int,
    *,
    acknowledged: bool,
    at: dt.datetime | None = None,
) -> dict[str, object]:
    """One greeting record. `acknowledged` is the field the gate turns on.

    Kept separate from `outcome` rather than derived from it, because a reader —
    an operator reading the greeted list, or a future driver — should not
    have to know which outcome strings count as delivered.
    """
    return {
        "outcome": outcome,
        "acknowledged": acknowledged,
        "attempts": attempts,
        "at": (at or _now()).isoformat(),
        "name": contact.display_name,
    }


def _outcome(record: object) -> str:
    if isinstance(record, dict):
        return str(record.get("outcome", ""))
    # A record written by hand, or by a version that stored something simpler.
    # Treated as settled: the safe reading of an unrecognised record is that
    # this contact has already had its greeting.
    return "acknowledged"


def _attempts(record: object) -> int:
    if not isinstance(record, dict):
        return 0
    try:
        return int(record.get("attempts", 0))
    except (TypeError, ValueError):
        return 0


def _attempted_at(record: object) -> dt.datetime | None:
    if not isinstance(record, dict):
        return None
    raw = record.get("at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _outcome_name(result: BotSendResult) -> str:
    """What the greeting record says happened, in the spec's own words."""
    match result:
        case BotSendResult.ACKNOWLEDGED:
            return "acknowledged"
        case BotSendResult.UNACKNOWLEDGED:
            return "unacknowledged"
        case BotSendResult.OBSERVED:
            return "would-have-greeted"
        case BotSendResult.REFUSED:
            return "refused"


def _now_iso() -> str:
    return _now().isoformat()


def _parse_node_types(value: str) -> list[int]:
    """`CHAT,ROOM_SERVER` or `1,3`, because both are things an operator types."""
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise BotConfigError(
            "node_types cannot be empty: a greeter with no acceptable node type "
            "would greet nobody and would look exactly like a broken one"
        )
    types: list[int] = []
    for part in parts:
        try:
            types.append(int(part))
            continue
        except ValueError:
            pass
        try:
            types.append(int(NodeType[part.upper()]))
        except KeyError as exc:
            known = ", ".join(node_type.name for node_type in NodeType)
            raise BotConfigError(
                f"{part!r} is not a node type; the known ones are {known}"
            ) from exc
    return types


def _node_type_name(value: object) -> str:
    if not isinstance(value, int):
        return "unknown"
    try:
        return NodeType(value).name
    except ValueError:
        return f"type_{value}"
