"""signal recommendation time and creation thresholds (service contract v1.1)

- ``signals.recommended_at``: when the selection first became a recommendation (UTC,
  nullable, set once).
- ``signals.min_prob`` / ``min_ev`` / ``odds_min`` / ``odds_max``: thresholds the signal
  was created with (recalc verdicts use them). Nullable: rows from before this revision
  have no recorded thresholds.
- Index ``(user_id, recommended_at)`` for report windows.
- ``signals.source`` gains the values ``analysis`` and ``retro``: enum columns have no
  database CHECK, so no DDL is needed for that.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21 12:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

THRESHOLD_COLUMNS = ("min_prob", "min_ev", "odds_min", "odds_max")


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("signals", schema=None) as batch_op:
        batch_op.add_column(sa.Column("recommended_at", sa.DateTime(timezone=True), nullable=True))
        for name in THRESHOLD_COLUMNS:
            batch_op.add_column(sa.Column(name, sa.Double(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_signals_user_id_recommended_at"),
            ["user_id", "recommended_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema (recommendation times and thresholds are dropped)."""
    with op.batch_alter_table("signals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_signals_user_id_recommended_at"))
        for name in reversed(THRESHOLD_COLUMNS):
            batch_op.drop_column(name)
        batch_op.drop_column("recommended_at")
