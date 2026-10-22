"""Migration 0008 over a database that already holds learning rows (T5 QA)."""

from pathlib import Path

from alembic import command

from tests.db.test_migrations import SEED_SIGNAL, alembic_config, connect

OLD_ROWS = (
    "INSERT INTO segment_stats (id, sport, league_id, market, odds_bucket, n, roi, clv,"
    " calib_bias, ev_threshold, enabled, updated_at)"
    " VALUES (1, 'football', 1, '1x2', '1.80-2.20', 80, -0.07, NULL, 0.02, 0.08, 0, '2024-01-01');"
    "INSERT INTO calibration_versions (id, version, method, params, n_samples, brier_before,"
    " brier_after, active, created_at)"
    " VALUES (1, 1, 'isotonic', '{\"x\": [0.5], \"y\": [0.5]}', 300, 0.25, 0.24, 1, '2024-01-01');"
)


def test_existing_learning_rows_survive_up_and_down_and_the_schema_matches_the_orm(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "learning_data.db"
    config = alembic_config(db_path)
    command.upgrade(config, "0007")
    with connect(db_path) as connection:
        connection.executescript(SEED_SIGNAL + OLD_ROWS)

    command.upgrade(config, "head")
    command.check(config)  # no drift between the migrations and the ORM models

    with connect(db_path) as connection:
        segment = connection.execute(
            "SELECT enabled, ev_threshold, state_since, hold_until FROM segment_stats"
        ).fetchone()
        calibration = connection.execute(
            "SELECT active, passed_gate, n_samples FROM calibration_versions"
        ).fetchone()
    assert segment == (0, 0.08, None, None)  # a disabled segment stays disabled, no hold
    # the pre-existing version never passed the new gate, so it is not trusted blindly
    assert calibration == (1, 0, 300)

    command.downgrade(config, "0007")
    with connect(db_path) as connection:
        assert connection.execute("SELECT n, enabled FROM segment_stats").fetchall() == [(80, 0)]
        assert connection.execute("SELECT version FROM calibration_versions").fetchall() == [(1,)]

    command.upgrade(config, "head")  # and up again
    command.check(config)
