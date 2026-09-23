"""The station as a whole: the radio, the schema, and the gate controls.

One read-only page for the facts that are about the run rather than about any
identity, room or channel in it: the board's readback, the schema revision this
build agrees with or does not, what is deliberately left to a terminal, and the
two station-wide guarded actions. Those two are linked, not performed: following
a link opens the existing confirmation view, which mints a nonce and changes
nothing (`web-admin`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.web.deps import Panel, panel
from sighop.web.render import DEGRADED, modem_readings
from sighop.web.routes.admin import (
    ACCOUNTS_ARE_NOT_MANAGED,
    MIGRATIONS_ARE_APPLIED_AT_START,
)

PanelDep = Annotated[Panel, Depends(panel)]

router = APIRouter(prefix="/system")


@router.get("", response_class=HTMLResponse)
async def index(request: Request, page: PanelDep) -> HTMLResponse:
    """The readback once, the revision agreement, and the station controls.

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
    )
