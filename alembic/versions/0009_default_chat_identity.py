"""`web_user` carries the account's default chat identity.

One nullable reference, so that an operator who almost always chats as the same
identity is not asked to choose it on every composer:

* ``web_user.default_entity_id`` — the ``entity`` row this account composes
                                   channel posts and direct messages as, unless
                                   another identity is chosen for that message

**The foreign key is the cleanup.** ``ON DELETE SET NULL`` means removing an
identity clears every default naming it, in the database rather than in a route
that can be forgotten — a default that outlived its identity would be a
composer quietly posting as something else.

**A preference, never a privilege.** The column grants nothing and is read only
by the interface the account signs into. It holds no key material and is not
consulted by the radio, the scheduler or any send path.

**No data migration.** The column is nullable with no server default: no
operator holds a default until they set one, and every existing row keeps
working exactly as it did.

**What a downgrade loses.** The column is dropped, so every operator's default
identity is forgotten and their composers ask for an identity again. No account,
identity or message is touched, and nothing else depends on the value.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "web_user",
        sa.Column("default_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_web_user_default_entity_id_entity",
        "web_user",
        "entity",
        ["default_entity_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Drop the column and its constraint. Tested — an untested downgrade does
    not exist.

    See the module docstring for what this costs: every operator's default chat
    identity, and nothing else.
    """
    op.drop_constraint("fk_web_user_default_entity_id_entity", "web_user", type_="foreignkey")
    op.drop_column("web_user", "default_entity_id")
