"""§6's tenth table: a person's own conversations (design D7).

`0001`-`0003` built the eight tables §6 sketched plus the `bot` table it did not.
This adds the tenth:

* ``direct_message`` — one direct message, in either direction, keyed by the
                       local identity and the peer whose keys carried it

§6's ``message`` looks like the table for this and is not. It hangs off
``room_id`` because a room server's whole purpose is to hold what was posted to
it; a direct message belongs to no room, and so had no store at all. Until this
revision every direct message the radio carried existed only in the process that
saw it, which made a page refresh a way to lose a conversation.

**A message is one row, written twice.** ``UNIQUE (entity_public_key, ref)`` is
what makes that true: the record is written when a send is submitted and updated
in place — ``ON CONFLICT (entity_public_key, ref) DO UPDATE`` — as its attempts
and outcome become known. So a message in flight is visible, a resolved message
does not appear twice, and a restart mid-send leaves a row whose outcome is
``in_flight``: unknown, rather than a claim of delivery or a row that vanished.

``ref`` is the send's ``message_id`` outbound and the reception's ``packet_id``
inbound. It is scoped to the identity rather than global because two identities
in one process may legitimately be told about the same packet.

**``wire_timestamp`` is ``BIGINT``, not a timestamp**, for the reason
``room_member.sync_since`` is: it is MeshCore's unsigned 32-bit epoch seconds as
they appear on the wire — the peer's own claim about its clock — and mixing that
representation with an instant in one column is how a comparison silently changes
meaning. ``handled_at`` is ours and is ``TIMESTAMPTZ``. The conversation index is
on ``handled_at`` because that is what the ordering uses: a peer with a wrong
clock must not be able to reorder a conversation.

**Message text is stored unencrypted.** ``text`` is ``bytea`` holding the bytes
that were on the wire, and nothing in this schema encrypts it. §6 was careful
that a database dump must not be sufficient to *impersonate* a room server —
``entity.sealed_seed`` is ciphertext under a key held only in the environment —
and it makes no equivalent promise about content. **A dump of this table exposes
conversation content in the clear.** That is a deliberate trade rather than an
oversight: the key that would encrypt it is one the interface reading these rows
would have to hold anyway.

**What a downgrade loses.** The table is dropped, so a downgrade discards
**every recorded conversation, in both directions**. There is nowhere else in
this schema those messages exist, and nothing on the mesh can be asked for them
again: a direct message is not a re-learnable route. `0003`'s downgrade loses
greeting records, which cost a stranger a second unsolicited message; this one
loses the conversations themselves.

There is no data migration. The table exists nowhere yet and the nine built by
`0001`-`0003` are untouched.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "direct_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # By public key rather than by `entity.id`: a conversation should outlive
        # the row that held the identity that carried it, and the key is what
        # both directions actually have in hand when the record is made.
        sa.Column("entity_public_key", sa.LargeBinary(), nullable=False),
        sa.Column("peer_public_key", sa.LargeBinary(), nullable=False),
        # `out` | `in`, as `packet_log.direction` is `tx` | `rx`.
        sa.Column("direction", sa.Text(), nullable=False),
        # The bytes that were on the wire, untranscoded and unencrypted. See the
        # module docstring: a dump of this column exposes conversation content.
        sa.Column("text", sa.LargeBinary(), nullable=False),
        # The peer's clock, as it travels. Never used for ordering.
        sa.Column("wire_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("handled_at", TIMESTAMPTZ, nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("packet_ids", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("route_flood", sa.Boolean(), nullable=True),
        # Empty rather than NULL for a zero-hop direct route, the distinction
        # `path.path_bytes` already draws. NULL means inbound, where we did not
        # choose the route.
        sa.Column("route_path", sa.LargeBinary(), nullable=True),
        # Never NULL: a record written at submission says `in_flight`.
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("ack_latency_ms", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_direct_message"),
        # One message, one row, updated in place.
        sa.UniqueConstraint(
            "entity_public_key", "ref", name="uq_direct_message_entity_public_key_ref"
        ),
    )
    # The conversation read: one identity, one peer, newest `handled_at` first.
    op.create_index(
        "ix_direct_message_entity_public_key_peer_public_key_handled_at",
        "direct_message",
        ["entity_public_key", "peer_public_key", "handled_at"],
    )


def downgrade() -> None:
    """Drop the table. Tested — an untested downgrade does not exist.

    See the module docstring for what this costs: every recorded conversation,
    in both directions, with nowhere else to read them from and nothing on the
    mesh that can be asked for them again.
    """
    op.drop_index(
        "ix_direct_message_entity_public_key_peer_public_key_handled_at",
        table_name="direct_message",
    )
    op.drop_table("direct_message")
