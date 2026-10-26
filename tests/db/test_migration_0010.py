"""Migration 0010: the provider call log table, with its indexes, on top of existing data."""

from pathlib import Path

from alembic import command

from tests.db.test_migrations import alembic_config, connect

OLD_ROWS = (
    "INSERT INTO learning_changes (id, kind, subject, before, after, reason, created_at)"
    " VALUES (1, 'segment_disabled', 'x', '{}', '{}', 'why', '2026-10-22');"
)


def test_0010_adds_the_call_log_and_downgrades_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "calls.db"
    config = alembic_config(db_path)
    command.upgrade(config, "0009")
    with connect(db_path) as connection:
        connection.executescript(OLD_ROWS)

    command.upgrade(config, "head")
    command.check(config)  # no drift between the migration and the ORM model

    with connect(db_path) as connection:
        connection.execute(
            "INSERT INTO provider_calls (at, provider, operation, outcome, duration_ms, request)"
            " VALUES ('2026-10-26 09:00:00', 'gemini', 'ReviewBatch', 'ok', 5, 'prompt')"
        )
        quota_cost = connection.execute("SELECT quota_cost FROM provider_calls").fetchone()
        assert quota_cost == (0,)  # the server default
        indexes = {row[1] for row in connection.execute("PRAGMA index_list('provider_calls')")}
        assert {"ix_provider_calls_at", "ix_provider_calls_provider_at"} <= indexes

    command.downgrade(config, "0009")

    with connect(db_path) as connection:
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "provider_calls" not in tables
        assert connection.execute("SELECT COUNT(*) FROM learning_changes").fetchone() == (1,)

    command.upgrade(config, "head")  # and up again
    command.check(config)
