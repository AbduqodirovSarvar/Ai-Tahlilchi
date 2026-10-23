"""Migration 0009: shadow flag of the change journal and the pending flag of a segment."""

from pathlib import Path

from alembic import command

from tests.db.test_migration_0008 import columns
from tests.db.test_migrations import alembic_config, connect

OLD_ROWS = (
    "INSERT INTO learning_changes (id, kind, subject, before, after, reason, created_at)"
    " VALUES (1, 'segment_disabled', 'x', '{}', '{}', 'why', '2026-10-22');"
)


def test_0009_defaults_old_entries_to_applied_and_downgrades_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "shadow.db"
    config = alembic_config(db_path)
    command.upgrade(config, "0008")
    with connect(db_path) as connection:
        connection.executescript(OLD_ROWS)

    command.upgrade(config, "head")
    command.check(config)  # no drift between the migrations and the ORM models

    with connect(db_path) as connection:
        # everything journaled before the shadow mode existed was really applied
        assert connection.execute("SELECT applied FROM learning_changes").fetchall() == [(1,)]
        assert "flagged" in columns(connection, "segment_stats")

    command.downgrade(config, "0008")

    with connect(db_path) as connection:
        assert "applied" not in columns(connection, "learning_changes")
        assert "flagged" not in columns(connection, "segment_stats")
        assert connection.execute("SELECT COUNT(*) FROM learning_changes").fetchone() == (1,)

    command.upgrade(config, "head")  # and up again
    command.check(config)
