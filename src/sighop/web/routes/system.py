"""The station as a whole: the radio, the schema, the gate controls, and collection.

One page for the facts that are about the run rather than about any identity,
room or channel in it: the board's readback, the schema revision this build
agrees with or does not, what is deliberately left to a terminal, and the two
station-wide guarded actions. Those two are linked, not performed: following a
link opens the existing confirmation view, which mints a nonce and changes
nothing (`web-admin`).

The one form here is the repeater collection settings (repeater-metrics D10),
validated strictly like the room delivery form: junk is refused with the reason
and nothing is stored, never read as blank. The collector reads the stored row
at every tick, so a saved change needs no reconcile call.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.db.engine import Succeeded
from sighop.db.repositories import (
    COLLECTION_INTERVAL_RANGE,
    COLLECTION_RECENT_DAYS_RANGE,
    COLLECTION_RETENTION_DAYS_RANGE,
    CollectionSettings,
    CollectionSettingsError,
    UnknownIdentityError,
)
from sighop.net.collect import is_eligible
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.web.deps import Panel, panel
from sighop.web.render import DEGRADED, modem_readings
from sighop.web.routes.admin import (
    ACCOUNTS_ARE_NOT_MANAGED,
    MIGRATIONS_ARE_APPLIED_AT_START,
)

PanelDep = Annotated[Panel, Depends(panel)]

router = APIRouter(prefix="/system")


SEE_OTHER = 303


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
) -> HTMLResponse | RedirectResponse:
    """Store the collection settings, or refuse them whole with the reason."""
    form = {
        "enabled": enabled == "true",
        "entity_id": entity_id,
        "interval_minutes": interval_minutes,
        "recent_days": recent_days,
        "retention_days": retention_days,
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
            retention_days=_whole_number(
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


def _whole_number(value: str, bounds: tuple[int, int], what: str, unit: str) -> int:
    low, high = bounds
    try:
        return int(value.strip())
    except ValueError:
        raise CollectionSettingsError(
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
        status_code=status_code,
    )


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
        "retention_days": str(shown.retention_days),
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
