"""Status/score merge rules (app.db.fixture_state)."""

from hypothesis import given
from hypothesis import strategies as st

from app.db.fixture_state import FixtureOutcome, merge_outcome
from app.domain import ExternalRef, FixtureResult, FixtureStatus, ResultStatus

REF = ExternalRef("src", "f1")
RESULT_STATUSES = {status.value for status in ResultStatus}


def finished(home: int = 1, away: int = 0, **extra: object) -> FixtureResult:
    return FixtureResult(
        fixture=REF, status=ResultStatus.FINISHED, home_score=home, away_score=away, **extra
    )


def readable(outcome: FixtureOutcome) -> bool:
    """What app.db.mappers.result_from_row does with a stored row: it must not raise."""
    if outcome.status.value in RESULT_STATUSES:
        FixtureResult(
            fixture=REF,
            status=ResultStatus(outcome.status.value),
            home_score=outcome.home_score,
            away_score=outcome.away_score,
            home_ht_score=outcome.home_ht_score,
            away_ht_score=outcome.away_ht_score,
            home_xg=outcome.home_xg,
            away_xg=outcome.away_xg,
        )
    return True


def test_result_is_authoritative() -> None:
    stored = FixtureOutcome(FixtureStatus.ABANDONED, 1, 0)
    merged = merge_outcome(stored, FixtureStatus.LIVE, finished(2, 2))
    assert (merged.status, merged.home_score, merged.away_score) == (FixtureStatus.FINISHED, 2, 2)


def test_finished_without_result_is_not_stored_as_finished() -> None:
    assert merge_outcome(None, FixtureStatus.FINISHED, None) == FixtureOutcome(
        FixtureStatus.SCHEDULED
    )
    live = FixtureOutcome(FixtureStatus.LIVE)
    assert merge_outcome(live, FixtureStatus.FINISHED, None) == live


def test_finished_without_result_keeps_known_score() -> None:
    stored = FixtureOutcome(FixtureStatus.LIVE, 1, 1)
    assert merge_outcome(stored, FixtureStatus.FINISHED, None) == FixtureOutcome(
        FixtureStatus.FINISHED, 1, 1
    )


def test_moving_to_postponed_clears_score_and_xg() -> None:
    stored = FixtureOutcome(FixtureStatus.ABANDONED, 1, 0, home_xg=0.8, away_xg=0.2)
    assert merge_outcome(stored, FixtureStatus.POSTPONED, None) == FixtureOutcome(
        FixtureStatus.POSTPONED
    )
    postponed = FixtureResult(fixture=REF, status=ResultStatus.POSTPONED)
    assert merge_outcome(stored, FixtureStatus.LIVE, postponed) == FixtureOutcome(
        FixtureStatus.POSTPONED
    )


def test_live_and_abandoned_keep_the_stored_score() -> None:
    stored = FixtureOutcome(FixtureStatus.LIVE, 2, 1)
    assert merge_outcome(stored, FixtureStatus.ABANDONED, None).home_score == 2
    assert merge_outcome(None, FixtureStatus.LIVE, None) == FixtureOutcome(FixtureStatus.LIVE)


def test_xg_is_merged_not_erased() -> None:
    stored = FixtureOutcome(FixtureStatus.FINISHED, 1, 0, home_xg=1.4, away_xg=0.6)
    merged = merge_outcome(stored, FixtureStatus.FINISHED, finished(1, 0, away_xg=0.7))
    assert (merged.home_xg, merged.away_xg) == (1.4, 0.7)


scores = st.integers(min_value=0, max_value=9)
xgs = st.none() | st.floats(min_value=0, max_value=6, allow_nan=False)


@st.composite
def results(draw: st.DrawFn) -> FixtureResult:
    status = draw(st.sampled_from(list(ResultStatus)))
    if status in {ResultStatus.POSTPONED, ResultStatus.CANCELLED} or (
        status is ResultStatus.ABANDONED and draw(st.booleans())
    ):
        return FixtureResult(fixture=REF, status=status, home_xg=draw(xgs))
    home, away = draw(scores), draw(scores)
    half_time = draw(st.booleans())
    return FixtureResult(
        fixture=REF,
        status=status,
        home_score=home,
        away_score=away,
        home_ht_score=draw(st.integers(0, home)) if half_time else None,
        away_ht_score=draw(st.integers(0, away)) if half_time else None,
        home_xg=draw(xgs),
        away_xg=draw(xgs),
    )


@given(st.lists(st.tuples(st.sampled_from(list(FixtureStatus)), st.none() | results()), max_size=8))
def test_any_update_sequence_leaves_a_readable_row(
    steps: list[tuple[FixtureStatus, FixtureResult | None]],
) -> None:
    stored: FixtureOutcome | None = None
    for status, result in steps:
        stored = merge_outcome(stored, status, result)
        assert readable(stored)
