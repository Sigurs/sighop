"""The identity lifecycle through the browser (`web-admin`, §8 area 1).

Milestone 8 could list identities and enable or disable one. Everything that
brings an identity into existence, or takes one out of the platform, was a
terminal command — which made the browser a viewer of a platform administered
somewhere else.

Four things live here and each is the call the equivalent `sighop keys`
subcommand makes:

* **create** — `generate_identity()` then `EntityRepository.store`, which is
  what `keys import` does with a key that came from a file. The panel has no
  filesystem to put a keyfile on, so creating is generating and sealing in one
  step (design D3), and the *file* is the export below.
* **show** — `keys show` for a stored identity rather than for a file.
* **import** — the document a keyfile holds, parsed by the keystore's own
  parser and sealed through the same repository call.
* **export** — a **guarded action** (design D2). An unencrypted seed leaving
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

from sighop.db.engine import Failed, Outcome, Succeeded
from sighop.db.repositories import (
    BOT_ENTITY_TYPE,
    BotRecord,
    EntityExistsError,
    EntityLoadError,
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
from sighop.protocol.identity import IdentityGenerationError, generate_identity
from sighop.protocol.payloads import NodeType
from sighop.web.deps import Panel, panel
from sighop.web.guarded import ACTION_DESCRIPTIONS, EXPORT_KEY, audit
from sighop.web.render import (
    NO_DATABASE,
    Refusal,
    collection_for,
    refused,
)

PanelDep = Annotated[Panel, Depends(panel)]

router = APIRouter(prefix="/admin/identities")

SEE_OTHER = 303

NO_SEALING_SECRET = (
    "this run holds no sealing secret, so a seed can be neither sealed nor "
    "opened here. It is SIGHOP_SECRET_KEY, and a run with a database has it"
)
"""Not a failure and not a bug: a run with no database seals nothing. Saying
which variable is missing is what an operator can act on."""

DOWNLOAD_IS_NOT_OWNER_ONLY = (
    "The command line creates a keyfile owner-only (mode 0600) in the same call "
    "that creates it, with no window in which it is broader. A file delivered to "
    "a browser has whatever protection the download directory gives it, which is "
    "usually the same as everything else there. The seed inside is identical; "
    "the protection around it is not."
)
"""Design D2. Stated at the point of export and on the identities page, because
it is the one respect in which the two surfaces genuinely differ."""

SECRET_IS_A_TERMINAL_COMMAND = (
    "The secret that seals stored identities is not generated here. It is "
    "printed once, must be kept, and must never be regenerated — losing it makes "
    "every stored identity unrecoverable — and a browser is a poor place to hand "
    "somebody something they must not lose. Generate it with `sighop keys secret`."
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
        stored=collection_for(stored, degraded="identities cannot be read"),
        bindings=_bindings(rooms, bots),
        loaded=list(page.state.adverts.stubs),
        node_types=CREATABLE_NODE_TYPES,
        refusal=refusal,
        can_seal=page.sealing_secret is not None,
        no_sealing_secret=NO_SEALING_SECRET,
        download_note=DOWNLOAD_IS_NOT_OWNER_ONLY,
        secret_note=SECRET_IS_A_TERMINAL_COMMAND,
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


# --- 2.1 Creating -------------------------------------------------------------


@router.post("/create", response_model=None)
async def create_identity(
    request: Request,
    page: PanelDep,
    name: Annotated[str, Form()] = "",
    node_type: Annotated[str, Form()] = "CHAT",
    role: Annotated[str, Form()] = "node",
) -> RedirectResponse | HTMLResponse:
    """Generate an identity and seal it, in the process that stores it (D3).

    `sighop keys new` writes a keyfile and `sighop keys import` seals it; the
    browser has no filesystem between the two, so creating does both at once and
    the *file* is the export. Neither surface gains a step the other lacks.

    The generated key avoids every node hash this run knows about, exactly as
    `create_keyfile` does: §3 rule 3 refuses two local identities sharing one,
    and a panel-created identity must not be what makes a later run fail to
    start.
    """
    submitted = {"name": name, "node_type": node_type, "role": role}
    if page.persistence is None:
        return await _refuse(request, page, NO_DATABASE, **submitted)
    if page.sealing_secret is None:
        return await _refuse(request, page, NO_SEALING_SECRET, **submitted)
    try:
        wanted = NodeType[node_type]
    except KeyError:
        return await _refuse(
            request,
            page,
            f"{node_type!r} is not a node type; this build has "
            f"{', '.join(CREATABLE_NODE_TYPES)}",
            field="node_type",
            **submitted,
        )

    try:
        identity = generate_identity(avoid_node_hashes=await _taken_hashes(page))
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
    except (EntityExistsError, EntityRoleError) as exc:
        return await _refuse(request, page, str(exc), **submitted)
    if isinstance(outcome, Failed):
        return await _refuse(request, page, str(outcome.error), **submitted)
    return RedirectResponse(
        f"/admin/identities/{outcome.value.id}", status_code=SEE_OTHER
    )


async def _taken_hashes(page: Panel) -> frozenset[int]:
    """Every node hash this run knows about — loaded and stored.

    Both halves, because §3 rule 3 is about the identities one run holds and a
    run loads stored ones: generating against only the loaded set would let the
    panel create the collision that stops the next start (design D3).
    """
    taken = {stub.node_hash for stub in page.state.adverts.stubs}
    if page.persistence is not None:
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
    """Seal a keyfile's seed into the store — `sighop keys import`'s own call.

    The document is parsed by the keystore's own parser, so a file the command
    line refuses is refused here for the same reason in the same words, and
    nothing is stored when it is.
    """
    submitted = {"document": document, "role": role}
    if page.persistence is None:
        return await _refuse(request, page, NO_DATABASE, **submitted)
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
    except (EntityExistsError, EntityRoleError) as exc:
        return await _refuse(request, page, str(exc), **submitted)
    if isinstance(outcome, Failed):
        return await _refuse(request, page, str(outcome.error), **submitted)
    return RedirectResponse(
        f"/admin/identities/{outcome.value.id}", status_code=SEE_OTHER
    )


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


# --- 2.2 One identity ---------------------------------------------------------


@router.get("/{entity_id}", response_class=HTMLResponse)
async def identity(entity_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """`sighop keys show`, for a stored identity rather than for a file.

    No seed and no ciphertext: `EntityRecord` holds neither, so there is no
    rendering path along which either could escape.
    """
    record = await _entity(page, entity_id)
    return page.page(
        request,
        "admin/identity.html",
        record=record,
        serving=_serving(await _bindings_for(page), entity_id),
        loaded=_is_loaded(page, record),
        can_seal=page.sealing_secret is not None,
        no_sealing_secret=NO_SEALING_SECRET,
        download_note=DOWNLOAD_IS_NOT_OWNER_ONLY,
        status_code=200 if record is not None else 404,
    )


async def _bindings_for(page: Panel) -> dict[str, list[str]]:
    if page.persistence is None:
        return {}
    return _bindings(
        await page.persistence.rooms.list_all(), await page.persistence.bots.list_all()
    )


def _serving(bindings: dict[str, list[str]], entity_id: str) -> list[str]:
    return bindings.get(entity_id, [])


def _is_loaded(page: Panel, record: EntityRecord | None) -> bool:
    if record is None:
        return False
    return any(
        stub.identity.public_key == record.public_key
        for stub in page.state.adverts.stubs
    )


async def _entity(page: Panel, entity_id: str) -> EntityRecord | None:
    if page.persistence is None:
        return None
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


# --- 3. Exporting, which is a guarded action ----------------------------------


@router.get("/{entity_id}/export", response_class=HTMLResponse)
async def export_form(
    entity_id: str, request: Request, page: PanelDep
) -> HTMLResponse:
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
) -> Response | HTMLResponse:
    """The keyfile itself, in one response, for any **stored** identity.

    Any stored one rather than any loaded one: a run loads only enabled
    entities, so exporting from the run's own opened identities would make
    "export" mean something narrower in the browser than on the command line.
    That is why design D1 passes the sealing secret rather than reusing what the
    run already holds.
    """
    record = await _entity(page, entity_id)
    if record is None or not page.nonces.spend(nonce, EXPORT_KEY, entity_id):
        audit(
            page.logger,
            action=EXPORT_KEY,
            target=entity_id,
            outcome="refused",
            reason="no confirmation was minted for this action",
        )
        return page.page(
            request, "admin/refused.html", title="export a private key", status_code=403
        )
    if page.sealing_secret is None or page.persistence is None:
        audit(
            page.logger,
            action=EXPORT_KEY,
            target=entity_id,
            outcome="refused",
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
            reason="the stored seed could not be opened",
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
    """This one stored identity, with its seed opened. Never logged."""
    assert page.persistence is not None and page.sealing_secret is not None
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
        character if character.isalnum() or character in "-_" else "-"
        for character in record.name
    ).strip("-")
    return f"{safe or 'identity'}.json"
