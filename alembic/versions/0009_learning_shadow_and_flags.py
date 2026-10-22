"""learning loop: shadow journal flag and the two-consecutive-runs flag of a segment

- ``learning_changes.applied``: false for a shadow-mode entry (what the loop *would* have
  changed). Existing rows were written when every change was applied, so they default to true.
- ``segment_stats.flagged``: the weekly review saw tightening evidence last week; the evidence
  must repeat on the next run before the segment is touched (repeated-testing guard).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-23 09:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "learning_changes",
        sa.Column("applied", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "segment_stats",
        sa.Column("flagged", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema (the shadow flag and the pending flag are dropped)."""
    op.drop_column("segment_stats", "flagged")
    op.drop_column("learning_changes", "applied")
