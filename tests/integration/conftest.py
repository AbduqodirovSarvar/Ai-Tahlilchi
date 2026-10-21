"""Fixtures for integration tests that drive real entry points against a migrated database."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config

from tests.integration.history_helpers import REPO_ROOT, ImportWorkspace


@pytest.fixture(scope="session")
def import_history() -> ModuleType:
    """``scripts/import_history.py`` loaded as a module (it is not part of the package)."""
    spec = importlib.util.spec_from_file_location(
        "qa_import_history", REPO_ROOT / "scripts" / "import_history.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    """A fresh SQLite file built by the real Alembic migrations (not ``create_all``)."""
    path = tmp_path / "qa.db"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["db_path"] = path
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    return path


@pytest.fixture
def workspace(import_history: ModuleType, migrated_db: Path, tmp_path: Path) -> ImportWorkspace:
    """Importer + migrated database + an empty football-data cache directory."""
    cache_dir = tmp_path / "history"
    cache_dir.mkdir()
    return ImportWorkspace(script=import_history, db=migrated_db, cache_dir=cache_dir)
