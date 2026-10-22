"""pipeline_ledger: the pipeline's idempotency keys, stamps, run status and errors

The table used to be created by the pipeline itself (``CREATE TABLE IF NOT EXISTS``), so a
database that ran the pipeline before this revision already has it, with the same columns
and names; then this revision changes nothing. A fresh database gets it here.

Downgrade drops the table: it holds only derived state (what was already sent or fetched),
so the worst case after a downgrade is that the pipeline repeats work it can safely repeat.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22 09:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "pipeline_ledger"
INDEX = "ix_pipeline_ledger_job_done_at"
OLD_INDEX = "ix_pipeline_ledger_job_done"


def _adopt_existing(inspector: sa.Inspector) -> None:
    """Rename the index of a table the pipeline created itself to the ORM naming."""
    names = {index["name"] for index in inspector.get_indexes(TABLE)}
    if OLD_INDEX not in names:
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_index(OLD_INDEX)
        batch_op.create_index(INDEX, ["job", "done_at"], unique=False)


def upgrade() -> None:
    """Upgrade schema."""
    if not context.is_offline_mode():  # offline SQL cannot look at the database
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table(TABLE):
            _adopt_existing(inspector)
            return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job", sa.String(length=48), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("subject", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_ledger")),
        sa.UniqueConstraint("job", "day", "user_id", "subject", name="uq_pipeline_ledger_key"),
    )
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.create_index(INDEX, ["job", "done_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema (ledger entries are dropped)."""
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_index(INDEX)
    op.drop_table(TABLE)
