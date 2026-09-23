"""The identity lifecycle through the browser (`web-admin`, §8 area 1).

Milestone 8 could list identities and enable or disable one. Everything that
brings an identity into existence, or takes one out of the platform, was a
terminal command — which made the browser a viewer of a platform administered
somewhere else.

Four things live here, and each is a repository call:

* **create** — `generate_identity()` then `EntityRepository.store`, which is
  what importing does with a key that came from a file. The panel has no
  filesystem to put a keyfile on, so creating is generating and sealing in one
  step (design D3), and the *file* is the export below.
* **show** — a stored identity's public facts, never its key.
* **import** — the document a keyfile holds, parsed by the keystore's own
  parser and sealed through the same repository call.
* **export** — a **guarded action** (design D2). An unencrypted private key leaving
  the platform is the same kind of act as revealing one, and the difference in
  protection between a file the command line creates and a browser download is
  stated at the point of export rather than in a document.

No rule is implemented here. Every refusal an operator meets — a public key
already stored, a bot's identity that adverts as something other than a chat
node, a keyfile document that does not say what it claims — is raised by the
repository or the keystore, in the words the command line prints.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.config import SECRET_KEY_COMMAND, SECRET_KEY_VARIABLE
from sighop.db.engine import Failed, Outcome, Succeeded
from sighop.db.repositories import (
    BOT_ENTITY_TYPE,
    BotRecord,
    EntityExistsError,
    EntityLoadError,
    EntityNameError,
    EntityRecord,
    EntityRoleError,
    LoadedEntity,
    RoomRecord,
    advert_config_for,
)
from sighop.db.sealing import SealError
from sighop.keystore import (
    KeyfileError,
    keyfile_bytes,
    keyfile_document,
    keyfile_from_text,
)
from sighop.protocol.identity import (
    IdentityGenerationError,
    LocalIdentity,
    PrivateKeyError,
    generate_identity,
    private_key_from_hex,
    refuse_unusable_node_hash,
)
from sighop.protocol.payloads import NodeType
from sighop.web.deps import Panel, panel
from sighop.web.guarded import (
    ACTION_DESCRIPTIONS,
    ADVERT_FLOOD,
    ADVERT_ZERO_HOP,
    EXPORT_KEY,
    REMOVE_IDENTITY,
    audit,
)
from sighop.web.render import (
    Refusal,
    advert_id,
    collection_for,
    entity_rows,
    refused,
    render_advert_request,
)

PanelDep = Annotated[Panel, Depends(panel)]

router = APIRouter(prefix="/admin/identities")

SEE_OTHER = 303

NO_SEALING_SECRET = (
    "this run holds no sealing secret, so a private key can be neither sealed nor "
    "opened here. It is SIGHOP_SECRET_KEY, and a run with a database has it"
)
"""Not a failure and not a bug: a run with no database seals nothing. Saying
which variable is missing is what an operator can act on."""

DOWNLOAD_IS_NOT_OWNER_ONLY = (
    "The command line creates a keyfile owner-only (mode 0600) in the same call "
    "that creates it, with no window in which it is broader. A file delivered to "
    "a browser has whatever protection the download directory gives it, which is "
    "usually the same as everything else there. The private key inside is identical; "
    "the protection around it is not."
)
"""Design D2. Stated at the point of export and on the identities page, because
it is the one respect in which the two surfaces genuinely differ."""

REMOVAL_IS_OFFERED_HERE = (
    "Removing a stored identity is offered here, per identity, and it is "
    "irreversible: the identity is gone unless you hold its private key "
    "elsewhere. It asks for your password and for the identity's name typed "
    "out, and it is refused outright for an identity a room or a bot is bound "
    "to, because those are deleted with it — delete them first. "
    "Disabling is the reversible action offered alongside it: it stops the "
    "identity being loaded and can be undone at any time."
)
"""`web-admin`: this used to name a terminal command and say removal was not
offered in the browser. The change `web-delete-and-rename` withdrew that
exclusion, so this says what the action is and what it costs instead. The other
four exclusions — migrations, the sealing secret, accounts and channel
pre-shared keys — are unchanged and still stated where they are looked for."""

SECRET_IS_NOT_GENERATED_HERE = (
    "The secret that seals stored identities is not generated here. It is "
    "generated once, must be kept, and must never be regenerated — losing it makes "
    "every stored identity unrecoverable — and a browser is a poor place to hand "
    f"somebody something they must not lose. Generate it with `{SECRET_KEY_COMMAND}` "
    f"and set it as {SECRET_KEY_VARIABLE}."
)
"""Task 7.3 / `web-admin`: named where an operator would look for it, rather
than left as an absence to be discovered."""

CREATABLE_NODE_TYPES = ("CHAT", "REPEATER", "ROOM_SERVER", "SENSOR")
"""What an identity may advert as. `NONE` is excluded because it is the absence
of a claim rather than one of the things a node can be."""


# --- The page -----------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def identities(
    request: Request,
    page: PanelDep,
    *,
    refusal: Refusal | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Every stored identity, with what each one is serving — and the forms.

    The identities this run *loaded* are in memory; the ones the database holds
    may be more than that, and the difference is the point of showing both.
    """
    stored = await page.persistence.entities.list_all()
    rooms = await page.persistence.rooms.list_all()
    bots = await page.persistence.bots.list_all()
    return page.page(
        request,
        "admin/identities.html",
        stored=collection_for(stored, degraded="identities cannot be read"),
        bindings=_bindings(rooms, bots),
        loaded=list(page.state.adverts.stubs),
        # `web-admin`: disabling an identity held as this viewer's default
        # states that consequence too, before it is applied.
        default_entity_id=page.session(request).default_entity_id,
        # Joined to the loaded rows by public key, the key the advert links use
        # (design D5); the node hash would join two identities that collide.
        traffic={row["public_key"]: row for row in entity_rows(page.state, page.feed)},
        advert_now=page.state.adverts.clock.now(),
        node_types=CREATABLE_NODE_TYPES,
        refusal=refusal,
        can_seal=page.sealing_secret is not None,
        no_sealing_secret=NO_SEALING_SECRET,
        download_note=DOWNLOAD_IS_NOT_OWNER_ONLY,
        secret_note=SECRET_IS_NOT_GENERATED_HERE,
        removal_note=REMOVAL_IS_OFFERED_HERE,
        status_code=status_code,
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


@router.post("/{entity_id}/enabled")
async def set_identity_enabled(
    request: Request,
    entity_id: str,
    enabled: Annotated[str, Form()],
    page: PanelDep,
) -> RedirectResponse:
    """Enable or disable one identity, through the entity repository.

    Reaches the run right after the store takes it (`web-admin`): the running
    process starts or stops holding the identity without a restart, and
    `identity.html`'s "loaded by this run" row reads that live on the next
    visit. Narrated through `page.say` the moment it happens, the channel
    `remove_identity` and `delete_room` already use for their own outcomes.
    """
    record = await _entity(page, entity_id)
    if record is not None:
        turning_on = enabled == "true"
        await page.persistence.entities.set_enabled(record.public_key, turning_on)
        await page.state.reconcile_entities()
        held = any(
            stub.identity.public_key == record.public_key for stub in page.state.adverts.stubs
        )
        page.say(
            f"identity {record.name!r} ({record.public_key.hex()[:16]}) "
            f"{'enabled' if turning_on else 'disabled'} through the web interface by account "
            f"{page.actor(request)!r}: this run "
            f"{'now holds it and advertises for it' if held else 'no longer holds it'}"
        )
    return RedirectResponse("/admin/identities", status_code=SEE_OTHER)


# --- 2.1 Creating -------------------------------------------------------------


@router.post("/create", response_model=None)
async def create_identity(
    request: Request,
    page: PanelDep,
    name: Annotated[str, Form()] = "",
    node_type: Annotated[str, Form()] = "CHAT",
    role: Annotated[str, Form()] = "node",
    private_key: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """Generate an identity and seal it, in the process that stores it (D3).

    Generating a key and sealing it are one step here: the browser has no
    filesystem to hold a keyfile between them, so creating does both at once and
    the *file* is the export.

    The generated key avoids every node hash this run knows about, exactly as
    `create_keyfile` does: §3 rule 3 refuses two local identities sharing one,
    and a panel-created identity must not be what makes a later run fail to
    start.

    `private_key` supplies an identity the operator already holds instead of
    generating one. It goes through the command line's own validation, so the
    same key is refused here for the same reason in the same words, and it is
    deliberately absent from `submitted`: every other field is echoed back into
    the form on a refusal, and key material must not be.
    """
    submitted = {"name": name, "node_type": node_type, "role": role}
    if page.sealing_secret is None:
        return await _refuse(request, page, NO_SEALING_SECRET, **submitted)
    try:
        wanted = NodeType[node_type]
    except KeyError:
        return await _refuse(
            request,
            page,
            f"{node_type!r} is not a node type; this build has {', '.join(CREATABLE_NODE_TYPES)}",
            field="node_type",
            **submitted,
        )

    taken = await _taken_hashes(page)
    if private_key.strip():
        try:
            identity = private_key_from_hex(private_key)
            refuse_unusable_node_hash(identity, avoid_node_hashes=taken)
        except PrivateKeyError as exc:
            return await _refuse(request, page, str(exc), field="private_key", **submitted)
    else:
        try:
            identity = generate_identity(avoid_node_hashes=taken)
        except IdentityGenerationError as exc:
            return await _refuse(request, page, str(exc), **submitted)

    as_bot = role == "bot"
    try:
        outcome = await page.persistence.entities.store(
            name=name,
            identity=identity,
            secret=page.sealing_secret,
            node_type=wanted,
            entity_type=BOT_ENTITY_TYPE if as_bot else None,
            advert_config=advert_config_for(NodeType.CHAT) if as_bot else None,
        )
    except (EntityExistsError, EntityRoleError, EntityNameError) as exc:
        return await _refuse(request, page, str(exc), **submitted)
    if isinstance(outcome, Failed):
        return await _refuse(request, page, str(outcome.error), **submitted)
    await _reconcile_and_report_entity(page, identity, name, actor=page.actor(request))
    return RedirectResponse(f"/admin/identities/{outcome.value.id}", status_code=SEE_OTHER)


async def _taken_hashes(page: Panel) -> frozenset[int]:
    """Every node hash this run knows about — loaded and stored.

    Both halves, because §3 rule 3 is about the identities one run holds and a
    run loads stored ones: generating against only the loaded set would let the
    panel create the collision that stops the next start (design D3).
    """
    taken = {stub.node_hash for stub in page.state.adverts.stubs}
    listed = await page.persistence.entities.list_all()
    if isinstance(listed, Succeeded):
        taken.update(record.node_hash for record in listed.value)
    return frozenset(taken)


# --- 2.3 Importing ------------------------------------------------------------


@router.post("/import", response_model=None)
async def import_identity(
    request: Request,
    page: PanelDep,
    document: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "node",
) -> RedirectResponse | HTMLResponse:
    """Seal a keyfile's private key into the store — the repository's own call.

    The document is parsed by the keystore's own parser, so a file the command
    line refuses is refused here for the same reason in the same words, and
    nothing is stored when it is.
    """
    submitted = {"document": document, "role": role}
    if page.sealing_secret is None:
        return await _refuse(request, page, NO_SEALING_SECRET, **submitted)
    try:
        keyfile = keyfile_from_text(document, "the submitted keyfile")
    except KeyfileError as exc:
        return await _refuse(request, page, str(exc), field="document", **submitted)

    as_bot = role == "bot"
    try:
        outcome = await page.persistence.entities.store(
            name=keyfile.name,
            identity=keyfile.identity,
            secret=page.sealing_secret,
            node_type=keyfile.node_type,
            entity_type=BOT_ENTITY_TYPE if as_bot else None,
            advert_config=advert_config_for(NodeType.CHAT) if as_bot else None,
        )
    except (EntityExistsError, EntityRoleError, EntityNameError) as exc:
        return await _refuse(request, page, str(exc), **submitted)
    if isinstance(outcome, Failed):
        return await _refuse(request, page, str(outcome.error), **submitted)
    await _reconcile_and_report_entity(
        page, keyfile.identity, keyfile.name, actor=page.actor(request)
    )
    return RedirectResponse(f"/admin/identities/{outcome.value.id}", status_code=SEE_OTHER)


async def _refuse(
    request: Request, page: Panel, reason: str, *, field: str = "", **submitted: str
) -> HTMLResponse:
    """Re-render the page with the reason and what the author typed (task 1.1)."""
    return await identities(
        request,
        page,
        refusal=refused(reason, field=field, **submitted),
        status_code=400,
    )


async def _reconcile_and_report_entity(
    page: Panel, identity: LocalIdentity, name: str, *, actor: str
) -> bool:
    """Call the live seam after a create or import, and say what happened.

    `web-admin`: the interface states the effect on the running process in
    the result of the write — held and advertising, or stored but not taken
    up, and why. `identity.html` already reads `adverts.stubs` live on every
    visit (`_is_loaded`), so the page an operator lands on next reflects this
    at once regardless; `page.say` is what narrates it to the run's own
    output the moment it happens, the same channel `remove_identity` and
    `delete_room` already use for their own outcomes.
    """
    await page.state.reconcile_entities()
    held = any(stub.identity.public_key == identity.public_key for stub in page.state.adverts.stubs)
    if held:
        page.say(
            f"identity {name!r} ({identity.public_key.hex()[:16]}) created through the web "
            f"interface by account {actor!r}: now held by this run and advertising"
        )
        return True
    reason = page.state.adverts.admission_refusal(identity, name) or ("not taken up by this run")
    page.say(
        f"identity {name!r} ({identity.public_key.hex()[:16]}) created through the web "
        f"interface by account {actor!r}: stored, but this run did not take it up ({reason})"
    )
    return False


# --- 2.2 One identity ---------------------------------------------------------


@router.get("/{entity_id}", response_class=HTMLResponse)
async def identity(
    entity_id: str, request: Request, page: PanelDep, bot_deleted: str = ""
) -> HTMLResponse:
    """One stored identity's public facts.

    No private key and no ciphertext: `EntityRecord` holds neither, so there is no
    rendering path along which either could escape.
    """
    return await render_identity(request, page, entity_id, bot_deleted=bot_deleted)


async def render_identity(
    request: Request,
    page: Panel,
    entity_id: str,
    *,
    bot_deleted: str = "",
    refusal: Refusal | None = None,
    status_code: int | None = None,
) -> HTMLResponse:
    """One identity's page, for a GET and for a refused bot write alike (design D4).

    A bot is configured here because a bot *is* the identity it runs on: it has
    no name of its own, and one identity plays one role, so there is at most one.
    """
    record = await _entity(page, entity_id)
    if status_code is None:
        status_code = 200 if record is not None else 404
    return page.page(
        request,
        "admin/identity.html",
        record=record,
        serving=_serving(await _bindings_for(page), entity_id),
        loaded=_is_loaded(page, record),
        can_seal=page.sealing_secret is not None,
        no_sealing_secret=NO_SEALING_SECRET,
        download_note=DOWNLOAD_IS_NOT_OWNER_ONLY,
        **await _bot_context(page, record),
        bot_deleted=bot_deleted,
        refusal=refusal,
        status_code=status_code,
    )


async def _bot_context(page: Panel, record: EntityRecord | None) -> dict[str, object]:
    """The bot this identity carries, or the offer to create one.

    Only for an identity stored as a bot: being a bot is an explicit choice at
    creation and never a side effect of having a bot bound to it.
    """
    from sighop.bots import drivers as bot_drivers

    context: dict[str, object] = {"is_bot": False, "bot": None}
    if record is None or record.type != BOT_ENTITY_TYPE:
        return context
    context["is_bot"] = True
    context["drivers"] = bot_drivers.driver_names()
    listed = await page.persistence.bots.list_all()
    if isinstance(listed, Failed):
        context["bots_unreadable"] = True
        return context
    bot = next((bot for bot in listed.value if bot.entity_id == record.id), None)
    if bot is None:
        return context
    stored = await page.persistence.bot_state.list(bot.id)
    running = {worker.name for worker in page.state.bots.workers}
    context.update(
        bot=bot,
        bot_state=stored.value if isinstance(stored, Succeeded) else {},
        running=bot.entity_name in running,
        advert_id=advert_id(page.state.adverts.stubs, record.public_key)
        if bot.entity_name in running
        else None,
    )
    return context


async def _bindings_for(page: Panel) -> dict[str, list[str]]:
    return _bindings(
        await page.persistence.rooms.list_all(), await page.persistence.bots.list_all()
    )


def _serving(bindings: dict[str, list[str]], entity_id: str) -> list[str]:
    return bindings.get(entity_id, [])


def _is_loaded(page: Panel, record: EntityRecord | None) -> bool:
    if record is None:
        return False
    return any(stub.identity.public_key == record.public_key for stub in page.state.adverts.stubs)


async def _entity(page: Panel, entity_id: str) -> EntityRecord | None:
    try:
        uuid.UUID(entity_id)
    except ValueError:
        return None
    listed = await page.persistence.entities.list_all()
    if isinstance(listed, Failed):
        return None
    for record in listed.value:
        if str(record.id) == entity_id:
            return record
    return None


# --- Renaming and removing a stored identity ----------------------------------


RENAME_IS_ON_THE_AIR = (
    "This name travels in this identity's adverts and is the sender name of "
    "every channel post it makes. Neighbours keep showing the old name until "
    "it adverts again."
)

ADVERT_KINDS = {"zero-hop": ADVERT_ZERO_HOP, "flood": ADVERT_FLOOD}


@router.get("/{entity_id}/rename", response_class=HTMLResponse)
async def rename_form(entity_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """The rename, with the advert offer that makes the new name propagate.

    One nonce per advert kind is minted here rather than one for the choice,
    because the choice is not known until the form is submitted; the handler
    spends only the one the operator picked.
    """
    record = await _entity(page, entity_id)
    stub = _stub_for(page, record)
    return page.page(
        request,
        "admin/identity_rename.html",
        record=record,
        loaded=stub is not None,
        on_the_air=RENAME_IS_ON_THE_AIR,
        not_held=NOT_HELD_HERE,
        zero_hop_description=ACTION_DESCRIPTIONS[ADVERT_ZERO_HOP],
        flood_description=ACTION_DESCRIPTIONS[ADVERT_FLOOD],
        next_flood_at=None if stub is None else stub.next_flood_at,
        zero_hop_nonce=(
            None if stub is None else page.nonces.mint(ADVERT_ZERO_HOP, stub.entity_id)
        ),
        flood_nonce=None if stub is None else page.nonces.mint(ADVERT_FLOOD, stub.entity_id),
        refusal=None,
        status_code=200 if record is not None else 404,
    )


@router.post("/{entity_id}/rename", response_model=None)
async def rename_identity(
    request: Request,
    entity_id: str,
    page: PanelDep,
    name: Annotated[str, Form()] = "",
    advert: Annotated[str, Form()] = "none",
    zero_hop_nonce: Annotated[str, Form()] = "",
    flood_nonce: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Rename, then offer the advert. In that order, and independently.

    The rename is applied first so the advert carries the new name, and a
    refused advert never rolls it back — the two outcomes are reported
    separately because they are two things that either happened or did not.
    No nonce and no password guard the rename itself: it is reversible by
    renaming back (design D6).
    """
    record = await _entity(page, entity_id)
    if record is None:
        return await rename_form(entity_id, request, page)

    def refuse(reason: str) -> HTMLResponse:
        return page.page(
            request,
            "admin/identity_rename.html",
            record=record,
            loaded=_stub_for(page, record) is not None,
            on_the_air=RENAME_IS_ON_THE_AIR,
            not_held=NOT_HELD_HERE,
            zero_hop_description=ACTION_DESCRIPTIONS[ADVERT_ZERO_HOP],
            flood_description=ACTION_DESCRIPTIONS[ADVERT_FLOOD],
            next_flood_at=None,
            zero_hop_nonce=None,
            flood_nonce=None,
            refusal=Refusal(reason=reason, submitted={"name": name}),
            status_code=400,
        )

    # Refused before the store is touched: a keyfile identity is loaded and not
    # stored, so the repository's own duplicate check cannot see this one.
    clash = page.state.adverts.name_clash(record.public_key, name.strip())
    if clash is not None:
        return refuse(
            f"the identity {clash.name!r} loaded by this run is already called "
            f"{name.strip()!r}; two loaded identities may not share a name, and "
            "nothing was renamed"
        )
    try:
        renamed = await page.persistence.entities.rename(record.public_key, name)
    except EntityNameError as exc:
        return refuse(str(exc))
    if isinstance(renamed, Failed) or renamed.value is None:
        return refuse("the identity could not be renamed")

    previous = renamed.value
    applied = name.strip()
    reached_the_run = page.state.rename_entity(record.public_key, applied)
    page.logger.info(
        "web_identity_renamed",
        outcome="success",
        entity_id=entity_id,
        previous_name=previous,
        name=applied,
        reached_the_run=reached_the_run,
        actor=page.actor(request),
    )

    advert_outcome = await _advert_after_rename(
        request, page, entity_id, applied, advert, zero_hop_nonce, flood_nonce
    )
    return page.page(
        request,
        "admin/identity_renamed.html",
        record=record,
        previous=previous,
        new_name=applied,
        reached_the_run=reached_the_run,
        on_the_air=RENAME_IS_ON_THE_AIR,
        advert=advert if advert in ADVERT_KINDS else None,
        advert_outcome=advert_outcome,
    )


async def _advert_after_rename(
    request: Request,
    page: Panel,
    entity_id: str,
    name: str,
    kind: str,
    zero_hop_nonce: str,
    flood_nonce: str,
) -> str | None:
    """Submit the advert the operator chose, or say why it was not submitted.

    Returns `None` when none was asked for. Every refusal is the one the
    standalone confirmation would give, because `advert_refusal` is the same
    function both call, and each is audited as its own event exactly as a
    standalone advert is.
    """
    from sighop.web.routes.admin import advert_refusal

    action = ADVERT_KINDS.get(kind)
    if action is None:
        return None
    actor = page.actor(request)
    stub = next(
        (
            stub
            for stub in page.state.adverts.stubs
            if stub.name == name or stub.entity_id == entity_id
        ),
        None,
    )

    def refused(reason: str, **fields: object) -> str:
        audit(
            page.logger,
            action=action,
            target=entity_id,
            outcome="refused",
            actor=actor,
            reason=reason,
            **fields,
        )
        return reason

    if stub is None:
        return refused(NOT_HELD_HERE)
    nonce = zero_hop_nonce if action == ADVERT_ZERO_HOP else flood_nonce
    if not page.nonces.spend(nonce, action, stub.entity_id):
        return refused("no confirmation was minted for this action", entity_name=stub.name)
    refusal = advert_refusal(page, stub, action)
    if refusal is not None:
        reason, fields = refusal
        return refused(reason, entity_name=stub.name, **fields)

    if action == ADVERT_FLOOD:
        page.state.adverts.request_flood(stub)
    else:
        page.state.adverts.request_zero_hop(stub)
    audit(
        page.logger,
        action=action,
        target=entity_id,
        outcome="success",
        actor=actor,
        entity_name=stub.name,
        next_flood_at=None if stub.next_flood_at is None else stub.next_flood_at.isoformat(),
    )
    page.say(render_advert_request(kind, stub.name, actor=actor, next_flood_at=stub.next_flood_at))
    return None


NOT_HELD_HERE = "this run does not hold that identity, so there is nothing to advert as"


def _stub_for(page: Panel, record: EntityRecord | None):  # type: ignore[no-untyped-def]
    if record is None:
        return None
    for stub in page.state.adverts.stubs:
        if stub.identity.public_key == record.public_key:
            return stub
    return None


@router.get("/{entity_id}/remove", response_class=HTMLResponse)
async def remove_form(entity_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """The confirmation in front of a removal. States what it costs.

    The refusal for a bound identity is decided here as well as on submission,
    so an operator sees what the identity serves before typing anything rather
    than after.
    """
    record = await _entity(page, entity_id)
    serving = _serving(await _bindings_for(page), entity_id)
    return page.page(
        request,
        "admin/identity_remove.html",
        record=record,
        serving=serving,
        loaded=_is_loaded(page, record),
        description=ACTION_DESCRIPTIONS[REMOVE_IDENTITY],
        refusal=None,
        nonce=(None if record is None or serving else page.nonces.mint(REMOVE_IDENTITY, entity_id)),
        status_code=200 if record is not None else 404,
    )


@router.post("/{entity_id}/remove", response_model=None)
async def remove_identity(
    entity_id: str,
    request: Request,
    page: PanelDep,
    nonce: Annotated[str, Form()] = "",
    password: Annotated[str | None, Form()] = None,
    confirm_name: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """The repository's own call, at the tier reveal and export are in.

    Three gates, not one: the nonce, the operator's password, and the identity's
    name typed out — the last because the command line asks for it and this
    destroys the same thing in the same unrecoverable way. The refusal for a
    bound identity is the repository's rule, applied here in the command line's
    own words.
    """
    from urllib.parse import quote

    title = "remove an identity"
    actor = page.actor(request)
    record = await _entity(page, entity_id)
    if record is None or not page.nonces.spend(nonce, REMOVE_IDENTITY, entity_id):
        audit(
            page.logger,
            action=REMOVE_IDENTITY,
            target=entity_id,
            outcome="refused",
            actor=actor,
            reason="no confirmation was minted for this action",
        )
        return page.page(request, "admin/refused.html", title=title, status_code=403)
    refusal = await page.reauthenticate(
        request,
        action=REMOVE_IDENTITY,
        target=entity_id,
        password=password,
        title=title,
        entity_name=record.name,
    )
    if refusal is not None:
        return refusal

    serving = _serving(await _bindings_for(page), entity_id)
    if serving:
        reason = (
            f"identity {record.name!r} is serving {' and '.join(serving)}, which "
            "would be deleted with it. Remove them first; nothing was removed"
        )
        return await _refuse_removal(request, page, record, serving, reason, entity_id, actor)
    if confirm_name.strip() != record.name:
        return await _refuse_removal(
            request,
            page,
            record,
            serving,
            f"that is not the identity's name; type {record.name!r} to remove it. "
            "Nothing was removed",
            entity_id,
            actor,
        )

    removed = await page.persistence.entities.remove(record.public_key)
    if isinstance(removed, Failed) or not removed.value:
        reason = str(removed.error) if isinstance(removed, Failed) else "no such identity"
        return await _refuse_removal(request, page, record, serving, reason, entity_id, actor)
    audit(
        page.logger,
        action=REMOVE_IDENTITY,
        target=entity_id,
        outcome="success",
        actor=actor,
        entity_name=record.name,
        public_key=record.public_key.hex(),
    )
    await page.state.reconcile_entities()
    page.say(
        f"identity {record.name!r} ({record.public_key.hex()[:16]}) removed from "
        f"the web interface by account {actor!r}: this run no longer holds it or advertises "
        "for it"
    )
    return RedirectResponse(
        f"/admin/identities?removed={quote(record.name)}", status_code=SEE_OTHER
    )


async def _refuse_removal(
    request: Request,
    page: Panel,
    record: EntityRecord,
    serving: list[str],
    reason: str,
    entity_id: str,
    actor: str,
) -> HTMLResponse:
    """One exit for every refusal after the nonce: recorded, then re-rendered.

    The nonce is already spent by the time any of these are reached, so the
    page mints a fresh one — a refused attempt must not leave a live
    confirmation behind, and must not make the operator start over either.
    """
    audit(
        page.logger,
        action=REMOVE_IDENTITY,
        target=entity_id,
        outcome="refused",
        actor=actor,
        reason=reason,
        entity_name=record.name,
    )
    return page.page(
        request,
        "admin/identity_remove.html",
        record=record,
        serving=serving,
        loaded=_is_loaded(page, record),
        description=ACTION_DESCRIPTIONS[REMOVE_IDENTITY],
        refusal=Refusal(reason=reason, submitted={}),
        nonce=None if serving else page.nonces.mint(REMOVE_IDENTITY, entity_id),
        status_code=409,
    )


# --- 3. Exporting, which is a guarded action ----------------------------------


@router.get("/{entity_id}/export", response_class=HTMLResponse)
async def export_form(entity_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """The confirmation in front of an export. Contains no key material."""
    record = await _entity(page, entity_id)
    return page.page(
        request,
        "admin/guarded.html",
        action=EXPORT_KEY,
        target=entity_id,
        title="export a private key",
        subject=None if record is None else record.name,
        description=ACTION_DESCRIPTIONS[EXPORT_KEY],
        post_to=f"/admin/identities/{entity_id}/export",
        nonce=page.nonces.mint(EXPORT_KEY, entity_id),
        extra_note=DOWNLOAD_IS_NOT_OWNER_ONLY,
        status_code=200 if record is not None else 404,
    )


@router.post("/{entity_id}/export", response_model=None)
async def export_identity(
    entity_id: str,
    request: Request,
    page: PanelDep,
    nonce: Annotated[str, Form()] = "",
    password: Annotated[str | None, Form()] = None,
) -> Response | HTMLResponse:
    """The keyfile itself, in one response, for any **stored** identity.

    Any stored one rather than any loaded one: a run loads only enabled
    entities, so exporting from the run's own opened identities would make
    "export" mean something narrower in the browser than on the command line.
    That is why design D1 passes the sealing secret rather than reusing what the
    run already holds.
    """
    actor = page.actor(request)
    record = await _entity(page, entity_id)
    if record is None or not page.nonces.spend(nonce, EXPORT_KEY, entity_id):
        audit(
            page.logger,
            action=EXPORT_KEY,
            target=entity_id,
            outcome="refused",
            actor=actor,
            reason="no confirmation was minted for this action",
        )
        return page.page(
            request, "admin/refused.html", title="export a private key", status_code=403
        )
    refusal = await page.reauthenticate(
        request,
        action=EXPORT_KEY,
        target=entity_id,
        password=password,
        title="export a private key",
        entity_name=record.name,
    )
    if refusal is not None:
        return refusal
    if page.sealing_secret is None:
        audit(
            page.logger,
            action=EXPORT_KEY,
            target=entity_id,
            outcome="refused",
            actor=actor,
            reason=NO_SEALING_SECRET,
            entity_name=record.name,
        )
        return page.page(
            request, "admin/refused.html", title="export a private key", status_code=409
        )

    opened = await _open(page, record)
    if opened is None:
        audit(
            page.logger,
            action=EXPORT_KEY,
            target=entity_id,
            outcome="refused",
            actor=actor,
            reason="the stored private key could not be opened",
            entity_name=record.name,
        )
        return page.page(
            request, "admin/refused.html", title="export a private key", status_code=409
        )

    document = keyfile_document(
        opened.identity,
        record.name,
        record.node_type,
        # The row's own creation time rather than now: this identity was created
        # then, and a document that re-dates it on every export would make two
        # exports of one identity differ in a field that is about the identity.
        created_at=record.created_at.isoformat(),
    )
    audit(
        page.logger,
        action=EXPORT_KEY,
        target=entity_id,
        outcome="success",
        actor=actor,
        entity_name=record.name,
        public_key=record.public_key.hex(),
        enabled=record.enabled,
    )
    return Response(
        content=keyfile_bytes(document),
        media_type="application/json",
        headers={
            "content-disposition": f'attachment; filename="{_filename(record)}"',
            # A keyfile is the identity. Nothing between here and the browser
            # gets to keep a copy.
            "cache-control": "no-store",
        },
    )


async def _open(page: Panel, record: EntityRecord) -> LoadedEntity | None:
    """This one stored identity, with its private key opened. Never logged."""
    assert page.sealing_secret is not None
    try:
        loaded = await page.persistence.entities.load_all(page.sealing_secret)
    except (EntityLoadError, SealError):
        return None
    if isinstance(loaded, Failed):
        return None
    for entity in loaded.value:
        if entity.record.id == record.id:
            return entity
    return None


def _filename(record: EntityRecord) -> str:
    """A filename a browser will accept, from a name an operator chose.

    Conservative on purpose: a name travels from an advert into a header here,
    and a header is not a place to find out what a peer put in a name field.
    """
    safe = "".join(
        character if character.isalnum() or character in "-_" else "-" for character in record.name
    ).strip("-")
    return f"{safe or 'identity'}.json"
