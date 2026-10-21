from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.domain import HistoryBatch
from app.models import ModelInputError
from app.models.backtest import (
    BacktestFixture,
    BettingSummary,
    ChosenParameters,
    ExperimentConfig,
    ExperimentResult,
    GridSpec,
    RunMetadata,
    SeasonSplit,
    build_dataset,
    complete_seasons,
    dataset_fingerprint,
    gate_checks,
    render_report,
    run_experiment,
    season_end,
    split_seasons,
)
from tests.models.backtest.conftest import HOLDOUT_SEASON, VALIDATION_SEASON

SMALL_GRID = GridSpec(
    xis=(0.0, 0.0065), weights=(0.5, 1.0), min_evs=(0.0, 0.05), min_probabilities=(0.3,)
)


@pytest.fixture(scope="module")
def result(small_dataset: tuple[BacktestFixture, ...]) -> ExperimentResult:
    config = ExperimentConfig(
        grid=SMALL_GRID, refit_days=14, n_resamples=100, min_validation_bets=10
    )
    return run_experiment(small_dataset, config)


def test_split_seasons() -> None:
    assert split_seasons([2020, 2021, 2022, 2023]) == SeasonSplit((2020, 2021), 2022, 2023)
    assert split_seasons({2020, 2021, 2022, 2023}, holdout=2022) == SeasonSplit((2020,), 2021, 2022)
    with pytest.raises(ModelInputError, match="not in the data"):
        split_seasons([2020, 2021, 2022], holdout=2030)
    with pytest.raises(ModelInputError, match="need >= 3"):
        split_seasons([2020, 2021])
    with pytest.raises(ModelInputError):
        split_seasons([])


def test_grid_validation() -> None:
    with pytest.raises(ModelInputError, match="at least one"):
        GridSpec(xis=())
    assert GridSpec().size == 3 * 4 * 4 * 3


def test_experiment_evaluates_every_configuration_and_holdout_once(
    result: ExperimentResult,
) -> None:
    assert result.split == SeasonSplit((2020,), VALIDATION_SEASON, HOLDOUT_SEASON)
    assert result.n_configurations == SMALL_GRID.size
    chosen = result.chosen
    assert chosen.xi in SMALL_GRID.xis
    assert chosen.weight in SMALL_GRID.weights
    # Stage 1: (xi, w) is the validation log-loss minimum.
    best = min(result.rows, key=lambda row: row.log_loss)
    assert (chosen.xi, chosen.weight) == (best.xi, best.weight)
    assert result.validation.season == VALIDATION_SEASON
    assert result.holdout.season == HOLDOUT_SEASON
    assert all(bet.season == HOLDOUT_SEASON for bet in result.holdout.bets)
    assert result.holdout.summary.n_bets == len(result.holdout.bets)
    assert sum(s.n_bets for s in result.holdout.by_league.values()) == len(result.holdout.bets)
    assert sum(s.n_bets for s in result.holdout.by_market.values()) == len(result.holdout.bets)
    assert result.holdout.scores
    assert result.holdout.calibration


def test_thresholds_fall_back_to_most_bets_when_underpowered(
    small_dataset: tuple[BacktestFixture, ...],
) -> None:
    config = ExperimentConfig(
        grid=GridSpec(xis=(0.0065,), weights=(1.0,), min_evs=(0.0, 0.2), min_probabilities=(0.3,)),
        refit_days=28,
        n_resamples=10,
        min_validation_bets=10**6,
    )
    outcome = run_experiment(small_dataset, config)
    assert outcome.chosen.underpowered
    most = max(outcome.rows, key=lambda row: row.n_bets)
    assert outcome.chosen.min_ev == most.min_ev
    assert "eng ko'p signalli" in render_report(outcome, metadata())


def test_experiment_without_scorable_predictions_fails_clearly(
    small_dataset: tuple[BacktestFixture, ...],
) -> None:
    unpriced = [replace(item, quotes=()) for item in small_dataset]
    with pytest.raises(ModelInputError, match="could be scored"):
        run_experiment(unpriced, ExperimentConfig(grid=SMALL_GRID, refit_days=28))


def metadata() -> RunMetadata:
    return RunMetadata(
        label="test",
        mode="sintetik",
        generated_at=datetime(2026, 10, 21, tzinfo=UTC),
        seed=1,
        data_fingerprint="abc",
        git_commit="deadbeef",
        n_fixtures=10,
        leagues=("L",),
        seasons=(2020, 2021, 2022),
        parameters={"synthetic.seed": "7"},
        figures={"Kalibratsiya": "x-calibration.png"},
        notes=("eslatma",),
    )


def test_report_contains_every_section(result: ExperimentResult) -> None:
    text = render_report(result, metadata())
    for fragment in (
        "# Backtest hisoboti: test",
        "> eslatma",
        "`deadbeef`",
        "synthetic.seed",
        f"**{SMALL_GRID.size}**",
        "## Hold-out natijasi",
        "## Validatsiya",
        "log-loss bozor (yopilish)",
        "![Kalibratsiya](x-calibration.png)",
        "## F3 darvozasi (hold-out)",
    ):
        assert fragment in text
    no_figures = render_report(result, replace(metadata(), figures={}, notes=()))
    assert "_grafik yaratilmagan_" in no_figures


def summary_like(base: BettingSummary, **change: object) -> BettingSummary:
    return replace(base, **change)  # type: ignore[arg-type]


def test_gate_checks_pass_and_fail(result: ExperimentResult) -> None:
    holdout = result.holdout
    base = holdout.summary
    good = summary_like(base, n_bets=600, roi=0.03, n_closing=600, roi_at_closing=0.01)
    leagues = dict.fromkeys(("A", "B", "C"), good)
    chosen = ChosenParameters(
        xi=0.0, weight=0.5, min_ev=0.05, min_probability=0.3, underpowered=False
    )
    passing = replace(holdout, summary=good, by_league=leagues)
    checks = gate_checks(passing, chosen)
    assert [c.passed for c in checks[1:]] == [True, True, True]

    bad = summary_like(base, n_bets=10, roi=-0.1, n_closing=10, roi_at_closing=-0.1)
    failing = replace(holdout, summary=bad, by_league={"A": bad}, calibration=())
    checks = gate_checks(failing, replace(chosen, min_ev=0.0))
    assert [c.passed for c in checks] == [False, False, False, False]
    assert "tekshirilmagan" in checks[2].detail
    assert "baholanadigan oraliq yo'q" in checks[0].detail


def test_dataset_is_finished_fixtures_in_kickoff_order(small_batch: HistoryBatch) -> None:
    dataset = build_dataset(small_batch)
    assert len(dataset) == len(small_batch.fixtures)
    kickoffs = [item.fixture.kickoff for item in dataset]
    assert kickoffs == sorted(kickoffs)
    item = dataset[0]
    assert item.key == str(item.fixture.ref)
    assert item.league == str(item.fixture.league.ref)
    assert item.home.startswith("synthetic:")
    assert item.quote(item.quotes[0].kind, item.quotes[0].bookmaker, item.quotes[0].market)
    # Without results nothing can be learnt from or settled.
    assert build_dataset(replace(small_batch, results=())) == ()


def test_fingerprint_is_order_independent_and_data_sensitive(small_batch: HistoryBatch) -> None:
    reordered = HistoryBatch(
        fixtures=small_batch.fixtures[::-1],
        results=small_batch.results[::-1],
        odds=small_batch.odds[::-1],
    )
    assert dataset_fingerprint(reordered) == dataset_fingerprint(small_batch)
    changed = replace(small_batch, odds=small_batch.odds[1:])
    assert dataset_fingerprint(changed) != dataset_fingerprint(small_batch)
    assert len(dataset_fingerprint(small_batch)) == 64


def test_complete_seasons_by_calendar_and_fixture_count(small_batch: HistoryBatch) -> None:
    fixtures = small_batch.fixtures  # seasons 2020..2022, every league fully played
    assert season_end(2022) == date(2023, 6, 30)
    assert complete_seasons(fixtures, today=date(2023, 7, 1)) == (2020, 2021, 2022)
    # On the last day of 2022/23 that season is not over yet.
    assert complete_seasons(fixtures, today=date(2023, 6, 30)) == (2020, 2021)
    # One league of 2021 lost a fifth of its matches: the whole season is unusable.
    league = fixtures[0].league.ref
    in_2021 = [f for f in fixtures if f.season == 2021 and f.league.ref == league]
    dropped = {f.ref for f in in_2021[: len(in_2021) // 5]}
    partial = [f for f in fixtures if f.ref not in dropped]
    assert complete_seasons(partial, today=date(2030, 1, 1)) == (2020, 2022)
    # A couple of missing rows is tolerated (unparseable source lines).
    almost = [f for f in fixtures if f.ref != in_2021[0].ref]
    assert complete_seasons(almost, today=date(2030, 1, 1)) == (2020, 2021, 2022)
    assert complete_seasons([], today=date(2030, 1, 1)) == ()
