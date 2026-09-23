"""Browsing a room the platform serves (`web-rooms`, §8 area 3).

The view that answers "why has this person not seen that message" without a
database client, and the one rule it must never break: **browsing is read-only
with respect to the mesh**. A room server's normal job is to push history at
members whose cursor is behind, so a page over the same rows has to be
demonstrably not doing that — nothing transmitted, no cursor moved.
"""

from __future__ import annotations

import base64

import httpx2
from fastapi import FastAPI

from sighop.config import generate_secret_key
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.net.contacts import Contact
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType, Permission, WireText
from sighop.web.app import allowed_hosts, create_app
from sighop.web.guard import TOKEN_FIELD
from sighop.web.routes.rooms import PAGE_SIZE
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    StubState,
    authenticator,
    csrf,
    signed_async_client,
    stub_state,
)

HOSTS = allowed_hosts("127.0.0.1", 8080)
SECRET = base64.b64decode(generate_secret_key())


def _live(app: FastAPI) -> httpx2.AsyncClient:
    """A client in this event loop, so the database engine is the test's own."""
    return signed_async_client(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    )


async def _room(database: Database, *, name: str = "[redacted]"):
    persistence = Persistence(database=database)
    entity = await persistence.entities.store(
        name=f"{name}-host",
        identity=generate_identity(),
        secret=SECRET,
        # A room is bound to an identity that adverts as a room server, which
        # `RoomRepository.create` now enforces for every caller rather than only
        # for the command line.
        node_type=NodeType.ROOM_SERVER,
    )
    assert isinstance(entity, Succeeded)
    room = await persistence.rooms.create(
        entity_id=entity.value.id, name=name, admin_password_hash="x"
    )
    assert isinstance(room, Succeeded)
    return persistence, room.value


def _app(state: StubState) -> FastAPI:
    return create_app(state, auth=authenticator(), hosts=HOSTS, logger=RecordingLogger())


# --- 14.1 History -----------------------------------------------------------


async def test_history_is_newest_first_with_its_ordering_timestamps(
    database: Database,
) -> None:
    """14.1: the room's own total order, as the protocol synchronises on it."""
    persistence, room = await _room(database)
    author = generate_identity().public_key
    for index in range(3):
        assert isinstance(
            await persistence.messages.store(
                room_id=room.id, author_public_key=author, text=f"post {index}".encode()
            ),
            Succeeded,
        )

    app = _app(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get(f"/rooms/{room.id}")).text

    assert body.index("post 2") < body.index("post 1") < body.index("post 0")
    assert "ordering timestamp" in body


async def test_paging_back_keeps_the_same_total_order_across_pages(
    database: Database,
) -> None:
    """14.1: a page boundary that shows nothing twice and skips nothing.

    The cursor is `post_timestamp`, which is unique within a room by design D3 —
    which is exactly what makes it a boundary rather than a guess.
    """
    persistence, room = await _room(database)
    author = generate_identity().public_key
    total = PAGE_SIZE + 5
    for index in range(total):
        assert isinstance(
            await persistence.messages.store(
                room_id=room.id, author_public_key=author, text=f"m{index:03d}".encode()
            ),
            Succeeded,
        )

    app = _app(stub_state(persistence=persistence))
    async with _live(app) as client:
        first = (await client.get(f"/rooms/{room.id}")).text
        assert "older messages" in first
        cursor = first.split("?before=")[1].split('"')[0]
        second = (await client.get(f"/rooms/{room.id}?before={cursor}")).text

    on_first = {f"m{index:03d}" for index in range(total) if f"m{index:03d}" in first}
    on_second = {f"m{index:03d}" for index in range(total) if f"m{index:03d}" in second}
    assert len(on_first) == PAGE_SIZE
    assert on_first & on_second == set(), "a message appeared on two pages"
    assert on_first | on_second == {f"m{index:03d}" for index in range(total)}


async def test_a_message_whose_bytes_are_not_text_is_shown_as_such(
    database: Database,
) -> None:
    """14.1: shown with that fact stated and its bytes, never substituted."""
    persistence, room = await _room(database)
    raw = b"\xff\xfe not text"
    assert isinstance(
        await persistence.messages.store(
            room_id=room.id, author_public_key=generate_identity().public_key, text=raw
        ),
        Succeeded,
    )

    app = _app(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get(f"/rooms/{room.id}")).text

    assert "not valid text" in body
    assert raw.hex() in body, "the bytes themselves are not available"


# --- 14.2 Authors -----------------------------------------------------------


async def test_a_verified_author_is_named_and_marked_verified(
    database: Database,
) -> None:
    """14.2: a name only from a source the platform verifies."""
    persistence, room = await _room(database)
    author = generate_identity()
    assert isinstance(
        await persistence.messages.store(
            room_id=room.id, author_public_key=author.public_key, text=b"hello"
        ),
        Succeeded,
    )
    state = stub_state(persistence=persistence)
    state.contacts.restore(
        [
            Contact(
                public_key=author.public_key,
                name=WireText.from_bytes(b"[redacted]"),
                advert_verified=True,
            )
        ]
    )

    app = _app(state)
    async with _live(app) as client:
        body = (await client.get(f"/rooms/{room.id}")).text

    assert "[redacted]" in body
    assert "identity-verified" in body


async def test_an_author_matching_no_verified_contact_is_shown_as_a_key(
    database: Database,
) -> None:
    """14.2: no name taken from an unverified source — the key, and a marking."""
    persistence, room = await _room(database)
    author = generate_identity()
    assert isinstance(
        await persistence.messages.store(
            room_id=room.id, author_public_key=author.public_key, text=b"hello"
        ),
        Succeeded,
    )

    app = _app(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get(f"/rooms/{room.id}")).text

    assert author.public_key.hex()[:16] in body
    assert "identity-key_only" in body
    assert "key only" in body


# --- 14.3 Members -----------------------------------------------------------


async def test_the_member_list_carries_permission_cursor_and_unsynced_count(
    database: Database,
) -> None:
    """14.3: the three values that answer "why has this member not seen that"."""
    persistence, room = await _room(database)
    member = await _add_member(persistence, room, generate_identity().public_key)
    for index in range(4):
        assert isinstance(
            await persistence.messages.store(
                room_id=room.id,
                author_public_key=generate_identity().public_key,
                text=f"m{index}".encode(),
            ),
            Succeeded,
        )

    app = _app(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get(f"/rooms/{room.id}/members")).text

    assert member.hex() in body
    assert "sync cursor" in body
    assert "unsynced" in body
    assert ">4<" in body, "the unsynced count is not shown"


async def test_two_members_sharing_a_node_hash_are_two_members(
    database: Database,
) -> None:
    """14.3, §3: one byte collides at 1 in 256, and that is not a conflict."""
    persistence, room = await _room(database)
    first = bytes([0xAB]) + generate_identity().public_key[1:]
    second = bytes([0xAB]) + generate_identity().public_key[1:]
    assert first[0] == second[0] and first != second
    await _add_member(persistence, room, first)
    await _add_member(persistence, room, second)

    app = _app(stub_state(persistence=persistence))
    async with _live(app) as client:
        body = (await client.get(f"/rooms/{room.id}/members")).text

    assert first.hex() in body
    assert second.hex() in body
    assert body.count("0xab") == 2, "a shared node hash collapsed two members into one"


async def _add_member(persistence: Persistence, room, public_key: bytes) -> bytes:
    import datetime as dt

    from sighop.db.repositories import MemberRecord

    now = dt.datetime.now(dt.UTC)
    stored = await persistence.members.upsert(
        MemberRecord(
            room_id=room.id,
            public_key=public_key,
            node_hash=public_key[0],
            permissions=int(Permission.READ_WRITE),
            sync_since=0,
            last_timestamp=0,
            first_login=now,
            last_activity=now,
        )
    )
    assert isinstance(stored, Succeeded)
    return public_key


# --- 14.4 Revocation --------------------------------------------------------


async def test_revocation_states_what_it_removes_and_then_removes_it(
    database: Database,
) -> None:
    """14.4: four things in one row, said before the row goes."""
    persistence, room = await _room(database)
    member = await _add_member(persistence, room, generate_identity().public_key)

    app = _app(stub_state(persistence=persistence))
    async with _live(app) as client:
        form = (await client.get(f"/rooms/{room.id}/members/{member.hex()}/revoke")).text
        assert "membership, permissions, sync cursor and replay guard" in form

        unconfirmed = await client.post(
            f"/rooms/{room.id}/members/{member.hex()}/revoke",
            data={TOKEN_FIELD: csrf(client)},
        )
        assert unconfirmed.status_code == 303

    listed = await persistence.members.load_for_room(room.id)
    assert isinstance(listed, Succeeded)
    assert len(listed.value) == 1, "the member went without the confirmation"

    async with _live(app) as client:
        confirmed = await client.post(
            f"/rooms/{room.id}/members/{member.hex()}/revoke",
            data={TOKEN_FIELD: csrf(client), "confirm": "yes"},
        )
        assert confirmed.status_code == 303

    listed = await persistence.members.load_for_room(room.id)
    assert isinstance(listed, Succeeded)
    assert listed.value == []


# --- 14.5 Unserved rooms ----------------------------------------------------


async def test_a_room_this_run_does_not_serve_is_listed_as_unserved(
    database: Database,
) -> None:
    """14.5: with the reason, and its history still browsable."""
    persistence, room = await _room(database)
    assert isinstance(
        await persistence.messages.store(
            room_id=room.id,
            author_public_key=generate_identity().public_key,
            text=b"still readable",
        ),
        Succeeded,
    )

    app = _app(stub_state(persistence=persistence))
    async with _live(app) as client:
        index = (await client.get("/rooms")).text
        history = (await client.get(f"/rooms/{room.id}")).text

    assert "unserved" in index
    assert "did not load the identity" in index
    assert "still readable" in history, "an unserved room's history is not browsable"


# --- 14.6 Browsing transmits nothing ----------------------------------------


async def test_opening_paging_and_refreshing_transmits_nothing(
    database: Database,
) -> None:
    """14.6: the rule a room view could most easily break, asserted directly."""
    persistence, room = await _room(database)
    member = await _add_member(persistence, room, generate_identity().public_key)
    for index in range(PAGE_SIZE + 2):
        assert isinstance(
            await persistence.messages.store(
                room_id=room.id,
                author_public_key=generate_identity().public_key,
                text=f"m{index}".encode(),
            ),
            Succeeded,
        )

    state = stub_state(persistence=persistence)
    before = state.scheduler.status().as_json()
    app = _app(state)

    async with _live(app) as client:
        first = (await client.get(f"/rooms/{room.id}")).text
        cursor = first.split("?before=")[1].split('"')[0]
        await client.get(f"/rooms/{room.id}?before={cursor}")
        await client.get(f"/rooms/{room.id}")
        await client.get(f"/rooms/{room.id}/members")
        await client.get(f"/rooms/{room.id}/members")

    assert state.scheduler.status().as_json() == before, "a room view transmitted"

    listed = await persistence.members.load_for_room(room.id)
    assert isinstance(listed, Succeeded)
    assert listed.value[0].public_key == member
    assert listed.value[0].sync_since == 0, "browsing moved a member's sync cursor"
    assert listed.value[0].last_timestamp == 0
