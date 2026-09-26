"""A repeater poll records how many of its requests were resends.

``repeater_poll.retries`` counts the requests sent again after going
unanswered, summed over the poll's steps (repeater-poll-retries D4). It is
nullable: a poll recorded before resends existed shows no count rather than a
zero it never measured.

**No data migration.** Existing rows keep NULL.

**What a downgrade loses.** The counts.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("repeater_poll", sa.Column("retries", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("repeater_poll", "retries")
