"""Repeater collection: its settings, the selected repeaters, and what polls returned.

Four tables:

* ``repeater_collection`` — the station-wide settings, one row (``id = 1``):
                            enabled, the login identity, the interval, the
                            recency and retention windows, and the last cycle's
                            summary. No row means the defaults — disabled,
                            60 minutes, 3 days, 30 days
* ``repeater_target``     — the repeaters an operator selected, by public key
* ``repeater_poll``       — one row per poll: when, from which identity, by
                            which route, the outcome, and the repeater's status
                            fields when it answered them
* ``repeater_neighbour``  — each poll's neighbour list, as received

**Removing the login identity stops collection.** ``entity_id`` is
``ON DELETE SET NULL`` on both ``repeater_collection`` and ``repeater_poll``. The
settings row may then be enabled with no identity — deliberately not a check
constraint, which would make the identity impossible to remove — and the
collector skips every cycle, saying why, until an operator chooses another.

**Selection is not a contact column**, so an advert refreshing a contact can
never clear it, and has no foreign key to ``contact``: a selection whose
contact is gone is harmless and shown nowhere.

**Neighbours go with their poll** (``ON DELETE CASCADE``), which is how pruning
by ``repeater_poll.started_at`` removes both.

**No data migration.** Collection is disabled until an operator enables it.

**What a downgrade loses.** All four tables are dropped: the settings, every
selection and all collected history. Nothing else references them.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)

OUTCOMES = (
    "succeeded",
    "not_sent",
    "login_unanswered",
    "status_unanswered",
    "neighbours_incomplete",
)
"""A literal, because a migration must not import application code."""


def upgrade() -> None:
    op.create_table(
        "repeater_collection",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("recent_days", sa.Integer(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("last_cycle_started_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_cycle_finished_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_cycle_polled", sa.Integer(), nullable=True),
        sa.Column("last_cycle_succeeded", sa.Integer(), nullable=True),
        sa.Column("last_cycle_note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_repeater_collection"),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entity.id"],
            name="fk_repeater_collection_entity_id_entity",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_repeater_collection_single_row")),
        sa.CheckConstraint(
            "interval_minutes BETWEEN 5 AND 1440",
            name=op.f("ck_repeater_collection_interval_minutes"),
        ),
        sa.CheckConstraint(
            "recent_days BETWEEN 1 AND 365", name=op.f("ck_repeater_collection_recent_days")
        ),
        sa.CheckConstraint(
            "retention_days BETWEEN 1 AND 365",
            name=op.f("ck_repeater_collection_retention_days"),
        ),
    )
    op.create_table(
        "repeater_target",
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("selected_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("public_key", name="pk_repeater_target"),
    )
    op.create_table(
        "repeater_poll",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", TIMESTAMPTZ, nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("batt_milli_volts", sa.Integer(), nullable=True),
        sa.Column("curr_tx_queue_len", sa.Integer(), nullable=True),
        sa.Column("noise_floor", sa.Integer(), nullable=True),
        sa.Column("last_rssi", sa.Integer(), nullable=True),
        sa.Column("n_packets_recv", sa.BigInteger(), nullable=True),
        sa.Column("n_packets_sent", sa.BigInteger(), nullable=True),
        sa.Column("total_air_time_secs", sa.BigInteger(), nullable=True),
        sa.Column("total_up_time_secs", sa.BigInteger(), nullable=True),
        sa.Column("n_sent_flood", sa.BigInteger(), nullable=True),
        sa.Column("n_sent_direct", sa.BigInteger(), nullable=True),
        sa.Column("n_recv_flood", sa.BigInteger(), nullable=True),
        sa.Column("n_recv_direct", sa.BigInteger(), nullable=True),
        sa.Column("err_events", sa.Integer(), nullable=True),
        sa.Column("last_snr_db", sa.Float(), nullable=True),
        sa.Column("n_direct_dups", sa.Integer(), nullable=True),
        sa.Column("n_flood_dups", sa.Integer(), nullable=True),
        sa.Column("total_rx_air_time_secs", sa.BigInteger(), nullable=True),
        sa.Column("n_recv_errors", sa.BigInteger(), nullable=True),
        sa.Column("neighbours_total", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_repeater_poll"),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entity.id"],
            name="fk_repeater_poll_entity_id_entity",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "outcome IN (" + ", ".join(f"'{o}'" for o in OUTCOMES) + ")",
            name=op.f("ck_repeater_poll_outcome"),
        ),
    )
    op.create_index(
        "ix_repeater_poll_public_key_started_at", "repeater_poll", ["public_key", "started_at"]
    )
    op.create_index("ix_repeater_poll_started_at", "repeater_poll", ["started_at"])
    op.create_table(
        "repeater_neighbour",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("poll_id", sa.BigInteger(), nullable=False),
        sa.Column("prefix", sa.LargeBinary(), nullable=False),
        sa.Column("heard_seconds_ago", sa.BigInteger(), nullable=False),
        sa.Column("snr_db", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_repeater_neighbour"),
        sa.ForeignKeyConstraint(
            ["poll_id"],
            ["repeater_poll.id"],
            name="fk_repeater_neighbour_poll_id_repeater_poll",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_repeater_neighbour_poll_id", "repeater_neighbour", ["poll_id"])


def downgrade() -> None:
    """Drop all four tables. Tested — an untested downgrade does not exist.

    See the module docstring for what this costs: the collection settings,
    every selection and all collected history.
    """
    op.drop_index("ix_repeater_neighbour_poll_id", table_name="repeater_neighbour")
    op.drop_table("repeater_neighbour")
    op.drop_index("ix_repeater_poll_started_at", table_name="repeater_poll")
    op.drop_index("ix_repeater_poll_public_key_started_at", table_name="repeater_poll")
    op.drop_table("repeater_poll")
    op.drop_table("repeater_target")
    op.drop_table("repeater_collection")
