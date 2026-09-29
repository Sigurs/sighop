"""The packet archive: every frame's wire bytes, partitioned by month.

``packet_archive`` is range-partitioned on ``at``; its primary key is
``(at, id)`` because a partitioned table's key must carry the partition key
(packet-archive D3). This migration creates the current UTC month's child so a
freshly migrated database accepts rows at once; every later month is created by
the runtime's maintainer (D4). There is deliberately no DEFAULT partition.

``packet_archive_settings`` is one row, seeded with ``retention_days = NULL``:
keep everything forever until an operator says otherwise (D5).

**No data migration.** ``packet_log`` keeps bytes only for undecodable frames,
so there is nothing to seed the archive from; it starts empty.

**What a downgrade loses.** The whole archive — the parent is dropped with every
monthly partition under it — and the retention setting. Nothing references them.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-29
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)

KINDS = ("rx", "unparsed", "tx")
"""A literal, because a migration must not import application code."""


def _month_bounds(now: dt.datetime) -> tuple[str, dt.datetime, dt.datetime]:
    start = dt.datetime(now.year, now.month, 1, tzinfo=dt.UTC)
    end = dt.datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=dt.UTC)
    return f"packet_archive_y{start.year:04d}m{start.month:02d}", start, end


def upgrade() -> None:
    op.create_table(
        "packet_archive",
        sa.Column("at", TIMESTAMPTZ, nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("packet_id", sa.Text(), nullable=False),
        sa.Column("raw", sa.LargeBinary(), nullable=False),
        sa.Column("snr_db", sa.Float(), nullable=True),
        sa.Column("rssi_dbm", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("tx_result", sa.Text(), nullable=True),
        sa.Column("airtime_ms", sa.Float(), nullable=True),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("priority_class", sa.SmallInteger(), nullable=True),
        sa.PrimaryKeyConstraint("at", "id", name="pk_packet_archive"),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in KINDS) + ")",
            name=op.f("ck_packet_archive_kind"),
        ),
        postgresql_partition_by="RANGE (at)",
    )
    op.create_index("ix_packet_archive_packet_id", "packet_archive", ["packet_id"])

    name, start, end = _month_bounds(dt.datetime.now(dt.UTC))
    op.execute(
        f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF packet_archive '
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )

    op.create_table(
        "packet_archive_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_packet_archive_settings"),
        sa.CheckConstraint("id = 1", name=op.f("ck_packet_archive_settings_single_row")),
        sa.CheckConstraint(
            "retention_days IS NULL OR retention_days BETWEEN 30 AND 3650",
            name=op.f("ck_packet_archive_settings_retention_days"),
        ),
    )
    op.execute("INSERT INTO packet_archive_settings (id, retention_days) VALUES (1, NULL)")


def downgrade() -> None:
    op.drop_table("packet_archive_settings")
    op.drop_table("packet_archive")
