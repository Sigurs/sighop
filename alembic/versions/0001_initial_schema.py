"""The four tables milestone 5 owns (design D3).

DESIGN.md §6 sketches eight tables. Four of them are deliberately **not** here,
and their absence is intent rather than oversight:

* ``room``        — milestone 6 (room server: login, ACLs, history)
* ``room_member`` — milestone 6 (ACLs must survive restart or every member
                    re-authenticates, §7)
* ``message``     — milestone 6 (durable history, and the retention policy that
                    goes with it)
* ``bot_state``   — milestone 7 (bots)

§6 calls its list a sketch and "not final DDL". Milestone 6 will discover things
about ACLs and retention that change those four tables, and shipping them
untested now would make the first real migration a rewrite rather than an
addition. Each gets its own migration in the milestone that owns it.

Revision ID: 0001
Revises:
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)
"""Every timestamp, without exception (design D7). The development server's
`TimeZone` is `Europe/Helsinki`; a `TIMESTAMP WITHOUT TIME ZONE` column would
record local wall-clock time here and something else against a UTC server."""


def upgrade() -> None:
    op.create_table(
        "entity",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("node_hash", sa.SmallInteger(), nullable=False),
        sa.Column("sealed_seed", sa.LargeBinary(), nullable=False),
        sa.Column("advert_config", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_entity"),
        sa.UniqueConstraint("public_key", name="uq_entity_public_key"),
    )
    # Indexed, never unique: a node hash is one byte and collides at 1 in 256
    # (§3). A unique constraint here is a bug waiting for a busy mesh.
    op.create_index("ix_entity_node_hash", "entity", ["node_hash"])

    op.create_table(
        "contact",
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("node_hash", sa.SmallInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("node_type", sa.SmallInteger(), nullable=True),
        sa.Column("flags", sa.SmallInteger(), nullable=True),
        sa.Column("advert_verified", sa.Boolean(), nullable=False),
        sa.Column("first_heard", TIMESTAMPTZ, nullable=False),
        sa.Column("last_heard", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("public_key", name="pk_contact"),
    )
    op.create_index("ix_contact_node_hash", "contact", ["node_hash"])

    op.create_table(
        "path",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # Exactly one of these two identifies the destination. A row carrying
        # only the hash is the ambiguous kind and stays that way.
        sa.Column("dest_public_key", sa.LargeBinary(), nullable=True),
        sa.Column("dest_node_hash", sa.SmallInteger(), nullable=True),
        # May legitimately be empty: an empty path is a zero-hop route, which is
        # the most useful route there is and is not the absence of a row.
        sa.Column("path_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("hash_size", sa.SmallInteger(), nullable=False),
        sa.Column("hop_count", sa.SmallInteger(), nullable=False),
        sa.Column("snr_db", sa.Float(), nullable=True),
        sa.Column("confirmed_at", TIMESTAMPTZ, nullable=False),
        sa.Column("packet_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_path"),
        # NULLS NOT DISTINCT, because Postgres would otherwise treat every
        # hash-keyed route as a fresh row and re-hearing one would accumulate
        # instead of re-confirming (design D12).
        sa.UniqueConstraint(
            "dest_public_key",
            "dest_node_hash",
            "path_bytes",
            "hash_size",
            name="uq_path_destination",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_path_dest_node_hash", "path", ["dest_node_hash"])

    op.create_table(
        "packet_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("packet_id", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("at", TIMESTAMPTZ, nullable=False),
        sa.Column("route_type", sa.Text(), nullable=True),
        sa.Column("payload_type", sa.Text(), nullable=True),
        sa.Column("path_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("hop_count", sa.SmallInteger(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("snr_db", sa.Float(), nullable=True),
        sa.Column("rssi_dbm", sa.SmallInteger(), nullable=True),
        sa.Column("airtime_ms", sa.Float(), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("priority_class", sa.SmallInteger(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        # §4.1: a malformed frame we silently drop is invisible forever. `raw`
        # and `reason` are how that rule reaches the database.
        sa.Column("raw", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_packet_log"),
    )
    op.create_index("ix_packet_log_packet_id", "packet_log", ["packet_id"])
    # `at` carries the pruner: the cap is on rows, and the oldest go first.
    op.create_index("ix_packet_log_at", "packet_log", ["at"])


def downgrade() -> None:
    """Drop the four tables. Tested — an untested downgrade does not exist."""
    op.drop_index("ix_packet_log_at", table_name="packet_log")
    op.drop_index("ix_packet_log_packet_id", table_name="packet_log")
    op.drop_table("packet_log")
    op.drop_index("ix_path_dest_node_hash", table_name="path")
    op.drop_table("path")
    op.drop_index("ix_contact_node_hash", table_name="contact")
    op.drop_table("contact")
    op.drop_index("ix_entity_node_hash", table_name="entity")
    op.drop_table("entity")
