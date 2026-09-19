"""View models for the panel's pages (design D12, D13).

`monitor/render.py` turns a typed event into a line for a terminal. This turns
the same kind of typed value into the handful of fields a template draws, and it
exists for the reason that module exists: a decision about *what a thing means*
belongs in Python, where it can be tested, rather than in a Jinja expression,
where it can only be looked at.

Two of those decisions are load-bearing:

* **The duty-cycle meter** (§8's "single most important element on the screen").
  It reports consumed airtime against the ceiling in force, says when that
  ceiling is above the regulatory default, and says when the gate is shut — in
  which case the number it shows is what *would* have been charged.
* **Verification** (§8's hard rule). One function decides whether an identity is
  verified, unverified or key-only, and one macro draws it, so the rule cannot
  become ninety per cent true by a view forgetting to apply it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from sighop.net.adverts import EntityStub
from sighop.net.contacts import Contact, ContactStore
from sighop.net.paths import PathStore
from sighop.net.tx import DEFAULT_CEILING_FRACTION, SchedulerStatus
from sighop.web.feed import EntityTraffic, FeedHub
from sighop.web.state import PanelState

VERIFIED_MARK = "✓"
UNVERIFIED_MARK = "✗"
KEY_ONLY_MARK = "?"
"""The same glyphs `monitor/render.py` uses, so a screenshot of the panel and a
line in the terminal say the same thing the same way. A glyph *and* a word,
never colour alone: the marking has to survive a screenshot, a colourblind
operator and a monochrome theme (design D13)."""

REGULATORY_NOTE = "the 10% default on EU 868 is a regulatory limit, not a tuning knob"


# --- The duty-cycle meter ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class MeterView:
    """What the meter draws, on every page (design D12)."""

    used_ms: float
    ceiling_ms: float
    used_pct: float
    remaining_pct: float
    ceiling_fraction: float
    above_regulatory_default: bool
    reserve_reached: bool
    transmit_enabled: bool

    @property
    def used_seconds(self) -> float:
        return self.used_ms / 1000.0

    @property
    def ceiling_seconds(self) -> float:
        return self.ceiling_ms / 1000.0

    @property
    def bar_pct(self) -> float:
        """The filled width, clamped: a meter cannot be more than full."""
        return max(0.0, min(100.0, self.used_pct))

    @property
    def level(self) -> str:
        """`ok` | `reserve` | `full`, for the one place colour is allowed.

        Never the only signal: `state_text` says the same thing in words.
        """
        if self.used_pct >= 100.0:
            return "full"
        if self.reserve_reached:
            return "reserve"
        return "ok"

    @property
    def state_text(self) -> str:
        match self.level:
            case "full":
                return "ceiling reached — nothing further will be admitted"
            case "reserve":
                return "reserve reached — only acknowledgements and responses"
            case _:
                return "within budget"

    @property
    def gate_text(self) -> str:
        """What the gate indicator says, in full.

        A closed gate is not "0% used": packets are still composed, scheduled
        and charged against the budget, and then dropped at the hand-off to the
        modem. The number beside this is what *would* have gone on the air.
        """
        if self.transmit_enabled:
            return "transmit enabled — this node is transmitting"
        return (
            "transmit disabled — nothing will be transmitted; the meter shows "
            "what would have been charged"
        )

    @property
    def ceiling_text(self) -> str:
        """What the ceiling in force is, and whether it is above the default."""
        percent = self.ceiling_fraction * 100.0
        if self.above_regulatory_default:
            return (
                f"ceiling raised to {percent:g}% of the hour, above the "
                f"{DEFAULT_CEILING_FRACTION * 100:g}% regulatory default"
            )
        return f"ceiling {percent:g}% of the hour"


def meter_for(status: SchedulerStatus) -> MeterView:
    """The meter, from the scheduler's own status. No second source of truth."""
    return MeterView(
        used_ms=status.duty_cycle_used_ms,
        ceiling_ms=status.duty_cycle_ceiling_ms,
        used_pct=status.duty_cycle_pct,
        remaining_pct=max(0.0, 100.0 - status.duty_cycle_pct),
        ceiling_fraction=(
            status.duty_cycle_ceiling_ms / (3600.0 * 1000.0)
            if status.duty_cycle_ceiling_ms
            else 0.0
        ),
        above_regulatory_default=status.above_regulatory_default,
        reserve_reached=status.reserve_reached,
        transmit_enabled=status.transmit_enabled,
    )


# --- Identity, and what the protocol does and does not authenticate ---------


@dataclass(frozen=True, slots=True)
class IdentityView:
    """One identity as every view draws it (design D13).

    `verification` is `verified`, `unverified` or `key_only`, and those are three
    different claims rather than two:

    * **verified** — an advert signed by this key was checked, so the name
      travelled with a signature over it.
    * **unverified** — we hold a name for this key that no signature covered: an
      operator pasted the contact in, or the advert did not verify.
    * **key_only** — we hold no contact at all. There is a key and nothing else,
      and inventing a name for it would be inventing the thing §8 forbids.
    """

    public_key: bytes
    name: str | None
    verification: str

    @property
    def key_hex(self) -> str:
        return self.public_key.hex()

    @property
    def short_key(self) -> str:
        return self.public_key.hex()[:16]

    @property
    def node_hash(self) -> int:
        return self.public_key[0]

    @property
    def verified(self) -> bool:
        return self.verification == "verified"

    @property
    def mark(self) -> str:
        match self.verification:
            case "verified":
                return VERIFIED_MARK
            case "unverified":
                return UNVERIFIED_MARK
            case _:
                return KEY_ONLY_MARK

    @property
    def label(self) -> str:
        """The name, or the key when there is no name to show."""
        return self.name or self.short_key

    @property
    def marking_text(self) -> str:
        """What the marking means, said rather than implied (§8, `web-chat`)."""
        match self.verification:
            case "verified":
                return "advert signature verified"
            case "unverified":
                return "unverified: no signature covers this name"
            case _:
                return "key only: no contact is held for this key"


def identity_for(contact: Contact) -> IdentityView:
    """One contact, marked by whether an advert of theirs actually verified."""
    return IdentityView(
        public_key=contact.public_key,
        name=None if contact.name is None else contact.name.text,
        verification="verified" if contact.advert_verified else "unverified",
    )


def advert_id(stubs: Iterable[EntityStub], public_key: bytes | None) -> str | None:
    """The loaded identity a served room or running bot speaks as, by public key.

    By key rather than by name, as `routes/rooms.py` matches, so the advert
    links go to the identity that is actually on the air (design D6).
    """
    if public_key is None:
        return None
    for stub in stubs:
        if stub.identity.public_key == public_key:
            return stub.entity_id
    return None


def identity_for_key(public_key: bytes, contacts: ContactStore) -> IdentityView:
    """An identity known only by its key, resolved against what we hold.

    This is what a room author and a feed row go through. A key that matches no
    contact is `key_only` — not "unknown", which reads like a failure, and not a
    blank, which reads like nothing at all.
    """
    for contact in contacts.contacts():
        if contact.public_key == public_key:
            return identity_for(contact)
    return IdentityView(public_key=public_key, name=None, verification="key_only")


# --- Empty is not the same as unavailable -----------------------------------


@dataclass(frozen=True, slots=True)
class Collection[T]:
    """A durable collection, or the reason it could not be read (`web-server`).

    "No rooms" and "cannot read rooms" must never be the same screen. Making
    that a type rather than a convention is what stops a view from rendering an
    empty list because a query failed.
    """

    items: tuple[T, ...] = ()
    unavailable: str = ""
    """Why it could not be read. Empty when it could."""

    @property
    def readable(self) -> bool:
        return not self.unavailable

    @property
    def empty(self) -> bool:
        return self.readable and not self.items

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


def read[T](items: list[T] | tuple[T, ...]) -> Collection[T]:
    return Collection(items=tuple(items))


def unreadable[T](reason: str) -> Collection[T]:
    return Collection(unavailable=reason)


def collection_for[T](outcome: object | None, *, degraded: str) -> Collection[T]:
    """A repository read as something a template can tell apart from empty.

    `None` is "there is no database to read", a `Failed` is "there is one and it
    would not answer", and either is a different screen from a successful read
    that found nothing. One function so every durable view draws the same three
    states (`web-server`).
    """
    from sighop.db.engine import Failed

    if outcome is None:
        return unreadable(NO_DATABASE)
    if isinstance(outcome, Failed):
        return unreadable(f"{degraded}: {outcome.error}")
    return read(outcome.value)  # type: ignore[attr-defined]


NO_DATABASE = "no database is configured, so nothing durable is stored or read"
DEGRADED = "the database is unreachable; this cannot be read until it returns"


# --- A refused form still holds what was typed ------------------------------


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a submitted form was refused, and what the author had typed.

    The author of a refused form is *present*, which is the whole reason a
    refusal here is worth more than a truncation: they can be shown the reason,
    told which field it is about, and handed back what they wrote rather than an
    empty form to retype. A refusal that clears the form is a refusal that costs
    the operator their post.

    `reason` is the owning code's own message — a repository's, a driver's, a
    keystore's — never one this surface invented, so a refusal reads the same in
    the browser as on the command line.
    """

    reason: str
    field: str = ""
    """The field the reason is about, or empty when it is about the whole form."""

    submitted: Mapping[str, str] = dataclass_field(default_factory=dict)

    def value(self, name: str, default: str = "") -> str:
        """What was typed into one field, for re-rendering it."""
        return self.submitted.get(name, default)

    def concerns(self, name: str) -> bool:
        return bool(self.field) and self.field == name


def refused(reason: str, *, field: str = "", **submitted: str) -> Refusal:
    """One refusal, with the author's own values carried back to the form."""
    return Refusal(reason=reason, field=field, submitted=dict(submitted))


# --- Durability, on every page too ------------------------------------------


@dataclass(frozen=True, slots=True)
class PersistenceView:
    """What the panel says about durability, wherever it says it.

    Three states rather than two: no database at all, a database that is
    answering, and one that is degraded. The third is the one an operator has to
    be able to see without looking for it, because everything durable on every
    page is wrong-by-omission while it lasts.
    """

    configured: bool
    state: str
    degraded: bool
    discarded: int = 0
    refused: int = 0

    @property
    def text(self) -> str:
        if not self.configured:
            return "persistence off — nothing here survives the process"
        if self.degraded:
            return f"persistence degraded ({self.state}) — durable state cannot be read"
        return f"persistence {self.state}"

    @property
    def losses_text(self) -> str:
        """Writes that will never land, said plainly or not at all."""
        if not (self.discarded or self.refused):
            return ""
        return f"{self.discarded} write(s) discarded, {self.refused} refused"

    @property
    def level(self) -> str:
        if not self.configured:
            return "off"
        return "degraded" if self.degraded else "ok"


def persistence_view(persistence: object | None) -> PersistenceView:
    """The durability line, from whatever the run is holding.

    Typed loosely on purpose: `web/state.py` names `Persistence`, and this
    reaches for a handful of its counters through `as_json()` rather than
    growing a second interface to the same object.
    """
    if persistence is None:
        return PersistenceView(configured=False, state="off", degraded=False)
    status = persistence.as_json()  # type: ignore[attr-defined]
    discarded = sum(
        int(value)
        for key, value in status.items()
        if key.endswith("_discarded") and isinstance(value, int)
    )
    refused = sum(
        int(value)
        for key, value in status.items()
        if key.endswith("_refused") and isinstance(value, int)
    )
    return PersistenceView(
        configured=True,
        state=str(persistence.state),  # type: ignore[attr-defined]
        degraded=bool(persistence.degraded),  # type: ignore[attr-defined]
        discarded=discarded,
        refused=refused,
    )


# --- The dashboard's pages --------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteView:
    """One learned route to one contact, with what is uncertain about it said.

    An **empty path is a zero-hop direct route** — the most useful route a node
    can have — and is not the same as no route at all. `net/paths.py` already
    draws that distinction by returning `None` for the second; drawing it in
    the table is the display's half of the same rule.
    """

    path: str
    hop_count: int
    snr_db: float | None
    confirmed_at: dt.datetime
    ambiguous: bool

    @property
    def zero_hop(self) -> bool:
        return self.hop_count == 0 and not self.path

    @property
    def text(self) -> str:
        if self.zero_hop:
            return "direct, zero hops"
        return f"{self.hop_count} hop(s) via {self.path}"

    @property
    def caveat(self) -> str:
        """Why this route might not be the route it looks like.

        A route matched by node hash is a route to *a* node carrying that byte.
        §3 puts that at 1 in 256, so the table says "ambiguous" rather than
        showing a hash match in the same shape as a key match.
        """
        return (
            "matched by node hash, not by public key — a byte collides at 1 in 256"
            if self.ambiguous
            else ""
        )


@dataclass(frozen=True, slots=True)
class ContactView:
    """One row of the contact table (`web-dashboard`)."""

    identity: IdentityView
    node_type: str
    first_heard: dt.datetime | None
    last_heard: dt.datetime | None
    route: RouteView | None

    @property
    def routed(self) -> bool:
        return self.route is not None

    @property
    def route_text(self) -> str:
        return "no route known" if self.route is None else self.route.text


def contact_rows(contacts: ContactStore, paths: PathStore) -> list[ContactView]:
    """The contact table, joined to what is known about reaching each peer."""
    rows: list[ContactView] = []
    for contact in sorted(contacts.contacts(), key=lambda c: c.last_heard or _EPOCH, reverse=True):
        rows.append(
            ContactView(
                identity=identity_for(contact),
                node_type=_node_type(contact),
                first_heard=contact.first_heard,
                last_heard=contact.last_heard,
                route=_route_for(contact, paths),
            )
        )
    return rows


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _node_type(contact: Contact) -> str:
    if contact.node_type is None:
        return ""
    return getattr(contact.node_type, "name", None) or str(int(contact.node_type))


def _route_for(contact: Contact, paths: PathStore) -> RouteView | None:
    """The route the send path would actually choose, marked as it marks it.

    Deliberately the same order of preference `choose_route` uses — public key
    first, node hash second and ambiguous — so the table shows the route a
    message would take rather than a different one that also exists.
    """
    learned = paths.lookup_public_key(contact.public_key)
    ambiguous = False
    if learned is None:
        learned = paths.lookup_node_hash(contact.node_hash)
        ambiguous = learned is not None
    if learned is None:
        return None
    return RouteView(
        path=learned.path.hex(),
        hop_count=learned.hop_count,
        snr_db=learned.snr_db,
        confirmed_at=learned.confirmed_at,
        ambiguous=ambiguous,
    )


@dataclass(frozen=True, slots=True)
class Reading:
    """One value the board was asked for, present or absent (§4.1).

    A value the board did not answer is **absent**, never zero and never a
    default: the two are different facts about the hardware and a panel that
    conflated them would report a flat battery on a board that has no battery
    sensor.
    """

    label: str
    value: str
    absent: bool = False
    reason: str = ""

    @property
    def text(self) -> str:
        return f"absent ({self.reason})" if self.absent else self.value


def _radio_text(radio: object) -> str:
    """The parameters in force, in the units an operator reads them in."""
    return (
        f"{radio.freq_hz / 1e6:g} MHz  BW {radio.bw_hz / 1000:g} kHz  "  # type: ignore[attr-defined]
        f"SF{radio.sf}  CR 4/{radio.cr}"  # type: ignore[attr-defined]
    )


def _reading(label: str, probed: object, render: object = None) -> Reading:
    from sighop.radio.probe import Absent

    if isinstance(probed, Absent):
        return Reading(label=label, value="", absent=True, reason=str(probed.reason))
    shown = render(probed) if callable(render) else str(probed)
    return Reading(label=label, value=shown)


def modem_readings(probe: object | None, radio: object | None) -> list[Reading]:
    """The board's own readback, as the panel shows it.

    Built from the probe rather than from the configuration: `configured_radio`
    is what we asked for and `radio` is what the board said, and the two are
    kept apart because a disagreement is the interesting case (design D5/D6).
    """
    if probe is None:
        return []
    return [
        _reading("device", probe.device_name),  # type: ignore[attr-defined]
        _reading(
            "firmware",
            probe.firmware_version,  # type: ignore[attr-defined]
            lambda v: f"v{v.version} (reserved {v.reserved})",
        ),
        _reading("radio readback", probe.radio, _radio_text),  # type: ignore[attr-defined]
        _reading("tx power", probe.tx_power_dbm, lambda v: f"{v} dBm"),  # type: ignore[attr-defined]
        _reading("battery", probe.battery_mv, lambda v: f"{v} mV"),  # type: ignore[attr-defined]
        _reading(
            "mcu temperature",
            probe.mcu_temp_tenths_c,  # type: ignore[attr-defined]
            lambda v: f"{v / 10:.1f} °C",
        ),
    ]


@dataclass(frozen=True, slots=True)
class QueueView:
    """Queue depth for one priority class, named rather than numbered."""

    priority: int
    name: str
    depth: int


PRIORITY_NAMES = {0: "ack", 1: "response", 2: "message", 3: "advert"}


def queue_rows(status: SchedulerStatus) -> list[QueueView]:
    return [
        QueueView(
            priority=priority,
            name=PRIORITY_NAMES.get(priority, str(priority)),
            depth=status.queue_depths.get(priority, 0),
        )
        for priority in sorted(PRIORITY_NAMES)
    ]


# --- Guarded actions, as the run's output says them (milestone 9, 8.6) -------


def render_transmit_change(enabled: bool, *, actor: str) -> str:
    """One line for the terminal: what the gate now is, and who changed it."""
    state = "ENABLED — this node now transmits" if enabled else "disabled — receive only"
    return f"web: transmission {state} (by account {actor!r} from the web interface)"


def render_advert_request(
    kind: str, entity_name: str, *, actor: str, next_flood_at: dt.datetime | None
) -> str:
    """One line for the terminal: which advert, for whom, who asked, and the schedule.

    The next scheduled flood is on the line for both kinds, because it is what
    tells a zero-hop request (unchanged) from a flood request (moved) at a glance.
    """
    advert = "flood" if kind == "flood" else "zero-hop"
    when = "none" if next_flood_at is None else next_flood_at.isoformat()
    return (
        f"web: {advert} advert requested for {entity_name!r} "
        f"(by account {actor!r} from the web interface); next scheduled flood {when}"
    )


def render_ceiling_change(previous: float, ceiling: float, *, actor: str) -> str:
    """One line for the terminal: the old and new ceiling, and who changed it."""
    from sighop.net.tx import DEFAULT_CEILING_FRACTION

    note = " — ABOVE the 10% regulatory default" if ceiling > DEFAULT_CEILING_FRACTION else ""
    return (
        f"web: airtime ceiling {previous * 100:g}% -> {ceiling * 100:g}%{note} "
        f"(by account {actor!r} from the web interface)"
    )


def entity_rows(state: PanelState, feed: FeedHub | None) -> list[dict[str, object]]:
    """Per-entity TX/RX counters (§8), from the traffic the hub has seen.

    With no hub attached the counts are zero and the identities are still
    listed: "this run holds three identities and none has transmitted" is a
    different screen from "this run holds no identities".
    """
    traffic = feed.traffic if feed is not None else EntityTraffic()
    rows: list[dict[str, object]] = []
    for stub in state.adverts.stubs:
        counts = traffic.for_entity(stub.entity_id, stub.node_hash)
        rows.append(
            {
                "name": stub.name,
                "entity_id": stub.entity_id,
                "public_key": stub.identity.public_key.hex(),
                "node_hash": stub.node_hash,
                "node_type": stub.node_type.name,
                "persistent": stub.persistent,
                "adverts_sent": stub.adverts_sent,
                **counts,
            }
        )
    return rows
