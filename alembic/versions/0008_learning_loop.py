"""learning loop: raw probability, segment state, calibration gate flag, change journal

- ``signals.p_raw``: the model probability before the active calibration; the learning job
  trains on it so a calibration is never fitted on its own output. NULL = ``p_model`` is raw.
- ``segment_stats.state_since`` / ``hold_until``: when the current restriction began and until
  when automatic review leaves the segment alone (after a manual rollback).
- ``calibration_versions.passed_gate``: the fit beat the raw model on its hold-out; only such a
  version may be active. Existing rows (none are written before this revision) default to false.
- ``learning_changes``: journal of what the weekly job changed, with ``before``/``after`` so a
  change can be listed (``/ozgarishlar``) and rolled back.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-22 18:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("signals", sa.Column("p_raw", sa.Double(), nullable=True))
    op.add_column(
        "segment_stats", sa.Column("state_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "segment_stats", sa.Column("hold_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "calibration_versions",
        sa.Column("passed_gate", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_table(
        "learning_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rolled_back", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_learning_changes")),
    )
    op.create_index(
        op.f("ix_learning_changes_created_at"), "learning_changes", ["created_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema (the change journal and the new columns are dropped)."""
    op.drop_index(op.f("ix_learning_changes_created_at"), table_name="learning_changes")
    op.drop_table("learning_changes")
    op.drop_column("calibration_versions", "passed_gate")
    op.drop_column("segment_stats", "hold_until")
    op.drop_column("segment_stats", "state_since")
    op.drop_column("signals", "p_raw")
