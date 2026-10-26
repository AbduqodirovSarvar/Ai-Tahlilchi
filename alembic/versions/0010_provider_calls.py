"""provider call log: every request sent to API-Football and Gemini, with its answer

- ``provider_calls``: one row per call (cache hits and quota refusals included) so the
  administrator can read what was asked and answered. Pruned by the retention job
  (``CALL_LOG_RETENTION_DAYS``). A new table: existing data is untouched.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-26 09:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "provider_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=255), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("quota_cost", sa.Integer(), server_default="0", nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_calls")),
    )
    op.create_index(op.f("ix_provider_calls_at"), "provider_calls", ["at"], unique=False)
    op.create_index(
        op.f("ix_provider_calls_provider_at"), "provider_calls", ["provider", "at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema (the call log is dropped; it is diagnostic data only)."""
    op.drop_index(op.f("ix_provider_calls_provider_at"), table_name="provider_calls")
    op.drop_index(op.f("ix_provider_calls_at"), table_name="provider_calls")
    op.drop_table("provider_calls")
