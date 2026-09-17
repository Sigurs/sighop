"""`entity.sealed_seed` becomes `entity.sealed_private_key`.

A local identity is held as MeshCore's 64-byte ``prv_key`` rather than as the
32-byte seed it expands from, because that is the only representation an
operator can bring in from a device — the seed behind one is behind a SHA-512
nobody can walk backwards. The column is renamed because what it holds changed.

**No key material is read, decrypted or rewritten.** This is a column rename and
nothing else, so it runs with no ``SIGHOP_SECRET_KEY`` in the environment, as
every migration before it has.

**Every row that exists when this runs will stop opening.** Their ciphertext
holds a seed, and seeds are no longer a format this system reads. Nothing here
deletes them: the rows stay, `keys list` still lists them, and loading one
refuses by name saying the row is intact and the format is gone. The count is
reported below so that this is met at deploy time rather than at the next start,
when a run would refuse the identity it was about to advert from.

A count only, never a name: migration output lands in deploy logs, and identity
names do not belong there. ``sighop keys list`` has the names.

**Carrying an identity forward** means supplying its 64-byte private key to
``sighop keys import --private-key``. An identity whose key is held only as a
sighop seed has to be expanded outside sighop — ``sha512(seed)``, then
``[0] &= 248; [31] &= 63; [31] |= 64`` — or recreated, which changes its public
key and means every peer re-adds it.

**What a downgrade does.** Reverses the rename, and nothing else. Rows stranded
by this migration become readable again by the previous build; rows written
*after* it hold 64 bytes and the previous build will report those as corrupt.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _report_stranded_rows() -> None:
    """Say how many identities this migration takes out of service.

    Every row present at this moment was sealed by a build that wrote seeds, so
    the count is simply the table's size. Reading it is a ``count(*)``: no
    column holding key material is selected, and nothing is decrypted.
    """
    stranded = op.get_bind().execute(sa.text("SELECT count(*) FROM entity")).scalar_one()
    if not stranded:
        return
    # A print, not a log line: a migration's report reaches an operator through
    # the terminal or the deploy log it ran in, and this one is the only warning
    # they get before a run refuses the identity it was about to advert from.
    print(
        f"0008: {stranded} stored "
        f"{'identity holds' if stranded == 1 else 'identities hold'} a sealed seed "
        "and will no longer open. The rows are intact and nothing here deletes "
        "them. Re-import each identity with `sighop keys import --private-key`; "
        "`sighop keys list` names them."
    )


def upgrade() -> None:
    _report_stranded_rows()
    op.alter_column("entity", "sealed_seed", new_column_name="sealed_private_key")


def downgrade() -> None:
    op.alter_column("entity", "sealed_private_key", new_column_name="sealed_seed")
