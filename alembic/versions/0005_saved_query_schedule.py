"""saved_queries: structured flags and schedule instead of the ``schedule_cron`` text

- ``per_day`` / ``include_no_value``: query flags that used to ride in ``schedule_cron`` as
  ``# per_day no_value``.
- ``schedule_weekdays`` (JSON list of ISO weekdays, empty = not scheduled) and
  ``schedule_at`` (local time of day, NULL = not scheduled) replace the cron text.
- Existing rows are converted; a ``schedule_cron`` that cannot be parsed becomes "not
  scheduled" (the flags in it are still honoured).
- Downgrade writes the old text format back.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22 09:10:00.000000+00:00
"""

from collections.abc import Sequence
from datetime import time

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FLAG_PER_DAY = "per_day"
FLAG_NO_VALUE = "no_value"
CRON_NOTE = " # "
CRON_FIELDS = 5
MAX_MINUTE = 59
MAX_HOUR = 23

saved_queries = sa.table(
    "saved_queries",
    sa.column("id", sa.Integer()),
    sa.column("schedule_cron", sa.String()),
    sa.column("per_day", sa.Boolean()),
    sa.column("include_no_value", sa.Boolean()),
    sa.column("schedule_weekdays", sa.JSON()),
    sa.column("schedule_at", sa.Time()),
)


def _parse_schedule(cron: str) -> tuple[list[int], time | None]:
    parts = cron.split()
    if len(parts) != CRON_FIELDS:
        return [], None
    try:
        minute, hour = int(parts[0]), int(parts[1])
        days = sorted({int(day) for day in parts[4].split(",")})
    except ValueError:
        return [], None
    if 0 <= minute <= MAX_MINUTE and 0 <= hour <= MAX_HOUR and days:
        return days, time(hour, minute)
    return [], None


def parse_cron(text: str | None) -> tuple[list[int], time | None, bool, bool]:
    """``(weekdays, at, per_day, include_no_value)`` from the old ``schedule_cron`` text."""
    if not text:
        return [], None, False, False
    cron, _, flag_text = text.partition(CRON_NOTE.strip())
    flags = flag_text.split()
    weekdays, at = _parse_schedule(cron)
    return weekdays, at, FLAG_PER_DAY in flags, FLAG_NO_VALUE in flags


def format_cron(
    weekdays: list[int], at: time | None, *, per_day: bool, no_value: bool
) -> str | None:
    """The old ``schedule_cron`` text for the structured values."""
    flags = [flag for flag, on in ((FLAG_PER_DAY, per_day), (FLAG_NO_VALUE, no_value)) if on]
    cron = f"{at.minute} {at.hour} * * " + ",".join(str(day) for day in weekdays) if at else ""
    if not cron and not flags:
        return None
    return cron + (CRON_NOTE + " ".join(flags) if flags else "")


def upgrade() -> None:
    """Upgrade schema."""
    # Plain ALTER TABLE (not batch): works offline too; SQLite >= 3.35 drops columns natively.
    op.add_column(
        "saved_queries",
        sa.Column("per_day", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "saved_queries",
        sa.Column("include_no_value", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "saved_queries",
        sa.Column("schedule_weekdays", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.add_column("saved_queries", sa.Column("schedule_at", sa.Time(), nullable=True))

    connection = op.get_bind()
    rows = (
        []  # offline SQL carries no data
        if context.is_offline_mode()
        else connection.execute(
            sa.select(saved_queries.c.id, saved_queries.c.schedule_cron)
        ).all()
    )
    for row_id, cron in rows:
        weekdays, at, per_day, no_value = parse_cron(cron)
        connection.execute(
            sa.update(saved_queries)
            .where(saved_queries.c.id == row_id)
            .values(
                per_day=per_day,
                include_no_value=no_value,
                schedule_weekdays=weekdays,
                schedule_at=at,
            )
        )

    op.drop_column("saved_queries", "schedule_cron")


def downgrade() -> None:
    """Downgrade schema (the structured values are written back as the old text)."""
    op.add_column("saved_queries", sa.Column("schedule_cron", sa.String(length=64), nullable=True))

    connection = op.get_bind()
    rows = [] if context.is_offline_mode() else connection.execute(
        sa.select(
            saved_queries.c.id,
            saved_queries.c.per_day,
            saved_queries.c.include_no_value,
            saved_queries.c.schedule_weekdays,
            saved_queries.c.schedule_at,
        )
    ).all()
    for row_id, per_day, no_value, weekdays, at in rows:
        text = format_cron(list(weekdays or []), at, per_day=per_day, no_value=no_value)
        connection.execute(
            sa.update(saved_queries).where(saved_queries.c.id == row_id).values(schedule_cron=text)
        )

    for name in ("schedule_at", "schedule_weekdays", "include_no_value", "per_day"):
        op.drop_column("saved_queries", name)
