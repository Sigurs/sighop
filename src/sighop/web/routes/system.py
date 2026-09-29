"""The station as a whole: the radio, the schema, the gate controls, and collection.

One page for the facts that are about the run rather than about any identity,
room or channel in it: the board's readback, the schema revision this build
agrees with or does not, what is deliberately left to a terminal, and the two
station-wide guarded actions. Those two are linked, not performed: following a
link opens the existing confirmation view, which mints a nonce and changes
nothing (`web-admin`).

The forms here are the repeater collection settings (repeater-metrics D10),
validated strictly like the room delivery form: junk is refused with the reason
and nothing is stored, never read as blank. The collector reads the stored row
at every tick, so a saved change needs no reconcile call. The preferred first
hop (preferred-first-hop D5) is held on the live path store instead, so a save
sets it there too — only once the row is written, so memory and the database
never disagree.

The packet archive's section (packet-archive D8) shows what the archive holds,
takes its retention bound under the same strict validation, and exports a range
as a capture file. The maintainer re-reads the bound on every pass, so a saved
change needs no restart either. The export is behind the same sign-in as every
page here; the request guard is what enforces that, not this module.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from sighop.db.archive import export_lines
from sighop.db.engine import Succeeded
from sighop.db.models import ARCHIVE_RETENTION_DAYS_RANGE
from sighop.db.repositories import (
    COLLECTION_INTERVAL_RANGE,
    COLLECTION_RECENT_DAYS_RANGE,
    COLLECTION_RETENTION_DAYS_RANGE,
    DEFAULT_COLLECTION_RETENTION_DAYS,
    ArchiveReadError,
    ArchiveSettingsError,
    CollectionSettings,
    CollectionSettingsError,
    RoutePreferenceError,
    UnknownIdentityError,
)
from sighop.net.collect import is_eligible
from sighop.net.paths import LearnedPath, PathKey
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.protocol.payloads import NodeType
from sighop.web.deps import Panel, panel
from sighop.web.render import DEGRADED, identity_for, identity_for_key, modem_readings
from sighop.web.routes.admin import (
    ACCOUNTS_ARE_NOT_MANAGED,
    MIGRATIONS_ARE_APPLIED_AT_START,
)

PanelDep = Annotated[Panel, Depends(panel)]

router = APIRouter(prefix="/system")


SEE_OTHER = 303

DEFAULT_ARCHIVE_RETENTION_DAYS = 365
"""What the days field shows while the archive is kept forever, so unticking
keep forever yields a sane bound rather than an empty field."""


@router.get("", response_class=HTMLResponse)
async def index(request: Request, page: PanelDep) -> HTMLResponse:
    return await _system_page(request, page)


@router.post("/collection", response_model=None)
async def set_collection(
    request: Request,
    page: PanelDep,
    enabled: Annotated[str, Form()] = "",
    entity_id: Annotated[str, Form()] = "",
    interval_minutes: Annotated[str, Form()] = "",
    recent_days: Annotated[str, Form()] = "",
    retention_days: Annotated[str, Form()] = "",
    retention_forever: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    """Store the collection settings, or refuse them whole with the reason.

    Keep forever is its own checkbox; a blank days field is refused, never
    read as forever (repeater-metrics-history D2)."""
    forever = retention_forever == "true"
    form = {
        "enabled": enabled == "true",
        "entity_id": entity_id,
        "interval_minutes": interval_minutes,
        "recent_days": recent_days,
        "retention_days": retention_days,
        "retention_forever": forever,
    }
    try:
        identity = uuid.UUID(entity_id) if entity_id.strip() else None
    except ValueError:
        return await _refuse(request, page, "that identity is not one of the stored ones", form)
    try:
        saved = await page.persistence.repeater_collection.save(
            enabled=enabled == "true",
            entity_id=identity,
            interval_minutes=_whole_number(
                interval_minutes, COLLECTION_INTERVAL_RANGE, "the interval", "minutes"
            ),
            recent_days=_whole_number(
                recent_days, COLLECTION_RECENT_DAYS_RANGE, "the recency window", "days"
            ),
            retention_days=None
            if forever
            else _whole_number(
                retention_days, COLLECTION_RETENTION_DAYS_RANGE, "the retention window", "days"
            ),
        )
    except (CollectionSettingsError, UnknownIdentityError) as exc:
        return await _refuse(request, page, str(exc), form)
    if not isinstance(saved, Succeeded):
        return await _refuse(
            request, page, "the settings could not be stored; nothing was changed", form, 503
        )
    return RedirectResponse("/system", status_code=SEE_OTHER)


@router.post("/archive", response_model=None)
async def set_archive_retention(
    request: Request,
    page: PanelDep,
    retention_days: Annotated[str, Form()] = "",
    retention_forever: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    """Store the archive's retention bound, or refuse it with the reason.

    Keep forever is its own checkbox, as on the collection form: a blank days
    field is refused, never read as forever."""
    forever = retention_forever == "true"
    form = {"retention_days": retention_days, "retention_forever": forever}
    try:
        days = (
            None
            if forever
            else _whole_number(
                retention_days,
                ARCHIVE_RETENTION_DAYS_RANGE,
                "the archive retention",
                "days",
                error=ArchiveSettingsError,
            )
        )
        saved = await page.persistence.packet_archive.set_retention(days)
    except ArchiveSettingsError as exc:
        return await _refuse_archive(request, page, str(exc), form)
    if not isinstance(saved, Succeeded):
        return await _refuse_archive(
            request, page, "the setting could not be stored; nothing was changed", form, 503
        )
    return RedirectResponse("/system#archive", status_code=SEE_OTHER)


@router.get("/archive/export", response_model=None)
async def export_archive(
    request: Request, page: PanelDep, since: str = "", until: str = ""
) -> HTMLResponse | StreamingResponse:
    """A range of the archive as a capture-format JSONL download.

    `since` is inclusive and `until` exclusive, each a date or an ISO time; one
    without an offset is UTC. A range that is empty or inverted is refused with
    the reason rather than answered with a header and nothing else. With no
    range at all this is somebody arriving at the address, not asking for a
    file, so they get the page whose form asks for one.
    """
    if not since.strip() and not until.strip():
        return await _system_page(request, page)
    try:
        start, end = _instant(since, "the start"), _instant(until, "the end")
    except ValueError as exc:
        return await _refuse_export(request, page, str(exc))
    if end <= start:
        return await _refuse_export(request, page, "the end of the range must be after its start")

    async def lines() -> AsyncIterator[str]:
        try:
            async for line in export_lines(page.persistence.packet_archive, start, end):
                yield line
        except ArchiveReadError as exc:
            # The response has started; all that is left is to stop after the
            # last whole line, which replay reads as a file cut short.
            page.logger.error(
                "archive_export_incomplete",
                outcome="error",
                since=start.isoformat(),
                until=end.isoformat(),
                error=str(exc),
            )

    name = f"sighop-archive-{_stamp(start)}-{_stamp(end)}.jsonl"
    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={
            "content-disposition": f'attachment; filename="{name}"',
            "cache-control": "no-store",
        },
    )


def _instant(value: str, what: str) -> dt.datetime:
    text = value.strip()
    if not text:
        raise ValueError(f"{what} of the range is required, as a date or an ISO time")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{what} of the range, {text!r}, is not a date or an ISO time") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _stamp(at: dt.datetime) -> str:
    return at.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


async def _refuse_archive(
    request: Request,
    page: Panel,
    reason: str,
    form: dict[str, object],
    status_code: int = 400,
) -> HTMLResponse:
    if "nothing was changed" not in reason:
        reason = f"{reason}; nothing was changed"
    return await _system_page(
        request, page, archive_error=reason, archive_form=form, status_code=status_code
    )


async def _refuse_export(request: Request, page: Panel, reason: str) -> HTMLResponse:
    return await _system_page(request, page, export_error=reason, status_code=400)


@router.post("/route-preference", response_model=None)
async def set_route_preference(
    request: Request,
    page: PanelDep,
    preferred_first_hop: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    """Set or clear the preferred first hop, or refuse with the reason.

    Only a known repeater contact is accepted. The live path store is changed
    only after the row is written, so a failed write leaves both as they were.
    """
    chosen = preferred_first_hop.strip().lower()
    key: bytes | None = None
    if chosen:
        try:
            key = bytes.fromhex(chosen)
        except ValueError:
            key = None
        contact = None if key is None else page.state.contacts.get(key)
        if contact is None or contact.node_type != NodeType.REPEATER:
            return await _refuse_route(
                request, page, f"{chosen[:16]!r} is not the public key of a known repeater"
            )
    try:
        saved = await page.persistence.route_preference.save(key)
    except RoutePreferenceError as exc:
        return await _refuse_route(request, page, str(exc))
    if not isinstance(saved, Succeeded):
        return await _refuse_route(
            request, page, "the setting could not be stored; nothing was changed", 503
        )
    page.state.pipeline.paths.preferred_first_hop = key
    return RedirectResponse("/system#route-preference", status_code=SEE_OTHER)


async def _refuse_route(
    request: Request, page: Panel, reason: str, status_code: int = 400
) -> HTMLResponse:
    if "nothing was changed" not in reason:
        reason = f"{reason}; nothing was changed"
    return await _system_page(request, page, route_error=reason, status_code=status_code)


def _whole_number(
    value: str,
    bounds: tuple[int, int],
    what: str,
    unit: str,
    *,
    error: type[ValueError] = CollectionSettingsError,
) -> int:
    low, high = bounds
    try:
        return int(value.strip())
    except ValueError:
        raise error(
            f"{what} must be a whole number of {unit} from {low} to {high}; "
            f"{value!r} is not one, and nothing was changed"
        ) from None


async def _refuse(
    request: Request,
    page: Panel,
    reason: str,
    form: dict[str, object],
    status_code: int = 400,
) -> HTMLResponse:
    if "nothing was changed" not in reason:
        reason = f"{reason}; nothing was changed"
    return await _system_page(request, page, error=reason, form=form, status_code=status_code)


async def _system_page(
    request: Request,
    page: Panel,
    *,
    error: str = "",
    form: dict[str, object] | None = None,
    route_error: str = "",
    archive_error: str = "",
    archive_form: dict[str, object] | None = None,
    export_error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """The readback once, the revision agreement, the station controls, and collection.

    The revision is the first question a degraded panel raises: is this
    database the one this build knows how to talk to? Everything durable on
    every page is wrong-by-omission if it is not.
    """
    from sighop.db import migrations

    expected = migrations.expected_revision()
    applied: str | None = None
    unavailable = ""
    try:
        applied = await page.persistence.database.read_applied_revision()
    except Exception as exc:
        # Classified by the engine; shown as the degraded state it is.
        unavailable = f"{DEGRADED} ({exc})"
    scheduler = page.state.scheduler
    return page.page(
        request,
        "system.html",
        readings=modem_readings(page.state.probe_result, page.state.radio),
        probe=page.state.probe_result,
        applied=applied,
        expected=expected,
        agree=applied == expected and not unavailable,
        unavailable=unavailable,
        reconcile=migrations.RESTART_TO_MIGRATE,
        migrations_note=MIGRATIONS_ARE_APPLIED_AT_START,
        accounts_note=ACCOUNTS_ARE_NOT_MANAGED,
        transmit_enabled=scheduler.transmit_enabled,
        ceiling_fraction=scheduler.budget.ceiling_fraction,
        regulatory_default=DEFAULT_CEILING_FRACTION,
        collection=await _collection_view(page, form),
        collection_error=error,
        route=await _route_preference_view(page),
        route_error=route_error,
        archive=await _archive_view(page, archive_form),
        archive_error=archive_error,
        export_error=export_error,
        status_code=status_code,
    )


async def _route_preference_view(page: Panel) -> dict[str, object]:
    """The preferred-first-hop section: the repeaters to choose from, the one in
    force, and whether this node has heard it directly — the setting's premise."""
    paths = page.state.pipeline.paths
    contacts = page.state.contacts
    repeaters = sorted(
        (
            (contact.public_key.hex(), f"{contact.display_name} ({contact.public_key.hex()[:6]})")
            for contact in contacts.contacts()
            if contact.node_type == NodeType.REPEATER
        ),
        key=lambda option: option[1].lower(),
    )
    stored = await page.persistence.route_preference.get()
    in_force = paths.preferred_first_hop
    identity = None
    known = False
    heard: LearnedPath | None = None
    if in_force is not None:
        contact = contacts.get(in_force)
        known = contact is not None
        identity = identity_for(contact) if contact else identity_for_key(in_force, contacts)
        zero_hop = [c for c in paths.candidates(PathKey.for_public_key(in_force)) if c.is_zero_hop]
        heard = zero_hop[-1] if zero_hop else None
    return {
        "readable": isinstance(stored, Succeeded),
        "repeaters": repeaters,
        "selected": "" if in_force is None else in_force.hex(),
        "in_force": in_force,
        "identity": identity,
        "known": known,
        "heard": heard,
        "prepend_overflow": paths.prepend_overflow,
    }


async def _collection_view(page: Panel, form: dict[str, object] | None) -> dict[str, object]:
    """What the collection section draws: the stored settings (or the refused
    form, re-shown as typed), the identities it may use, and the counts."""
    stored = await page.persistence.repeater_collection.get()
    settings = stored.value if isinstance(stored, Succeeded) else None
    shown = settings or CollectionSettings()

    identities: list[tuple[str, str]] = []
    listed = await page.persistence.entities.list_all()
    rooms = await page.persistence.rooms.list_all()
    serving = {room.entity_id for room in rooms.value} if isinstance(rooms, Succeeded) else set()
    if isinstance(listed, Succeeded):
        identities = [
            (str(record.id), record.name) for record in listed.value if record.id not in serving
        ]

    selected: frozenset[bytes] = frozenset()
    targets = await page.persistence.repeater_targets.list_keys()
    if isinstance(targets, Succeeded):
        selected = targets.value
    now = dt.datetime.now(dt.UTC)
    in_window = sum(
        is_eligible(
            page.state.contacts.get(key),
            selected=selected,
            recent_days=shown.recent_days,
            now=now,
        )
        for key in selected
    )
    values = form or {
        "enabled": shown.enabled,
        "entity_id": "" if shown.entity_id is None else str(shown.entity_id),
        "interval_minutes": str(shown.interval_minutes),
        "recent_days": str(shown.recent_days),
        # Forever shows the default days, so unticking it yields a sane value.
        "retention_days": str(
            DEFAULT_COLLECTION_RETENTION_DAYS
            if shown.retention_days is None
            else shown.retention_days
        ),
        "retention_forever": shown.retention_days is None,
    }
    return {
        "readable": settings is not None,
        "settings": shown,
        "form": values,
        "identities": identities,
        "identity_missing": shown.enabled and shown.entity_id is None,
        "selected": len(selected),
        "in_window": in_window,
        "interval_range": COLLECTION_INTERVAL_RANGE,
        "recent_range": COLLECTION_RECENT_DAYS_RANGE,
        "retention_range": COLLECTION_RETENTION_DAYS_RANGE,
    }


async def _archive_view(page: Panel, form: dict[str, object] | None) -> dict[str, object]:
    """What the archive section draws: the summary, this run's counters, the
    retention bound (or the refused form, as typed), and a default export range.

    A summary or setting that cannot be read is shown as unreadable rather than
    as an empty archive: the two look alike and mean opposite things."""
    persistence = page.persistence
    summary = await persistence.packet_archive.summary()
    retention = await persistence.packet_archive.get_retention()
    stored = retention.value if isinstance(retention, Succeeded) else None
    values = form or {
        "retention_days": str(DEFAULT_ARCHIVE_RETENTION_DAYS if stored is None else stored),
        "retention_forever": stored is None,
    }
    today = dt.datetime.now(dt.UTC).date()
    return {
        "summary": summary.value if isinstance(summary, Succeeded) else None,
        "retention_readable": isinstance(retention, Succeeded),
        "retention_days": stored,
        "written": persistence.archive_writer.written,
        "discarded": persistence.archive_writer.discarded,
        "form": values,
        "retention_range": ARCHIVE_RETENTION_DAYS_RANGE,
        "export_since": (today - dt.timedelta(days=1)).isoformat(),
        "export_until": (today + dt.timedelta(days=1)).isoformat(),
    }
