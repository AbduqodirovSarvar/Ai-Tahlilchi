"""Migration 0011: card and referee columns on fixtures, on top of existing data."""

from pathlib import Path

from alembic import command

from tests.db.test_migrations import alembic_config, connect

NEW_COLUMNS = {"home_yellow", "away_yellow", "home_red", "away_red", "referee"}


def _columns(db_path: Path) -> set[str]:
    with connect(db_path) as connection:
        return {row[1] for row in connection.execute("PRAGMA table_info('fixtures')")}


def test_0011_adds_nullable_cards_and_referee_and_downgrades_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "cards.db"
    config = alembic_config(db_path)
    command.upgrade(config, "0010")
    assert not NEW_COLUMNS & _columns(db_path)

    command.upgrade(config, "head")
    command.check(config)  # no drift between the migration and the ORM model
    assert _columns(db_path) >= NEW_COLUMNS

    command.downgrade(config, "0010")
    assert not NEW_COLUMNS & _columns(db_path)
    command.upgrade(config, "head")
    command.check(config)
