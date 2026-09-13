"""Configuration through the browser (`web-admin`, §8 area 1).

Every write here goes through the same repository call the equivalent `sighop`
subcommand makes. That is the whole rule and it is not a stylistic one: a
validation that lives in a route handler is a rule the command line does not
have, and two surfaces with different rules is how a room ends up with a
password the CLI would have refused.

So there is no validation in this module. There is reading, rendering, and
calling the repository — plus the guarded actions, which are the only things
here that are more than configuration.

The identity lifecycle — creating, showing, importing and exporting — lives in
`web/routes/keys.py`, beside the one guarded action that writes key material
out of the platform.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.db.engine import Failed, Outcome, Succeeded
from sighop.db.repositories import (
    BOT_ENTITY_TYPE,
    BotExistsError,
    BotRecord,
    EntityHasRoleError,
    EntityRecord,
    EntityRoleError,
    RoomExistsError,
    RoomRecord,
)
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.protocol.payloads import NodeType
from sighop.web.deps import Panel, panel
from sighop.web.guarded import (
    ACTION_DESCRIPTIONS,
    ENABLE_TRANSMIT,
    RAISE_CEILING,
    REVEAL_KEY,
    audit,
)
from sighop.web.render import (
    DEGRADED,
    NO_DATABASE,
    Refusal,
    collection_for,
    refused,
    render_ceiling_change,
    render_transmit_change,
)

PanelDep = Annotated[Panel, Depends(panel)]
"""FastAPI's `Annotated` form rather than a `Depends()` default, so the
dependency is part of the type rather than a function call evaluated once at
import (`B008`, and FastAPI's own recommendation)."""

router = APIRouter(prefix="/admin")

SEE_OTHER = 303
"""A write answers with a redirect, so a reload re-reads rather than re-writes."""


# --- 12.3 / 12.4 Rooms ------------------------------------------------------


@router.get("/rooms", response_class=HTMLResponse)
async def rooms(
    request: Request,
    page: PanelDep,
    *,
    refusal: Refusal | None = None,
    status_code: int = 200,
) -> HTMLResponse:
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
        rooms=collection_for(listed, degraded="rooms cannot be read"),
        counts=counts,
        served={str(server.room.id) for server in page.state.rooms},
        hosts=await _room_server_identities(page),
        refusal=refusal,
        status_code=status_code,
    )


async def _room_server_identities(page: Panel) -> list[EntityRecord]:
    """The stored identities a room could be bound to.

    Only the ones that advert as room servers, because that is what a room runs
    on; offering the rest would be offering a choice the repository refuses.
    """
    if page.persistence is None:
        return []
    listed = await page.persistence.entities.list_all()
    if isinstance(listed, Failed):
        return []
    bound = await page.persistence.rooms.list_all()
    taken = (
        {room.entity_id for room in bound.value}
        if isinstance(bound, Succeeded)
        else set()
    )
    return [
        record
        for record in listed.value
        if record.node_type is NodeType.ROOM_SERVER and record.id not in taken
    ]


@router.post("/rooms/create", response_model=None)
async def create_room(
    request: Request,
    page: PanelDep,
    name: Annotated[str, Form()] = "",
    entity_id: Annotated[str, Form()] = "",
    admin_password: Annotated[str, Form()] = "",
    guest_password: Annotated[str, Form()] = "",
    guest_open: Annotated[str, Form()] = "",
    allow_read_only: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """Bind a room to a stored identity — `sighop room create`'s own call.

    The admin password is required for the reason the command line requires it:
    it is the only credential that admits an administrator, and a room with none
    has no administrator and no way to gain one.
    """
    submitted = {"name": name, "entity_id": entity_id}
    if page.persistence is None:
        return await _refuse_room(request, page, NO_DATABASE, **submitted)
    if not admin_password:
        return await _refuse_room(
            request,
            page,
            "an admin password is required: it is the only credential that "
            "admits an administrator, and §7 asks for a distinct one per room "
            "server",
            field="admin_password",
            **submitted,
        )
    try:
        entity = uuid.UUID(entity_id)
    except ValueError:
        return await _refuse_room(
            request,
            page,
            "choose the stored identity this room runs on",
            field="entity_id",
            **submitted,
        )

    # Through `PasswordHasher`, never `hash_password`: Argon2id in a thread
    # under a semaphore, because the loop this would otherwise block is the one
    # the radio's serial transport is running on (milestone 6, design D1).
    from sighop.passwords import PasswordHasher

    hasher = PasswordHasher()
    admin_hash = await hasher.hash(admin_password)
    guest_hash = await hasher.hash(guest_password) if guest_password else None
    try:
        created = await page.persistence.rooms.create(
            entity_id=entity,
            name=name,
            admin_password_hash=admin_hash,
            guest_password_hash=guest_hash,
            guest_open=guest_open == "true",
            allow_read_only=allow_read_only == "true",
        )
    except (EntityRoleError, RoomExistsError) as exc:
        return await _refuse_room(request, page, str(exc), **submitted)
    if isinstance(created, Failed):
        return await _refuse_room(request, page, str(created.error), **submitted)
    return RedirectResponse("/admin/rooms", status_code=SEE_OTHER)


async def _refuse_room(
    request: Request, page: Panel, reason: str, *, field: str = "", **submitted: str
) -> HTMLResponse:
    """Re-render the room page with the reason and what was typed (task 1.1).

    The passwords are deliberately not among the preserved values: a password
    that came back in a response body would be a password in a page.
    """
    return await rooms(
        request,
        page,
        refusal=refused(reason, field=field, **submitted),
        status_code=400,
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
    allow_read_only: Annotated[str, Form()] = "",
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
        # Both flags are part of what a login is gated by, which is what this
        # page is for — the clause `web-admin` asked for and milestone 8 marked
        # done without building.
        allow_read_only=allow_read_only == "true",
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
async def bots(
    request: Request,
    page: PanelDep,
    *,
    refusal: Refusal | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Every configured bot, with the durable state it has accumulated."""
    from sighop.bots import drivers as bot_drivers

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
        bots=collection_for(listed, degraded="bots cannot be read"),
        bot_state=state,
        running={worker.name for worker in page.state.bots.workers},
        drivers=bot_drivers.driver_names(),
        identities=await _bot_identities(page),
        refusal=refusal,
        status_code=status_code,
    )


async def _bot_identities(page: Panel) -> list[EntityRecord]:
    """The stored identities a bot could run on.

    Only identities stored as bots and not already carrying one: being a bot is
    an explicit choice at creation and never a side effect of having a bot bound
    to it, and one identity plays one role.
    """
    if page.persistence is None:
        return []
    listed = await page.persistence.entities.list_all()
    if isinstance(listed, Failed):
        return []
    bound = await page.persistence.bots.list_all()
    taken = (
        {bot.entity_id for bot in bound.value}
        if isinstance(bound, Succeeded)
        else set()
    )
    return [
        record
        for record in listed.value
        if record.type == BOT_ENTITY_TYPE and record.id not in taken
    ]


@router.post("/bots/create", response_model=None)
async def create_bot(
    request: Request,
    page: PanelDep,
    entity_id: Annotated[str, Form()] = "",
    driver: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """Bind a driver to an identity — `sighop bot create`'s own call.

    With the driver's own defaults, enabled and in observe mode: a new bot runs
    its whole decision path and transmits nothing until an operator says
    otherwise, which is milestone 7's safety posture and not this surface's
    choice to make.

    And **seeded**, for the same reason the command line seeds: a greeter
    created on a node that already knows fifty peers would otherwise start out
    owing fifty strangers a message (design D7).
    """
    from sighop.bots import drivers as bot_drivers
    from sighop.bots.base import UnknownDriverError
    from sighop.bots.greeter import seeded_entries

    submitted = {"entity_id": entity_id, "driver": driver}
    if page.persistence is None:
        return await _refuse_bot(request, page, NO_DATABASE, **submitted)
    try:
        config = bot_drivers.default_config(driver)
    except UnknownDriverError as exc:
        return await _refuse_bot(request, page, str(exc), field="driver", **submitted)
    try:
        entity = uuid.UUID(entity_id)
    except ValueError:
        return await _refuse_bot(
            request,
            page,
            "choose the stored identity this bot runs on",
            field="entity_id",
            **submitted,
        )
    record = await page.persistence.entities.get_by_id(entity)
    name = (
        record.value.name
        if isinstance(record, Succeeded) and record.value is not None
        else ""
    )

    try:
        created = await page.persistence.bots.create(
            entity_id=entity, driver=driver, config=config, entity_name=name
        )
    except (BotExistsError, EntityHasRoleError) as exc:
        return await _refuse_bot(request, page, str(exc), **submitted)
    if isinstance(created, Failed):
        return await _refuse_bot(request, page, str(created.error), **submitted)

    # After the row exists, because `bot_state` has a foreign key to it. A seed
    # that fails leaves a bot owing the whole contact table, so the failure is
    # said rather than swallowed — the bot stays created either way.
    contacts = await page.persistence.contacts.load_all()
    if isinstance(contacts, Succeeded):
        await page.persistence.bot_state.set_many(
            created.value.id, seeded_entries(contacts.value, at=_now_iso())
        )
    else:
        page.logger.error(
            "web_bot_seed_failed",
            outcome="refused",
            bot_id=str(created.value.id),
            bot_name=name,
            reason=str(contacts.error),
            detail=(
                "the bot was created and owes a greeting to every contact this "
                "node knows; seed it before making it active"
            ),
        )
    return RedirectResponse("/admin/bots", status_code=SEE_OTHER)


async def _refuse_bot(
    request: Request, page: Panel, reason: str, *, field: str = "", **submitted: str
) -> HTMLResponse:
    return await bots(
        request,
        page,
        refusal=refused(reason, field=field, **submitted),
        status_code=400,
    )


def _now_iso() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat()


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


@router.post("/bots/{bot_id}/config", response_model=None)
async def set_bot_config(
    request: Request,
    bot_id: str,
    page: PanelDep,
    key: Annotated[str, Form()] = "",
    value: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """One key and one value, as `sighop bot set` takes them (design D7).

    Per key rather than a whole object, for the reason the command line is per
    key: a refusal names the key it is about, and a valid change is not lost
    because another key in the same object was wrong. `drivers.validate_config`
    is exactly what `sighop bot set` calls — it routes the runtime's two
    reserved keys to the runtime and the rest to the driver that owns them — so
    a value this refuses is one the command line refuses, in the same words, and
    the stored configuration is exactly what it was.
    """
    from sighop.bots import drivers as bot_drivers
    from sighop.bots.base import BotConfigError, UnknownDriverError

    bot = await _bot(page, bot_id)
    if bot is None or page.persistence is None:
        return RedirectResponse("/admin/bots", status_code=SEE_OTHER)
    try:
        # Against the driver named by the *stored* row, so the check is the one
        # the bot will actually run under.
        parsed = bot_drivers.validate_config(bot.driver, key, value)
    except (BotConfigError, UnknownDriverError) as exc:
        page.logger.error(
            "web_bot_config_refused",
            outcome="refused",
            bot_id=bot_id,
            driver=bot.driver,
            config_key=key,
            reason=str(exc),
        )
        return await _refuse_bot(
            request,
            page,
            f"{key}: {exc} The stored configuration is unchanged.",
            field=key,
            bot_id=bot_id,
            **{key: value},
        )
    await page.persistence.bots.set_config(bot.id, {**bot.config, key: parsed})
    return RedirectResponse("/admin/bots", status_code=SEE_OTHER)


async def _bot(page: Panel, bot_id: str) -> BotRecord | None:
    if page.persistence is None:
        return None
    try:
        uuid.UUID(bot_id)
    except ValueError:
        return None
    listed = await page.persistence.bots.list_all()
    if isinstance(listed, Failed):
        return None
    for bot in listed.value:
        if str(bot.id) == bot_id:
            return bot
    return None


# --- 6.3 / 6.4 The records that decide whether a bot acts again -------------
#
# Design D5: the key convention is `bots/greeter.py`'s and stays there — it is
# the part both surfaces must agree on. Only the *rendering* of a record's state
# is this surface's own, and a rendering is allowed to differ between a terminal
# and a table.

CLEARING_RELEASES_A_CONTACT = (
    "Clearing a record makes that contact eligible to be acted on again: the "
    "next time it adverts, this bot will greet it if the hop, node-type and rate "
    "gates pass — and, when the bot is active, that means transmitting at it."
)

SEEDING_KEEPS_EXISTING_RECORDS = (
    "Seeding records every contact this node already knows as already greeted, "
    "so the bot starts owing them nothing. Contacts that already have a record "
    "keep the one they have — a seeded contact and a greeted one stay "
    "distinguishable."
)

CLEARING_STATE_FORGETS = (
    "Clearing a bot's durable state makes it forget everything it recorded, the "
    "seed included. A greeter will greet a previously greeted node again the "
    "next time it adverts."
)


@router.get("/bots/{bot_id}/greeted", response_class=HTMLResponse)
async def greeted(
    bot_id: str,
    request: Request,
    page: PanelDep,
    *,
    cleared: int | None = None,
) -> HTMLResponse:
    """Who this bot considers already acted on, and the operator's say in it.

    The greeting record is the whole gate (design D7), so this page is the whole
    of the policy an operator can turn — and every statement about what a change
    releases is on it, in front of the control that makes the change.
    """
    bot = await _bot(page, bot_id)
    records: list[dict[str, object]] = []
    other_keys = 0
    if bot is not None and page.persistence is not None:
        stored = await page.persistence.bot_state.list(bot.id)
        if isinstance(stored, Succeeded):
            records, other_keys = _greeting_rows(stored.value)
    return page.page(
        request,
        "admin/greeted.html",
        bot=bot,
        records=records,
        other_keys=other_keys,
        contacts=len(page.state.contacts),
        clearing_note=CLEARING_RELEASES_A_CONTACT,
        seeding_note=SEEDING_KEEPS_EXISTING_RECORDS,
        state_note=CLEARING_STATE_FORGETS,
        cleared=cleared,
        status_code=200 if bot is not None else 404,
    )


def _greeting_rows(
    stored: dict[str, object],
) -> tuple[list[dict[str, object]], int]:
    """The `greeted:` keys as rows, and how many other keys there are.

    Both halves, because "this bot has stored nothing about anyone" and "this
    bot has stored things that are not greeting records" are different facts and
    a page showing only the first would be hiding the second.
    """
    from sighop.bots.greeter import SETTLED, greeted_public_key

    rows: list[dict[str, object]] = []
    others = 0
    for key, value in sorted(stored.items()):
        public_key = greeted_public_key(key)
        if public_key is None:
            others += 1
            continue
        record = value if isinstance(value, dict) else {}
        outcome = str(record.get("outcome", "unknown"))
        rows.append(
            {
                "public_key": public_key.hex(),
                "short_key": public_key.hex()[:16],
                "name": str(record.get("name", "")),
                "outcome": outcome,
                "settled": outcome in SETTLED,
                "attempts": record.get("attempts", 0),
                "at": str(record.get("at", "")),
            }
        )
    return rows, others


@router.post("/bots/{bot_id}/greeted/clear")
async def clear_greeting(
    bot_id: str, page: PanelDep, public_key: Annotated[str, Form()] = ""
) -> RedirectResponse:
    """Release one contact — `sighop bot greeted <peer> --clear`'s own call."""
    from sighop.bots.greeter import greeted_key

    bot = await _bot(page, bot_id)
    if bot is not None and page.persistence is not None:
        try:
            key = greeted_key(bytes.fromhex(public_key))
        except ValueError:
            return RedirectResponse(
                f"/admin/bots/{bot_id}/greeted", status_code=SEE_OTHER
            )
        await page.persistence.bot_state.delete(bot.id, key)
    return RedirectResponse(f"/admin/bots/{bot_id}/greeted", status_code=SEE_OTHER)


@router.post("/bots/{bot_id}/greeted/set")
async def set_greeting(
    bot_id: str, page: PanelDep, public_key: Annotated[str, Form()] = ""
) -> RedirectResponse:
    """Excuse one contact — `sighop bot greeted <peer> --set`'s own call.

    Marked as set by an operator rather than as greeted or seeded: three
    different facts about why a bot owes a contact nothing.
    """
    from sighop.bots.greeter import greeted_key, operator_entry

    bot = await _bot(page, bot_id)
    if bot is not None and page.persistence is not None:
        try:
            raw = bytes.fromhex(public_key)
        except ValueError:
            return RedirectResponse(
                f"/admin/bots/{bot_id}/greeted", status_code=SEE_OTHER
            )
        contact = page.state.contacts.get(raw)
        if contact is not None:
            await page.persistence.bot_state.set(
                bot.id, greeted_key(raw), operator_entry(contact, at=_now_iso())
            )
    return RedirectResponse(f"/admin/bots/{bot_id}/greeted", status_code=SEE_OTHER)


@router.post("/bots/{bot_id}/greeted/seed")
async def seed_greetings(bot_id: str, page: PanelDep) -> RedirectResponse:
    """Record every known contact as already acted on — `--seed`'s own call.

    `set_many` leaves an existing record alone, which is what makes "contacts
    that already had a record keep the one they had" true rather than hoped for.
    """
    from sighop.bots.greeter import seeded_entries

    bot = await _bot(page, bot_id)
    if bot is not None and page.persistence is not None:
        contacts = await page.persistence.contacts.load_all()
        if isinstance(contacts, Succeeded):
            await page.persistence.bot_state.set_many(
                bot.id, seeded_entries(contacts.value, at=_now_iso())
            )
    return RedirectResponse(f"/admin/bots/{bot_id}/greeted", status_code=SEE_OTHER)


# --- 6.5 Clearing everything a bot has persisted ----------------------------


@router.get("/bots/{bot_id}/state/clear", response_class=HTMLResponse)
async def clear_state_form(
    bot_id: str, request: Request, page: PanelDep
) -> HTMLResponse:
    """What clearing forgets, before it forgets it."""
    bot = await _bot(page, bot_id)
    keys = 0
    if bot is not None and page.persistence is not None:
        stored = await page.persistence.bot_state.list(bot.id)
        keys = len(stored.value) if isinstance(stored, Succeeded) else -1
    return page.page(
        request,
        "admin/bot_state.html",
        bot=bot,
        keys=keys,
        cleared=None,
        state_note=CLEARING_STATE_FORGETS,
        status_code=200 if bot is not None else 404,
    )


@router.post("/bots/{bot_id}/state/clear", response_class=HTMLResponse)
async def clear_state(
    bot_id: str,
    request: Request,
    page: PanelDep,
    confirm: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Forget everything, and report how many keys went.

    The count is the answer to "did that do what I thought": a clear that
    removed nothing and one that removed four hundred records are different
    events, and a redirect would lose the difference.
    """
    bot = await _bot(page, bot_id)
    if bot is None or page.persistence is None or confirm != "yes":
        return await clear_state_form(bot_id, request, page)
    removed = await page.persistence.bot_state.clear(bot.id)
    return page.page(
        request,
        "admin/bot_state.html",
        bot=bot,
        keys=0,
        cleared=removed.value if isinstance(removed, Succeeded) else -1,
        state_note=CLEARING_STATE_FORGETS,
    )


# --- 7. The schema, and what is deliberately absent -------------------------

MIGRATIONS_ARE_A_TERMINAL_ACT = (
    "Migrations are not applied from here, and that is deliberate rather than "
    "unbuilt. There are no roles in this interface — every account is an "
    "operator — so offering DDL here would make a stolen session enough to "
    "change the schema; and DESIGN.md §6 makes applying a migration an act an "
    "operator takes on purpose, never a side effect of starting something. It "
    "is one command in a terminal."
)
"""Design D6. An operator who has just been told the revisions disagree will
look for the button, and not finding one is ambiguous between "not built yet"
and "deliberately not offered". This says which."""

ACCOUNT_COMMAND = "sighop web user"

ACCOUNTS_ARE_A_TERMINAL_ACT = (
    "Accounts are not managed from here: adding one, setting a password and "
    "disabling or removing one are done in a terminal on the host with "
    f"`{ACCOUNT_COMMAND} add|passwd|disable|enable|remove|list`. With no roles, "
    "anyone signed in could otherwise create a second account for themselves, "
    "and a stolen session would become a credential that outlives it. Terminal "
    "access to the host is the stronger proof of being the operator. A change "
    "made there ends affected sessions within a minute."
)
"""Milestone 9 design D2, stated where it would be looked for."""


@router.get("/schema", response_class=HTMLResponse)
async def schema(request: Request, page: PanelDep) -> HTMLResponse:
    """The revision the database reports, against the one this code expects.

    Read-only, and the first question a degraded panel raises: is this database
    the one this build knows how to talk to? Everything durable on every page is
    wrong-by-omission if it is not.
    """
    from sighop.db import migrations

    expected = migrations.expected_revision()
    applied: str | None = None
    unavailable = ""
    if page.persistence is None:
        unavailable = NO_DATABASE
    else:
        try:
            applied = await page.persistence.database.read_applied_revision()
        except Exception as exc:
            # Classified by the engine; shown as the degraded state it is, which
            # is a different screen from "no database is configured".
            unavailable = f"{DEGRADED} ({exc})"
    return page.page(
        request,
        "admin/schema.html",
        applied=applied,
        expected=expected,
        agree=applied == expected and not unavailable,
        unavailable=unavailable,
        upgrade_command=migrations.UPGRADE_COMMAND,
        migrations_note=MIGRATIONS_ARE_A_TERMINAL_ACT,
        accounts_note=ACCOUNTS_ARE_A_TERMINAL_ACT,
        account_command=ACCOUNT_COMMAND,
    )


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
    password: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    """The one response body in this application that contains key material.

    Not reachable by navigation, not linked from anywhere, and not rendered
    again: reloading this page re-posts nothing, because the nonce that
    authorised it has been spent. The acting user's password is verified after
    the nonce, against a fresh read of their account (milestone 9 design D7).
    """
    stub = _loaded(page, entity_id)
    if stub is None or not page.nonces.spend(nonce, REVEAL_KEY, entity_id):
        audit(
            page.logger,
            action=REVEAL_KEY,
            target=entity_id,
            outcome="refused",
            actor=page.actor(request),
            reason="no confirmation was minted for this action",
        )
        return page.page(
            request, "admin/refused.html", title="reveal a private key", status_code=403
        )
    refusal = await page.reauthenticate(
        request,
        action=REVEAL_KEY,
        target=entity_id,
        password=password,
        title="reveal a private key",
    )
    if refusal is not None:
        return refusal
    audit(
        page.logger,
        action=REVEAL_KEY,
        target=entity_id,
        outcome="success",
        actor=page.actor(request),
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
    password: Annotated[str | None, Form()] = None,
) -> RedirectResponse | HTMLResponse:
    """Change the run's gate and the panel's indication together.

    One value, read from the scheduler: the indicator is not a copy of the gate
    kept in the panel, it *is* the gate, so the two cannot drift. The change is
    also written to the run's own output, naming the account that made it.
    """
    actor = page.actor(request)
    if not page.nonces.spend(nonce, ENABLE_TRANSMIT, "run"):
        audit(
            page.logger,
            action=ENABLE_TRANSMIT,
            target="run",
            outcome="refused",
            actor=actor,
            reason="no confirmation was minted for this action",
        )
        return page.page(
            request, "admin/refused.html", title="enable transmission", status_code=403
        )
    refusal = await page.reauthenticate(
        request,
        action=ENABLE_TRANSMIT,
        target="run",
        password=password,
        title="enable transmission",
    )
    if refusal is not None:
        return refusal
    wanted = enabled == "true"
    page.state.scheduler.enable_transmit(wanted)
    audit(
        page.logger,
        action=ENABLE_TRANSMIT,
        target="run",
        outcome="success",
        actor=actor,
        transmit_enabled=wanted,
    )
    page.say(render_transmit_change(wanted, actor=actor))
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
    password: Annotated[str | None, Form()] = None,
) -> RedirectResponse | HTMLResponse:
    """Carry the old and the new value into the event, not only the new one."""
    budget = page.state.scheduler.budget
    previous = budget.ceiling_fraction
    actor = page.actor(request)
    if not page.nonces.spend(nonce, RAISE_CEILING, "budget"):
        audit(
            page.logger,
            action=RAISE_CEILING,
            target="budget",
            outcome="refused",
            actor=actor,
            reason="no confirmation was minted for this action",
            ceiling_fraction=previous,
        )
        return page.page(
            request,
            "admin/refused.html",
            title="raise the airtime ceiling",
            status_code=403,
        )
    refusal = await page.reauthenticate(
        request,
        action=RAISE_CEILING,
        target="budget",
        password=password,
        title="raise the airtime ceiling",
        ceiling_fraction=previous,
    )
    if refusal is not None:
        return refusal
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
            actor=actor,
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
        actor=actor,
        previous_ceiling_fraction=previous,
        ceiling_fraction=wanted,
        above_regulatory_default=wanted > DEFAULT_CEILING_FRACTION,
    )
    page.say(render_ceiling_change(previous, wanted, actor=actor))
    return RedirectResponse("/", status_code=SEE_OTHER)
