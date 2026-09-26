"""Repeater collection may keep what it collects forever.

``repeater_collection.retention_days`` becomes nullable: NULL means keep
forever, so pruning deletes nothing. The check constraint is rewritten to
admit NULL beside the old 1 to 365 range. NULL, not a sentinel number, matches
``room.retention_days`` and keeps a magic value out of the form and every
reader.

**No data migration.** An existing row keeps its number of days.

**What a downgrade loses.** A row kept forever becomes 365 days, so the next
pruning after the downgrade deletes polls older than a year.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_repeater_collection_retention_days"


def upgrade() -> None:
    op.drop_constraint(op.f(CHECK), "repeater_collection", type_="check")
    op.alter_column(
        "repeater_collection", "retention_days", existing_type=sa.Integer(), nullable=True
    )
    op.create_check_constraint(
        op.f(CHECK),
        "repeater_collection",
        "retention_days IS NULL OR retention_days BETWEEN 1 AND 365",
    )


def downgrade() -> None:
    """Map keep forever to 365 days and restore NOT NULL. Tested."""
    op.drop_constraint(op.f(CHECK), "repeater_collection", type_="check")
    op.execute("UPDATE repeater_collection SET retention_days = 365 WHERE retention_days IS NULL")
    op.alter_column(
        "repeater_collection", "retention_days", existing_type=sa.Integer(), nullable=False
    )
    op.create_check_constraint(
        op.f(CHECK), "repeater_collection", "retention_days BETWEEN 1 AND 365"
    )
