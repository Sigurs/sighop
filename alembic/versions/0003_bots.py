"""The last of §6's eight tables, and one it did not sketch (design D3).

`0001` named four tables as deliberately absent and `0002` built three of them.
The fourth arrives here:

* ``bot_state``   — the durable per-bot key/value store a driver persists in,
                    named-but-absent since `0001` for exactly this milestone

It does not arrive alone. ``bot`` joins it, and that is a departure from §6's
sketch rather than an addition to it:

* ``bot``         — a driver bound to exactly one identity, with its enablement,
                    its mode and its driver configuration

§6's sketch predates a bot having a driver name, a mode and configuration to
store, and those are per *bot* rather than per key — there is nowhere in a
key/value table for them to live. The alternative, keeping them in
`entity.advert_config`, was rejected: that column describes how an identity
adverts, and putting a greeting template in it would make one column mean two
things and make "list the bots" a scan of every entity (design D3).

With this revision §6's eight-table sketch is complete.

**What a downgrade loses.** Both tables are dropped, so a downgrade discards
every bot's configuration *and* its greeting records. The greeting record is
what makes "a contact is greeted at most once for the lifetime of that contact"
true across a restart (design D6/D7), so after a downgrade a node that was
already greeted may be greeted again — if its `contact` row is also lost, since
the greeter's other gate is that the advert *created* the contact. Losing the
records alone is survivable; losing them together with the contact table is what
produces a second unsolicited direct message to a stranger.

There is no data migration. Neither table exists anywhere yet and the seven
built by `0001` and `0002` are untouched.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)
"""Every timestamp is an instant here (design D7), and both of these are ours
rather than the wire's — nothing in this milestone puts a new byte on the air."""


def upgrade() -> None:
    op.create_table(
        "bot",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Unique for the reason `room.entity_id` is: one identity is one node to
        # the mesh, and a node plays one role.
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        # A registry name, never an import path (design D14).
        sa.Column("driver", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Observe by default, at the schema level as well as in the repository:
        # transmitting is the opt-in and it is a stored decision, not a flag on
        # the last command line anyone typed (design D4).
        sa.Column("mode", sa.Text(), nullable=False, server_default=sa.text("'observe'")),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_bot"),
        sa.UniqueConstraint("entity_id", name="uq_bot_entity_id"),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entity.id"],
            name="fk_bot_entity_id_entity",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "bot_state",
        sa.Column("bot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        # Per bot and per key: two bots may hold the same key with different
        # values, and one bot may hold a key exactly once. The isolation between
        # bots is the schema's, not a convention the runtime has to remember.
        sa.PrimaryKeyConstraint("bot_id", "key", name="pk_bot_state"),
        sa.ForeignKeyConstraint(
            ["bot_id"],
            ["bot.id"],
            name="fk_bot_state_bot_id_bot",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    """Drop both tables. Tested — an untested downgrade does not exist.

    See the module docstring for what this costs: bot configuration and every
    greeting record, which is the guard against greeting a stranger twice.
    """
    op.drop_table("bot_state")
    op.drop_table("bot")
