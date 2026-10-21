import math
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from scipy.optimize import OptimizeResult, check_grad
from scipy.stats import poisson

from app.models import (
    DixonColesModel,
    MatchRecord,
    ModelFitError,
    ModelInputError,
    UnknownTeamError,
    feasible_rho,
    fit_dixon_coles,
    score_matrix,
)
from app.models import poisson as poisson_module

START = date(2020, 1, 1)


@dataclass(frozen=True)
class Truth:
    """Known parameters of a synthetic league (log scale, strengths centred)."""

    attack: np.ndarray
    defence: np.ndarray
    intercept: float = 0.1
    home_advantage: float = 0.25
    rho: float = -0.1


def simulate_league(
    rng: np.random.Generator, truth: Truth, seasons: int, played: date | None = None
) -> list[MatchRecord]:
    """Double round robin per season, scores drawn from the Dixon-Coles distribution.

    Ten matches a day from START, or every match on ``played`` when given.
    """
    n_teams = len(truth.attack)
    teams = [f"Team {i:02d}" for i in range(n_teams)]
    matches = []
    for number, (home, away) in enumerate(
        (h, a) for _ in range(seasons) for h in range(n_teams) for a in range(n_teams) if h != a
    ):
        log_home = truth.intercept + truth.home_advantage + truth.attack[home] - truth.defence[away]
        log_away = truth.intercept + truth.attack[away] - truth.defence[home]
        matrix = score_matrix(math.exp(log_home), math.exp(log_away), truth.rho, max_goals=12)
        cell = rng.choice(matrix.size, p=matrix.ravel())
        home_goals, away_goals = divmod(int(cell), matrix.shape[1])
        day = played or START + timedelta(days=number // 10)
        matches.append(MatchRecord(teams[home], teams[away], home_goals, away_goals, day))
    return matches


def dixon_coles_tau(match: MatchRecord, lam: float, mu: float, rho: float) -> float:
    """Dixon-Coles (1997) eq. 4.2, written out independently of the implementation."""
    table = {
        (0, 0): 1 - lam * mu * rho,
        (0, 1): 1 + lam * rho,
        (1, 0): 1 + mu * rho,
        (1, 1): 1 - rho,
    }
    return table.get((match.home_goals, match.away_goals), 1.0)


def centred(values: np.ndarray) -> np.ndarray:
    return values - values.mean()


@pytest.fixture(scope="module")
def truth() -> Truth:
    rng = np.random.default_rng(20261021)
    return Truth(
        attack=centred(rng.normal(0.0, 0.3, 20)), defence=centred(rng.normal(0.0, 0.3, 20))
    )


@pytest.fixture(scope="module")
def league(truth: Truth) -> list[MatchRecord]:
    return simulate_league(np.random.default_rng(7), truth, seasons=8)


def day_after(matches: list[MatchRecord]) -> date:
    return max(m.match_date for m in matches) + timedelta(days=1)


# --------------------------------------------------------------------------- score matrix


@given(
    st.floats(min_value=0.05, max_value=6.0),
    st.floats(min_value=0.05, max_value=6.0),
)
def test_independent_matrix_matches_scipy_poisson(home_rate: float, away_rate: float) -> None:
    matrix = score_matrix(home_rate, away_rate)
    goals = np.arange(matrix.shape[0])
    oracle = np.outer(poisson.pmf(goals, home_rate), poisson.pmf(goals, away_rate))
    np.testing.assert_allclose(matrix, oracle / oracle.sum(), rtol=1e-9, atol=1e-15)
    assert matrix.sum() == pytest.approx(1.0, abs=1e-12)


def test_dixon_coles_correction_hand_computed() -> None:
    lam, mu, rho = 1.2, 0.9, -0.1
    matrix = score_matrix(lam, mu, rho)
    base = math.exp(-lam - mu)
    # The correction moves mass among the four low scores but keeps the total, so the
    # renormalisation only removes the < 1e-10 truncated tail.
    assert matrix[0, 0] == pytest.approx(base * (1 - lam * mu * rho), rel=1e-9)
    assert matrix[0, 1] == pytest.approx(base * mu * (1 + lam * rho), rel=1e-9)
    assert matrix[1, 0] == pytest.approx(base * lam * (1 + mu * rho), rel=1e-9)
    assert matrix[1, 1] == pytest.approx(base * lam * mu * (1 - rho), rel=1e-9)
    assert matrix[2, 1] == pytest.approx(base * lam**2 / 2 * mu, rel=1e-9)


@pytest.mark.parametrize("rate", [0.3, 1.4, 3.0, 9.0])
def test_matrix_size_is_the_smallest_with_a_negligible_tail(rate: float) -> None:
    max_goals = score_matrix(rate, 0.5).shape[0] - 1
    assert max_goals >= 10
    assert poisson.sf(max_goals, rate) < 1e-10
    assert max_goals == 10 or poisson.sf(max_goals - 1, rate) >= 1e-10


def test_explicit_truncation_is_renormalised() -> None:
    matrix = score_matrix(3.0, 2.5, max_goals=3)
    assert matrix.shape == (4, 4)
    assert matrix.sum() == pytest.approx(1.0, abs=1e-12)


@pytest.mark.parametrize(
    ("home_rate", "away_rate", "rho", "max_goals"),
    [
        (0.0, 1.0, 0.0, None),
        (-1.0, 1.0, 0.0, None),
        (1.0, math.nan, 0.0, None),
        (16.0, 1.0, 0.0, None),
        (1.0, 1.0, math.inf, None),
        (2.0, 2.0, 0.3, None),  # 1 - lambda * mu * rho < 0
        (4.0, 1.0, -0.3, None),  # 1 + lambda * rho < 0
        (1.0, 1.0, 0.0, 0),
        (1.0, 1.0, 0.0, 51),
    ],
)
def test_invalid_matrix_inputs_are_rejected(
    home_rate: float, away_rate: float, rho: float, max_goals: int | None
) -> None:
    with pytest.raises(ModelInputError):
        score_matrix(home_rate, away_rate, rho, max_goals)


def test_matrix_is_stable_at_extreme_rates() -> None:
    for home_rate, away_rate in [(1e-6, 1e-6), (15.0, 15.0), (15.0, 1e-6)]:
        matrix = score_matrix(home_rate, away_rate)
        assert np.all(np.isfinite(matrix))
        assert np.all(matrix >= 0.0)
        assert matrix.sum() == pytest.approx(1.0, abs=1e-12)


@given(
    st.floats(min_value=0.05, max_value=15.0),
    st.floats(min_value=0.05, max_value=15.0),
    st.floats(min_value=-2.0, max_value=2.0),
)
def test_feasible_rho_always_gives_a_valid_matrix(
    home_rate: float, away_rate: float, rho: float
) -> None:
    clamped = feasible_rho(home_rate, away_rate, rho)
    matrix = score_matrix(home_rate, away_rate, clamped, max_goals=2)
    assert np.all(matrix > 0.0)


def test_feasible_rho_keeps_ordinary_values() -> None:
    assert feasible_rho(1.4, 1.1, -0.1) == -0.1
    assert feasible_rho(5.0, 1.0, -0.3) == pytest.approx(-0.2, rel=1e-5)
    assert feasible_rho(3.0, 3.0, 0.3) == pytest.approx(1 / 9, rel=1e-5)


# --------------------------------------------------------------------------- records


@pytest.mark.parametrize(
    "kwargs",
    [
        {"home_team": "A", "away_team": "A"},
        {"home_team": ""},
        {"home_goals": -1},
        {"match_date": datetime(2024, 1, 1, tzinfo=UTC)},
    ],
)
def test_invalid_match_records_are_rejected(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "home_team": "A",
        "away_team": "B",
        "home_goals": 1,
        "away_goals": 0,
        "match_date": START,
    }
    with pytest.raises(ModelInputError):
        MatchRecord(**(values | kwargs))


# --------------------------------------------------------------------------- fitting


def test_fit_recovers_known_parameters(league: list[MatchRecord], truth: Truth) -> None:
    # 3040 matches: each team strength has a standard error of ~0.04, so 0.15 is ~4 sigma.
    model = fit_dixon_coles(league, day_after(league), xi=0.0)
    teams = [f"Team {i:02d}" for i in range(20)]
    attack = np.array([model.attack[t] for t in teams])
    defence = np.array([model.defence[t] for t in teams])
    assert sum(model.attack.values()) == pytest.approx(0.0, abs=1e-9)
    assert sum(model.defence.values()) == pytest.approx(0.0, abs=1e-9)
    np.testing.assert_allclose(attack, truth.attack, atol=0.15)
    np.testing.assert_allclose(defence, truth.defence, atol=0.15)
    assert np.mean(np.abs(attack - truth.attack)) < 0.06
    assert model.home_advantage == pytest.approx(truth.home_advantage, abs=0.05)
    assert model.intercept == pytest.approx(truth.intercept, abs=0.05)
    assert model.rho == pytest.approx(truth.rho, abs=0.06)
    assert model.teams == tuple(teams)
    assert model.n_matches == len(league)


def test_log_likelihood_matches_independent_computation(league: list[MatchRecord]) -> None:
    sample = league[:400]
    model = fit_dixon_coles(sample, day_after(sample), xi=0.0)
    total = 0.0
    for match in sample:
        rates = model.expected_goals(match.home_team, match.away_team)
        total += poisson.logpmf(match.home_goals, rates.home)
        total += poisson.logpmf(match.away_goals, rates.away)
        total += math.log(dixon_coles_tau(match, rates.home, rates.away, model.rho))
    assert model.log_likelihood == pytest.approx(total, rel=1e-9)


def test_time_decay_equals_weighted_likelihood(truth: Truth) -> None:
    # Weighted MLE oracle: with xi = ln 2 / gap, an old match counts half as much as a new
    # one, exactly as if every new match appeared twice with xi = 0.
    gap_days = 200
    small = Truth(attack=truth.attack[:8] - truth.attack[:8].mean(), defence=truth.defence[:8])
    small = replace(small, defence=small.defence - small.defence.mean())
    old = simulate_league(np.random.default_rng(1), small, seasons=1, played=START)
    new_day = START + timedelta(days=gap_days)
    # Form changed: attack strengths are reversed, so the decay visibly matters.
    reversed_form = replace(small, attack=-small.attack)
    new = simulate_league(np.random.default_rng(2), reversed_form, seasons=1, played=new_day)
    as_of = new_day + timedelta(days=30)

    decayed = fit_dixon_coles(old + new, as_of, xi=math.log(2) / gap_days)
    duplicated = fit_dixon_coles(old + new + new, as_of, xi=0.0)

    for team in decayed.teams:
        assert decayed.attack[team] == pytest.approx(duplicated.attack[team], abs=1e-5)
        assert decayed.defence[team] == pytest.approx(duplicated.defence[team], abs=1e-5)
    assert decayed.rho == pytest.approx(duplicated.rho, abs=1e-5)
    assert decayed.home_advantage == pytest.approx(duplicated.home_advantage, abs=1e-5)


def test_fit_is_stable_with_very_old_data(league: list[MatchRecord]) -> None:
    # Ten years of age with a strong decay: raw weights would be ~1e-16 and underflow-prone.
    sample = league[:380]
    recent = fit_dixon_coles(sample, day_after(sample), xi=0.01)
    later = fit_dixon_coles(sample, day_after(sample) + timedelta(days=3650), xi=0.01)
    for team in recent.teams:
        assert later.attack[team] == pytest.approx(recent.attack[team], abs=1e-6)
    assert math.isfinite(later.log_likelihood)


def test_team_that_never_scores_stays_finite(league: list[MatchRecord]) -> None:
    sample = [
        MatchRecord(m.home_team, m.away_team, 0, m.away_goals, m.match_date)
        if m.home_team == "Team 00"
        else MatchRecord(m.home_team, m.away_team, m.home_goals, 0, m.match_date)
        if m.away_team == "Team 00"
        else m
        for m in league[:380]
    ]
    model = fit_dixon_coles(sample, day_after(sample), xi=0.0)
    rates = model.expected_goals("Team 00", "Team 01")
    assert 0.0 < rates.home < 0.1
    assert np.all(np.isfinite(model.score_matrix("Team 00", "Team 01")))


def test_analytic_gradient_matches_finite_differences(league: list[MatchRecord]) -> None:
    sample = league[:380]
    design = poisson_module._build_design(sample, day_after(sample), 0.003)
    rng = np.random.default_rng(3)
    for scale in (0.0, 0.1, 0.3):
        point = poisson_module._initial_params(design) + rng.normal(0.0, scale, 3 + 2 * 19)
        point[2] = np.clip(point[2], -0.15, 0.15)
        error = check_grad(
            lambda p: poisson_module._objective(p, design)[0],
            lambda p: poisson_module._objective(p, design)[1],
            point,
        )
        assert error < 1e-5


def test_prediction_uses_the_documented_formula(league: list[MatchRecord]) -> None:
    model = fit_dixon_coles(league[:380], day_after(league[:380]))
    rates = model.expected_goals("Team 03", "Team 07")
    log_home = (
        model.intercept + model.home_advantage + model.attack["Team 03"] - model.defence["Team 07"]
    )
    assert rates.home == pytest.approx(math.exp(log_home))
    assert rates.away == pytest.approx(
        math.exp(model.intercept + model.attack["Team 07"] - model.defence["Team 03"])
    )
    np.testing.assert_allclose(
        model.score_matrix("Team 03", "Team 07"), score_matrix(rates.home, rates.away, model.rho)
    )


def test_model_clamps_rho_for_unseen_extreme_pairings() -> None:
    # The fit constrains rho only on observed pairings; a lopsided unseen one must still
    # give a valid matrix.
    model = DixonColesModel(
        attack=MappingProxyType({"Strong": 1.0, "Weak": -1.0}),
        defence=MappingProxyType({"Strong": 0.5, "Weak": -0.5}),
        intercept=0.0,
        home_advantage=0.0,
        rho=-0.4,
        xi=0.0,
        as_of=START,
        n_matches=2,
        log_likelihood=-1.0,
        iterations=1,
    )
    rates = model.expected_goals("Strong", "Weak")
    assert rates.home == pytest.approx(math.exp(1.5))
    with pytest.raises(ModelInputError):
        score_matrix(rates.home, rates.away, model.rho)
    matrix = model.score_matrix("Strong", "Weak")
    assert np.all(matrix > 0.0)
    assert matrix.sum() == pytest.approx(1.0)


def test_unknown_team_raises(league: list[MatchRecord]) -> None:
    model = fit_dixon_coles(league[:380], day_after(league[:380]))
    with pytest.raises(UnknownTeamError, match="Promoted FC"):
        model.expected_goals("Promoted FC", "Team 01")
    with pytest.raises(UnknownTeamError):
        model.score_matrix("Team 01", "Promoted FC")


def test_matches_on_or_after_as_of_are_leakage(league: list[MatchRecord]) -> None:
    sample = league[:380]
    last_day = max(m.match_date for m in sample)
    with pytest.raises(ModelInputError, match="look-ahead"):
        fit_dixon_coles(sample, last_day)


def test_disconnected_schedule_is_rejected() -> None:
    matches = [
        MatchRecord(home, away, 1, 0, START)
        for home, away in [("A", "B"), ("B", "A"), ("C", "D"), ("D", "C")] * 3
    ]
    with pytest.raises(ModelInputError, match="never play each other"):
        fit_dixon_coles(matches, START + timedelta(days=1))


@pytest.mark.parametrize(
    "matches",
    [[], [MatchRecord("A", "B", 1, 0, START)]],
)
def test_too_little_data_is_rejected(matches: list[MatchRecord]) -> None:
    with pytest.raises(ModelInputError):
        fit_dixon_coles(matches, START + timedelta(days=1))


@pytest.mark.parametrize("xi", [-0.001, math.nan, math.inf])
def test_invalid_xi_is_rejected(league: list[MatchRecord], xi: float) -> None:
    with pytest.raises(ModelInputError, match="xi"):
        fit_dixon_coles(league[:100], day_after(league[:100]), xi=xi)


def test_as_of_must_be_a_plain_date(league: list[MatchRecord]) -> None:
    with pytest.raises(ModelInputError, match="as_of"):
        fit_dixon_coles(league[:100], datetime(2030, 1, 1, tzinfo=UTC))


def test_non_convergence_raises(league: list[MatchRecord], monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_minimize(*_args: object, **_kwargs: object) -> OptimizeResult:
        return OptimizeResult(success=False, x=np.zeros(41), nit=2000, message="ABNORMAL", fun=1.0)

    monkeypatch.setattr(poisson_module, "minimize", failing_minimize)
    with pytest.raises(ModelFitError, match="did not converge"):
        fit_dixon_coles(league[:380], day_after(league[:380]))
