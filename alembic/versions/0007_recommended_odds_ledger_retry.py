"""recommended odds, stored fair odds and ledger retry bookkeeping

- ``signals.recommended_odds``: the odds the user was first told; set once together with
  ``recommended_at`` and never changed, while ``signals.odds`` follows recalculations. Profit
  and CLV are judged on it (decision #17). Backfilled from each signal's first
  ``signal_versions`` row (its odds when it was first stored), falling back to ``odds``.
- ``signals.fair_odds``: the zero-EV odds of the selection (not ``1 / p_model`` for a
  quarter-line Asian pick). Nullable: old rows have none and readers fall back to
  ``1 / p_model``.
- ``signals.quoted_at``: when the bookmaker price behind ``odds`` was taken (the card's "data
  as of"); nullable, old rows have none.
- ``pipeline_ledger.attempts`` / ``retry_after``: failed attempts of a key and the earliest
  time the next one may start (exponential backoff), so a failing delivery is retried
  rarely instead of every minute and gives up after a few attempts.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22 14:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = sa.text(
    "UPDATE signals SET recommended_odds = COALESCE("
    "(SELECT v.odds FROM signal_versions v WHERE v.signal_id = signals.id "
    "ORDER BY v.version ASC LIMIT 1), odds) "
    "WHERE recommended_at IS NOT NULL"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("signals", sa.Column("recommended_odds", sa.Double(), nullable=True))
    op.add_column("signals", sa.Column("fair_odds", sa.Double(), nullable=True))
    op.add_column("signals", sa.Column("quoted_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(BACKFILL)
    op.add_column(
        "pipeline_ledger",
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "pipeline_ledger", sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema (recommended odds, fair odds and retry state are dropped)."""
    op.drop_column("pipeline_ledger", "retry_after")
    op.drop_column("pipeline_ledger", "attempts")
    op.drop_column("signals", "quoted_at")
    op.drop_column("signals", "fair_odds")
    op.drop_column("signals", "recommended_odds")
