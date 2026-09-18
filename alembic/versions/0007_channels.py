"""The thirteenth and fourteenth tables: group channels and their history.

`0001`-`0006` built twelve tables. This adds two:

* ``channel``         — a group channel the station holds: a unique name, a kind
                        (``public``, ``hashtag`` or ``psk``), the hashtag for a
                        hashtag channel, the pre-shared key **sealed** under
                        ``SIGHOP_SECRET_KEY`` for a ``psk`` channel, and the
                        one-byte channel hash in the clear
* ``channel_message`` — one channel message in either direction, keyed by
                        ``(channel_id, ref)`` and written once, updated in place

**Keys are sealed by kind.** A pre-shared key is a read-and-post credential and is
sealed (seal version 2). A hashtag channel's key is derived from its name, and the
Public channel's is published in every stock client, so neither is stored as a
secret — which is also what lets this migration add the Public channel without
``SIGHOP_SECRET_KEY``, which migrations do not have.

**The Public channel is added.** One ``public`` row named ``Public``, channel hash
``0x11`` (key ``izOH6cXN6mrJ5e26oRXNcg==``). Adding it transmits nothing: posting
still requires the run's transmit gate. An operator may remove it, and nothing
re-adds it.

**Stored channel text is not encrypted at rest.** ``channel_message.text`` is the
message as it travelled, and a database dump exposes channel content in the clear
— the same trade ``direct_message`` makes, stated here rather than discovered from
a column type.

**What a downgrade loses.** Both tables are dropped, so a downgrade **deletes every
channel and all channel history**. Pre-shared keys cannot be recovered from sighop
and must be re-added from wherever they were shared.

There is no data migration. The twelve tables built by `0001`-`0006` are untouched.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)

PUBLIC_CHANNEL_HASH = 0x11
"""`sha256(base64decode("izOH6cXN6mrJ5e26oRXNcg=="))[0]`. A literal, because a
migration must not import application code that may change after it is written."""


def upgrade() -> None:
    op.create_table(
        "channel",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("hashtag", sa.Text(), nullable=True),
        # `bytes([2]) + SecretBox(SIGHOP_SECRET_KEY).encrypt(key)`, psk only.
        sa.Column("sealed_key", sa.LargeBinary(), nullable=True),
        sa.Column("channel_hash", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_channel"),
        sa.UniqueConstraint("name", name="uq_channel_name"),
        sa.CheckConstraint("kind IN ('public', 'hashtag', 'psk')", name=op.f("ck_channel_kind")),
        sa.CheckConstraint(
            "(kind = 'hashtag') = (hashtag IS NOT NULL)", name=op.f("ck_channel_hashtag")
        ),
        sa.CheckConstraint(
            "(kind = 'psk') = (sealed_key IS NOT NULL)", name=op.f("ck_channel_sealed_key")
        ),
        sa.CheckConstraint("channel_hash BETWEEN 0 AND 255", name=op.f("ck_channel_channel_hash")),
    )
    op.create_table(
        "channel_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("entity_public_key", sa.LargeBinary(), nullable=True),
        sa.Column("unverified_sender_name", sa.Text(), nullable=True),
        # Not encrypted at rest; see the module docstring.
        sa.Column("text", sa.LargeBinary(), nullable=False),
        sa.Column("wire_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("handled_at", TIMESTAMPTZ, nullable=False),
        sa.Column("packet_id", sa.Text(), nullable=True),
        sa.Column("hop_count", sa.SmallInteger(), nullable=True),
        sa.Column("snr_db", sa.Float(), nullable=True),
        sa.Column("rssi_dbm", sa.SmallInteger(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("repeats_heard", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id", name="pk_channel_message"),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channel.id"],
            name="fk_channel_message_channel_id_channel",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("channel_id", "ref", name="uq_channel_message_channel_id_ref"),
        sa.CheckConstraint("direction IN ('in', 'out')", name=op.f("ck_channel_message_direction")),
        sa.CheckConstraint(
            "outcome IN ('awaiting', 'transmitted', 'not_transmitted', 'unknown', 'received')",
            name=op.f("ck_channel_message_outcome"),
        ),
        sa.CheckConstraint(
            "(direction = 'out') = (entity_public_key IS NOT NULL)",
            name=op.f("ck_channel_message_entity_public_key"),
        ),
        sa.CheckConstraint("repeats_heard >= 0", name=op.f("ck_channel_message_repeats_heard")),
    )
    op.create_index(
        "ix_channel_message_channel_id_handled_at",
        "channel_message",
        ["channel_id", "handled_at"],
    )
    op.execute(
        "INSERT INTO channel (name, kind, channel_hash, created_at) "
        f"VALUES ('Public', 'public', {PUBLIC_CHANNEL_HASH}, now())"
    )


def downgrade() -> None:
    """Drop both tables. Tested — an untested downgrade does not exist.

    See the module docstring for what this costs: every channel, every sealed
    pre-shared key and all channel history.
    """
    op.drop_index("ix_channel_message_channel_id_handled_at", table_name="channel_message")
    op.drop_table("channel_message")
    op.drop_table("channel")
