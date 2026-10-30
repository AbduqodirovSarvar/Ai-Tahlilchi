"""Production wiring of the data aggregator."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.errors import ConfigurationError
from app.pipeline.aggregator import open_data_aggregator
from app.sports.aggregator import DataAggregatorService


async def test_needs_the_football_data_key(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "a.db")  # type: ignore[call-arg]
    with pytest.raises(ConfigurationError, match="FOOTBALL_DATA_ORG_KEY"):
        async with open_data_aggregator(settings):
            pass


async def test_opens_and_closes_with_a_key(tmp_path: Path) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        db_path=tmp_path / "a.db",
        football_data_org_key=SecretStr("k" * 24),
        odds_csv_cache_dir=tmp_path / "csv",
    )
    async with open_data_aggregator(settings) as aggregator:
        assert isinstance(aggregator, DataAggregatorService)
