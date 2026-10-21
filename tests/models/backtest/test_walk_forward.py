"""Walk-forward correctness, above all: no information from the future reaches a prediction."""

import importlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

import pytest

from app.domain import Market, OddsKind
from app.models import DixonColesModel, MatchRecord, ModelInputError
from app.models.backtest import (
    BacktestFixture,
    PricingConfig,
    SkipReason,
    StakingConfig,
    WalkForwardConfig,
    WalkForwardResult,
    candidate_bets,
    walk_forward,
)
from app.models.poisson import fit_dixon_coles
from tests.models.backtest.conftest import HOLDOUT_SEASON, VALIDATION_SEASON

# The package re-exports the function under the module's name, so fetch the module itself.
walk_forward_module = importlib.import_module("app.models.backtest.walk_forward")


def test_predictions_cover_the_requested_seasons_in_date_order(
    small_run: WalkForwardResult, small_dataset: tuple[BacktestFixture, ...]
) -> None:
    expected = [i for i in small_dataset if i.season in {VALIDATION_SEASON, HOLDOUT_SEASON}]
    assert len(small_run.predictions) == len(expected)
    assert small_run.skipped == {}
    kickoffs = [p.item.fixture.kickoff for p in small_run.predictions]
    assert kickoffs == sorted(kickoffs)
    first = small_run.predictions[0]
    assert set(first.model) == {q.market for q in first.item.quotes if q.kind is OddsKind.OPENING}
    assert Market.match_result() in first.market
    assert Market.match_result() in first.closing
    assert first.team_matches >= 6


def test_every_fit_uses_only_matches_strictly_before_as_of(
    small_dataset: tuple[BacktestFixture, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    fits: list[tuple[date, date | None]] = []

    def recording_fit(matches: Sequence[MatchRecord], as_of: date, *, xi: float) -> DixonColesModel:
        fits.append((as_of, max((m.match_date for m in matches), default=None)))
        return fit_dixon_coles(matches, as_of, xi=xi)

    monkeypatch.setattr(walk_forward_module, "fit_dixon_coles", recording_fit)
    run = walk_forward(small_dataset, WalkForwardConfig(refit_days=7), seasons={HOLDOUT_SEASON})
    assert fits
    assert run.fits == len(fits)
    for as_of, newest in fits:
        assert newest is not None
        assert newest < as_of
    for prediction in run.predictions:
        assert prediction.as_of <= prediction.item.match_date
        assert (prediction.item.match_date - prediction.as_of).days < 7


def test_changing_the_future_does_not_change_past_predictions(
    small_dataset: tuple[BacktestFixture, ...],
) -> None:
    # Black-box leakage test: rewrite every score from the cut-off date on (and drop their
    # closing prices); predictions up to the cut-off date must be bit-for-bit identical.
    holdout = [i for i in small_dataset if i.season == HOLDOUT_SEASON]
    cutoff = holdout[len(holdout) // 2].match_date

    def scramble(item: BacktestFixture) -> BacktestFixture:
        if item.match_date < cutoff:
            return item
        record = replace(item.record, home_goals=item.record.away_goals + 3, away_goals=0)
        return replace(item, record=record)

    config = WalkForwardConfig(refit_days=7)
    original = walk_forward(small_dataset, config, seasons={HOLDOUT_SEASON})
    altered = walk_forward([scramble(i) for i in small_dataset], config, seasons={HOLDOUT_SEASON})

    def known(run: WalkForwardResult) -> list[tuple[str, object]]:
        return [(p.item.key, dict(p.model)) for p in run.predictions if p.item.match_date <= cutoff]

    assert known(original) == known(altered)
    assert len(known(original)) > 10


def test_closing_prices_never_influence_the_bet_decision(small_run: WalkForwardResult) -> None:
    def without_closing(run: WalkForwardResult) -> WalkForwardResult:
        stripped = tuple(replace(p, closing={}) for p in run.predictions)
        return replace(run, predictions=stripped)

    def decisions(run: WalkForwardResult) -> list[tuple[str, str, float, float]]:
        bets = candidate_bets(
            run.predictions, weight=0.5, pricing=PricingConfig(), staking=StakingConfig()
        )
        return [(b.fixture, str(b.selection), b.odds, b.ev) for b in bets]

    assert decisions(small_run) == decisions(without_closing(small_run))


def test_thin_history_and_fit_failures_are_skipped_and_counted(
    small_dataset: tuple[BacktestFixture, ...],
) -> None:
    first_season = min(i.season for i in small_dataset)
    # In the first season every early fixture lacks history; the very first refit fails.
    run = walk_forward(small_dataset, WalkForwardConfig(), seasons={first_season})
    assert run.skipped[SkipReason.FIT_FAILED] > 0
    assert run.skipped[SkipReason.THIN_HISTORY] > 0
    assert run.fit_failures > 0


def test_fixtures_without_prices_and_unknown_teams_are_skipped(
    small_dataset: tuple[BacktestFixture, ...],
) -> None:
    holdout_dates = sorted({i.match_date for i in small_dataset if i.season == HOLDOUT_SEASON})
    target = next(i for i in small_dataset if i.match_date == holdout_dates[0])
    renamed_home = replace(target.fixture.home, name="Newcomer")
    newcomer = replace(
        target,
        fixture=replace(
            target.fixture,
            home=replace(renamed_home, ref=replace(renamed_home.ref, external_id="new")),
        ),
    )
    unpriced = replace(
        next(i for i in small_dataset if i.match_date == holdout_dates[1]), quotes=()
    )
    data = [
        newcomer if i is target else unpriced if i.key == unpriced.key else i for i in small_dataset
    ]
    run = walk_forward(data, WalkForwardConfig(min_team_matches=0), seasons={HOLDOUT_SEASON})
    assert run.skipped[SkipReason.UNKNOWN_TEAM] == 1
    assert run.skipped[SkipReason.NO_PRICES] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"refit_days": 0},
        {"max_history_days": 0},
        {"min_team_matches": -1},
        {"prediction_kind": OddsKind.CLOSING},
    ],
)
def test_config_validation(change: dict[str, object]) -> None:
    with pytest.raises(ModelInputError):
        WalkForwardConfig(**change)  # type: ignore[arg-type]
