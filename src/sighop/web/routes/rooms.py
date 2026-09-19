"""Browsing a room the platform serves (`web-rooms`, §8 area 3).

The one view that answers "why has this person not seen that message" without a
database client: a member's sync cursor beside the room's own history, in the
order the protocol orders it.

Everything here is **read-only with respect to the mesh** except one thing.
Opening a room, paging back through it and refreshing it transmit nothing, push
nothing, and move no member's cursor — which is not obvious from the outside,
because a room server's normal job is precisely to push history at members whose
cursor is behind. The push loop is the room server's; this module only reads
rows.

The exception is **posting**, and it is a guarded action (design D4). What
`sighop room post` does is `MessageRepository.store` plus one length check: it
does not go through `RoomServer` at all, and whichever run is serving that room
picks the row up through its own push loop. So the panel's post is the same one
call — and the fact that a post is a stored row rather than a transmission is
stated at the point of posting, because the panel is the first surface where
somebody might expect otherwise: it is showing them a running platform at the
time.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.db.engine import Failed, Succeeded
from sighop.db.repositories import EntityRecord, MemberRecord, PostRecord, RoomRecord
from sighop.net.room import POST_SYNC_DELAY_SECS, STORED_POST_TEXT_LEN
from sighop.protocol.payloads import NodeType
from sighop.web.deps import Panel, panel
from sighop.web.guarded import POST_TO_ROOM, audit
from sighop.web.render import (
    DEGRADED,
    NO_DATABASE,
    Collection,
    Refusal,
    advert_id,
    identity_for_key,
    read,
    refused,
    unreadable,
)

PanelDep = Annotated[Panel, Depends(panel)]

router = APIRouter(prefix="/rooms")

PAGE_SIZE = 50
"""Messages per page. Bounded for the reason every read is bounded here: an
unbounded one is a statement whose cost is set by how long the room has run."""

SEE_OTHER = 303


@router.get("", response_class=HTMLResponse)
async def index(request: Request, page: PanelDep, deleted: str = "") -> HTMLResponse:
    """Every room the database holds, served by this run or not.

    A room this run is not serving is still browsable: its history is in the
    database and reading it needs no identity. What it cannot do is answer a
    login, and the reason is stated rather than left as an absence.
    """
    return await render_index(request, page, deleted=deleted)


async def render_index(
    request: Request,
    page: Panel,
    *,
    deleted: str = "",
    refusal: Refusal | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The rooms page, for a GET and for a refused room write alike (design D2).

    One builder, so a refused create is shown on the page it was made from and
    with the same columns — not on a second list that has drifted from this one.
    """
    context: dict[str, object] = {
        "reasons": {},
        "served": set(),
        "counts": {},
        "advert_ids": {},
        "hosts": [],
        "database": page.persistence is not None,
        "deleted": deleted,
        "refusal": refusal,
    }
    if page.persistence is None:
        rooms: Collection[RoomRecord] = unreadable(NO_DATABASE)
    else:
        listed = await page.persistence.rooms.list_all()
        if isinstance(listed, Failed):
            rooms = unreadable(DEGRADED)
        else:
            rooms = read(listed.value)
            context.update(await rooms_context(page, listed.value))
    return page.page(request, "rooms/index.html", rooms=rooms, **context, status_code=status_code)


async def rooms_context(page: Panel, listed: list[RoomRecord]) -> dict[str, object]:
    """Served state, counts, advert links and bindable hosts for the rooms page."""
    served = {str(server.room.id) for server in page.state.rooms}
    loaded = {stub.identity.public_key for stub in page.state.adverts.stubs}
    stubs = page.state.adverts.stubs
    return {
        "reasons": {str(room.id): _unserved_reason(room, served, loaded, page) for room in listed},
        "served": served,
        "counts": {str(room.id): await room_counts(page, room) for room in listed},
        "advert_ids": {
            str(server.room.id): entity_id
            for server in page.state.rooms
            if (entity_id := advert_id(stubs, server.entity.identity.public_key)) is not None
        },
        "hosts": await _room_server_identities(page, listed),
    }


async def room_counts(page: Panel, room: RoomRecord) -> tuple[int, int]:
    """Members and stored messages, or -1 for a count that could not be read."""
    assert page.persistence is not None
    members = await page.persistence.members.load_for_room(room.id)
    messages = await page.persistence.messages.count(room.id)
    return (
        len(members.value) if isinstance(members, Succeeded) else -1,
        messages.value if isinstance(messages, Succeeded) else -1,
    )


async def _room_server_identities(page: Panel, bound: list[RoomRecord]) -> list[EntityRecord]:
    """The stored identities a room could be bound to.

    Only the ones that advert as room servers, because that is what a room runs
    on; offering the rest would be offering a choice the repository refuses.
    """
    assert page.persistence is not None
    listed = await page.persistence.entities.list_all()
    if isinstance(listed, Failed):
        return []
    taken = {room.entity_id for room in bound}
    return [
        record
        for record in listed.value
        if record.node_type is NodeType.ROOM_SERVER and record.id not in taken
    ]


def _unserved_reason(room: RoomRecord, served: set[str], loaded: set[bytes], page: Panel) -> str:
    """Why this run is not serving a room, in terms an operator can act on."""
    if str(room.id) in served:
        return ""
    return (
        "this run did not load the identity this room is bound to, so it cannot "
        "answer a login or push history — the stored history below is unaffected"
    )


@router.get("/{room_id}", response_class=HTMLResponse)
async def history(
    room_id: str,
    request: Request,
    page: PanelDep,
    before: int | None = None,
) -> HTMLResponse:
    """One page of a room's stored history, newest first.

    `before` is the oldest ordering timestamp already shown. The column is
    unique per room, so paging back cannot show a message twice or skip one.
    """
    room = await _room(page, room_id)
    if room is None or page.persistence is None:
        return page.page(
            request,
            "rooms/history.html",
            room=None,
            posts=unreadable(NO_DATABASE),
            status_code=404 if page.persistence is not None else 200,
        )
    stored = await page.persistence.messages.history(
        room.id, limit=PAGE_SIZE, newest_first=True, before=before
    )
    if isinstance(stored, Failed):
        return page.page(request, "rooms/history.html", room=room, posts=unreadable(DEGRADED))
    posts = [_post_view(post, page) for post in stored.value]
    oldest = stored.value[-1].post_timestamp if stored.value else None
    return page.page(
        request,
        "rooms/history.html",
        room=room,
        posts=read(posts),
        older=oldest if len(stored.value) == PAGE_SIZE else None,
    )


def _post_view(post: PostRecord, page: Panel) -> dict[str, object]:
    """One message, with its author identified by key and marked accordingly.

    A name is shown only where it comes from a source the platform verifies —
    a contact whose advert signature checked out. Anything else is the key, or
    the key with an unverified marking: §8's rule does not stop at the contact
    table (design D13).
    """
    rendered = post.rendered
    return {
        "post_timestamp": post.post_timestamp,
        "sender_timestamp": post.sender_timestamp,
        "posted_at": post.posted_at,
        "author": identity_for_key(post.author_public_key, page.state.contacts),
        "text": rendered.text,
        "displayable": rendered.is_valid_utf8,
        "raw": post.text.hex(),
        "bytes": len(post.text),
    }


@router.get("/{room_id}/members", response_class=HTMLResponse)
async def members(room_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """Who is entitled to receive this room's history, and how far behind."""
    room = await _room(page, room_id)
    if room is None or page.persistence is None:
        return page.page(
            request,
            "rooms/members.html",
            room=None,
            members=unreadable(NO_DATABASE),
            status_code=404 if page.persistence is not None else 200,
        )
    listed = await page.persistence.members.load_for_room(room.id)
    if isinstance(listed, Failed):
        return page.page(request, "rooms/members.html", room=room, members=unreadable(DEGRADED))
    rows = [await _member_view(member, room, page) for member in listed.value]
    return page.page(request, "rooms/members.html", room=room, members=read(rows))


async def _member_view(member: MemberRecord, room: RoomRecord, page: Panel) -> dict[str, object]:
    """One member, with how many stored messages they have yet to receive.

    Two members sharing a node hash are two members: the hash is one byte, and
    the public key is what makes a member a member (§3). Nothing here treats a
    shared hash as a conflict.
    """
    assert page.persistence is not None
    unsynced = await page.persistence.messages.unsynced_count(
        room.id, since=member.sync_since, author_to_skip=member.public_key
    )
    return {
        "identity": identity_for_key(member.public_key, page.state.contacts),
        "public_key": member.public_key.hex(),
        "node_hash": member.node_hash,
        "permission": member.permission.name.lower(),
        "sync_since": member.sync_since,
        "last_timestamp": member.last_timestamp,
        "first_login": member.first_login,
        "last_activity": member.last_activity,
        "unsynced": unsynced.value if isinstance(unsynced, Succeeded) else -1,
    }


@router.get("/{room_id}/members/{public_key}/revoke", response_class=HTMLResponse)
async def revoke_form(
    room_id: str, public_key: str, request: Request, page: PanelDep
) -> HTMLResponse:
    """What revocation removes, before it removes it."""
    room = await _room(page, room_id)
    return page.page(
        request,
        "rooms/revoke.html",
        room=room,
        member_key=public_key,
        status_code=200 if room is not None else 404,
    )


@router.post("/{room_id}/members/{public_key}/revoke")
async def revoke(
    room_id: str,
    public_key: str,
    page: PanelDep,
    confirm: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Remove a membership through `RoomMemberRepository.delete`.

    One row, and it carries four things at once: membership, permissions, the
    sync cursor and the replay guard. They go together because they *are* one
    row — which is what the confirmation says, so revoking is not mistaken for
    a demotion.
    """
    room = await _room(page, room_id)
    if room is None or page.persistence is None or confirm != "yes":
        return RedirectResponse(f"/rooms/{room_id}/members", status_code=SEE_OTHER)
    await page.persistence.members.delete(room.id, bytes.fromhex(public_key))
    return RedirectResponse(f"/rooms/{room_id}/members", status_code=SEE_OTHER)


# --- 5. Posting to a room, which is a guarded action -------------------------


@router.get("/{room_id}/post", response_class=HTMLResponse)
async def compose(room_id: str, request: Request, page: PanelDep) -> HTMLResponse:
    """The composer. Stores nothing, transmits nothing, mints no confirmation."""
    room = await _room(page, room_id)
    return page.page(
        request,
        "rooms/compose.html",
        room=room,
        limit=STORED_POST_TEXT_LEN,
        refusal=None,
        status_code=200 if room is not None else 404,
    )


@router.post("/{room_id}/post", response_model=None)
async def review(
    room_id: str,
    request: Request,
    page: PanelDep,
    text: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """The confirmation: what this post does, before it is a row.

    The length check is `sighop room post`'s, applied for the same reason
    milestone 6 gave: a post arriving over the air is truncated because its
    author cannot be told, and an author who is *present* can be asked to
    shorten it instead. So it is refused with the limit, the overage and what
    they typed — which is the whole argument for refusing here rather than
    truncating.
    """
    room = await _room(page, room_id)
    if room is None:
        return page.page(
            request,
            "rooms/compose.html",
            room=None,
            limit=STORED_POST_TEXT_LEN,
            refusal=None,
            status_code=404,
        )
    encoded = text.encode("utf-8")
    if len(encoded) > STORED_POST_TEXT_LEN:
        return page.page(
            request,
            "rooms/compose.html",
            room=room,
            limit=STORED_POST_TEXT_LEN,
            refusal=refused(
                f"the post is {len(encoded)} bytes and a room keeps "
                f"{STORED_POST_TEXT_LEN}, which is all a stock client can show — "
                f"{len(encoded) - STORED_POST_TEXT_LEN} over. Shorten it and post "
                "again. A post arriving over the air is truncated instead, "
                "because its author cannot be told; you can be",
                field="text",
                text=text,
            ),
            status_code=400,
        )
    return page.page(
        request,
        "rooms/confirm_post.html",
        room=room,
        text=text,
        bytes_used=len(encoded),
        limit=STORED_POST_TEXT_LEN,
        served=any(str(server.room.id) == room_id for server in page.state.rooms),
        transmit_enabled=page.state.scheduler.status().transmit_enabled,
        hold_seconds=POST_SYNC_DELAY_SECS,
        action=POST_TO_ROOM,
        nonce=page.nonces.mint(POST_TO_ROOM, room_id),
    )


@router.post("/{room_id}/post/confirm", response_model=None)
async def post(
    room_id: str,
    request: Request,
    page: PanelDep,
    text: Annotated[str, Form()] = "",
    nonce: Annotated[str, Form()] = "",
) -> RedirectResponse | HTMLResponse:
    """Store the post as the room's own identity — `sighop room post`'s call.

    Not through `RoomServer`: a post is a row, and delivery belongs to the push
    loop of whichever run is serving the room. A closed transmit gate does not
    refuse it, because refusing would be this surface inventing a rule the
    command line does not have; what the panel does instead is *say* that
    nothing goes on the air until the gate opens (design D4).
    """
    actor = page.actor(request)
    unverified = page.unverified(
        request, action=POST_TO_ROOM, target=room_id, title="post to a room"
    )
    if unverified is not None:
        return unverified
    room = await _room(page, room_id)
    encoded = text.encode("utf-8")
    if (
        room is None
        or len(encoded) > STORED_POST_TEXT_LEN
        or not page.nonces.spend(nonce, POST_TO_ROOM, room_id)
    ):
        audit(
            page.logger,
            action=POST_TO_ROOM,
            target=room_id,
            outcome="refused",
            actor=actor,
            reason="no confirmation was minted for this action",
            room_name="" if room is None else room.name,
        )
        return page.page(request, "admin/refused.html", title="post to a room", status_code=403)

    assert page.persistence is not None
    author = await _room_author(page, room)
    if author is None:  # pragma: no cover - the foreign key makes this unreachable
        audit(
            page.logger,
            action=POST_TO_ROOM,
            target=room_id,
            outcome="refused",
            actor=actor,
            reason="the identity this room is bound to is not stored",
            room_name=room.name,
        )
        return page.page(request, "admin/refused.html", title="post to a room", status_code=409)

    stored = await page.persistence.messages.store(
        room_id=room.id, author_public_key=author, text=encoded
    )
    if isinstance(stored, Failed):
        audit(
            page.logger,
            action=POST_TO_ROOM,
            target=room_id,
            outcome="refused",
            actor=actor,
            reason=str(stored.error),
            room_name=room.name,
        )
        return page.page(request, "admin/refused.html", title="post to a room", status_code=409)
    audit(
        page.logger,
        action=POST_TO_ROOM,
        target=room_id,
        outcome="success",
        actor=actor,
        room_name=room.name,
        post_timestamp=stored.value.post_timestamp,
        bytes=len(encoded),
        served=any(str(server.room.id) == room_id for server in page.state.rooms),
        transmit_enabled=page.state.scheduler.status().transmit_enabled,
    )
    return RedirectResponse(f"/rooms/{room_id}", status_code=SEE_OTHER)


async def _room_author(page: Panel, room: RoomRecord) -> bytes | None:
    """The room's own identity, which is what it posts as."""
    assert page.persistence is not None
    entity = await page.persistence.entities.get_by_id(room.entity_id)
    if isinstance(entity, Failed) or entity.value is None:
        return None
    return entity.value.public_key


async def _room(page: Panel, room_id: str) -> RoomRecord | None:
    if page.persistence is None:
        return None
    try:
        uuid.UUID(room_id)
    except ValueError:
        return None
    listed = await page.persistence.rooms.list_all()
    if isinstance(listed, Failed):
        return None
    for room in listed.value:
        if str(room.id) == room_id:
            return room
    return None
