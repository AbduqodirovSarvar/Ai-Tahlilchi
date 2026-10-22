"""Migration 0008: raw probability, segment state, calibration gate flag, change journal."""

from pathlib import Path

from alembic import command

from tests.db.test_migrations import SEED_SIGNAL, alembic_config, connect


def columns(connection, table: str) -> set[str]:  # type: ignore[no-untyped-def]
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_0008_adds_columns_and_the_journal_and_downgrades_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "learning.db"
    config = alembic_config(db_path)
    command.upgrade(config, "0007")
    with connect(db_path) as connection:
        connection.executescript(SEED_SIGNAL)

    command.upgrade(config, "0008")

    with connect(db_path) as connection:
        assert connection.execute("SELECT id, p_raw FROM signals").fetchall() == [(1, None)]
        assert {"state_since", "hold_until"} <= columns(connection, "segment_stats")
        assert "passed_gate" in columns(connection, "calibration_versions")
        connection.execute(
            "INSERT INTO learning_changes (kind, subject, before, after, reason, created_at)"
            " VALUES ('segment_raised', 'x', '{}', '{}', 'why', '2026-10-22')"
        )
        assert connection.execute("SELECT rolled_back FROM learning_changes").fetchall() == [(0,)]

    command.downgrade(config, "0007")

    with connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "learning_changes" not in tables
        assert "p_raw" not in columns(connection, "signals")
        assert "passed_gate" not in columns(connection, "calibration_versions")
        assert connection.execute("SELECT COUNT(*) FROM signals").fetchone() == (1,)
