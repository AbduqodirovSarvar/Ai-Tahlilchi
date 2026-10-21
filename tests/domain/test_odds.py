"""Tests for app.domain.odds."""

import math
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import DomainValidationError, Market, OddsKind, OddsSnapshot, Selection
from tests.domain.factories import KICKOFF, make_fixture, make_snapshot

FIXTURE = make_fixture()


@given(st.floats(min_value=1.0, max_value=1000.0, exclude_min=True))
def test_odds_above_one_are_accepted(odds: float) -> None:
    assert make_snapshot(FIXTURE, odds=odds).odds == odds


@given(st.floats(max_value=1.0) | st.just(math.nan) | st.just(math.inf))
def test_odds_at_or_below_one_or_non_finite_are_rejected(odds: float) -> None:
    with pytest.raises(DomainValidationError):
        make_snapshot(FIXTURE, odds=odds)


def test_snapshot_exposes_line_and_requires_matching_selection() -> None:
    snapshot = make_snapshot(FIXTURE, market=Market.totals(2.5), selection=Selection.OVER)
    assert snapshot.line == 2.5
    assert make_snapshot(FIXTURE).line is None
    with pytest.raises(DomainValidationError, match="not valid"):
        make_snapshot(FIXTURE, market=Market.totals(2.5), selection=Selection.DRAW)


@pytest.mark.parametrize("bookmaker", ["", "Pinnacle", "bet 365"])
def test_bookmaker_must_be_a_slug(bookmaker: str) -> None:
    with pytest.raises(DomainValidationError, match="bookmaker"):
        OddsSnapshot(
            fixture=FIXTURE.ref,
            bookmaker=bookmaker,
            market=Market.btts(),
            selection=Selection.YES,
            odds=1.9,
            taken_at=KICKOFF,
            kind=OddsKind.MORNING,
        )


def test_taken_at_is_normalised_to_utc_and_naive_rejected() -> None:
    tashkent = timezone(timedelta(hours=5))
    snapshot = OddsSnapshot(
        fixture=FIXTURE.ref,
        bookmaker="pinnacle",
        market=Market.btts(),
        selection=Selection.YES,
        odds=1.9,
        taken_at=KICKOFF.astimezone(tashkent),
        kind=OddsKind.MORNING,
    )
    assert snapshot.taken_at == KICKOFF
    assert snapshot.taken_at.tzinfo is not None
    assert snapshot.taken_at.utcoffset() == timedelta(0)
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        OddsSnapshot(
            fixture=FIXTURE.ref,
            bookmaker="pinnacle",
            market=Market.btts(),
            selection=Selection.YES,
            odds=1.9,
            taken_at=datetime(2024, 1, 1),  # noqa: DTZ001
            kind=OddsKind.MORNING,
        )
