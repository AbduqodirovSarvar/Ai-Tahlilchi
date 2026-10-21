"""single-moment odds identity, user loss limits, user_bets.signal_id index

- At most one opening and one closing price per fixture, bookmaker, market, selection
  and line (partial unique index). Rows that would violate it (left by imports before this
  revision, when ``taken_at`` was part of the key) are reduced to the most recent one.
- ``user_settings.daily_loss_limit`` / ``weekly_loss_limit`` (plan §6.9), nullable money.
- Index on ``user_bets.signal_id`` (foreign key lookups, ON DELETE SET NULL).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21 09:20:42.941043+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SINGLE_MOMENT = "kind IN ('opening', 'closing')"
MONEY = sa.Numeric(precision=18, scale=4).with_variant(sa.String(length=20), "sqlite")


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "DELETE FROM odds_snapshots"
        f" WHERE {SINGLE_MOMENT} AND id NOT IN ("
        "  SELECT max(id) FROM odds_snapshots"
        f"  WHERE {SINGLE_MOMENT}"
        "  GROUP BY fixture_id, bookmaker, market, selection, line, kind)"
    )
    with op.batch_alter_table("odds_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            "uq_odds_snapshots_single_moment",
            ["fixture_id", "bookmaker", "market", "selection", "line", "kind"],
            unique=True,
            sqlite_where=sa.text(SINGLE_MOMENT),
            postgresql_where=sa.text(SINGLE_MOMENT),
        )

    with op.batch_alter_table("user_bets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_user_bets_signal_id"), ["signal_id"], unique=False)

    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("daily_loss_limit", MONEY, nullable=True))
        batch_op.add_column(sa.Column("weekly_loss_limit", MONEY, nullable=True))


def downgrade() -> None:
    """Downgrade schema (the duplicate prices removed by upgrade are not restored)."""
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("weekly_loss_limit")
        batch_op.drop_column("daily_loss_limit")

    with op.batch_alter_table("user_bets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_bets_signal_id"))

    with op.batch_alter_table("odds_snapshots", schema=None) as batch_op:
        batch_op.drop_index("uq_odds_snapshots_single_moment")
