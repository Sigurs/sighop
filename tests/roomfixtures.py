"""In-memory storage for a room server, so room tests need no Postgres.

Design D5 makes the room server depend on *behaviour* — a member sink and a
history store — rather than on SQLAlchemy, and this is what that buys: every
test about login, posting, syncing and retention runs with no database at all,
and the database-marked tests are the ones that are actually about storage.

The semantics here are the repository's, not a simplification of it. In
particular `store` stamps `max(now, last + 1)` per room and refuses a duplicate
ordering value, because that is design D3's rule and a fixture that let it slide
would hide exactly the bug the rule exists to prevent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sighop.db.engine import Outcome, Succeeded
from sighop.db.repositories import MemberRecord, PostRecord, RoomRecord
from sighop.passwords import hash_password
from sighop.protocol.payloads import Permission


@dataclass(slots=True)
class MemoryMembers:
    """The ACL's write side, in memory."""

    rows: dict[tuple[uuid.UUID, bytes], MemberRecord] = field(default_factory=dict)
    upserts: int = 0
    fail: bool = False

    async def upsert(self, member: MemberRecord) -> Outcome[int]:
        if self.fail:
            from sighop.db.engine import DatabaseError, Failed

            return Failed(
                operation="upsert_room_member",
                error=DatabaseError("storage is unavailable"),
            )
        self.upserts += 1
        key = (member.room_id, member.public_key)
        existing = self.rows.get(key)
        if existing is not None:
            # `first_login` does not move on a re-login, exactly as the real
            # upsert leaves it out of its update set.
            from dataclasses import replace

            member = replace(member, first_login=existing.first_login)
        self.rows[key] = member
        return Succeeded(value=1)

    async def delete(self, room_id: uuid.UUID, public_key: bytes) -> Outcome[bool]:
        return Succeeded(value=self.rows.pop((room_id, public_key), None) is not None)

    def of(self, room_id: uuid.UUID) -> list[MemberRecord]:
        return [row for (rid, _), row in self.rows.items() if rid == room_id]


@dataclass(slots=True)
class MemoryHistory:
    """The history, in memory, with the real ordering rule."""

    posts: list[PostRecord] = field(default_factory=list)
    pruned: int = 0
    pruned_unsynced: int = 0
    members: MemoryMembers | None = None
    """Consulted only by `prune`, to count what retention outran (design D15)."""

    never_answers: bool = False
    """A storage sink that accepts a post and never completes it — the shape
    design D6's "not acknowledged unless it landed" is actually tested against."""

    _next_id: int = 1

    async def store(
        self,
        *,
        room_id: uuid.UUID,
        author_public_key: bytes,
        text: bytes,
        sender_timestamp: int | None = None,
        now: int | None = None,
        posted_at: dt.datetime | None = None,
    ) -> Outcome[PostRecord]:
        if self.never_answers:
            import asyncio

            await asyncio.Event().wait()  # pragma: no cover - cancelled by the caller
        at = posted_at or dt.datetime.now(dt.UTC)
        stamp = int(at.timestamp()) if now is None else now
        highest = max(
            (post.post_timestamp for post in self.posts if post.room_id == room_id),
            default=None,
        )
        post_timestamp = stamp if highest is None else max(stamp, highest + 1)
        record = PostRecord(
            id=self._next_id,
            room_id=room_id,
            author_public_key=author_public_key,
            post_timestamp=post_timestamp,
            sender_timestamp=sender_timestamp,
            text=text,
            posted_at=at,
        )
        self._next_id += 1
        self.posts.append(record)
        return Succeeded(value=record)

    async def find_retry(
        self, room_id: uuid.UUID, author_public_key: bytes, sender_timestamp: int
    ) -> Outcome[PostRecord | None]:
        for post in self.posts:
            if (
                post.room_id == room_id
                and post.author_public_key == author_public_key
                and post.sender_timestamp == sender_timestamp
            ):
                return Succeeded(value=post)
        return Succeeded(value=None)

    async def next_for_member(
        self,
        room_id: uuid.UUID,
        *,
        since: int,
        author_to_skip: bytes,
        not_after: int | None = None,
    ) -> Outcome[PostRecord | None]:
        eligible = [
            post
            for post in self.posts
            if post.room_id == room_id
            and post.post_timestamp > since
            and post.author_public_key != author_to_skip
            and (not_after is None or post.post_timestamp <= not_after)
        ]
        eligible.sort(key=lambda post: post.post_timestamp)
        return Succeeded(value=eligible[0] if eligible else None)

    async def unsynced_count(
        self, room_id: uuid.UUID, *, since: int, author_to_skip: bytes
    ) -> Outcome[int]:
        return Succeeded(
            value=sum(
                1
                for post in self.posts
                if post.room_id == room_id
                and post.post_timestamp > since
                and post.author_public_key != author_to_skip
            )
        )

    async def history(
        self, room_id: uuid.UUID, *, limit: int = 100, newest_first: bool = False
    ) -> Outcome[list[PostRecord]]:
        rows = sorted(
            (post for post in self.posts if post.room_id == room_id),
            key=lambda post: post.post_timestamp,
            reverse=newest_first,
        )
        return Succeeded(value=rows[:limit])

    async def count(self, room_id: uuid.UUID) -> Outcome[int]:
        return Succeeded(value=sum(1 for post in self.posts if post.room_id == room_id))

    async def prune(
        self,
        room_id: uuid.UUID,
        *,
        retention_days: int | None,
        retention_messages: int | None,
        now: dt.datetime | None = None,
    ) -> Outcome[tuple[int, int]]:
        if retention_days is None and retention_messages is None:
            return Succeeded(value=(0, 0))
        cutoff = now or dt.datetime.now(dt.UTC)
        mine = [post for post in self.posts if post.room_id == room_id]
        doomed: set[int] = set()
        if retention_days is not None:
            horizon = cutoff - dt.timedelta(days=retention_days)
            doomed |= {post.id for post in mine if post.posted_at < horizon}
        if retention_messages is not None:
            newest = sorted(mine, key=lambda post: post.post_timestamp, reverse=True)
            doomed |= {post.id for post in newest[retention_messages:]}
        if not doomed:
            return Succeeded(value=(0, 0))

        behind = min(
            (member.sync_since for member in (self.members.of(room_id) if self.members else [])),
            default=None,
        )
        unsynced = 0
        if behind is not None:
            unsynced = sum(1 for post in mine if post.id in doomed and post.post_timestamp > behind)
        self.posts = [post for post in self.posts if post.id not in doomed]
        self.pruned += len(doomed)
        self.pruned_unsynced += unsynced
        return Succeeded(value=(len(doomed), unsynced))


@dataclass(slots=True)
class MemoryStorage:
    """What a `RoomServer` is handed instead of `Persistence`."""

    members: MemoryMembers = field(default_factory=MemoryMembers)
    messages: MemoryHistory = field(default_factory=MemoryHistory)
    degraded: bool = False

    def __post_init__(self) -> None:
        self.messages.members = self.members

    def rows_seed(self, member: MemberRecord) -> None:
        """Put a member in storage without going through a login.

        What a test means by "this member logged in during an earlier run".
        """
        self.members.rows[(member.room_id, member.public_key)] = member


ADMIN_PASSWORD = "admin-password"
GUEST_PASSWORD = "guest-password"


def room_record(
    *,
    entity_id: uuid.UUID | None = None,
    name: str = "lounge",
    admin_password: str = ADMIN_PASSWORD,
    guest_password: str | None = GUEST_PASSWORD,
    guest_open: bool = False,
    allow_read_only: bool = False,
    retention_days: int | None = None,
    retention_messages: int | None = None,
    push_ack_window_seconds: int | None = None,
    push_recent_days: int | None = None,
) -> RoomRecord:
    return RoomRecord(
        id=uuid.uuid4(),
        entity_id=entity_id or uuid.uuid4(),
        name=name,
        admin_password_hash=hash_password(admin_password),
        guest_password_hash=None if guest_password is None else hash_password(guest_password),
        guest_open=guest_open,
        allow_read_only=allow_read_only,
        retention_days=retention_days,
        retention_messages=retention_messages,
        created_at=dt.datetime(2026, 9, 5, tzinfo=dt.UTC),
        push_ack_window_seconds=push_ack_window_seconds,
        push_recent_days=push_recent_days,
    )


def member_record(
    room: RoomRecord,
    public_key: bytes,
    *,
    permission: Permission = Permission.READ_WRITE,
    sync_since: int = 0,
    last_timestamp: int = 0,
    at: dt.datetime | None = None,
) -> MemberRecord:
    when = at or dt.datetime(2026, 9, 5, tzinfo=dt.UTC)
    return MemberRecord(
        room_id=room.id,
        public_key=public_key,
        node_hash=public_key[0],
        permissions=int(permission),
        sync_since=sync_since,
        last_timestamp=last_timestamp,
        first_login=when,
        last_activity=when,
    )
