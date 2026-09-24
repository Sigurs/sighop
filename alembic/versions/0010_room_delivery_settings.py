"""`room` carries its two history delivery settings.

Two nullable integers, so that an operator can tune how a room pushes history to
its members on a mesh the firmware's defaults do not suit:

* ``room.push_ack_window_seconds`` — how long a push waits for the member's
                                     acknowledgement once it has been
                                     transmitted, for flooded and direct pushes
                                     alike; NULL keeps the firmware's windows
* ``room.push_recent_days``        — push history only to members heard, by
                                     room activity or by advert, within this
                                     many days; NULL pushes to every member

**NULL is the default, and the default is today's behaviour.** A room created
before or after this migration uses the firmware's windows and pushes to every
member until an operator sets either value.

**Settings, never state.** Neither column records anything about a member or a
delivery. A member skipped for recency keeps its sync position in
``room_member``; nothing here moves it.

**No data migration.** Both columns are nullable with no server default, so
every existing row keeps working exactly as it did.

**What a downgrade loses.** Both columns are dropped, so every room returns to
the firmware's windows and to pushing to every member. No member, message or
sync position is touched.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("room", sa.Column("push_ack_window_seconds", sa.Integer(), nullable=True))
    op.add_column("room", sa.Column("push_recent_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop both columns. Tested — an untested downgrade does not exist.

    See the module docstring for what this costs: every room's delivery
    settings, and nothing else.
    """
    op.drop_column("room", "push_recent_days")
    op.drop_column("room", "push_ack_window_seconds")
