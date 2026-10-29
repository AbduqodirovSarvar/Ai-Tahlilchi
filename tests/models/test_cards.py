"""Yellow-card model: discipline, provocation, referee strictness, shrinkage, Poisson lines."""

from datetime import date, timedelta
from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.models.cards import (
    MIN_CARD_MATCHES,
    CardMatch,
    fit_cards,
    poisson_over,
    referee_key,
)
from app.models.exceptions import ModelInputError

AS_OF = date(2026, 10, 1)
TEAMS = ("a", "b", "c", "d", "e", "f")


def league(
    *,
    rough: str = "a",
    strict: str = "M Strict",
    rounds: int = 12,
    calm_referee: str = "J Calm",
) -> list[CardMatch]:
    """Every pairing ``rounds`` times: ``rough`` collects 2 extra yellows, ``strict`` adds 3."""
    matches = []
    day = AS_OF - timedelta(days=400)
    pairings = [(h, a) for h in TEAMS for a in TEAMS if h != a]
    for number, (home, away) in product(range(rounds), pairings):
        referee = strict if number % 3 == 0 else calm_referee
        extra = 3 if referee == strict else 0
        matches.append(
            CardMatch(
                played_on=day,
                home=home,
                away=away,
                home_yellow=1 + (2 if home == rough else 0) + extra // 2,
                away_yellow=2 + (2 if away == rough else 0) + extra - extra // 2,
                referee=referee,
            )
        )
        day += timedelta(days=1)
    return matches


def test_rough_team_and_strict_referee_raise_the_expectation() -> None:
    model = fit_cards(league(), as_of=AS_OF)
    assert model.discipline["a"] > 1.3
    assert all(model.discipline[t] < 1.0 for t in TEAMS[1:])
    assert model.strictness[referee_key("M Strict")] > 1.2  # type: ignore[index]
    calm = model.expected("b", "c", "J Calm")
    strict = model.expected("b", "c", "Michael Strict")  # the other source's spelling
    assert strict.referee_known
    assert strict.total > calm.total
    assert model.expected("a", "c").home > model.expected("b", "c").home


def test_unknown_team_and_referee_fall_back_to_the_league() -> None:
    model = fit_cards(league(), as_of=AS_OF)
    unknown = model.expected("zz", "yy", "Nobody Known")
    assert not unknown.referee_known
    assert unknown.home == pytest.approx(model.home_mean)
    assert unknown.away == pytest.approx(model.away_mean)
    assert not model.expected("b", "c").referee_known


def test_a_team_seen_once_stays_near_the_league() -> None:
    matches = league()
    matches.append(
        CardMatch(
            played_on=AS_OF - timedelta(days=1), home="new", away="b", home_yellow=9, away_yellow=1
        )
    )
    model = fit_cards(matches, as_of=AS_OF)
    assert 1.0 < model.discipline["new"] < 1.6  # nine cards once is not a 4x team
    assert model.team_matches["new"] == 1


def test_too_little_history_or_no_cards_is_refused() -> None:
    few = league()[: MIN_CARD_MATCHES - 1]
    with pytest.raises(ModelInputError, match="needs"):
        fit_cards(few, as_of=AS_OF)
    future = [CardMatch(played_on=AS_OF, home="a", away="b", home_yellow=1, away_yellow=1)] * 50
    with pytest.raises(ModelInputError):
        fit_cards(future, as_of=AS_OF)  # nothing before as_of
    blank = [
        CardMatch(
            played_on=AS_OF - timedelta(days=i + 1),
            home="a",
            away="b",
            home_yellow=0,
            away_yellow=0,
        )
        for i in range(MIN_CARD_MATCHES)
    ]
    with pytest.raises(ModelInputError, match="no yellow"):
        fit_cards(blank, as_of=AS_OF)
    with pytest.raises(ModelInputError):
        CardMatch(played_on=AS_OF, home="a", away="b", home_yellow=-1, away_yellow=0)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Michael Oliver", "m oliver"),
        ("M Oliver", "m oliver"),
        ("M. Oliver", "m oliver"),
        ("Anthony  Taylor", "a taylor"),
        ("Collina", "collina"),
        ("   ", None),
    ],
)
def test_referee_keys(name: str, key: str | None) -> None:
    assert referee_key(name) == key


def test_poisson_over_known_values() -> None:
    assert poisson_over(4.0, 4.5) == pytest.approx(0.3711, abs=1e-4)
    assert poisson_over(0.0, 0.5) == 0.0
    with pytest.raises(ModelInputError):
        poisson_over(-1.0, 2.5)


@given(
    rate=st.floats(min_value=0.0, max_value=15.0, allow_nan=False),
    low=st.integers(min_value=0, max_value=8),
)
def test_poisson_over_falls_as_the_line_rises(rate: float, low: int) -> None:
    lower, higher = poisson_over(rate, low + 0.5), poisson_over(rate, low + 1.5)
    assert 0.0 <= higher <= lower <= 1.0


@given(st.integers(min_value=1, max_value=4))
def test_rates_are_positive_and_follow_the_league(rough_extra: int) -> None:
    model = fit_cards(league(rounds=6 + rough_extra), as_of=AS_OF)
    for home in TEAMS:
        for away in TEAMS:
            if home != away:
                expectation = model.expected(home, away)
                assert expectation.home > 0
                assert expectation.away > 0
