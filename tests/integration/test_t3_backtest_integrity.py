"""T3 QA: integrity of the backtest library at its public boundary (``HistoryBatch`` in).

- hold-out is walked and evaluated exactly once, after the configuration was chosen, and the
  selection path only ever sees validation predictions (instrumented);
- the same input and seed give an identical report (determinism);
- future perturbation: rewriting every result and price dated at or after a cut-off inside
  the hold-out season changes neither the selection nor any hold-out bet struck before it.
"""

import importlib
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest

from app.domain import (
    BOOKMAKER_PINNACLE,
    FixtureResult,
    HistoryBatch,
    Market,
    OddsKind,
    OddsSnapshot,
    Selection,
)
from app.models.backtest import (
    BacktestFixture,
    Bet,
    ExperimentConfig,
    ExperimentResult,
    GridSpec,
    RunMetadata,
    SyntheticConfig,
    build_dataset,
    dataset_fingerprint,
    generate_history,
    render_report,
    run_experiment,
    summarize_bets,
)
from tests.models.domain_helpers import KICKOFF, finished, fixture, snapshots

# The package re-exports functions under module names; patch the module's globals instead.
experiment_module = importlib.import_module("app.models.backtest.experiment")

QA_HISTORY: Final = SyntheticConfig(
    leagues=2, teams_per_league=8, seasons=3, first_season=2020, seed=7
)
VALIDATION, HOLDOUT = 2021, 2022
QA_CONFIG: Final = ExperimentConfig(
    grid=GridSpec(xis=(0.0065,), weights=(0.5, 1.0), min_evs=(0.0, 0.05), min_probabilities=(0.3,)),
    # Weekly rounds + weekly refits: every match day is a refit day, so a same-day leak
    # (fit including the prediction day) would show on the cut-off day (mutation-checked).
    refit_days=7,
    n_resamples=200,
    min_validation_bets=10,
)
METADATA: Final = RunMetadata(
    label="qa",
    mode="sintetik",
    generated_at=datetime(2026, 10, 21, tzinfo=UTC),
    seed=QA_CONFIG.seed,
    data_fingerprint="qa",
    git_commit="qa",
    n_fixtures=0,
    leagues=(),
    seasons=(),
)


@dataclass(frozen=True, slots=True)
class Instrumented:
    """A run plus the ordered log of the calls on its selection and evaluation path."""

    result: ExperimentResult
    calls: list[tuple[str, Any]]


@pytest.fixture(scope="module")
def batch() -> HistoryBatch:
    return generate_history(QA_HISTORY)


@pytest.fixture(scope="module")
def baseline(batch: HistoryBatch) -> Instrumented:
    calls: list[tuple[str, Any]] = []
    real_walk = experiment_module.walk_forward
    real_rows = experiment_module._grid_rows
    real_choose = experiment_module._choose
    real_evaluate = experiment_module.evaluate

    def walk(dataset: Sequence[BacktestFixture], config: Any, *, seasons: Collection[int]) -> Any:
        calls.append(("walk_forward", frozenset(seasons)))
        return real_walk(dataset, config, seasons=seasons)

    def rows(runs: Any, config: Any) -> Any:
        seen = {p.item.season for run in runs.values() for p in run.predictions}
        calls.append(("grid_rows", frozenset(seen)))
        return real_rows(runs, config)

    def choose(grid_rows: Any, config: Any) -> Any:
        calls.append(("choose", None))
        return real_choose(grid_rows, config)

    def evaluate(run: Any, chosen: Any, config: Any, *, season: int) -> Any:
        calls.append(("evaluate", season))
        return real_evaluate(run, chosen, config, season=season)

    patches: dict[str, Callable[..., Any]] = {
        "walk_forward": walk,
        "_grid_rows": rows,
        "_choose": choose,
        "evaluate": evaluate,
    }
    with pytest.MonkeyPatch.context() as patcher:
        for name, replacement in patches.items():
            patcher.setattr(experiment_module, name, replacement)
        result = run_experiment(build_dataset(batch), QA_CONFIG)
    return Instrumented(result, calls)


def test_holdout_is_walked_and_evaluated_once_after_the_selection(baseline: Instrumented) -> None:
    calls = baseline.calls
    holdout_walks = [i for i, call in enumerate(calls) if call == ("walk_forward", {HOLDOUT})]
    holdout_evaluations = [i for i, call in enumerate(calls) if call == ("evaluate", HOLDOUT)]
    choose_at = calls.index(("choose", None))
    assert len(holdout_walks) == 1
    assert len(holdout_evaluations) == 1
    assert choose_at < holdout_walks[0] < holdout_evaluations[0]
    assert calls[-1] == ("evaluate", HOLDOUT)
    # Every other walk is a validation walk; the grid only ever saw validation predictions.
    walks = [seasons for name, seasons in calls if name == "walk_forward"]
    assert walks.count(frozenset({VALIDATION})) == len(QA_CONFIG.grid.xis)
    assert [seen for name, seen in calls if name == "grid_rows"] == [frozenset({VALIDATION})]
    assert baseline.result.split.holdout == HOLDOUT
    assert {bet.season for bet in baseline.result.holdout.bets} == {HOLDOUT}
    assert baseline.result.holdout.summary.n_bets > 0


def test_same_input_and_seed_give_an_identical_report(
    batch: HistoryBatch, baseline: Instrumented
) -> None:
    again = run_experiment(build_dataset(batch), QA_CONFIG)
    assert again.rows == baseline.result.rows
    assert again.chosen == baseline.result.chosen
    assert again.holdout.bets == baseline.result.holdout.bets
    assert again.holdout.summary == baseline.result.holdout.summary
    assert render_report(again, METADATA) == render_report(baseline.result, METADATA)


def test_bootstrap_intervals_depend_on_the_seed_only(baseline: Instrumented) -> None:
    bets = baseline.result.holdout.bets
    first = summarize_bets(bets, bank=100.0, seed=1, n_resamples=300)
    assert summarize_bets(bets, bank=100.0, seed=1, n_resamples=300) == first
    other = summarize_bets(bets, bank=100.0, seed=2, n_resamples=300)
    assert other.roi == first.roi
    assert other.roi_ci != first.roi_ci  # the seed is really used


def test_data_fingerprint_is_order_independent_and_sensitive(batch: HistoryBatch) -> None:
    reordered = HistoryBatch(
        fixtures=batch.fixtures[::-1], results=batch.results[::-1], odds=batch.odds[::-1]
    )
    assert dataset_fingerprint(reordered) == dataset_fingerprint(batch)
    first = batch.odds[0]
    nudged = replace(first, odds=round(first.odds + 0.01, 2))
    changed = replace(batch, odds=(nudged, *batch.odds[1:]))
    assert dataset_fingerprint(changed) != dataset_fingerprint(batch)


def _perturb_from(batch: HistoryBatch, cutoff: datetime) -> HistoryBatch:
    """Rewrite every result of a match kicking off at/after ``cutoff`` and every price
    taken at/after it (closing prices of those matches, opening prices a day later)."""
    kickoff = {item.ref: item.kickoff for item in batch.fixtures}

    def result(item: FixtureResult) -> FixtureResult:
        if kickoff[item.fixture] < cutoff:
            return item
        assert item.home_score is not None
        assert item.away_score is not None
        return replace(item, home_score=item.away_score + 2, away_score=item.home_score + 1)

    def price(item: OddsSnapshot) -> OddsSnapshot:
        if item.taken_at < cutoff:
            return item
        return replace(item, odds=round(item.odds * 1.07 + 0.05, 2))

    return HistoryBatch(
        fixtures=batch.fixtures,
        results=tuple(result(item) for item in batch.results),
        odds=tuple(price(item) for item in batch.odds),
    )


def _decision(bet: Bet) -> tuple[object, ...]:
    return (
        bet.fixture,
        bet.market,
        bet.selection,
        bet.odds,
        bet.probability,
        bet.ev,
        bet.flat_stake,
        bet.kelly_stake,
    )


def test_future_data_never_changes_an_earlier_bet_or_the_selection(
    batch: HistoryBatch, baseline: Instrumented
) -> None:
    holdout = sorted(f.kickoff for f in batch.fixtures if f.season == HOLDOUT)
    cutoff = holdout[len(holdout) // 3]
    perturbed = run_experiment(build_dataset(_perturb_from(batch, cutoff)), QA_CONFIG)
    original = baseline.result

    # Validation lies wholly before the cut-off: the selection cannot move.
    assert perturbed.rows == original.rows
    assert perturbed.chosen == original.chosen
    assert perturbed.validation.bets == original.validation.bets

    kickoff = {str(f.ref): f.kickoff for f in batch.fixtures}

    def before(run: ExperimentResult) -> list[Bet]:
        return [bet for bet in run.holdout.bets if kickoff[bet.fixture] < cutoff]

    def on_cutoff_day(run: ExperimentResult) -> list[tuple[object, ...]]:
        return [_decision(b) for b in run.holdout.bets if kickoff[b.fixture] == cutoff]

    assert len(before(original)) >= 5
    assert before(perturbed) == before(original)  # every field, settlement and CLV included
    # Bets kicking off at the cut-off: their own result and closing price changed, the
    # decision (what, at which price, how much) must not.
    assert on_cutoff_day(perturbed) == on_cutoff_day(original)
    # Sanity of the perturbation itself: later bets did change.
    assert perturbed.holdout.bets != original.holdout.bets


@pytest.mark.xfail(
    strict=True,
    reason="F-3: prices labelled 'opening' are trusted even when taken after kickoff; "
    "the newest snapshot wins, so a late re-sent price decides the bet",
)
def test_an_opening_price_taken_after_kickoff_is_not_used_for_the_bet() -> None:
    match = fixture()
    before_kickoff = {Selection.HOME: 2.10, Selection.DRAW: 3.40, Selection.AWAY: 3.60}
    after_kickoff = {Selection.HOME: 1.30, Selection.DRAW: 5.00, Selection.AWAY: 9.00}
    batch = HistoryBatch(
        fixtures=(match,),
        results=(finished(match, 2, 0),),
        odds=(
            *snapshots(match, Market.match_result(), before_kickoff),
            *snapshots(
                match,
                Market.match_result(),
                after_kickoff,
                taken_at=KICKOFF + timedelta(minutes=30),
            ),
        ),
    )
    (item,) = build_dataset(batch)
    quote = item.quote(OddsKind.OPENING, BOOKMAKER_PINNACLE, Market.match_result())
    assert quote is not None
    assert dict(quote.prices) == before_kickoff
