"""The three tables milestone 6 owns (design D2).

`0001` named four of §6's eight tables as deliberately absent. Three of them
arrive here, each with behaviour and tests behind it:

* ``room``        — a room bound to exactly one identity, its passwords stored
                    as Argon2id hashes, and its retention policy
* ``room_member`` — the ACL, which is also the sync-cursor and replay-guard
                    table (§7: "ACLs must survive restart or every member
                    re-authenticates")
* ``message``     — the durable ordered history §6 calls "the whole reason a
                    room server beats a walkie-talkie"

One of §6's eight remains deliberately absent, and its absence is still intent
rather than oversight:

* ``bot_state``   — milestone 7 (bots)

§6 called its list a sketch and "not final DDL", and it was right to. Three
shapes here differ from it: the ACL carries routing and cursor state rather than
permissions alone, ``message`` needs an ordering value the wire protocol can
name rather than a bare timestamp, and retention needs two independent bounds
rather than one policy field. All three are recorded in DESIGN.md §6.

There is no data migration. None of the three tables exists anywhere yet and the
four `0001` built are untouched.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)
"""Every timestamp that is an instant (design D7). The columns that are *not*
instants — `sync_since`, `last_timestamp`, `post_timestamp` — are `BIGINT`,
because they are MeshCore's unsigned 32-bit epoch seconds as they appear on the
wire and comparing them against an instant is how a comparison silently changes
meaning."""


def upgrade() -> None:
    op.create_table(
        "room",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Unique: one identity is one node to the mesh, and a node is a room.
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # The encoded `$argon2id$…` string, parameters and salt included, so a
        # future parameter change needs no schema change (design D1).
        sa.Column("admin_password_hash", sa.Text(), nullable=False),
        # NULL + guest_open false refuses guests; NULL + guest_open true admits
        # an empty password. §7's "legal, but never the default", as columns.
        sa.Column("guest_password_hash", sa.Text(), nullable=True),
        sa.Column("guest_open", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allow_read_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Both NULL by default: nothing is deleted until an operator sets a
        # policy (design D15).
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("retention_messages", sa.Integer(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_room"),
        sa.UniqueConstraint("entity_id", name="uq_room_entity_id"),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entity.id"],
            name="fk_room_entity_id_entity",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "room_member",
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("node_hash", sa.SmallInteger(), nullable=False),
        sa.Column("permissions", sa.SmallInteger(), nullable=False),
        sa.Column("sync_since", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_timestamp", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("first_login", TIMESTAMPTZ, nullable=False),
        sa.Column("last_activity", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("room_id", "public_key", name="pk_room_member"),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["room.id"],
            name="fk_room_member_room_id_room",
            ondelete="CASCADE",
        ),
    )
    # Indexed, never unique: two members of one room may share a node hash, and
    # at 1 in 256 (§3) they eventually will.
    op.create_index("ix_room_member_node_hash", "room_member", ["node_hash"])

    op.create_table(
        "message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_public_key", sa.LargeBinary(), nullable=False),
        # The cursor itself, not decoration: the push carries it and the
        # acknowledgement advances `room_member.sync_since` to it (design D3).
        sa.Column("post_timestamp", sa.BigInteger(), nullable=False),
        # The author's own claim, nullable because a server post has no sender.
        sa.Column("sender_timestamp", sa.BigInteger(), nullable=True),
        # bytea, because text off the wire is not guaranteed valid UTF-8 and a
        # push must reproduce it byte for byte (design D4).
        sa.Column("text", sa.LargeBinary(), nullable=False),
        sa.Column("posted_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_message"),
        # What makes `max(now, last + 1)` safe rather than hopeful.
        sa.UniqueConstraint("room_id", "post_timestamp", name="uq_message_room_id_post_timestamp"),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["room.id"],
            name="fk_message_room_id_room",
            ondelete="CASCADE",
        ),
    )
    # Carries both the push loop's "next after this member's cursor" scan and
    # the retention pruner's "oldest first" one.
    op.create_index("ix_message_room_id_post_timestamp", "message", ["room_id", "post_timestamp"])


def downgrade() -> None:
    """Drop the three tables. Tested — an untested downgrade does not exist."""
    op.drop_index("ix_message_room_id_post_timestamp", table_name="message")
    op.drop_table("message")
    op.drop_index("ix_room_member_node_hash", table_name="room_member")
    op.drop_table("room_member")
    op.drop_table("room")
