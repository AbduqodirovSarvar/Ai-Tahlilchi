"""user_bets.client_token (idempotent place_bet) and the signal_notes table

Until this revision the pipeline kept both in ``pipeline_ledger``: bet tokens as
``job='bet'`` entries (subject = token, detail = bet id) and notes as ``job='note'``
entries (subject = ``<signal id>:<random>``, detail = text). Upgrade moves them into their
own columns/table (entries whose bet or signal no longer exists are dropped) and removes
them from the ledger; downgrade writes them back.

- ``user_bets.client_token``: nullable, unique per user (NULLs never collide).
- ``signal_notes``: one row per note; deleted with its signal or user.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22 09:20:00.000000+00:00
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BET_JOB = "bet"
NOTE_JOB = "note"
STATUS_DONE = "done"
STAMP_DAY = date(1970, 1, 1)
UNIQUE_TOKEN = "ix_user_bets_user_id_client_token"  # unique index: NULL tokens never collide

ledger = sa.table(
    "pipeline_ledger",
    sa.column("id", sa.Integer()),
    sa.column("job", sa.String()),
    sa.column("day", sa.Date()),
    sa.column("user_id", sa.BigInteger()),
    sa.column("subject", sa.String()),
    sa.column("status", sa.String()),
    sa.column("claimed_at", sa.DateTime(timezone=True)),
    sa.column("done_at", sa.DateTime(timezone=True)),
    sa.column("detail", sa.Text()),
)
user_bets = sa.table(
    "user_bets",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.BigInteger()),
    sa.column("client_token", sa.String()),
    sa.column("placed_at", sa.DateTime(timezone=True)),
)
signals = sa.table("signals", sa.column("id", sa.Integer()), sa.column("user_id", sa.BigInteger()))
signal_notes = sa.table(
    "signal_notes",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.BigInteger()),
    sa.column("signal_id", sa.Integer()),
    sa.column("text", sa.Text()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def _int_or_none(text: str | None) -> int | None:
    try:
        return int(text or "")
    except ValueError:
        return None


def _move_ledger_into_tables(connection: sa.Connection) -> None:
    """Copy ledger bet tokens and notes to their new homes, then forget them in the ledger."""
    if not sa.inspect(connection).has_table(ledger.name):
        return
    bet_ids = {row[0]: row[1] for row in connection.execute(sa.select(user_bets.c.id, user_bets.c.user_id))}
    signal_owners = {row[0]: row[1] for row in connection.execute(sa.select(signals.c.id, signals.c.user_id))}
    entries = connection.execute(
        sa.select(ledger).where(ledger.c.job.in_((BET_JOB, NOTE_JOB))).order_by(ledger.c.id)
    ).all()
    for entry in entries:
        if entry.job == BET_JOB and entry.status == STATUS_DONE:
            bet_id = _int_or_none(entry.detail)
            if bet_id is not None and bet_ids.get(bet_id) == entry.user_id:
                connection.execute(
                    sa.update(user_bets)
                    .where(user_bets.c.id == bet_id, user_bets.c.client_token.is_(None))
                    .values(client_token=entry.subject)
                )
        elif entry.job == NOTE_JOB and entry.detail:
            signal_id = _int_or_none(entry.subject.partition(":")[0])
            if signal_id is not None and signal_owners.get(signal_id) == entry.user_id:
                connection.execute(
                    sa.insert(signal_notes).values(
                        user_id=entry.user_id,
                        signal_id=signal_id,
                        text=entry.detail,
                        created_at=entry.done_at or entry.claimed_at,
                    )
                )
    connection.execute(sa.delete(ledger).where(ledger.c.job.in_((BET_JOB, NOTE_JOB))))


def _move_tables_into_ledger(connection: sa.Connection) -> None:
    """Write bet tokens and notes back as ledger entries (the pre-0006 layout)."""
    for bet in connection.execute(
        sa.select(user_bets).where(user_bets.c.client_token.is_not(None))
    ):
        connection.execute(
            sa.insert(ledger).values(
                job=BET_JOB,
                day=STAMP_DAY,
                user_id=bet.user_id,
                subject=bet.client_token,
                status=STATUS_DONE,
                claimed_at=bet.placed_at,
                done_at=bet.placed_at,
                detail=str(bet.id),
            )
        )
    for note in connection.execute(sa.select(signal_notes).order_by(signal_notes.c.id)):
        connection.execute(
            sa.insert(ledger).values(
                job=NOTE_JOB,
                day=STAMP_DAY,
                user_id=note.user_id,
                subject=f"{note.signal_id}:{note.id}",
                status=STATUS_DONE,
                claimed_at=note.created_at,
                done_at=note.created_at,
                detail=note.text,
            )
        )


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("user_bets", sa.Column("client_token", sa.String(length=64), nullable=True))
    op.create_index(UNIQUE_TOKEN, "user_bets", ["user_id", "client_token"], unique=True)

    op.create_table(
        "signal_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("signal_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["signals.id"],
            name=op.f("fk_signal_notes_signal_id_signals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_signal_notes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signal_notes")),
    )
    with op.batch_alter_table("signal_notes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_signal_notes_signal_id_created_at"),
            ["signal_id", "created_at"],
            unique=False,
        )

    if not context.is_offline_mode():
        _move_ledger_into_tables(op.get_bind())


def downgrade() -> None:
    """Downgrade schema (tokens and notes go back into the ledger)."""
    if not context.is_offline_mode():
        _move_tables_into_ledger(op.get_bind())

    with op.batch_alter_table("signal_notes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_signal_notes_signal_id_created_at"))
    op.drop_table("signal_notes")

    op.drop_index(UNIQUE_TOKEN, table_name="user_bets")
    op.drop_column("user_bets", "client_token")
