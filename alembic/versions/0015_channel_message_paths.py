"""Every copy's path, kept with a received channel message.

Two nullable columns on ``channel_message``: ``paths``, each copy's raw path in
arrival order (``b""`` for a copy heard directly), and ``path_hash_size``, how
wide each hop hash in them is (channel-message-path-copy D1). NULL is "not
recorded", never "no hops".

**No data migration.** Existing rows keep NULL: ``packet_log`` is a
best-effort ring buffer, not a record to backfill from.

**What a downgrade loses.** The recorded paths. Nothing else references them.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channel_message",
        sa.Column("paths", postgresql.ARRAY(sa.LargeBinary()), nullable=True),
    )
    op.add_column(
        "channel_message",
        sa.Column("path_hash_size", sa.SmallInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_message", "path_hash_size")
    op.drop_column("channel_message", "paths")
