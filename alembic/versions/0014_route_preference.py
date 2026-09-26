"""The station-wide preferred first hop.

One table, ``route_preference``, one row (``id = 1``): the public key of the
repeater every DIRECT send leaves through first, or NULL for none
(preferred-first-hop D4). No row means none is set — the behaviour before this
revision. No foreign key to ``contact``: an advert upsert must never clear the
setting, and a key whose contact is gone is harmless.

**No data migration.** Nothing is preferred until an operator chooses.

**What a downgrade loses.** The setting. Nothing else references it.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "route_preference",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("preferred_first_hop", sa.LargeBinary(), nullable=True),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_route_preference"),
        sa.CheckConstraint("id = 1", name=op.f("ck_route_preference_single_row")),
        sa.CheckConstraint(
            "preferred_first_hop IS NULL OR octet_length(preferred_first_hop) = 32",
            name=op.f("ck_route_preference_preferred_first_hop_length"),
        ),
    )


def downgrade() -> None:
    op.drop_table("route_preference")
