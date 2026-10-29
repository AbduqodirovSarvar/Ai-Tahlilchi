"""fixture cards and referee: the facts the card model learns from

- ``fixtures.home_yellow`` / ``away_yellow`` / ``home_red`` / ``away_red``: card counts of a
  played match (nullable: not every source publishes them).
- ``fixtures.referee``: the referee's name as the source spells it (nullable).

New nullable columns only: existing rows are untouched until a source fills them (a history
re-import, or a later result). ADR 0027.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-29 12:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CARD_COLUMNS = ("home_yellow", "away_yellow", "home_red", "away_red")


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("fixtures", schema=None) as batch_op:
        for name in _CARD_COLUMNS:
            batch_op.add_column(sa.Column(name, sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("referee", sa.String(length=128), nullable=True))


def downgrade() -> None:
    """Downgrade schema (stored cards and referees are dropped)."""
    with op.batch_alter_table("fixtures", schema=None) as batch_op:
        batch_op.drop_column("referee")
        for name in reversed(_CARD_COLUMNS):
            batch_op.drop_column(name)
