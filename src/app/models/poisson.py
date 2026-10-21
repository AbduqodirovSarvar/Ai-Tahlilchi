"""Dixon-Coles (1997) bivariate Poisson model for football scores.

Expected goals of a match between ``home`` and ``away``::

    log(lambda_home) = intercept + home_advantage + attack[home] - defence[away]
    log(lambda_away) = intercept + attack[away] - defence[home]

with ``sum(attack) = sum(defence) = 0`` (identifiability). Goals are independent Poisson
except for the four low scores 0-0, 1-0, 0-1 and 1-1, which are corrected by Dixon-Coles'
``tau(x, y; lambda, mu, rho)``. Parameters are fitted by weighted maximum likelihood with
time-decay weights ``exp(-xi * age_days)``.

Derivation, choices and trade-offs: docs/adr/0003-dixon-coles-and-devig.md.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy.optimize import minimize  # type: ignore[import-untyped]  # no scipy stubs in the lock (see report)
from scipy.sparse import coo_matrix  # type: ignore[import-untyped]  # no scipy stubs in the lock (see report)
from scipy.sparse.csgraph import connected_components  # type: ignore[import-untyped]  # no scipy stubs in the lock (see report)
from scipy.special import gammaln, pdtrc  # type: ignore[import-untyped]  # no scipy stubs in the lock (see report)

from app.models.exceptions import ModelFitError, ModelInputError, UnknownTeamError

type FloatArray = npt.NDArray[np.float64]
type IntArray = npt.NDArray[np.int64]

# Time decay per day. 0.0019/day ~ Dixon-Coles' 0.0065 per half-week: half-life ~ 1 year.
# A starting point only; the walk-forward backtest (F3.7) tunes it.
DEFAULT_XI_PER_DAY: Final = 0.0019

# Score matrix: at least this many goals per side, more if the Poisson tail is still heavier
# than MAX_TAIL_MASS; never more than MAX_GOALS_LIMIT.
DEFAULT_MIN_MAX_GOALS: Final = 10
MAX_GOALS_LIMIT: Final = 50
MAX_TAIL_MASS: Final = 1e-10
# Sanity bound: no football team is expected to score more than this many goals.
MAX_EXPECTED_GOALS: Final = 15.0

# Optimiser box (log scale for strengths): wide enough never to bind for real leagues, tight
# enough to keep a degenerate team (e.g. never scored) finite instead of drifting to -inf.
_STRENGTH_BOUND: Final = 4.0
_INTERCEPT_BOUNDS: Final = (-5.0, 3.0)
_HOME_ADVANTAGE_BOUND: Final = 1.5
_RHO_BOUND: Final = 0.5
_MAX_ITERATIONS: Final = 2000
# The objective is a per-unit-weight NLL (~3); these tolerances are far below its noise.
_FTOL: Final = 1e-13
_GTOL: Final = 1e-9
# If L-BFGS-B stops on its iteration or line-search limit, the point is still accepted when
# the projected gradient (sup norm, per unit weight) shows it is a stationary point. Near-flat
# directions left by lightly weighted data make the strict _GTOL unreachable there. With a
# curvature of ~0.05 per team (its weight share), 1e-5 bounds the parameter error at ~2e-4
# on the log scale, far below a strength's standard error (~0.05).
_ACCEPTABLE_PROJECTED_GRADIENT: Final = 1e-5
# Stale data is dropped before fitting: a match whose time weight is below this share of the
# newest match's, then every team whose total weight is below this share of the heaviest
# team's (e.g. relegated long ago). Such teams carry no information and only create
# near-flat directions; asking for them later raises UnknownTeamError, which is correct.
_MIN_RELATIVE_MATCH_WEIGHT: Final = 1e-4
_MIN_RELATIVE_TEAM_WEIGHT: Final = 1e-3
# tau below this is treated as this (log barrier) during the line search; never at optimum.
_TAU_FLOOR: Final = 1e-12
# Relative distance kept from the boundary of the feasible rho interval at prediction time.
_RHO_FEASIBILITY_MARGIN: Final = 1e-6
# Parameter vector layout: [intercept, home_advantage, rho, attack[:-1], defence[:-1]].
_N_GLOBAL_PARAMS: Final = 3
_MIN_TEAMS: Final = 2


@dataclass(frozen=True, slots=True)
class MatchRecord:
    """A finished match, as needed to fit the model."""

    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    match_date: date

    def __post_init__(self) -> None:
        """Reject records that would silently corrupt the fit."""
        if not self.home_team or not self.away_team or self.home_team == self.away_team:
            msg = f"teams must be two different non-empty names: {self!r}"
            raise ModelInputError(msg)
        if min(self.home_goals, self.away_goals) < 0:
            msg = f"goals must be >= 0: {self!r}"
            raise ModelInputError(msg)
        _check_plain_date(self.match_date, "match_date")


@dataclass(frozen=True, slots=True)
class ExpectedGoals:
    """Poisson rates (expected goals) of one match."""

    home: float
    away: float


@dataclass(frozen=True, slots=True)
class DixonColesModel:
    """Fitted Dixon-Coles parameters (log scale), plus fit diagnostics.

    ``n_matches`` counts the matches actually used (stale ones are dropped). ``converged`` is
    the optimiser's own strict criterion; when it is False the fit was still accepted because
    ``projected_gradient`` was below the acceptance tolerance (see ADR 0003).
    """

    attack: Mapping[str, float]
    defence: Mapping[str, float]
    intercept: float
    home_advantage: float
    rho: float
    xi: float
    as_of: date
    n_matches: int
    log_likelihood: float
    iterations: int = field(compare=False)
    converged: bool = field(compare=False)
    termination: str = field(compare=False)
    projected_gradient: float = field(compare=False)

    @property
    def teams(self) -> tuple[str, ...]:
        """Teams the model can predict, sorted."""
        return tuple(sorted(self.attack))

    def expected_goals(self, home_team: str, away_team: str) -> ExpectedGoals:
        """Expected goals for ``home_team`` hosting ``away_team``.

        Raises:
            UnknownTeamError: a team was not in the fitting data. There is deliberately no
                league-average fallback: unknown teams are mostly promoted sides, which an
                average rating systematically overrates (see ADR 0003).
        """
        for team in (home_team, away_team):
            if team not in self.attack:
                msg = f"team {team!r} is not in the fitted model (as_of={self.as_of})"
                raise UnknownTeamError(msg)
        log_home = (
            self.intercept + self.home_advantage + self.attack[home_team] - self.defence[away_team]
        )
        log_away = self.intercept + self.attack[away_team] - self.defence[home_team]
        return ExpectedGoals(home=float(np.exp(log_home)), away=float(np.exp(log_away)))

    def score_matrix(
        self, home_team: str, away_team: str, max_goals: int | None = None
    ) -> FloatArray:
        """Score probability matrix for a fixture, see :func:`score_matrix`.

        ``rho`` is clamped into the range that keeps this fixture's probabilities positive
        (:func:`feasible_rho`): the fit only constrains it on the pairings it has seen.
        """
        rates = self.expected_goals(home_team, away_team)
        rho = feasible_rho(rates.home, rates.away, self.rho)
        return score_matrix(rates.home, rates.away, rho, max_goals)


def feasible_rho(home_rate: float, away_rate: float, rho: float) -> float:
    """Clamp ``rho`` so every Dixon-Coles ``tau`` stays positive for these rates.

    Positivity needs ``-1/max(lambda, mu) < rho < min(1, 1/(lambda * mu))``; the interval is
    shrunk by a tiny relative margin so the corrected cells never reach exactly zero.
    """
    lower = -1.0 / max(home_rate, away_rate) * (1.0 - _RHO_FEASIBILITY_MARGIN)
    upper = min(1.0, 1.0 / (home_rate * away_rate)) * (1.0 - _RHO_FEASIBILITY_MARGIN)
    return min(max(rho, lower), upper)


def score_matrix(
    home_rate: float, away_rate: float, rho: float = 0.0, max_goals: int | None = None
) -> FloatArray:
    """Joint score probabilities ``m[home_goals, away_goals]`` with the Dixon-Coles correction.

    The matrix is truncated at ``max_goals`` per side (chosen automatically when ``None`` so
    that the dropped Poisson tail is below ``MAX_TAIL_MASS``) and renormalised to sum to 1,
    so derived market probabilities always sum to 1.
    """
    for name, rate in (("home_rate", home_rate), ("away_rate", away_rate)):
        if not np.isfinite(rate) or not 0.0 < rate <= MAX_EXPECTED_GOALS:
            msg = f"{name} must be in (0, {MAX_EXPECTED_GOALS}], got {rate!r}"
            raise ModelInputError(msg)
    if not np.isfinite(rho):
        msg = f"rho must be finite, got {rho!r}"
        raise ModelInputError(msg)
    size = _matrix_size(max(home_rate, away_rate), max_goals)
    goals = np.arange(size, dtype=np.float64)
    home_pmf = np.exp(_poisson_log_pmf(goals, home_rate))
    away_pmf = np.exp(_poisson_log_pmf(goals, away_rate))
    matrix = np.outer(home_pmf, away_pmf)
    tau = _low_score_tau(home_rate, away_rate, rho)
    if np.any(tau <= 0.0):
        msg = f"rho={rho!r} gives negative probabilities for rates {home_rate}, {away_rate}"
        raise ModelInputError(msg)
    matrix[:2, :2] *= tau
    return np.asarray(matrix / matrix.sum(), dtype=np.float64)


def fit_dixon_coles(
    matches: Sequence[MatchRecord], as_of: date, *, xi: float = DEFAULT_XI_PER_DAY
) -> DixonColesModel:
    """Fit the model on matches played strictly before ``as_of``.

    ``as_of`` is exclusive and a match on or after it is rejected rather than dropped: in a
    walk-forward setting such a match is look-ahead leakage and must fail loudly.

    Raises:
        ModelInputError: leakage, too little data, or teams that never meet (disconnected
            schedule, e.g. two leagues mixed): their relative strength is not identifiable.
        ModelFitError: the optimiser did not converge.
    """
    design = _build_design(matches, as_of, xi)
    bounds = _bounds(design.n_teams)
    result = minimize(
        _objective,
        _initial_params(design),
        args=(design,),
        jac=True,
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": _MAX_ITERATIONS, "ftol": _FTOL, "gtol": _GTOL},
    )
    stationarity = _projected_gradient(result.x, design, bounds)
    # NaN stationarity means non-finite parameters: never acceptable.
    if not np.isfinite(stationarity) or (
        not result.success and stationarity > _ACCEPTABLE_PROJECTED_GRADIENT
    ):
        msg = (
            f"Dixon-Coles fit did not converge after {result.nit} iterations "
            f"(projected gradient {stationarity:.2e}): {result.message}"
        )
        raise ModelFitError(msg)
    intercept, home_advantage, rho, attack, defence = _unpack(result.x, design.n_teams)
    total_weight = float(design.weights.sum())
    return DixonColesModel(
        attack=MappingProxyType(dict(zip(design.teams, attack.tolist(), strict=True))),
        defence=MappingProxyType(dict(zip(design.teams, defence.tolist(), strict=True))),
        intercept=float(intercept),
        home_advantage=float(home_advantage),
        rho=float(rho),
        xi=xi,
        as_of=as_of,
        n_matches=design.n_matches,
        log_likelihood=-float(result.fun) * total_weight - design.log_factorial_term,
        iterations=int(result.nit),
        converged=bool(result.success),
        termination=str(result.message),
        projected_gradient=stationarity,
    )


def _projected_gradient(
    params: FloatArray, design: "_Design", bounds: list[tuple[float, float]]
) -> float:
    """Sup norm of the bound-projected gradient: 0 exactly at a constrained optimum.

    NaN when the parameters are not finite.
    """
    if not np.all(np.isfinite(params)):
        return float("nan")
    gradient = _objective(params, design)[1]
    lower, upper = np.array(bounds).T
    return float(np.max(np.abs(np.clip(params - gradient, lower, upper) - params)))


@dataclass(frozen=True, slots=True)
class _Design:
    """Matches encoded as arrays for the vectorised likelihood."""

    teams: tuple[str, ...]
    home_index: IntArray
    away_index: IntArray
    home_goals: FloatArray
    away_goals: FloatArray
    weights: FloatArray
    log_factorial_term: float

    @property
    def n_teams(self) -> int:
        return len(self.teams)

    @property
    def n_matches(self) -> int:
        return len(self.weights)


def _build_design(matches: Sequence[MatchRecord], as_of: date, xi: float) -> _Design:
    _check_plain_date(as_of, "as_of")
    if not np.isfinite(xi) or xi < 0.0:
        msg = f"xi must be finite and >= 0, got {xi!r}"
        raise ModelInputError(msg)
    leaked = [match for match in matches if match.match_date >= as_of]
    if leaked:
        msg = f"{len(leaked)} match(es) on or after as_of={as_of} (look-ahead), e.g. {leaked[0]}"
        raise ModelInputError(msg)
    matches, weights = _drop_stale(matches, as_of, xi)
    teams = tuple(sorted({m.home_team for m in matches} | {m.away_team for m in matches}))
    _check_identifiable(matches, teams)
    index = {team: position for position, team in enumerate(teams)}
    home_goals = np.array([m.home_goals for m in matches], dtype=np.float64)
    away_goals = np.array([m.away_goals for m in matches], dtype=np.float64)
    log_factorials = gammaln(home_goals + 1.0) + gammaln(away_goals + 1.0)
    return _Design(
        teams=teams,
        home_index=np.array([index[m.home_team] for m in matches], dtype=np.int64),
        away_index=np.array([index[m.away_team] for m in matches], dtype=np.int64),
        home_goals=home_goals,
        away_goals=away_goals,
        weights=weights,
        log_factorial_term=float(np.dot(weights, log_factorials)),
    )


def _drop_stale(
    matches: Sequence[MatchRecord], as_of: date, xi: float
) -> tuple[list[MatchRecord], FloatArray]:
    """Time weights, without matches and teams whose weight is negligible (see constants)."""
    if not matches:
        return [], np.zeros(0)
    ages = np.array([(as_of - m.match_date).days for m in matches], dtype=np.float64)
    # Relative to the newest match: the optimum is invariant to a common scale and this
    # keeps the largest weight at 1 so old data can never underflow every weight to 0.
    weights = np.exp(-xi * (ages - ages.min()))
    team_weight: dict[str, float] = {}
    for match, weight in zip(matches, weights.tolist(), strict=True):
        for team in (match.home_team, match.away_team):
            team_weight[team] = team_weight.get(team, 0.0) + weight
    min_team_weight = _MIN_RELATIVE_TEAM_WEIGHT * max(team_weight.values())
    keep = np.array(
        [
            weight >= _MIN_RELATIVE_MATCH_WEIGHT
            and min(team_weight[match.home_team], team_weight[match.away_team]) >= min_team_weight
            for match, weight in zip(matches, weights.tolist(), strict=True)
        ],
        dtype=bool,
    )
    kept = [match for match, used in zip(matches, keep.tolist(), strict=True) if used]
    return kept, weights[keep]


def _check_identifiable(matches: Sequence[MatchRecord], teams: tuple[str, ...]) -> None:
    n_teams = len(teams)
    if n_teams < _MIN_TEAMS:
        msg = f"need matches between at least {_MIN_TEAMS} teams, got {n_teams}"
        raise ModelInputError(msg)
    n_params = _N_GLOBAL_PARAMS + 2 * (n_teams - 1)
    if 2 * len(matches) < n_params:
        msg = f"{len(matches)} matches give fewer observations than {n_params} parameters"
        raise ModelInputError(msg)
    index = {team: position for position, team in enumerate(teams)}
    rows = [index[m.home_team] for m in matches]
    cols = [index[m.away_team] for m in matches]
    graph = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n_teams, n_teams))
    n_components, _ = connected_components(graph, directed=False)
    if n_components > 1:
        msg = f"teams form {n_components} groups that never play each other; fit them apart"
        raise ModelInputError(msg)


def _initial_params(design: _Design) -> FloatArray:
    mean_home = max(float(np.average(design.home_goals, weights=design.weights)), 0.1)
    mean_away = max(float(np.average(design.away_goals, weights=design.weights)), 0.1)
    params = np.zeros(_N_GLOBAL_PARAMS + 2 * (design.n_teams - 1))
    params[0] = np.log(mean_away)
    params[1] = np.clip(
        np.log(mean_home / mean_away), -_HOME_ADVANTAGE_BOUND, _HOME_ADVANTAGE_BOUND
    )
    return params


def _bounds(n_teams: int) -> list[tuple[float, float]]:
    strengths = [(-_STRENGTH_BOUND, _STRENGTH_BOUND)] * (2 * (n_teams - 1))
    return [
        _INTERCEPT_BOUNDS,
        (-_HOME_ADVANTAGE_BOUND, _HOME_ADVANTAGE_BOUND),
        (-_RHO_BOUND, _RHO_BOUND),
        *strengths,
    ]


def _unpack(params: FloatArray, n_teams: int) -> tuple[float, float, float, FloatArray, FloatArray]:
    """Split the vector; the last attack/defence is minus the sum of the others."""
    free = n_teams - 1
    attack_free = params[_N_GLOBAL_PARAMS : _N_GLOBAL_PARAMS + free]
    defence_free = params[_N_GLOBAL_PARAMS + free :]
    attack = np.append(attack_free, -attack_free.sum())
    defence = np.append(defence_free, -defence_free.sum())
    return float(params[0]), float(params[1]), float(params[2]), attack, defence


def _objective(params: FloatArray, design: _Design) -> tuple[float, FloatArray]:
    """Weighted negative log-likelihood per unit weight and its analytic gradient.

    The constant ``log(x!) + log(y!)`` is omitted (it does not move the optimum).
    """
    intercept, home_advantage, rho, attack, defence = _unpack(params, design.n_teams)
    home, away = design.home_index, design.away_index
    log_home_rate = intercept + home_advantage + attack[home] - defence[away]
    log_away_rate = intercept + attack[away] - defence[home]
    home_rate, away_rate = np.exp(log_home_rate), np.exp(log_away_rate)
    tau, d_tau = _match_tau(design, home_rate, away_rate, rho)
    safe_tau = np.maximum(tau, _TAU_FLOOR)
    log_likelihood = (
        np.log(safe_tau)
        + design.home_goals * log_home_rate
        - home_rate
        + design.away_goals * log_away_rate
        - away_rate
    )
    total_weight = design.weights.sum()
    value = -float(np.dot(design.weights, log_likelihood)) / total_weight
    # d log(tau) / d(.) = d tau / tau; zero where the floor is active (flat barrier).
    inverse_tau = np.where(tau > _TAU_FLOOR, 1.0 / safe_tau, 0.0)
    d_log_home = design.home_goals - home_rate + d_tau[0] * inverse_tau
    d_log_away = design.away_goals - away_rate + d_tau[1] * inverse_tau
    d_rho = d_tau[2] * inverse_tau
    gradient = _chain_rule(design, d_log_home, d_log_away, d_rho)
    return value, -gradient / total_weight


def _match_tau(
    design: _Design, home_rate: FloatArray, away_rate: FloatArray, rho: float
) -> tuple[FloatArray, tuple[FloatArray, FloatArray, FloatArray]]:
    """Per-match tau and its derivatives w.r.t. log(lambda), log(mu) and rho."""
    x, y = design.home_goals, design.away_goals
    is_00 = (x == 0) & (y == 0)
    is_01 = (x == 0) & (y == 1)
    is_10 = (x == 1) & (y == 0)
    is_11 = (x == 1) & (y == 1)
    rates_product = home_rate * away_rate
    zeros = np.zeros_like(home_rate)
    tau = np.select(
        [is_00, is_01, is_10, is_11],
        [1.0 - rates_product * rho, 1.0 + home_rate * rho, 1.0 + away_rate * rho, zeros + 1 - rho],
        default=1.0,
    )
    d_log_home = np.select([is_00, is_01], [-rates_product * rho, home_rate * rho], default=0.0)
    d_log_away = np.select([is_00, is_10], [-rates_product * rho, away_rate * rho], default=0.0)
    d_rho = np.select(
        [is_00, is_01, is_10, is_11], [-rates_product, home_rate, away_rate, zeros - 1.0]
    )
    return tau, (d_log_home, d_log_away, d_rho)


def _chain_rule(
    design: _Design, d_log_home: FloatArray, d_log_away: FloatArray, d_rho: FloatArray
) -> FloatArray:
    """Map per-match derivatives of the weighted log-likelihood to the parameter vector."""
    weighted_home = design.weights * d_log_home
    weighted_away = design.weights * d_log_away
    n_teams = design.n_teams
    attack = np.bincount(design.home_index, weighted_home, n_teams) + np.bincount(
        design.away_index, weighted_away, n_teams
    )
    defence = -np.bincount(design.away_index, weighted_home, n_teams) - np.bincount(
        design.home_index, weighted_away, n_teams
    )
    global_gradient = [
        weighted_home.sum() + weighted_away.sum(),
        weighted_home.sum(),
        np.dot(design.weights, d_rho),
    ]
    # The last team's strength is minus the sum of the free ones.
    return np.concatenate(
        [global_gradient, attack[:-1] - attack[-1], defence[:-1] - defence[-1]]
    ).astype(np.float64)


def _low_score_tau(home_rate: float, away_rate: float, rho: float) -> FloatArray:
    """Dixon-Coles correction for scores (0,0), (0,1), (1,0), (1,1) as a 2x2 array."""
    return np.array(
        [
            [1.0 - home_rate * away_rate * rho, 1.0 + home_rate * rho],
            [1.0 + away_rate * rho, 1.0 - rho],
        ]
    )


def _poisson_log_pmf(goals: FloatArray, rate: float) -> FloatArray:
    return np.asarray(goals * np.log(rate) - rate - gammaln(goals + 1.0), dtype=np.float64)


def _matrix_size(max_rate: float, max_goals: int | None) -> int:
    """Rows/columns of the score matrix: ``max_goals + 1``."""
    if max_goals is not None:
        if not 1 <= max_goals <= MAX_GOALS_LIMIT:
            msg = f"max_goals must be in [1, {MAX_GOALS_LIMIT}], got {max_goals!r}"
            raise ModelInputError(msg)
        return max_goals + 1
    candidates = np.arange(DEFAULT_MIN_MAX_GOALS, MAX_GOALS_LIMIT + 1)
    # pdtrc(k, rate) = P(X > k): the mass dropped by truncating at k goals.
    small_tail = pdtrc(candidates, max_rate) < MAX_TAIL_MASS
    chosen = int(candidates[np.argmax(small_tail)]) if small_tail.any() else MAX_GOALS_LIMIT
    return chosen + 1


def _check_plain_date(value: date, name: str) -> None:
    # datetime is a date subclass; mixing them breaks day arithmetic and hides time zones.
    if isinstance(value, datetime) or not isinstance(value, date):
        msg = f"{name} must be a datetime.date (not a datetime), got {value!r}"
        raise ModelInputError(msg)
