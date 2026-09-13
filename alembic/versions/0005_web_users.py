"""The eleventh table: operator accounts for the web interface (milestone 9 D1).

`0001`-`0004` built ten tables about the mesh and the identities on it. This adds
one that is about the people operating it:

* ``web_user`` — one account that can sign in to the web interface: a normalised
                 username, an Argon2id hash, an enabled flag, and when its
                 password was last set

**Usernames are unique as stored, and stored normalised** (NFKC then
``casefold``), so ``UNIQUE (username)`` is a case-insensitive uniqueness without
the ``citext`` extension the measured role may not be able to create.

**``password_set_at`` is the credential epoch.** A session records the value it
was issued under and ends when the row says otherwise, which is how a
``sighop web user passwd`` in another process reaches a running panel.

**What a downgrade loses.** The table is dropped, so a downgrade **deletes every
account**. No password can be recovered from anywhere else — only the hashes
were ever stored — and a run started with ``--web`` then cannot start: it
refuses a database with no enabled account, and after a downgrade there is no
table to hold one. Recreating them means upgrading again and running
``sighop web user add`` for each operator.

There is no data migration. The ten tables built by `0001`-`0004` are untouched.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "web_user",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Normalised by `normalise_username` before it is written; the unique
        # constraint below is only case-insensitive because of that.
        sa.Column("username", sa.Text(), nullable=False),
        # The encoded `$argon2id$…` string, parameters and salt included.
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        # The credential epoch sessions are revalidated against.
        sa.Column("password_set_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_web_user"),
        sa.UniqueConstraint("username", name="uq_web_user_username"),
    )


def downgrade() -> None:
    """Drop the table. Tested — an untested downgrade does not exist.

    See the module docstring for what this costs: every account, and with them
    the ability of any run to start its web interface until accounts are added
    again.
    """
    op.drop_table("web_user")
