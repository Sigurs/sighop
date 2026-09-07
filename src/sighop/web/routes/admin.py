"""Configuration through the browser (`web-admin`, §8 area 1).

Every write here goes through the same repository call the equivalent `sighop`
subcommand makes. That is the whole rule and it is not a stylistic one: a
validation that lives in a route handler is a rule the command line does not
have, and two surfaces with different rules is how a room ends up with a
password the CLI would have refused.

So there is no validation in this module. There is reading, rendering, and
calling the repository — plus the three guarded actions, which are the only
things here that are more than configuration.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.db.engine import Failed, Outcome, Succeeded
from sighop.db.repositories import BotRecord, EntityRecord, RoomRecord
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.web.deps import Panel, panel
from sighop.web.guarded import (
    ACTION_DESCRIPTIONS,
    ENABLE_TRANSMIT,
    RAISE_CEILING,
    REVEAL_KEY,
    audit,
)
from sighop.web.render import NO_DATABASE, Collection, read, unreadable

PanelDep = Annotated[Panel, Depends(panel)]
"""FastAPI's `Annotated` form rather than a `Depends()` default, so the
dependency is part of the type rather than a function call evaluated once at
import (`B008`, and FastAPI's own recommendation)."""

router = APIRouter(prefix="/admin")

SEE_OTHER = 303
"""A write answers with a redirect, so a reload re-reads rather than re-writes."""


def _collection[T](
    page: Panel, outcome: Outcome[list[T]] | None, *, degraded: str
) -> Collection[T]:
    """A repository read as something a template can tell apart from empty."""
    if outcome is None:
        return unreadable(NO_DATABASE)
    if isinstance(outcome, Failed):
        return unreadable(f"{degraded}: {outcome.error}")
    return read(outcome.value)


# --- 12.1 / 12.2 Identities -------------------------------------------------


@router.get("/identities", response_class=HTMLResponse)
async def identities(request: Request, page: PanelDep) -> HTMLResponse:
    """Every stored identity, with what each one is serving.

    The identities this run *loaded* are in memory; the ones the database holds
    may be more than that, and the difference is the point of showing both.
    """
    stored: Outcome[list[EntityRecord]] | None = None
    rooms: Outcome[list[RoomRecord]] | None = None
    bots: Outcome[list[BotRecord]] | None = None
    if page.persistence is not None:
        stored = await page.persistence.entities.list_all()
        rooms = await page.persistence.rooms.list_all()
        bots = await page.persistence.bots.list_all()
    return page.page(
        request,
        "admin/identities.html",
        stored=_collection(page, stored, degraded="identities cannot be read"),
        bindings=_bindings(rooms, bots),
        loaded=list(page.state.adverts.stubs),
    )


def _bindings(
    rooms: Outcome[list[RoomRecord]] | None, bots: Outcome[list[BotRecord]] | None
) -> dict[str, list[str]]:
    """What each identity is serving, keyed by entity id.

    Read before an identity is disabled, so the warning can name the room or bot
    rather than saying that something, somewhere, might break (`web-admin`).
    """
    bound: dict[str, list[str]] = {}
    if isinstance(rooms, Succeeded):
        for room in rooms.value:
            bound.setdefault(str(room.entity_id), []).append(f"room {room.name!r}")
    if isinstance(bots, Succeeded):
        for bot in bots.value:
            bound.setdefault(str(bot.entity_id), []).append(f"bot {bot.driver!r}")
    return bound


@router.post("/identities/{entity_id}/enabled")
async def set_identity_enabled(
    entity_id: str,
    enabled: Annotated[str, Form()],
    page: PanelDep,
) -> RedirectResponse:
    """Enable or disable one identity, through `sighop keys`' own repository."""
    if page.persistence is None:
        return RedirectResponse("/admin/identities", status_code=SEE_OTHER)
    record = await _entity(page, entity_id)
    if record is not None:
        await page.persistence.entities.set_enabled(
            record.public_key, enabled == "true"
        )
    return RedirectResponse("/admin/identities", status_code=SEE_OTHER)


async def _entity(page: Panel, entity_id: str) -> EntityRecord | None:
    if page.persistence is None:
        return None
    listed = await page.persistence.entities.list_all()
    if isinstance(listed, Failed):
        return None
    for record in listed.value:
        if str(record.id) == entity_id:
            return record
    return None


# --- 12.3 / 12.4 Rooms ------------------------------------------------------


@router.get("/rooms", response_class=HTMLResponse)
async def rooms(request: Request, page: PanelDep) -> HTMLResponse:
    """Rooms with their member and message counts, and their retention."""
    listed: Outcome[list[RoomRecord]] | None = None
    counts: dict[str, tuple[int, int]] = {}
    if page.persistence is not None:
        listed = await page.persistence.rooms.list_all()
        if isinstance(listed, Succeeded):
            for room in listed.value:
                counts[str(room.id)] = await _room_counts(page, room)
    return page.page(
        request,
        "admin/rooms.html",
        rooms=_collection(page, listed, degraded="rooms cannot be read"),
        counts=counts,
        served={str(server.room.id) for server in page.state.rooms},
    )


async def _room_counts(page: Panel, room: RoomRecord) -> tuple[int, int]:
    assert page.persistence is not None
    members = await page.persistence.members.load_for_room(room.id)
    messages = await page.persistence.messages.count(room.id)
    return (
        len(members.value) if isinstance(members, Succeeded) else -1,
        messages.value if isinstance(messages, Succeeded) else -1,
    )


@router.get("/rooms/{room_id}/password", response_class=HTMLResponse)
async def rotate_password_form(
    room_id: str, request: Request, page: PanelDep
) -> HTMLResponse:
    """The consequence, before the change: members must log in again.

    Stated on the way in rather than after the fact, because a rotation an
    operator did not understand is a room full of members who cannot post and
    do not know why (`web-admin`).
    """
    room = await _room(page, room_id)
    return page.page(
        request,
        "admin/room_password.html",
        room=room,
        status_code=200 if room is not None else 404,
    )


@router.post("/rooms/{room_id}/password")
async def rotate_password(
    room_id: str,
    page: PanelDep,
    admin_password: Annotated[str, Form()] = "",
    guest_password: Annotated[str, Form()] = "",
    guest_open: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Rotate through `RoomRepository.set_passwords` — `sighop room passwd`'s own.

    Hashing happens in `passwords.py`, off the event loop and bounded, as it has
    since milestone 6. Nothing about a password reaches a log, a page or a
    request event: the form is a POST body, and the guard never carries a body
    into an event.
    """
    room = await _room(page, room_id)
    if room is None or page.persistence is None:
        return RedirectResponse("/admin/rooms", status_code=SEE_OTHER)

    # Through `PasswordHasher`, which runs Argon2id in a thread under a
    # semaphore — never `hash_password` directly, which blocks the event loop
    # the radio's serial transport is running on (milestone 6, design D1).
    from sighop.passwords import PasswordHasher

    hasher = PasswordHasher()
    admin_hash = await hasher.hash(admin_password) if admin_password else None
    guest_hash = await hasher.hash(guest_password) if guest_password else None
    await page.persistence.rooms.set_passwords(
        room.id,
        admin_password_hash=admin_hash,
        guest_password_hash=guest_hash,
        guest_open=guest_open == "true",
    )
    return RedirectResponse("/admin/rooms", status_code=SEE_OTHER)


@router.get("/rooms/{room_id}/retention", response_class=HTMLResponse)
async def retention_form(
    room_id: str, request: Request, page: PanelDep
) -> HTMLResponse:
    """How many stored messages a bound would remove, before it is applied."""
    room = await _room(page, room_id)
    stored = 0
    if room is not None and page.persistence is not None:
        counted = await page.persistence.messages.count(room.id)
        stored = counted.value if isinstance(counted, Succeeded) else -1
    return page.page(
        request,
        "admin/room_retention.html",
        room=room,
        stored=stored,
        status_code=200 if room is not None else 404,
    )


@router.post("/rooms/{room_id}/retention")
async def set_retention(
    room_id: str,
    page: PanelDep,
    retention_days: Annotated[str, Form()] = "",
    retention_messages: Annotated[str, Form()] = "",
) -> RedirectResponse:
    room = await _room(page, room_id)
    if room is None or page.persistence is None:
        return RedirectResponse("/admin/rooms", status_code=SEE_OTHER)
    await page.persistence.rooms.set_retention(
        room.id,
        retention_days=_optional_int(retention_days),
        retention_messages=_optional_int(retention_messages),
    )
    return RedirectResponse("/admin/rooms", status_code=SEE_OTHER)


def _optional_int(value: str) -> int | None:
    """An empty field means "no bound", which is what unlimited *is* (D15)."""
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


async def _room(page: Panel, room_id: str) -> RoomRecord | None:
    if page.persistence is None:
        return None
    listed = await page.persistence.rooms.list_all()
    if isinstance(listed, Failed):
        return None
    for room in listed.value:
        if str(room.id) == room_id:
            return room
    return None


# --- 12.5 / 12.6 Bots -------------------------------------------------------


@router.get("/bots", response_class=HTMLResponse)
async def bots(request: Request, page: PanelDep) -> HTMLResponse:
    """Every configured bot, with the durable state it has accumulated."""
    listed: Outcome[list[BotRecord]] | None = None
    state: dict[str, dict[str, object]] = {}
    if page.persistence is not None:
        listed = await page.persistence.bots.list_all()
        if isinstance(listed, Succeeded):
            for bot in listed.value:
                stored = await page.persistence.bot_state.list(bot.id)
                state[str(bot.id)] = (
                    stored.value if isinstance(stored, Succeeded) else {}
                )
    return page.page(
        request,
        "admin/bots.html",
        bots=_collection(page, listed, degraded="bots cannot be read"),
        bot_state=state,
        running={worker.name for worker in page.state.bots.workers},
    )


@router.post("/bots/{bot_id}/enabled")
async def set_bot_enabled(
    bot_id: str, enabled: Annotated[str, Form()], page: PanelDep
) -> RedirectResponse:
    if page.persistence is not None:
        await page.persistence.bots.set_enabled(uuid.UUID(bot_id), enabled == "true")
    return RedirectResponse("/admin/bots", status_code=SEE_OTHER)


@router.get("/bots/{bot_id}/mode", response_class=HTMLResponse)
async def bot_mode_form(
    bot_id: str, request: Request, page: PanelDep
) -> HTMLResponse:
    """A switch to active is confirmed explicitly, and says what it means.

    "Active" is not a verbosity setting: an active bot transmits at strangers
    without being asked again, which is the whole safety posture milestone 7
    built (`web-admin`).
    """
    bot = await _bot(page, bot_id)
    return page.page(
        request,
        "admin/bot_mode.html",
        bot=bot,
        status_code=200 if bot is not None else 404,
    )


@router.post("/bots/{bot_id}/mode")
async def set_bot_mode(
    bot_id: str,
    mode: Annotated[str, Form()],
    page: PanelDep,
    confirm: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Observe needs no confirmation; active does, and is refused without one."""
    if page.persistence is None:
        return RedirectResponse("/admin/bots", status_code=SEE_OTHER)
    if mode == "active" and confirm != "yes":
        # Unchanged, and the operator is sent back to the page that explains it.
        return RedirectResponse(f"/admin/bots/{bot_id}/mode", status_code=SEE_OTHER)
    await page.persistence.bots.set_mode(uuid.UUID(bot_id), mode)
    return RedirectResponse("/admin/bots", status_code=SEE_OTHER)


@router.post("/bots/{bot_id}/config")
async def set_bot_config(
    bot_id: str, configuration: Annotated[str, Form()], page: PanelDep
) -> RedirectResponse:
    """Refused with the driver's own reason, leaving the stored one unchanged.

    The validator is the driver's, reached through the same registry
    `sighop bot config` uses — so a configuration this refuses is one the
    command line refuses, for the same stated reason.
    """
    import json

    bot = await _bot(page, bot_id)
    if bot is None or page.persistence is None:
        return RedirectResponse("/admin/bots", status_code=SEE_OTHER)
    try:
        parsed = json.loads(configuration)
        if not isinstance(parsed, dict):
            raise ValueError("a driver configuration is an object")
        _validate_bot_config(bot.driver, parsed)
    except Exception as exc:
        page.logger.error(
            "web_bot_config_refused",
            outcome="refused",
            bot_id=bot_id,
            driver=bot.driver,
            reason=str(exc),
        )
        return RedirectResponse(
            f"/admin/bots?refused={bot_id}", status_code=SEE_OTHER
        )
    await page.persistence.bots.set_config(bot.id, parsed)
    return RedirectResponse("/admin/bots", status_code=SEE_OTHER)


def _validate_bot_config(driver: str, config: dict[str, object]) -> None:
    """The driver's own validator, key by key (design D14).

    `drivers.validate_config` is exactly what `sighop bot config` calls, so a
    value this refuses is one the command line refuses, with the same reason in
    the same words. The runtime's two reserved keys are validated by the runtime
    and the rest by the driver that owns them — that routing is the registry's,
    not this module's.
    """
    from sighop.bots import drivers as bot_drivers

    bot_drivers.lookup(driver)
    for key, value in config.items():
        bot_drivers.validate_config(driver, key, str(value))


async def _bot(page: Panel, bot_id: str) -> BotRecord | None:
    if page.persistence is None:
        return None
    listed = await page.persistence.bots.list_all()
    if isinstance(listed, Failed):
        return None
    for bot in listed.value:
        if str(bot.id) == bot_id:
            return bot
    return None


# --- 12.7 The radio ---------------------------------------------------------


@router.get("/radio", response_class=HTMLResponse)
async def radio(request: Request, page: PanelDep) -> HTMLResponse:
    """The parameters in force, and what changing one does and does not do."""
    from sighop.web.render import modem_readings

    return page.page(
        request,
        "admin/radio.html",
        readings=modem_readings(page.state.probe_result, page.state.radio),
        probe=page.state.probe_result,
    )


# --- 13 Guarded actions -----------------------------------------------------


@router.get("/reveal/{entity_id}", response_class=HTMLResponse)
async def reveal_form(
    entity_id: str, request: Request, page: PanelDep
) -> HTMLResponse:
    """The confirmation in front of a key reveal. Contains no key material."""
    stub = _loaded(page, entity_id)
    return page.page(
        request,
        "admin/guarded.html",
        action=REVEAL_KEY,
        target=entity_id,
        title="reveal a private key",
        subject=None if stub is None else stub.name,
        description=ACTION_DESCRIPTIONS[REVEAL_KEY],
        post_to=f"/admin/reveal/{entity_id}",
        nonce=page.nonces.mint(REVEAL_KEY, entity_id),
        status_code=200 if stub is not None else 404,
    )


@router.post("/reveal/{entity_id}", response_class=HTMLResponse)
async def reveal(
    entity_id: str,
    request: Request,
    page: PanelDep,
    nonce: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """The one response body in this application that contains key material.

    Not reachable by navigation, not linked from anywhere, and not rendered
    again: reloading this page re-posts nothing, because the nonce that
    authorised it has been spent.
    """
    stub = _loaded(page, entity_id)
    if stub is None or not page.nonces.spend(nonce, REVEAL_KEY, entity_id):
        audit(
            page.logger,
            action=REVEAL_KEY,
            target=entity_id,
            outcome="refused",
            reason="no confirmation was minted for this action",
        )
        return page.page(
            request, "admin/refused.html", title="reveal a private key", status_code=403
        )
    audit(
        page.logger,
        action=REVEAL_KEY,
        target=entity_id,
        outcome="success",
        entity_name=stub.name,
        public_key=stub.identity.public_key.hex(),
    )
    return page.page(
        request,
        "admin/revealed.html",
        entity_name=stub.name,
        public_key=stub.identity.public_key.hex(),
        seed=stub.identity.seed.hex(),
    )


def _loaded(page: Panel, entity_id: str):  # type: ignore[no-untyped-def]
    for stub in page.state.adverts.stubs:
        if stub.entity_id == entity_id:
            return stub
    return None


@router.get("/transmit", response_class=HTMLResponse)
async def transmit_form(request: Request, page: PanelDep) -> HTMLResponse:
    return page.page(
        request,
        "admin/guarded.html",
        action=ENABLE_TRANSMIT,
        target="run",
        title="enable transmission",
        subject=None,
        description=ACTION_DESCRIPTIONS[ENABLE_TRANSMIT],
        post_to="/admin/transmit",
        nonce=page.nonces.mint(ENABLE_TRANSMIT, "run"),
    )


@router.post("/transmit", response_model=None)
async def enable_transmit(
    request: Request,
    page: PanelDep,
    nonce: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "true",
) -> RedirectResponse | HTMLResponse:
    """Change the run's gate and the panel's indication together.

    One value, read from the scheduler: the indicator is not a copy of the gate
    kept in the panel, it *is* the gate, so the two cannot drift.
    """
    if not page.nonces.spend(nonce, ENABLE_TRANSMIT, "run"):
        audit(
            page.logger,
            action=ENABLE_TRANSMIT,
            target="run",
            outcome="refused",
            reason="no confirmation was minted for this action",
        )
        return page.page(
            request, "admin/refused.html", title="enable transmission", status_code=403
        )
    wanted = enabled == "true"
    page.state.scheduler.enable_transmit(wanted)
    audit(
        page.logger,
        action=ENABLE_TRANSMIT,
        target="run",
        outcome="success",
        transmit_enabled=wanted,
    )
    return RedirectResponse("/", status_code=SEE_OTHER)


@router.get("/ceiling", response_class=HTMLResponse)
async def ceiling_form(request: Request, page: PanelDep) -> HTMLResponse:
    return page.page(
        request,
        "admin/guarded.html",
        action=RAISE_CEILING,
        target="budget",
        title="raise the airtime ceiling",
        subject=None,
        description=ACTION_DESCRIPTIONS[RAISE_CEILING],
        post_to="/admin/ceiling",
        nonce=page.nonces.mint(RAISE_CEILING, "budget"),
        ceiling_field=True,
        current_fraction=page.state.scheduler.budget.ceiling_fraction,
        regulatory_default=DEFAULT_CEILING_FRACTION,
    )


@router.post("/ceiling", response_model=None)
async def raise_ceiling(
    request: Request,
    page: PanelDep,
    nonce: Annotated[str, Form()] = "",
    fraction: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """Carry the old and the new value into the event, not only the new one."""
    budget = page.state.scheduler.budget
    previous = budget.ceiling_fraction
    if not page.nonces.spend(nonce, RAISE_CEILING, "budget"):
        audit(
            page.logger,
            action=RAISE_CEILING,
            target="budget",
            outcome="refused",
            reason="no confirmation was minted for this action",
            ceiling_fraction=previous,
        )
        return page.page(
            request,
            "admin/refused.html",
            title="raise the airtime ceiling",
            status_code=403,
        )
    try:
        wanted = float(fraction)
        if not 0 < wanted <= 1:
            raise ValueError(f"ceiling fraction {wanted} outside (0, 1]")
    except ValueError as exc:
        audit(
            page.logger,
            action=RAISE_CEILING,
            target="budget",
            outcome="refused",
            reason=str(exc),
            ceiling_fraction=previous,
        )
        return page.page(
            request,
            "admin/refused.html",
            title="raise the airtime ceiling",
            status_code=400,
        )
    budget.ceiling_fraction = wanted
    audit(
        page.logger,
        action=RAISE_CEILING,
        target="budget",
        outcome="success",
        previous_ceiling_fraction=previous,
        ceiling_fraction=wanted,
        above_regulatory_default=wanted > DEFAULT_CEILING_FRACTION,
    )
    return RedirectResponse("/", status_code=SEE_OTHER)
