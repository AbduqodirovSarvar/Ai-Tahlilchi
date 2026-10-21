"""Tests for app.domain.entities and app.domain.batch."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import (
    DomainValidationError,
    ExternalRef,
    Fixture,
    FixtureResult,
    HistoryBatch,
    League,
    ResultStatus,
    Sport,
    Team,
    normalize_name,
)
from app.errors import DataError
from tests.domain.factories import KICKOFF, SOURCE, make_fixture, make_league, make_team


def test_validation_error_is_a_data_error_and_a_value_error() -> None:
    assert issubclass(DomainValidationError, DataError)
    assert issubclass(DomainValidationError, ValueError)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Nott'm Forest", "nottm forest"),
        ("Bayern München", "bayern munchen"),
        ("  Man   United ", "man united"),
        ("Paris S-G", "paris s g"),
        ("Atlético Madrid", "atletico madrid"),
        ("Fortuna Düsseldorf", "fortuna dusseldorf"),
    ],
)
def test_normalize_name_examples(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


@given(st.text())
def test_normalize_name_is_idempotent_and_canonical(name: str) -> None:
    normalized = normalize_name(name)
    assert normalize_name(normalized) == normalized
    assert normalized == normalized.strip()
    assert "  " not in normalized


def test_external_ref_strips_id_and_formats() -> None:
    ref = ExternalRef("api-football", " 39 ")
    assert ref.external_id == "39"
    assert str(ref) == "api-football:39"


@pytest.mark.parametrize("source", ["", "API", "has space", "-x"])
def test_external_ref_rejects_bad_source(source: str) -> None:
    with pytest.raises(DomainValidationError, match="slug"):
        ExternalRef(source, "1")


@pytest.mark.parametrize("external_id", ["", "   ", "x" * 256])
def test_external_ref_rejects_bad_id(external_id: str) -> None:
    with pytest.raises(DomainValidationError, match="external_id"):
        ExternalRef("src", external_id)


def test_team_and_league_strip_names_and_reject_empty() -> None:
    team = Team(
        ref=ExternalRef(SOURCE, "1"), sport=Sport.FOOTBALL, name=" Arsenal ", country=" England "
    )
    assert (team.name, team.country, team.normalized_name) == ("Arsenal", "England", "arsenal")
    league = League(ref=ExternalRef(SOURCE, "E0"), sport=Sport.FOOTBALL, name="EPL", country=" GB ")
    assert league.country == "GB"
    with pytest.raises(DomainValidationError, match="team name"):
        Team(ref=ExternalRef(SOURCE, "1"), sport=Sport.FOOTBALL, name=" ")
    with pytest.raises(DomainValidationError, match="league country"):
        League(ref=ExternalRef(SOURCE, "1"), sport=Sport.FOOTBALL, name="x", country="")
    with pytest.raises(DomainValidationError, match="team country"):
        Team(ref=ExternalRef(SOURCE, "1"), sport=Sport.FOOTBALL, name="x", country="")


@given(st.integers(min_value=-14 * 60, max_value=14 * 60))
def test_fixture_kickoff_is_converted_to_utc(offset_minutes: int) -> None:
    zone = timezone(timedelta(minutes=offset_minutes))
    local = KICKOFF.astimezone(zone)
    fixture = make_fixture(kickoff=local)
    assert fixture.kickoff == KICKOFF
    assert fixture.kickoff.utcoffset() == timedelta(0)


def test_fixture_accepts_iana_zone() -> None:
    london = datetime(2024, 8, 16, 20, 0, tzinfo=ZoneInfo("Europe/London"))
    assert make_fixture(kickoff=london).kickoff == KICKOFF


def test_fixture_rejects_naive_kickoff() -> None:
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        make_fixture(kickoff=datetime(2024, 8, 16, 19, 0))  # noqa: DTZ001


@pytest.mark.parametrize("season", [1849, 2201])
def test_fixture_rejects_implausible_season(season: int) -> None:
    fixture = make_fixture()
    with pytest.raises(DomainValidationError, match="season"):
        Fixture(
            ref=fixture.ref,
            sport=fixture.sport,
            league=fixture.league,
            season=season,
            kickoff=fixture.kickoff,
            home=fixture.home,
            away=fixture.away,
        )


def test_fixture_rejects_team_playing_itself() -> None:
    with pytest.raises(DomainValidationError, match="cannot play itself"):
        make_fixture("Arsenal", "Arsenal")


def test_fixture_rejects_mixed_sports() -> None:
    hockey_team = Team(ref=ExternalRef(SOURCE, "Rangers"), sport=Sport.HOCKEY, name="Rangers")
    with pytest.raises(DomainValidationError, match="mixes sports"):
        Fixture(
            ref=ExternalRef(SOURCE, "f1"),
            sport=Sport.FOOTBALL,
            league=make_league(),
            season=2024,
            kickoff=KICKOFF,
            home=make_team("Arsenal"),
            away=hockey_team,
        )


def ref() -> ExternalRef:
    return ExternalRef(SOURCE, "f1")


def test_finished_result_requires_scores() -> None:
    with pytest.raises(DomainValidationError, match="needs both scores"):
        FixtureResult(fixture=ref(), status=ResultStatus.FINISHED)


def test_result_requires_both_scores_or_neither() -> None:
    with pytest.raises(DomainValidationError, match="both full-time scores or neither"):
        FixtureResult(fixture=ref(), status=ResultStatus.ABANDONED, home_score=1)


@pytest.mark.parametrize("status", [ResultStatus.POSTPONED, ResultStatus.CANCELLED])
def test_unplayed_result_cannot_have_score(status: ResultStatus) -> None:
    assert FixtureResult(fixture=ref(), status=status).home_score is None
    with pytest.raises(DomainValidationError, match="cannot have a score"):
        FixtureResult(fixture=ref(), status=status, home_score=0, away_score=0)


def test_abandoned_result_may_keep_partial_score() -> None:
    result = FixtureResult(fixture=ref(), status=ResultStatus.ABANDONED, home_score=1, away_score=0)
    assert result.home_score == 1


def test_result_rejects_negative_scores() -> None:
    with pytest.raises(DomainValidationError, match="away_score must be >= 0"):
        FixtureResult(fixture=ref(), status=ResultStatus.FINISHED, home_score=0, away_score=-1)


@given(st.integers(0, 15), st.integers(0, 15), st.integers(0, 15))
def test_half_time_score_cannot_exceed_full_time(full: int, other: int, half: int) -> None:
    def build() -> FixtureResult:
        return FixtureResult(
            fixture=ref(),
            status=ResultStatus.FINISHED,
            home_score=full,
            away_score=other,
            home_ht_score=half,
        )

    if half <= full:
        assert build().home_ht_score == half
    else:
        with pytest.raises(DomainValidationError, match="half-time"):
            build()


def test_half_time_without_full_time_is_rejected() -> None:
    with pytest.raises(DomainValidationError, match="half-time"):
        FixtureResult(fixture=ref(), status=ResultStatus.ABANDONED, away_ht_score=0)


def test_xg_must_be_finite_and_non_negative() -> None:
    result = FixtureResult(
        fixture=ref(), status=ResultStatus.FINISHED, home_score=1, away_score=0, home_xg=1
    )
    assert isinstance(result.home_xg, float)
    with pytest.raises(DomainValidationError, match="away_xg must be >= 0"):
        FixtureResult(
            fixture=ref(), status=ResultStatus.FINISHED, home_score=1, away_score=0, away_xg=-0.1
        )
    with pytest.raises(DomainValidationError, match="finite"):
        FixtureResult(
            fixture=ref(),
            status=ResultStatus.FINISHED,
            home_score=1,
            away_score=0,
            home_xg=float("nan"),
        )


def test_batch_lists_distinct_leagues_and_teams_in_first_seen_order() -> None:
    other_league = make_league("E1", "Championship")
    batch = HistoryBatch(
        fixtures=(
            make_fixture("A", "B"),
            make_fixture("B", "C", kickoff=KICKOFF + timedelta(days=7)),
            make_fixture("D", "A", league=other_league),
        )
    )
    assert [league.ref.external_id for league in batch.leagues] == ["E0", "E1"]
    assert [team.name for team in batch.teams] == ["A", "B", "C", "D"]
    assert HistoryBatch().teams == ()
