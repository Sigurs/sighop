"""Repeater collection on the contact table, and each repeater's metrics page.

The contact table gains a "collect metrics" checkbox on repeater rows only,
posted by htmx and answered with the re-rendered cell, plus the last poll's
time and outcome (repeater-metrics D10). The metrics page shows what the last
answered poll returned — counters exactly as the repeater reported them, a
field it did not return as absent rather than zero — its neighbours, and the
poll history.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse

from sighop.db.engine import Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    DEFAULT_POLL_HISTORY,
    CollectionSettings,
    PollRecord,
)
from sighop.net.collect import is_eligible
from sighop.net.contacts import Contact, ContactStore
from sighop.protocol.payloads import NeighbourEntry, NodeType, RepeaterStats
from sighop.web import battery, charts
from sighop.web.deps import Panel, panel
from sighop.web.render import IdentityView, duration, identity_for

PanelDep = Annotated[Panel, Depends(panel)]

router = APIRouter(prefix="/contacts")

NOT_REPORTED = "not reported"
NO_BATTERY_SENSED = "no battery sensed"


@dataclass(frozen=True, slots=True)
class ChartRange:
    key: str
    label: str
    span: dt.timedelta | None
    """None is everything kept."""


RANGES = (
    ChartRange("24h", "last 24 hours", dt.timedelta(hours=24)),
    ChartRange("7d", "last 7 days", dt.timedelta(days=7)),
    ChartRange("30d", "last 30 days", dt.timedelta(days=30)),
    ChartRange("all", "everything kept", None),
)
DEFAULT_RANGE = "7d"


@dataclass(frozen=True, slots=True)
class BatteryMark:
    """A low battery reading, from the latest poll that returned a status."""

    milli_volts: int
    percent: int
    collected_at: dt.datetime


@dataclass(frozen=True, slots=True)
class CollectCell:
    """The contact table's collection cell for one repeater row."""

    key_hex: str
    selected: bool
    last_poll: PollRecord | None = None
    skipped_days: int | None = None
    """The recency window this selected repeater is outside of, or None."""

    unavailable: bool = False
    """The selection could not be read, so the checkbox is not offered."""

    battery: BatteryMark | None = None
    """Set only when the latest recorded status estimates under 50%."""


async def collection_cells(
    persistence: Persistence, contacts: ContactStore, *, now: dt.datetime | None = None
) -> dict[str, CollectCell]:
    """One cell per repeater contact, keyed by key hex. Four reads in all.

    The battery mark reads every repeater's latest status, selected or not: a
    deselected repeater's last reading is still the best knowledge (D5)."""
    now = now or dt.datetime.now(dt.UTC)
    repeaters = [c for c in contacts.contacts() if c.node_type == NodeType.REPEATER]
    if not repeaters:
        return {}
    targets = await persistence.repeater_targets.list_keys()
    if not isinstance(targets, Succeeded):
        return {
            c.public_key.hex(): CollectCell(
                key_hex=c.public_key.hex(), selected=False, unavailable=True
            )
            for c in repeaters
        }
    selected = targets.value
    settings = await _settings(persistence)
    latest = await persistence.repeater_polls.latest_for(
        [c.public_key for c in repeaters if c.public_key in selected]
    )
    polls = latest.value if isinstance(latest, Succeeded) else {}
    statuses = await persistence.repeater_polls.latest_status_for([c.public_key for c in repeaters])
    marks = _battery_marks(statuses.value) if isinstance(statuses, Succeeded) else {}
    return {
        contact.public_key.hex(): _cell(
            contact, selected, polls, settings, now, marks.get(contact.public_key)
        )
        for contact in repeaters
    }


def _battery_marks(statuses: dict[bytes, PollRecord]) -> dict[bytes, BatteryMark]:
    marks = {}
    for key, poll in statuses.items():
        mv = None if poll.stats is None else poll.stats.batt_milli_volts
        percent = battery.estimate(mv)
        if mv is not None and percent is not None and battery.is_low(mv):
            marks[key] = BatteryMark(milli_volts=mv, percent=percent, collected_at=poll.started_at)
    return marks


def _cell(
    contact: Contact,
    selected: frozenset[bytes],
    polls: dict[bytes, PollRecord],
    settings: CollectionSettings,
    now: dt.datetime,
    mark: BatteryMark | None = None,
) -> CollectCell:
    chosen = contact.public_key in selected
    skipped = (
        settings.recent_days
        if chosen
        and not is_eligible(contact, selected=selected, recent_days=settings.recent_days, now=now)
        else None
    )
    return CollectCell(
        key_hex=contact.public_key.hex(),
        selected=chosen,
        last_poll=polls.get(contact.public_key),
        skipped_days=skipped,
        battery=mark,
    )


async def _settings(persistence: Persistence) -> CollectionSettings:
    read = await persistence.repeater_collection.get()
    return read.value if isinstance(read, Succeeded) else CollectionSettings()


def _contact(page: Panel, key_hex: str) -> Contact | None:
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        return None
    return page.state.contacts.get(key)


@router.post("/{key_hex}/collect", response_class=HTMLResponse)
async def set_collect(
    key_hex: str,
    request: Request,
    page: PanelDep,
    collect: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Select or deselect a repeater; answers with its re-rendered cell.

    A checkbox posts its value only when ticked, so an absent field is "no".
    """
    contact = _contact(page, key_hex)
    if contact is None:
        return HTMLResponse("no contact with that key is known", status_code=404)
    if contact.node_type != NodeType.REPEATER:
        return HTMLResponse(
            "only a repeater can be selected for collection; this contact is not one",
            status_code=400,
        )
    targets = page.persistence.repeater_targets
    if collect == "true":
        outcome = await targets.select(contact.public_key, at=dt.datetime.now(dt.UTC))
    else:
        outcome = await targets.deselect(contact.public_key)
    if not isinstance(outcome, Succeeded):
        return HTMLResponse("the selection could not be stored; nothing changed", status_code=503)
    cells = await collection_cells(page.persistence, page.state.contacts)
    return page.page(request, "_collect_cell.html", cell=cells[contact.public_key.hex()])


@dataclass(frozen=True, slots=True)
class StatReading:
    label: str
    text: str
    absent: bool = False


@dataclass(frozen=True, slots=True)
class NeighbourView:
    prefix_hex: str
    identity: IdentityView | None
    ambiguous: bool
    snr_db: float
    heard_ago: str


def stat_readings(stats: RepeaterStats) -> list[StatReading]:
    """Every field, as reported. Counters are counts, not rates."""
    rows: list[tuple[str, int | None, Callable[[int], str]]] = [
        ("battery", stats.batt_milli_volts, _battery_text),
        ("transmit queue", stats.curr_tx_queue_len, str),
        ("noise floor", stats.noise_floor, lambda v: f"{v} dBm"),
        ("last RSSI", stats.last_rssi, lambda v: f"{v} dBm"),
        ("last SNR", stats.last_snr, lambda v: f"{v / 4:+.2f} dB"),
        ("packets received", stats.n_packets_recv, str),
        ("packets sent", stats.n_packets_sent, str),
        ("flood received", stats.n_recv_flood, str),
        ("flood sent", stats.n_sent_flood, str),
        ("direct received", stats.n_recv_direct, str),
        ("direct sent", stats.n_sent_direct, str),
        ("flood duplicates", stats.n_flood_dups, str),
        ("direct duplicates", stats.n_direct_dups, str),
        ("transmit airtime", stats.total_air_time_secs, duration),
        ("receive airtime", stats.total_rx_air_time_secs, duration),
        ("uptime", stats.total_up_time_secs, duration),
        ("error flags", stats.err_events, lambda v: f"0x{v:04x}"),
        ("receive errors", stats.n_recv_errors, str),
    ]
    return [
        StatReading(label=label, text=NOT_REPORTED, absent=True)
        if value is None
        else StatReading(label=label, text=render(value))
        for label, value, render in rows
    ]


def _battery_text(mv: int) -> str:
    percent = battery.estimate(mv)
    estimate = NO_BATTERY_SENSED if percent is None else f"~{percent}%"
    return f"{mv / 1000:.3f} V · {estimate}"


def neighbour_views(
    entries: tuple[NeighbourEntry, ...], contacts: ContactStore
) -> list[NeighbourView]:
    """A prefix is a contact only when exactly one contact's key starts with it."""
    views = []
    for entry in entries:
        matches = contacts.by_prefix(entry.prefix)
        only = next(iter(matches)) if len(matches) == 1 else None
        views.append(
            NeighbourView(
                prefix_hex=entry.prefix.hex(),
                identity=None if only is None else identity_for(only),
                ambiguous=len(matches) > 1,
                snr_db=entry.snr_db,
                heard_ago=duration(entry.heard_seconds_ago),
            )
        )
    return views


@router.get("/{key_hex}/metrics", response_class=HTMLResponse)
async def metrics(
    key_hex: str,
    request: Request,
    page: PanelDep,
    range_key: Annotated[str, Query(alias="range")] = DEFAULT_RANGE,
) -> HTMLResponse:
    """Latest status, charts over the chosen range (D8), neighbours, history.

    An unknown range reads as the default rather than being refused: it is a
    view choice, and nothing is stored."""
    contact = _contact(page, key_hex)
    if contact is None or contact.node_type != NodeType.REPEATER:
        return page.page(request, "metrics.html", contact=None, status_code=404)
    persistence = page.persistence
    settings = await _settings(persistence)
    targets = await persistence.repeater_targets.list_keys()
    selected = isinstance(targets, Succeeded) and contact.public_key in targets.value
    latest = await persistence.repeater_polls.latest_with_status(contact.public_key)
    history = await persistence.repeater_polls.history(
        contact.public_key, limit=DEFAULT_POLL_HISTORY
    )
    unavailable = not (isinstance(latest, Succeeded) and isinstance(history, Succeeded))
    status_poll = latest.value if isinstance(latest, Succeeded) else None
    polls = history.value if isinstance(history, Succeeded) else []
    chosen = next((r for r in RANGES if r.key == range_key), None)
    chosen = chosen or next(r for r in RANGES if r.key == DEFAULT_RANGE)
    now = dt.datetime.now(dt.UTC)
    since = None if chosen.span is None else now - chosen.span
    series = await persistence.repeater_polls.series(contact.public_key, since=since)
    rows = series.value if isinstance(series, Succeeded) else []
    unavailable = unavailable or not isinstance(series, Succeeded)
    start = since or (rows[0].started_at if rows else now)
    answered = any(charts.has_status(row) for row in rows)
    failed: dict[str, int] = {}
    for _at, outcome in charts.failures(rows):
        failed[outcome.label] = failed.get(outcome.label, 0) + 1
    next_due: dt.datetime | None = None
    if settings.enabled and settings.last_cycle_started_at is not None:
        next_due = settings.last_cycle_started_at + dt.timedelta(minutes=settings.interval_minutes)
    return page.page(
        request,
        "metrics.html",
        contact=contact,
        identity=identity_for(contact),
        selected=selected,
        settings=settings,
        next_due=next_due,
        unavailable=unavailable,
        status_poll=status_poll,
        readings=[]
        if status_poll is None or status_poll.stats is None
        else stat_readings(status_poll.stats),
        neighbours=[]
        if status_poll is None
        else neighbour_views(status_poll.neighbours, page.state.contacts),
        history=polls,
        history_cap=DEFAULT_POLL_HISTORY,
        ranges=RANGES,
        chosen=chosen,
        wider=RANGES[RANGES.index(chosen) + 1 :],
        range_start=start,
        range_end=now,
        charts=charts.charts(rows, start=start, end=now) if answered else [],
        failed_counts=sorted(failed.items()),
        max_points=charts.MAX_CHART_POINTS,
    )


@router.get("/{key_hex}/metrics/polls/{poll_id}", response_class=HTMLResponse)
async def metrics_poll(
    key_hex: str, poll_id: str, request: Request, page: PanelDep
) -> HTMLResponse:
    """One recorded poll as it was returned then (repeater-metrics-history D9).

    Not found when the poll is missing, pruned, or another repeater's."""
    contact = _contact(page, key_hex)
    poll: PollRecord | None = None
    unavailable = False
    if contact is not None and contact.node_type == NodeType.REPEATER and poll_id.isdigit():
        read = await page.persistence.repeater_polls.get(int(poll_id))
        unavailable = not isinstance(read, Succeeded)
        poll = read.value if isinstance(read, Succeeded) else None
    if poll is None or contact is None or poll.public_key != contact.public_key:
        return page.page(
            request,
            "metrics_poll.html",
            contact=contact,
            poll=None,
            unavailable=unavailable,
            status_code=503 if unavailable else 404,
        )
    return page.page(
        request,
        "metrics_poll.html",
        contact=contact,
        identity=identity_for(contact),
        poll=poll,
        login_name=await _identity_name(page.persistence, poll),
        unavailable=False,
        readings=[] if poll.stats is None else stat_readings(poll.stats),
        neighbours=neighbour_views(poll.neighbours, page.state.contacts),
    )


async def _identity_name(persistence: Persistence, poll: PollRecord) -> str | None:
    """The login identity's name, or None when it was removed since."""
    if poll.entity_id is None:
        return None
    listed = await persistence.entities.list_all()
    if not isinstance(listed, Succeeded):
        return None
    return next((e.name for e in listed.value if e.id == poll.entity_id), None)
