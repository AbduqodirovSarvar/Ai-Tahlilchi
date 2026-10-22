"""Migration 0007: recommended odds (backfilled), stored fair odds, price time, ledger retries."""

from pathlib import Path

from alembic import command

from tests.db.test_migrations import SEED_SIGNAL, alembic_config, connect


def test_0007_backfills_recommended_odds_from_the_first_version(tmp_path: Path) -> None:
    db_path = tmp_path / "odds.db"
    config = alembic_config(db_path)
    command.upgrade(config, "0006")
    with connect(db_path) as connection:
        connection.executescript(SEED_SIGNAL)
        connection.executescript(
            # the signal was recommended at 2.0 and recalculated down to 1.7 since
            "UPDATE signals SET odds = 1.7, recommended_at = '2024-01-01' WHERE id = 1;"
            "INSERT INTO signal_versions"
            " (signal_id, version, odds, p_model, ev, reason, created_at)"
            " VALUES (1, 1, 2.0, 0.55, 0.1, 'initial', '2024-01-01'),"
            " (1, 2, 1.7, 0.55, -0.06, 'recalc', '2024-01-02');"
            # a never recommended one keeps no recommended odds
            "INSERT INTO signals (id, user_id, fixture_id, market, selection, line, odds, p_model,"
            " ev, confidence, stake_units, source, status, created_at, updated_at)"
            " VALUES (2, 1, 1, '1x2', 'draw', 0, 3.4, 0.25, -0.1, 'low', '0.0000', 'custom',"
            " 'active', '2024-01-01', '2024-01-01');"
        )

    command.upgrade(config, "0007")

    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT id, odds, recommended_odds, fair_odds, quoted_at FROM signals ORDER BY id"
        ).fetchall()
    assert rows == [(1, 1.7, 2.0, None, None), (2, 3.4, None, None, None)]


def test_0007_adds_retry_state_to_the_ledger_and_downgrades_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    config = alembic_config(db_path)
    command.upgrade(config, "0006")
    with connect(db_path) as connection:
        connection.execute(
            "INSERT INTO pipeline_ledger (job, day, user_id, subject, status, claimed_at)"
            " VALUES ('daily', '2026-10-21', 7, 'digest', 'done', '2026-10-21')"
        )

    command.upgrade(config, "0007")
    with connect(db_path) as connection:
        stored = connection.execute("SELECT attempts, retry_after FROM pipeline_ledger")
        assert stored.fetchall() == [(0, None)]

    command.downgrade(config, "0006")
    with connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(pipeline_ledger)")}
        assert "attempts" not in columns
        assert connection.execute("SELECT count(*) FROM pipeline_ledger").fetchone() == (1,)
        signal_columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    assert not {"recommended_odds", "fair_odds", "quoted_at"} & signal_columns
