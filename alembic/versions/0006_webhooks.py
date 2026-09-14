"""The twelfth table: webhooks that tell outside systems about the mesh.

`0001`-`0005` built eleven tables. This adds one:

* ``webhook`` — a named target that receives events: its URL sealed under
                ``SIGHOP_SECRET_KEY``, the URL's scheme and host in the clear for
                display, a payload format, the triggers it subscribes to, an
                optional hop limit, an enabled flag, and the outcome of its last
                delivery

**The URL is sealed** (seal version 2, a variable-length value) because a Discord,
n8n or Home Assistant webhook URL is itself the posting credential, and §6's
stance is that a database dump must not hand out credentials. ``url_host`` is
stored beside it so a listing needs no secret and shows no path.

**Triggers are ``TEXT[]``, not a database enum.** They are validated in the
repository against the code's ``Trigger`` enumeration, so adding a trigger such as
``new_chatter`` later is a code change with no migration.

**What a downgrade loses.** The table is dropped, so a downgrade **deletes every
webhook**, its sealed URL included. The URLs cannot be recovered from sighop —
nothing else stored them — and must be copied again from wherever they were
issued (the Discord channel settings, the n8n workflow) and re-added with
``sighop webhook add``. Pending deliveries are never stored, so none are lost by
the downgrade itself.

There is no data migration. The eleven tables built by `0001`-`0005` are untouched.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "webhook",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        # `bytes([2]) + SecretBox(SIGHOP_SECRET_KEY).encrypt(url)`.
        sa.Column("sealed_url", sa.LargeBinary(), nullable=False),
        # `scheme://host[:port]`, never a path or query.
        sa.Column("url_host", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        # Validated against `sighop.webhooks.triggers.Trigger` by the repository.
        sa.Column("triggers", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("max_hops", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_delivered_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_failed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_failure", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_webhook"),
        sa.UniqueConstraint("name", name="uq_webhook_name"),
        sa.CheckConstraint("format IN ('json', 'discord')", name=op.f("ck_webhook_format")),
        sa.CheckConstraint(
            "max_hops IS NULL OR max_hops >= 0", name=op.f("ck_webhook_max_hops")
        ),
    )


def downgrade() -> None:
    """Drop the table. Tested — an untested downgrade does not exist.

    See the module docstring for what this costs: every webhook and its URL.
    """
    op.drop_table("webhook")
