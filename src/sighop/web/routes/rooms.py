"""Browsing a room the platform serves (`web-rooms`, §8 area 3).

The one view that answers "why has this person not seen that message" without a
database client: a member's sync cursor beside the room's own history, in the
order the protocol orders it.

Everything here is **read-only with respect to the mesh**. Opening a room,
paging back through it and refreshing it transmit nothing, push nothing, and
move no member's cursor — which is not obvious from the outside, because a room
server's normal job is precisely to push history at members whose cursor is
behind. The push loop is the room server's; this module only reads rows.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from sighop.db.engine import Failed, Succeeded
from sighop.db.repositories import MemberRecord, PostRecord, RoomRecord
from sighop.web.deps import Panel, panel
from sighop.web.render import DEGRADED, NO_DATABASE, identity_for_key, read, unreadable

PanelDep = Annotated[Panel, Depends(panel)]

router = APIRouter(prefix="/rooms")

PAGE_SIZE = 50
"""Messages per page. Bounded for the reason every read is bounded here: an
unbounded one is a statement whose cost is set by how long the room has run."""

SEE_OTHER = 303


@router.get("", response_class=HTMLResponse)
async def index(request: Request, page: PanelDep) -> HTMLResponse:
    """Every room the database holds, served by this run or not.

    A room this run is not serving is still browsable: its history is in the
    database and reading it needs no identity. What it cannot do is answer a
    login, and the reason is stated rather than left as an absence.
    """
    if page.persistence is None:
        return page.page(request, "rooms/index.html", rooms=unreadable(NO_DATABASE), reasons={})
    listed = await page.persistence.rooms.list_all()
    if isinstance(listed, Failed):
        return page.page(
            request, "rooms/index.html", rooms=unreadable(DEGRADED), reasons={}
        )
    served = {str(server.room.id) for server in page.state.rooms}
    loaded = {stub.identity.public_key for stub in page.state.adverts.stubs}
    reasons = {
        str(room.id): _unserved_reason(room, served, loaded, page)
        for room in listed.value
    }
    return page.page(
        request,
        "rooms/index.html",
        rooms=read(listed.value),
        reasons=reasons,
        served=served,
    )


def _unserved_reason(
    room: RoomRecord, served: set[str], loaded: set[bytes], page: Panel
) -> str:
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
            request, "rooms/history.html", room=None, posts=unreadable(NO_DATABASE),
            status_code=404 if page.persistence is not None else 200,
        )
    stored = await page.persistence.messages.history(
        room.id, limit=PAGE_SIZE, newest_first=True, before=before
    )
    if isinstance(stored, Failed):
        return page.page(
            request, "rooms/history.html", room=room, posts=unreadable(DEGRADED)
        )
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
        return page.page(
            request, "rooms/members.html", room=room, members=unreadable(DEGRADED)
        )
    rows = [await _member_view(member, room, page) for member in listed.value]
    return page.page(request, "rooms/members.html", room=room, members=read(rows))


async def _member_view(
    member: MemberRecord, room: RoomRecord, page: Panel
) -> dict[str, object]:
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
