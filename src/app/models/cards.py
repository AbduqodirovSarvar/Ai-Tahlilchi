"""Yellow-card model: team discipline, how much a team draws cards out of its opponent, referee.

Expected yellow cards of a match between ``home`` and ``away`` refereed by ``ref``::

    lambda_home = mean_home * discipline[home] * provocation[away] * strictness[ref]
    lambda_away = mean_away * discipline[away] * provocation[home] * strictness[ref]

``mean_home``/``mean_away`` are the league's time-weighted yellows per home/away side;
``discipline[t]`` is how many cards ``t`` gets relative to what an average side would get in
the same place; ``provocation[t]`` how many its opponents get relative to average;
``strictness[r]`` how many cards referee ``r`` shows relative to the league. Each ratio is
shrunk towards 1 with a prior of ``prior`` average matches, so a team or referee seen a few
times stays close to the league (no wild guesses from three matches). An unknown team or
referee has ratio 1.

Card totals are modelled as Poisson with these rates. Real card counts are a little
over-dispersed, so the tails (``over 6.5``) are somewhat underestimated; lines around the mean
(3.5-5.5) are where the model is meant to be read. Red cards are too rare to model here.

Weights are ``exp(-xi * age_days)`` like the goal model, so the current season counts most.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Final

from scipy.stats import poisson

from app.domain import normalize_name
from app.models.exceptions import ModelInputError

DEFAULT_CARD_XI_PER_DAY: Final = 0.0019
"""Same half-life (~1 year) as the goal model."""
DEFAULT_TEAM_PRIOR_MATCHES: Final = 10.0
DEFAULT_REFEREE_PRIOR_MATCHES: Final = 15.0
MIN_CARD_MATCHES: Final = 40
"""A league needs at least this many matches with cards before card forecasts are made."""


@dataclass(frozen=True, slots=True)
class CardMatch:
    """One played match with its yellow cards (team keys as in the goal model)."""

    played_on: date
    home: str
    away: str
    home_yellow: int
    away_yellow: int
    referee: str | None = None

    def __post_init__(self) -> None:
        """Card counts are non-negative."""
        if self.home_yellow < 0 or self.away_yellow < 0:
            msg = f"card counts must be >= 0 (got {self.home_yellow}, {self.away_yellow})"
            raise ModelInputError(msg)


@dataclass(frozen=True, slots=True)
class CardExpectation:
    """Expected yellow cards of each side, and whether the referee was known to the model."""

    home: float
    away: float
    referee_known: bool
    strictness: float = 1.0
    """The referee's ratio to the league (1.0 when unknown)."""

    @property
    def total(self) -> float:
        """Expected yellow cards of the match."""
        return self.home + self.away


@dataclass(frozen=True, slots=True)
class CardModel:
    """A fitted card model (see the module docstring)."""

    home_mean: float
    away_mean: float
    discipline: Mapping[str, float]
    provocation: Mapping[str, float]
    strictness: Mapping[str, float]
    team_matches: Mapping[str, int]
    referee_matches: Mapping[str, int]
    matches: int

    def expected(self, home: str, away: str, referee: str | None = None) -> CardExpectation:
        """Expected yellows of ``home`` v ``away`` (``referee`` as any source spells it)."""
        key = None if referee is None else referee_key(referee)
        strict = self.strictness.get(key, 1.0) if key else 1.0
        return CardExpectation(
            home=self.home_mean
            * self.discipline.get(home, 1.0)
            * self.provocation.get(away, 1.0)
            * strict,
            away=self.away_mean
            * self.discipline.get(away, 1.0)
            * self.provocation.get(home, 1.0)
            * strict,
            referee_known=key is not None and key in self.strictness,
            strictness=strict,
        )


def referee_key(name: str) -> str | None:
    """One key for one referee across sources: initial and surname.

    ``Michael Oliver`` (Football-Data.org) and ``M Oliver`` (football-data.co.uk) are both
    ``m oliver``. ``None`` for an empty name.
    """
    tokens = normalize_name(name).split()
    if not tokens:
        return None
    if len(tokens) == 1:
        return tokens[0]
    return f"{tokens[0][0]} {tokens[-1]}"


def poisson_over(rate: float, line: float) -> float:
    """``P(X > line)`` for ``X ~ Poisson(rate)`` (``line`` a half line such as 4.5)."""
    if not math.isfinite(rate) or rate < 0:
        msg = f"rate must be finite and >= 0 (got {rate!r})"
        raise ModelInputError(msg)
    return float(poisson.sf(math.floor(line), rate))


@dataclass
class _Tally:
    """Weighted observed and expected cards of one team or referee."""

    observed: float = 0.0
    expected: float = 0.0
    matches: int = 0

    def add(self, observed: float, expected: float, weight: float) -> None:
        self.observed += weight * observed
        self.expected += weight * expected
        self.matches += 1

    def ratio(self, prior: float) -> float:
        """Observed over expected, shrunk to 1 by ``prior`` expected cards of evidence."""
        return (self.observed + prior) / (self.expected + prior)


def fit_cards(
    matches: Sequence[CardMatch],
    *,
    as_of: date,
    xi: float = DEFAULT_CARD_XI_PER_DAY,
    team_prior: float = DEFAULT_TEAM_PRIOR_MATCHES,
    referee_prior: float = DEFAULT_REFEREE_PRIOR_MATCHES,
) -> CardModel:
    """Fit the card model on ``matches`` played before ``as_of``.

    Raises:
        ModelInputError: fewer than :data:`MIN_CARD_MATCHES` usable matches, or a league
            that never shows a card (nothing to scale by).
    """
    usable = [m for m in matches if m.played_on < as_of]
    if len(usable) < MIN_CARD_MATCHES:
        msg = f"needs {MIN_CARD_MATCHES} matches with cards, got {len(usable)}"
        raise ModelInputError(msg)
    weights = [math.exp(-xi * (as_of - m.played_on).days) for m in usable]
    total_weight = sum(weights)
    home_mean = sum(w * m.home_yellow for w, m in zip(weights, usable, strict=True)) / total_weight
    away_mean = sum(w * m.away_yellow for w, m in zip(weights, usable, strict=True)) / total_weight
    if home_mean <= 0 or away_mean <= 0:
        msg = "the league has no yellow cards to learn from"
        raise ModelInputError(msg)
    side_mean = (home_mean + away_mean) / 2
    own: dict[str, _Tally] = {}
    drawn: dict[str, _Tally] = {}
    referees: dict[str, _Tally] = {}
    for weight, match in zip(weights, usable, strict=True):
        own.setdefault(match.home, _Tally()).add(match.home_yellow, home_mean, weight)
        own.setdefault(match.away, _Tally()).add(match.away_yellow, away_mean, weight)
        drawn.setdefault(match.home, _Tally()).add(match.away_yellow, away_mean, weight)
        drawn.setdefault(match.away, _Tally()).add(match.home_yellow, home_mean, weight)
        key = None if match.referee is None else referee_key(match.referee)
        if key is not None:
            total = match.home_yellow + match.away_yellow
            referees.setdefault(key, _Tally()).add(total, home_mean + away_mean, weight)
    team_prior_cards = team_prior * side_mean
    referee_prior_cards = referee_prior * (home_mean + away_mean)
    return CardModel(
        home_mean=home_mean,
        away_mean=away_mean,
        discipline=_ratios(own, team_prior_cards),
        provocation=_ratios(drawn, team_prior_cards),
        strictness=_ratios(referees, referee_prior_cards),
        team_matches=MappingProxyType({team: tally.matches for team, tally in own.items()}),
        referee_matches=MappingProxyType({key: t.matches for key, t in referees.items()}),
        matches=len(usable),
    )


def _ratios(tallies: Mapping[str, _Tally], prior: float) -> Mapping[str, float]:
    return MappingProxyType({key: tally.ratio(prior) for key, tally in tallies.items()})
