"""Small synthetic histories shared by the backtest tests (built once per session)."""

import pytest

from app.domain import HistoryBatch
from app.models.backtest import (
    BacktestFixture,
    SyntheticConfig,
    WalkForwardConfig,
    WalkForwardResult,
    build_dataset,
    generate_history,
    walk_forward,
)

# 2 leagues x 8 teams x 3 seasons: 336 fixtures, enough for every code path in < 1 s.
SMALL = SyntheticConfig(leagues=2, teams_per_league=8, seasons=3, first_season=2020, seed=7)
VALIDATION_SEASON = 2021
HOLDOUT_SEASON = 2022


@pytest.fixture(scope="session")
def small_batch() -> HistoryBatch:
    return generate_history(SMALL)


@pytest.fixture(scope="session")
def small_dataset(small_batch: HistoryBatch) -> tuple[BacktestFixture, ...]:
    return build_dataset(small_batch)


@pytest.fixture(scope="session")
def small_run(small_dataset: tuple[BacktestFixture, ...]) -> WalkForwardResult:
    return walk_forward(
        small_dataset, WalkForwardConfig(refit_days=14), seasons={VALIDATION_SEASON, HOLDOUT_SEASON}
    )
