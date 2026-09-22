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

import datetime as dt
import math
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.db.engine import Failed, Outcome, Succeeded
from sighop.db.repositories import (
    BotExistsError,
    BotRecord,
    EntityHasRoleError,
    EntityRoleError,
    RoomExistsError,
    RoomNameError,
    RoomNameTakenError,
    RoomRecord,
)
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.web.deps import Panel, panel
from sighop.web.guarded import (
    ACTION_DESCRIPTIONS,
    ADVERT_FLOOD,
    ADVERT_ZERO_HOP,
    ENABLE_TRANSMIT,
    RAISE_CEILING,
    REMOVE_BOT,
    REMOVE_CHANNEL,
    REMOVE_ROOM,
    REVEAL_KEY,
    audit,
)
from sighop.web.render import (
    NO_DATABASE,
    Refusal,
    collection_for,
    refused,
    render_advert_request,
    render_ceiling_change,
    render_transmit_change,
)
from sighop.web.routes import chat, keys, rooms
from sighop.web.routes.chat import (
    CHANNEL_KEY_NEEDS_THE_SECRET,
    CHANNELS_NEED_DURABLE_STORAGE,
)

PanelDep = Annotated[Panel, Depends(panel)]
"""FastAPI's `Annotated` form rather than a `Depends()` default, so the
dependency is part of the type rather than a function call evaluated once at
import (`B008`, and FastAPI's own recommendation)."""

router = APIRouter(prefix="/admin")

SEE_OTHER = 303
"""A write answers with a redirect, so a reload re-reads rather than re-writes."""


# --- 12.3 / 12.4 Rooms ------------------------------------------------------


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
    except (EntityRoleError, RoomExistsError, RoomNameError) as exc:
        return await _refuse_room(request, page, str(exc), **submitted)
    if isinstance(created, Failed):
        return await _refuse_room(request, page, str(created.error), **submitted)
    # After the store has taken it, so it reaches the run without a restart
    # and without waiting for the periodic re-read (`room-server`, `web-admin`).
    await page.state.reconcile_rooms()
    served = any(server.room.id == created.value.id for server in page.state.rooms)
    page.say(
        f"room {name!r} created through the web interface by account "
        f"{page.actor(request)!r}: "
        f"{'now served' if served else 'stored, but its identity was not loaded; not served'}"
    )
    return RedirectResponse("/rooms", status_code=SEE_OTHER)


async def _refuse_room(
    request: Request, page: Panel, reason: str, *, field: str = "", **submitted: str
) -> HTMLResponse:
    """Re-render the rooms page with the reason and what was typed (task 1.1).

    The passwords are deliberately not among the preserved values: a password
    that came back in a response body would be a password in a page.
    """
    return await rooms.render_index(
        request,
        page,
        refusal=refused(reason, field=field, **submitted),
        status_code=400,
    )


@router.get("/rooms/{room_id}/password", response_class=HTMLResponse)
async def rotate_password_form(room_id: str, request: Request, page: PanelDep) -> HTMLResponse:
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
        return RedirectResponse("/rooms", status_code=SEE_OTHER)

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
    return RedirectResponse("/rooms", status_code=SEE_OTHER)


@router.get("/rooms/{room_id}/retention", response_class=HTMLResponse)
async def retention_form(room_id: str, request: Request, page: PanelDep) -> HTMLResponse:
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
        return RedirectResponse("/rooms", status_code=SEE_OTHER)
    await page.persistence.rooms.set_retention(
        room.id,
        retention_days=_optional_int(retention_days),
        retention_messages=_optional_int(retention_messages),
    )
    return RedirectResponse("/rooms", status_code=SEE_OTHER)


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


@router.get("/rooms/{room_id}/rename", response_class=HTMLResponse)
async def rename_room_form(room_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """A room's name is a local label; the form says so before it is changed."""
    room = await _room(page, room_id)
    return page.page(
        request,
        "admin/room_rename.html",
        room=room,
        refusal=None,
        status_code=200 if room is not None else 404,
    )


@router.post("/rooms/{room_id}/rename", response_model=None)
async def rename_room(
    room_id: str,
    request: Request,
    page: PanelDep,
    name: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop room rename`'s own call. No nonce and no password: a rename is
    reversible by renaming back and destroys nothing (design D6)."""
    room = await _room(page, room_id)
    if room is None or page.persistence is None:
        return await rename_room_form(room_id, request, page)
    try:
        renamed = await page.persistence.rooms.rename(room.id, name)
    except (RoomNameError, RoomNameTakenError) as exc:
        return page.page(
            request,
            "admin/room_rename.html",
            room=room,
            refusal=Refusal(reason=str(exc), submitted={"name": name}),
            status_code=400,
        )
    if isinstance(renamed, Failed) or renamed.value is None:
        return await rename_room_form(room_id, request, page)
    page.logger.info(
        "web_room_renamed",
        outcome="success",
        room_id=room_id,
        previous_name=renamed.value,
        name=name.strip(),
        actor=page.actor(request),
    )
    return RedirectResponse("/rooms", status_code=SEE_OTHER)


@router.get("/rooms/{room_id}/delete", response_class=HTMLResponse)
async def delete_room_form(room_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """What deleting takes, counted, before it takes it.

    The foreign keys cascade silently, so the counts have to be read here —
    there is nothing in the delete itself that could report them.
    """
    room = await _room(page, room_id)
    members = messages = None
    identity = None
    if room is not None and page.persistence is not None:
        members, messages = await rooms.room_counts(page, room)
        held = await page.persistence.entities.get_by_id(room.entity_id)
        identity = held.value if isinstance(held, Succeeded) else None
    return page.page(
        request,
        "admin/room_delete.html",
        room=room,
        members=members,
        messages=messages,
        identity=identity,
        description=ACTION_DESCRIPTIONS[REMOVE_ROOM],
        nonce=None if room is None else page.nonces.mint(REMOVE_ROOM, room_id),
        status_code=200 if room is not None else 404,
    )


@router.post("/rooms/{room_id}/delete", response_model=None)
async def delete_room(
    room_id: str,
    request: Request,
    page: PanelDep,
    nonce: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop room delete`'s own call, behind confirm-and-nonce.

    No password: this destroys stored content rather than key material or what
    the station may do, which is the tier `REMOVE_CHANNEL` is in (design D6).
    """
    from urllib.parse import quote

    title = "delete room"
    actor = page.actor(request)
    room = await _room(page, room_id)
    if room is None or page.persistence is None:
        return await delete_room_form(room_id, request, page)
    if not page.nonces.spend(nonce, REMOVE_ROOM, room_id):
        audit(
            page.logger,
            action=REMOVE_ROOM,
            target=room_id,
            outcome="refused",
            actor=actor,
            reason="no confirmation was minted for this action",
            room=room.name,
        )
        return page.page(request, "admin/refused.html", title=title, refusal=None, status_code=403)
    members, messages = await rooms.room_counts(page, room)
    deleted = await page.persistence.rooms.delete(room.id)
    if isinstance(deleted, Failed) or not deleted.value:
        reason = str(deleted.error) if isinstance(deleted, Failed) else "no such room"
        audit(
            page.logger,
            action=REMOVE_ROOM,
            target=room_id,
            outcome="failed",
            actor=actor,
            reason=reason,
            room=room.name,
        )
        return page.page(
            request, "admin/refused.html", title=title, refusal=reason, status_code=409
        )
    # The store has taken it; now the run, which is what actually answers.
    served = await page.state.stop_serving_room(room.id)
    audit(
        page.logger,
        action=REMOVE_ROOM,
        target=room_id,
        outcome="success",
        actor=actor,
        room=room.name,
        members_deleted=members,
        messages_deleted=messages,
        stopped_serving=served,
    )
    page.say(
        f"room {room.name!r} deleted with {members} member(s) and {messages} "
        f"message(s) from the web interface by account {actor!r}"
    )
    return RedirectResponse(f"/rooms?deleted={quote(room.name)}", status_code=SEE_OTHER)


# --- 12.5 / 12.6 Bots -------------------------------------------------------


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
    name = record.value.name if isinstance(record, Succeeded) and record.value is not None else ""

    try:
        created = await page.persistence.bots.create(
            entity_id=entity, driver=driver, config=config, entity_name=name
        )
    except (BotExistsError, EntityHasRoleError) as exc:
        return await _refuse_bot(request, page, str(exc), **submitted)
    if isinstance(created, Failed):
        return await _refuse_bot(request, page, str(created.error), **submitted)
    home = _identity_page(created.value.entity_id)

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
    # After the row (and its seed) exist, so it reaches the run without a
    # restart and without waiting for the periodic re-read (`bot-runtime`,
    # `web-admin`).
    await page.state.reconcile_bots()
    running = any(worker.record.id == created.value.id for worker in page.state.bots)
    page.say(
        f"bot on identity {name!r} created through the web interface by account "
        f"{page.actor(request)!r}: "
        f"{'now running' if running else 'stored, but this run did not start it'}"
    )
    return RedirectResponse(home, status_code=SEE_OTHER)


async def _refuse_bot(
    request: Request,
    page: Panel,
    reason: str,
    *,
    field: str = "",
    entity_id: str = "",
    **submitted: str,
) -> HTMLResponse:
    """Re-render the identity page the bot runs on, with the reason (design D4)."""
    return await keys.render_identity(
        request,
        page,
        entity_id,
        refusal=refused(reason, field=field, entity_id=entity_id, **submitted),
        status_code=400,
    )


def _identity_page(entity_id: uuid.UUID) -> str:
    """Where a bot write returns to: the page of the identity the bot runs on."""
    return f"/admin/identities/{entity_id}"


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


@router.post("/bots/{bot_id}/enabled")
async def set_bot_enabled(
    request: Request, bot_id: str, enabled: Annotated[str, Form()], page: PanelDep
) -> RedirectResponse:
    """Enable or disable one bot, reaching the run right after the store
    takes it — the same immediacy `web-admin` already gives a room delete."""
    bot = await _bot(page, bot_id)
    if bot is None or page.persistence is None:
        return RedirectResponse("/admin/identities", status_code=SEE_OTHER)
    turning_on = enabled == "true"
    await page.persistence.bots.set_enabled(bot.id, turning_on)
    await page.state.reconcile_bots()
    running = any(worker.record.id == bot.id for worker in page.state.bots)
    page.say(
        f"bot {bot.entity_name!r} {'enabled' if turning_on else 'disabled'} through the web "
        f"interface by account {page.actor(request)!r}: "
        f"{'now running' if running else 'stopped; its durable state survives'}"
    )
    return RedirectResponse(_identity_page(bot.entity_id), status_code=SEE_OTHER)


@router.get("/bots/{bot_id}/mode", response_class=HTMLResponse)
async def bot_mode_form(bot_id: str, request: Request, page: PanelDep) -> HTMLResponse:
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
    bot = await _bot(page, bot_id)
    if bot is None or page.persistence is None:
        return RedirectResponse("/admin/identities", status_code=SEE_OTHER)
    if mode == "active" and confirm != "yes":
        # Unchanged, and the operator is sent back to the page that explains it.
        return RedirectResponse(f"/admin/bots/{bot_id}/mode", status_code=SEE_OTHER)
    await page.persistence.bots.set_mode(bot.id, mode)
    return RedirectResponse(_identity_page(bot.entity_id), status_code=SEE_OTHER)


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
        return RedirectResponse("/admin/identities", status_code=SEE_OTHER)
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
            entity_id=str(bot.entity_id),
            bot_id=bot_id,
            **{key: value},
        )
    await page.persistence.bots.set_config(bot.id, {**bot.config, key: parsed})
    return RedirectResponse(_identity_page(bot.entity_id), status_code=SEE_OTHER)


@router.get("/bots/{bot_id}/delete", response_class=HTMLResponse)
async def delete_bot_form(bot_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """What deleting forgets, counted, before it forgets it."""
    bot = await _bot(page, bot_id)
    keys = None
    identity = None
    if bot is not None and page.persistence is not None:
        stored = await page.persistence.bot_state.list(bot.id)
        keys = len(stored.value) if isinstance(stored, Succeeded) else None
        held = await page.persistence.entities.get_by_id(bot.entity_id)
        identity = held.value if isinstance(held, Succeeded) else None
    return page.page(
        request,
        "admin/bot_delete.html",
        bot=bot,
        keys=keys,
        identity=identity,
        description=ACTION_DESCRIPTIONS[REMOVE_BOT],
        nonce=None if bot is None else page.nonces.mint(REMOVE_BOT, bot_id),
        status_code=200 if bot is not None else 404,
    )


@router.post("/bots/{bot_id}/delete", response_model=None)
async def delete_bot(
    bot_id: str,
    request: Request,
    page: PanelDep,
    nonce: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop bot delete`'s own call, behind confirm-and-nonce.

    No password, on `REMOVE_CHANNEL`'s terms: this destroys stored content, not
    key material and not what the station may do (design D6).
    """
    from urllib.parse import quote

    title = "delete bot"
    actor = page.actor(request)
    bot = await _bot(page, bot_id)
    if bot is None or page.persistence is None:
        return await delete_bot_form(bot_id, request, page)
    if not page.nonces.spend(nonce, REMOVE_BOT, bot_id):
        audit(
            page.logger,
            action=REMOVE_BOT,
            target=bot_id,
            outcome="refused",
            actor=actor,
            reason="no confirmation was minted for this action",
            bot=bot.entity_name,
        )
        return page.page(request, "admin/refused.html", title=title, refusal=None, status_code=403)
    stored = await page.persistence.bot_state.list(bot.id)
    keys = len(stored.value) if isinstance(stored, Succeeded) else -1
    # The worker stops before the row goes, so a dispatch in flight finishes
    # against a bot whose state it can still write.
    ran = await page.state.stop_bot(bot.id)
    deleted = await page.persistence.bots.delete(bot.id)
    if isinstance(deleted, Failed) or not deleted.value:
        reason = str(deleted.error) if isinstance(deleted, Failed) else "no such bot"
        audit(
            page.logger,
            action=REMOVE_BOT,
            target=bot_id,
            outcome="failed",
            actor=actor,
            reason=reason,
            bot=bot.entity_name,
        )
        return page.page(
            request, "admin/refused.html", title=title, refusal=reason, status_code=409
        )
    audit(
        page.logger,
        action=REMOVE_BOT,
        target=bot_id,
        outcome="success",
        actor=actor,
        bot=bot.entity_name,
        driver=bot.driver,
        keys_deleted=keys,
        stopped_running=ran,
    )
    page.say(
        f"bot {bot.entity_name!r} deleted with {keys} stored key(s) from the "
        f"web interface by account {actor!r}"
    )
    return RedirectResponse(
        f"{_identity_page(bot.entity_id)}?bot_deleted={quote(bot.entity_name)}",
        status_code=SEE_OTHER,
    )


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
                "name": str(record.get("name", "")),
                "outcome": outcome,
                "settled": outcome in SETTLED,
                "attempts": record.get("attempts", 0),
                "at": _recorded_at(record.get("at")),
                "at_raw": str(record.get("at", "")),
            }
        )
    return rows, others


def _recorded_at(value: object) -> dt.datetime | None:
    """A greeting record's `at`, as a time the page can draw, or `None` when the
    stored text is not one (the page then shows the text as it is)."""
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


@router.post("/bots/{bot_id}/greeted/clear")
async def clear_greeting(
    bot_id: str, page: PanelDep, public_key: Annotated[str, Form()] = ""
) -> RedirectResponse:
    """Release one contact — `sighop bot greeted <peer> --clear`'s own call."""
    from sighop.bots.greeter import greeted_key

    bot = await _bot(page, bot_id)
    if bot is None or page.persistence is None:
        return RedirectResponse("/admin/identities", status_code=SEE_OTHER)
    try:
        key = greeted_key(bytes.fromhex(public_key))
    except ValueError:
        return RedirectResponse(_identity_page(bot.entity_id), status_code=SEE_OTHER)
    await page.persistence.bot_state.delete(bot.id, key)
    return RedirectResponse(_identity_page(bot.entity_id), status_code=SEE_OTHER)


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
    if bot is None or page.persistence is None:
        return RedirectResponse("/admin/identities", status_code=SEE_OTHER)
    try:
        raw = bytes.fromhex(public_key)
    except ValueError:
        return RedirectResponse(_identity_page(bot.entity_id), status_code=SEE_OTHER)
    contact = page.state.contacts.get(raw)
    if contact is not None:
        await page.persistence.bot_state.set(
            bot.id, greeted_key(raw), operator_entry(contact, at=_now_iso())
        )
    return RedirectResponse(_identity_page(bot.entity_id), status_code=SEE_OTHER)


@router.post("/bots/{bot_id}/greeted/seed")
async def seed_greetings(bot_id: str, page: PanelDep) -> RedirectResponse:
    """Record every known contact as already acted on — `--seed`'s own call.

    `set_many` leaves an existing record alone, which is what makes "contacts
    that already had a record keep the one they had" true rather than hoped for.
    """
    from sighop.bots.greeter import seeded_entries

    bot = await _bot(page, bot_id)
    if bot is None or page.persistence is None:
        return RedirectResponse("/admin/identities", status_code=SEE_OTHER)
    contacts = await page.persistence.contacts.load_all()
    if isinstance(contacts, Succeeded):
        await page.persistence.bot_state.set_many(
            bot.id, seeded_entries(contacts.value, at=_now_iso())
        )
    return RedirectResponse(_identity_page(bot.entity_id), status_code=SEE_OTHER)


# --- 6.5 Clearing everything a bot has persisted ----------------------------


@router.get("/bots/{bot_id}/state/clear", response_class=HTMLResponse)
async def clear_state_form(bot_id: str, request: Request, page: PanelDep) -> HTMLResponse:
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


# --- 7. What is deliberately absent (shown on `/system`) --------------------

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


# --- 13 Guarded actions -----------------------------------------------------


@router.get("/reveal/{entity_id}", response_class=HTMLResponse)
async def reveal_form(entity_id: str, request: Request, page: PanelDep) -> HTMLResponse:
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
        private_key=stub.identity.private_key.hex(),
    )


def _loaded(page: Panel, entity_id: str):  # type: ignore[no-untyped-def]
    for stub in page.state.adverts.stubs:
        if stub.entity_id == entity_id:
            return stub
    return None


# --- Adverting now (web-advert-now) -----------------------------------------

ADVERT_KINDS = {"zero-hop": ADVERT_ZERO_HOP, "flood": ADVERT_FLOOD}
"""The path segment each advert action is addressed by."""

NOT_HELD = "this run does not hold that identity, so there is nothing to advert as"

GATE_CLOSED = (
    "transmission is disabled for this run. A submitted advert would be charged "
    "against the airtime budget and suppressed at the modem — for a flood, also "
    "moving the next scheduled one a day out — with nothing on the air"
)

NO_RADIO = (
    "airtime cannot be computed until the board has answered with its radio "
    "parameters, and the scheduler would drop the advert"
)


def _advert_title(kind: str) -> str:
    return f"advert {kind} now"


@router.get("/advert/{entity_id}/{kind}", response_class=HTMLResponse)
async def advert_form(entity_id: str, kind: str, request: Request, page: PanelDep) -> HTMLResponse:
    """The confirmation in front of an advert, with its cost and the schedule.

    The gate and the gap are shown so a refusal can be seen coming; the POST
    decides from live state again, never from what this page showed (design D4).
    """
    action = ADVERT_KINDS.get(kind)
    if action is None:
        raise HTTPException(status_code=404)
    stub = _loaded(page, entity_id)
    adverts = page.state.adverts
    return page.page(
        request,
        "admin/advert.html",
        kind=kind,
        title=_advert_title(kind),
        stub=stub,
        not_held=NOT_HELD,
        description=ACTION_DESCRIPTIONS[action],
        post_to=f"/admin/advert/{entity_id}/{kind}",
        nonce=None if stub is None else page.nonces.mint(action, entity_id),
        transmit_enabled=page.state.scheduler.transmit_enabled,
        radio_known=page.state.radio is not None,
        gap_remaining=adverts.flood_gap_remaining(adverts.clock.now()),
        status_code=200 if stub is not None else 404,
    )


@router.post("/advert/{entity_id}/{kind}", response_model=None)
async def advert(
    entity_id: str,
    kind: str,
    request: Request,
    page: PanelDep,
    nonce: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """Submit one advert through the transmit scheduler, or refuse and submit nothing.

    Confirmation-only: a nonce and no password (design D3). Every refusal is
    decided here, in a fixed order so each is audited exactly once, and none of
    them defers — an operator who pressed "advert now" is told whether an
    advert was submitted (design D1).
    """
    action = ADVERT_KINDS.get(kind)
    if action is None:
        raise HTTPException(status_code=404)
    title = _advert_title(kind)
    actor = page.actor(request)
    unverified = page.unverified(request, action=action, target=entity_id, title=title)
    if unverified is not None:
        return unverified

    def refuse(reason: str, status_code: int, **fields: object) -> HTMLResponse:
        audit(
            page.logger,
            action=action,
            target=entity_id,
            outcome="refused",
            actor=actor,
            reason=reason,
            **fields,
        )
        return page.page(
            request,
            "admin/refused.html",
            title=title,
            refusal=None if status_code == 403 else reason,
            status_code=status_code,
        )

    stub = _loaded(page, entity_id)
    if stub is None:
        return refuse(NOT_HELD, 404)
    named = {"entity_name": stub.name, "node_hash": stub.node_hash}
    if not page.nonces.spend(nonce, action, entity_id):
        return refuse("no confirmation was minted for this action", 403, **named)
    refusal = advert_refusal(page, stub, action)
    if refusal is not None:
        reason, fields = refusal
        return refuse(reason, 409, **fields, **named)

    adverts = page.state.adverts
    if action == ADVERT_FLOOD:
        adverts.request_flood(stub)
    else:
        adverts.request_zero_hop(stub)
    next_flood_at = stub.next_flood_at
    audit(
        page.logger,
        action=action,
        target=entity_id,
        outcome="success",
        actor=actor,
        next_flood_at=None if next_flood_at is None else next_flood_at.isoformat(),
        **named,
    )
    page.say(render_advert_request(kind, stub.name, actor=actor, next_flood_at=next_flood_at))
    return RedirectResponse("/admin/identities", status_code=SEE_OTHER)


def advert_refusal(page: Panel, stub: object, action: str) -> tuple[str, dict[str, object]] | None:
    """Why this advert cannot go out now, or `None` when it can.

    The same four refusals `advert` applies, in the same order, factored out so
    that the rename form's advert offer is decided by *this* and cannot drift
    from the standalone confirmation (`web-admin`). It decides nothing about
    nonces and submits nothing: the caller owns both.
    """
    if not page.state.scheduler.transmit_enabled:
        return GATE_CLOSED, {}
    if page.state.radio is None:
        return NO_RADIO, {}
    if action == ADVERT_FLOOD:
        adverts = page.state.adverts
        gap = adverts.flood_gap_remaining(adverts.clock.now())
        if gap > 0:
            seconds = math.ceil(gap)
            return (
                "a flood advert from this run went out less than the "
                f"{adverts.min_entity_gap_seconds:g} s inter-entity gap ago; another "
                f"flood is accepted in {seconds} s",
                {"gap_remaining_seconds": seconds},
            )
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


# --- Webhooks (webhook-notifications, `web-admin`) ---------------------------
#
# Every write is `WebhookRepository`'s own call, which is where the command line
# validates too: a URL, a trigger or a hop limit this refuses is one
# `sighop webhook` refuses in the same words. A submitted URL is never put back
# into a page — not after it is stored, and not in a form re-shown after a
# refusal — because the URL is the credential that lets anyone post to it.

WEBHOOKS_NEED_DURABLE_STORAGE = (
    "Webhooks are stored configuration and require durable storage. This run has "
    "no database, so none can be configured or sent."
)

WEBHOOK_URL_NEEDS_THE_SECRET = (
    "SIGHOP_SECRET_KEY is not available to this panel, and webhook URLs are "
    "sealed under it; nothing was changed"
)


@router.get("/webhooks", response_class=HTMLResponse)
async def webhooks(
    request: Request,
    page: PanelDep,
    *,
    refusal: Refusal | None = None,
    test_result: dict[str, object] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Every webhook with its target as scheme and host, and its last outcomes."""
    from sighop.db.repositories import WebhookRecord
    from sighop.webhooks.config import WebhookFormat
    from sighop.webhooks.triggers import Trigger

    listed: Outcome[list[WebhookRecord]] | None = None
    if page.persistence is not None:
        listed = await page.persistence.webhooks.list_all()
    dispatcher = page.state.webhooks
    return page.page(
        request,
        "admin/webhooks.html",
        webhooks=collection_for(listed, degraded="webhooks cannot be read"),
        no_database=page.persistence is None,
        no_database_note=WEBHOOKS_NEED_DURABLE_STORAGE,
        counters=None if dispatcher is None else dispatcher.as_json(),
        triggers=[trigger.value for trigger in Trigger],
        formats=[kind.value for kind in WebhookFormat],
        refusal=refusal,
        test_result=test_result,
        status_code=status_code,
    )


async def _refuse_webhook(
    request: Request, page: Panel, reason: str, *, field: str = "", **submitted: str
) -> HTMLResponse:
    return await webhooks(
        request, page, refusal=refused(reason, field=field, **submitted), status_code=400
    )


async def _webhook(page: Panel, webhook_id: str):  # type: ignore[no-untyped-def]
    if page.persistence is None:
        return None
    try:
        wanted = uuid.UUID(webhook_id)
    except ValueError:
        return None
    found = await page.persistence.webhooks.get_by_id(wanted)
    return found.value if isinstance(found, Succeeded) else None


@router.post("/webhooks/create", response_model=None)
async def create_webhook(
    request: Request,
    page: PanelDep,
    name: Annotated[str, Form()] = "",
    url: Annotated[str, Form()] = "",
    format: Annotated[str, Form()] = "",
    triggers: Annotated[list[str] | None, Form()] = None,
    max_hops: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop webhook add`'s own call. The URL is not carried back on refusal."""
    from sighop.webhooks.config import WebhookConfigError

    submitted = {
        "name": name,
        "format": format,
        "triggers": ",".join(triggers or ()),
        "max_hops": max_hops,
    }
    if page.persistence is None:
        return await _refuse_webhook(request, page, WEBHOOKS_NEED_DURABLE_STORAGE, **submitted)
    if page.sealing_secret is None:
        return await _refuse_webhook(request, page, WEBHOOK_URL_NEEDS_THE_SECRET, **submitted)
    try:
        created = await page.persistence.webhooks.create(
            name=name,
            url=url,
            format=format,
            triggers=triggers or (),
            max_hops=max_hops,
            secret=page.sealing_secret,
        )
    except WebhookConfigError as exc:
        return await _refuse_webhook(request, page, str(exc), **submitted)
    if isinstance(created, Failed):
        return await _refuse_webhook(request, page, str(created.error), **submitted)
    return RedirectResponse("/admin/webhooks", status_code=SEE_OTHER)


@router.post("/webhooks/{webhook_id}/enabled")
async def set_webhook_enabled(
    webhook_id: str, enabled: Annotated[str, Form()], page: PanelDep
) -> RedirectResponse:
    record = await _webhook(page, webhook_id)
    if record is not None and page.persistence is not None:
        await page.persistence.webhooks.set_enabled(record.id, enabled == "true")
    return RedirectResponse("/admin/webhooks", status_code=SEE_OTHER)


@router.post("/webhooks/{webhook_id}/settings", response_model=None)
async def set_webhook_settings(
    request: Request,
    webhook_id: str,
    page: PanelDep,
    format: Annotated[str, Form()] = "",
    triggers: Annotated[list[str] | None, Form()] = None,
    max_hops: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop webhook set`'s own call: format, triggers and hop limit together.

    An empty hop limit removes the limit, as `--no-max-hops` does.
    """
    from sighop.webhooks.config import WebhookConfigError

    record = await _webhook(page, webhook_id)
    if record is None or page.persistence is None:
        return RedirectResponse("/admin/webhooks", status_code=SEE_OTHER)
    try:
        changed = await page.persistence.webhooks.update(
            record.id, triggers=triggers or (), format=format, max_hops=max_hops
        )
    except WebhookConfigError as exc:
        return await _refuse_webhook(
            request,
            page,
            f"{record.name}: {exc}. The stored webhook is unchanged.",
            field="settings",
            webhook_id=webhook_id,
        )
    if isinstance(changed, Failed):
        return await _refuse_webhook(
            request, page, str(changed.error), field="settings", webhook_id=webhook_id
        )
    return RedirectResponse("/admin/webhooks", status_code=SEE_OTHER)


@router.post("/webhooks/{webhook_id}/url", response_model=None)
async def set_webhook_url(
    request: Request,
    webhook_id: str,
    page: PanelDep,
    url: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop webhook set-url`'s own call. What was typed is never shown back."""
    from sighop.webhooks.config import WebhookConfigError

    record = await _webhook(page, webhook_id)
    if record is None or page.persistence is None:
        return RedirectResponse("/admin/webhooks", status_code=SEE_OTHER)
    if page.sealing_secret is None:
        return await _refuse_webhook(
            request, page, WEBHOOK_URL_NEEDS_THE_SECRET, field="url", webhook_id=webhook_id
        )
    try:
        changed = await page.persistence.webhooks.set_url(
            record.id, url, secret=page.sealing_secret
        )
    except WebhookConfigError as exc:
        return await _refuse_webhook(
            request,
            page,
            f"{record.name}: {exc}. The stored URL is unchanged.",
            field="url",
            webhook_id=webhook_id,
        )
    if isinstance(changed, Failed):
        return await _refuse_webhook(
            request, page, str(changed.error), field="url", webhook_id=webhook_id
        )
    return RedirectResponse("/admin/webhooks", status_code=SEE_OTHER)


@router.get("/webhooks/{webhook_id}/rename", response_class=HTMLResponse)
async def rename_webhook_form(webhook_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    record = await _webhook(page, webhook_id)
    return page.page(
        request,
        "admin/webhook_rename.html",
        webhook=record,
        refusal=None,
        status_code=200 if record is not None else 404,
    )


@router.post("/webhooks/{webhook_id}/rename", response_model=None)
async def rename_webhook(
    webhook_id: str,
    request: Request,
    page: PanelDep,
    name: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop webhook rename`'s own call. The sealed URL is not touched, and
    nothing beyond scheme and host is rendered, refusal included."""
    from sighop.webhooks.config import WebhookConfigError

    record = await _webhook(page, webhook_id)
    if record is None or page.persistence is None:
        return await rename_webhook_form(webhook_id, request, page)
    try:
        renamed = await page.persistence.webhooks.rename(record.id, name)
    except WebhookConfigError as exc:
        return page.page(
            request,
            "admin/webhook_rename.html",
            webhook=record,
            refusal=Refusal(reason=str(exc), submitted={"name": name}),
            status_code=400,
        )
    if isinstance(renamed, Failed) or renamed.value is None:
        return await rename_webhook_form(webhook_id, request, page)
    page.logger.info(
        "web_webhook_renamed",
        outcome="success",
        webhook_id=webhook_id,
        previous_name=renamed.value,
        name=name.strip(),
        actor=page.actor(request),
    )
    return RedirectResponse("/admin/webhooks", status_code=SEE_OTHER)


@router.get("/webhooks/{webhook_id}/remove", response_class=HTMLResponse)
async def remove_webhook_form(webhook_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """What removing deletes, before it deletes it."""
    record = await _webhook(page, webhook_id)
    return page.page(
        request,
        "admin/webhook_remove.html",
        webhook=record,
        status_code=200 if record is not None else 404,
    )


@router.post("/webhooks/{webhook_id}/remove", response_model=None)
async def remove_webhook(
    webhook_id: str,
    request: Request,
    page: PanelDep,
    confirm: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop webhook remove`'s own call, only with the explicit confirmation."""
    record = await _webhook(page, webhook_id)
    if record is None or page.persistence is None or confirm != "yes":
        return await remove_webhook_form(webhook_id, request, page)
    await page.persistence.webhooks.remove(record.id)
    return RedirectResponse("/admin/webhooks", status_code=SEE_OTHER)


@router.post("/webhooks/{webhook_id}/test", response_class=HTMLResponse)
async def send_webhook_sample(
    webhook_id: str,
    request: Request,
    page: PanelDep,
    trigger: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """`sighop webhook test`'s own call: one sample, no retry, outcome shown.

    CSRF-protected like every write, and not a guarded action: it neither
    transmits on air nor reveals a secret (design D8).
    """
    from sighop.webhooks.dispatcher import send_sample
    from sighop.webhooks.triggers import Trigger

    record = await _webhook(page, webhook_id)
    if record is None or page.persistence is None:
        return await webhooks(request, page, status_code=404)
    try:
        chosen = Trigger(trigger)
    except ValueError:
        return await _refuse_webhook(
            request,
            page,
            f"unknown trigger {trigger!r}; the triggers are "
            + ", ".join(item.value for item in Trigger),
            field="test",
            webhook_id=webhook_id,
        )
    if page.sealing_secret is None:
        return await _refuse_webhook(
            request, page, WEBHOOK_URL_NEEDS_THE_SECRET, field="test", webhook_id=webhook_id
        )
    opened = await page.persistence.webhooks.open_url(record.id, page.sealing_secret)
    if isinstance(opened, Failed) or opened.value is None:
        reason = str(opened.error) if isinstance(opened, Failed) else "no such webhook"
        return await _refuse_webhook(request, page, reason, field="test", webhook_id=webhook_id)
    result = await send_sample(opened.value, chosen, logger=page.logger)
    detail = result.summary
    if result.status is not None and result.reason and not result.delivered:
        detail = f"{detail}, {result.reason}"
    return await webhooks(
        request,
        page,
        test_result={
            "webhook_id": webhook_id,
            "name": record.name,
            "trigger": chosen.value,
            "delivered": result.delivered,
            "detail": detail,
        },
    )


# --- Channels (channel-messaging, `web-admin`) -------------------------------
#
# Every write is `ChannelRepository`'s own call, so a hashtag or key this refuses
# is one `sighop channel` refuses in the same words, and every change is followed
# by `reload_channels()` so this run decrypts on it at once. The list and the
# forms are on `/chat` (design D3); a refusal is re-shown there.


async def _refuse_channel(
    request: Request, page: Panel, reason: str, *, field: str = "", **submitted: str
) -> HTMLResponse:
    return await chat.render_index(
        request, page, refusal=refused(reason, field=field, **submitted), status_code=400
    )


async def _channel_added(
    request: Request, page: Panel, outcome: Outcome[object], field: str, **submitted: str
) -> RedirectResponse | HTMLResponse:
    from urllib.parse import quote

    if isinstance(outcome, Failed):
        return await _refuse_channel(request, page, str(outcome.error), field=field, **submitted)
    await page.state.reload_channels()
    name = getattr(outcome.value, "name", "")
    return RedirectResponse(f"/chat?added={quote(name)}", status_code=SEE_OTHER)


@router.post("/channels/hashtag", response_model=None)
async def add_hashtag_channel(
    request: Request,
    page: PanelDep,
    hashtag: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop channel add --hashtag`'s own call."""
    from sighop.db.repositories import ChannelConfigError

    submitted = {"hashtag": hashtag, "name": name}
    if page.persistence is None:
        return await _refuse_channel(request, page, CHANNELS_NEED_DURABLE_STORAGE, **submitted)
    try:
        outcome = await page.persistence.channels.add_hashtag(
            hashtag, name=name or None, secret=page.sealing_secret
        )
    except ChannelConfigError as exc:
        return await _refuse_channel(request, page, str(exc), field="hashtag", **submitted)
    return await _channel_added(request, page, outcome, "hashtag", **submitted)


@router.post("/channels/psk", response_model=None)
async def add_psk_channel(
    request: Request,
    page: PanelDep,
    name: Annotated[str, Form()] = "",
    key: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop channel add --psk-stdin`'s own call. The key is never carried back."""
    from sighop.db.repositories import ChannelConfigError

    if page.persistence is None:
        return await _refuse_channel(request, page, CHANNELS_NEED_DURABLE_STORAGE, name=name)
    if page.sealing_secret is None:
        return await _refuse_channel(
            request, page, CHANNEL_KEY_NEEDS_THE_SECRET, field="psk", name=name
        )
    try:
        outcome = await page.persistence.channels.add_psk(
            key, name=name, secret=page.sealing_secret
        )
    except ChannelConfigError as exc:
        return await _refuse_channel(request, page, str(exc), field="psk", name=name)
    return await _channel_added(request, page, outcome, "psk", name=name)


@router.post("/channels/public", response_model=None)
async def add_public_channel(request: Request, page: PanelDep) -> RedirectResponse | HTMLResponse:
    """`sighop channel add --public`'s own call: re-adding Public after a removal."""
    from sighop.db.repositories import ChannelConfigError

    if page.persistence is None:
        return await _refuse_channel(request, page, CHANNELS_NEED_DURABLE_STORAGE)
    try:
        outcome = await page.persistence.channels.add_public()
    except ChannelConfigError as exc:
        return await _refuse_channel(request, page, str(exc), field="public")
    return await _channel_added(request, page, outcome, "public")


async def _stored_channel(page: Panel, channel_id: str):  # type: ignore[no-untyped-def]
    if page.persistence is None:
        return None
    try:
        wanted = int(channel_id)
    except ValueError:
        return None
    found = await page.persistence.channels.get_by_id(wanted)
    return found.value if isinstance(found, Succeeded) else None


@router.get("/channels/{channel_id}/rename", response_class=HTMLResponse)
async def rename_channel_form(channel_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    record = await _stored_channel(page, channel_id)
    return page.page(
        request,
        "admin/channel_rename.html",
        channel=record,
        refusal=None,
        status_code=200 if record is not None else 404,
    )


@router.post("/channels/{channel_id}/rename", response_model=None)
async def rename_channel(
    channel_id: str,
    request: Request,
    page: PanelDep,
    name: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop channel rename`'s own call. No key material is touched, and
    none is rendered — including in the form re-shown after a refusal."""
    from sighop.db.repositories import ChannelConfigError

    record = await _stored_channel(page, channel_id)
    if record is None or page.persistence is None:
        return await rename_channel_form(channel_id, request, page)
    try:
        renamed = await page.persistence.channels.rename(record.id, name)
    except ChannelConfigError as exc:
        return page.page(
            request,
            "admin/channel_rename.html",
            channel=record,
            refusal=Refusal(reason=str(exc), submitted={"name": name}),
            status_code=400,
        )
    if isinstance(renamed, Failed) or renamed.value is None:
        return await rename_channel_form(channel_id, request, page)
    await page.state.reload_channels()
    page.logger.info(
        "web_channel_renamed",
        outcome="success",
        channel_id=channel_id,
        previous_name=renamed.value,
        name=name.strip(),
        actor=page.actor(request),
    )
    return RedirectResponse("/chat", status_code=SEE_OTHER)


@router.get("/channels/{channel_id}/remove", response_class=HTMLResponse)
async def remove_channel_form(channel_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """What removing deletes, counted, before it deletes it."""
    record = await _stored_channel(page, channel_id)
    messages: int | None = None
    if record is not None and page.persistence is not None:
        counted = await page.persistence.channels.message_count(record.id)
        messages = counted.value if isinstance(counted, Succeeded) else None
    return page.page(
        request,
        "admin/channel_remove.html",
        channel=record,
        messages=messages,
        description=ACTION_DESCRIPTIONS[REMOVE_CHANNEL],
        nonce=None if record is None else page.nonces.mint(REMOVE_CHANNEL, channel_id),
        status_code=200 if record is not None else 404,
    )


@router.post("/channels/{channel_id}/remove", response_model=None)
async def remove_channel(
    channel_id: str,
    request: Request,
    page: PanelDep,
    nonce: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """`sighop channel remove --delete-history`'s own call, behind confirm-and-nonce."""
    from urllib.parse import quote

    title = "remove channel"
    actor = page.actor(request)
    record = await _stored_channel(page, channel_id)
    if record is None or page.persistence is None:
        return await remove_channel_form(channel_id, request, page)
    if not page.nonces.spend(nonce, REMOVE_CHANNEL, channel_id):
        audit(
            page.logger,
            action=REMOVE_CHANNEL,
            target=channel_id,
            outcome="refused",
            actor=actor,
            reason="no confirmation was minted for this action",
            channel=record.name,
        )
        return page.page(request, "admin/refused.html", title=title, refusal=None, status_code=403)
    removed = await page.persistence.channels.remove(record.id)
    if isinstance(removed, Failed) or removed.value is None:
        reason = str(removed.error) if isinstance(removed, Failed) else "no such channel"
        audit(
            page.logger,
            action=REMOVE_CHANNEL,
            target=channel_id,
            outcome="failed",
            actor=actor,
            reason=reason,
            channel=record.name,
        )
        return page.page(
            request, "admin/refused.html", title=title, refusal=reason, status_code=409
        )
    await page.state.reload_channels()
    audit(
        page.logger,
        action=REMOVE_CHANNEL,
        target=channel_id,
        outcome="success",
        actor=actor,
        channel=record.name,
        messages_deleted=removed.value,
    )
    page.say(
        f"channel {record.name!r} removed with {removed.value} recorded message(s) "
        f"from the web interface by account {actor!r}"
    )
    return RedirectResponse(f"/chat?removed={quote(record.name)}", status_code=SEE_OTHER)
