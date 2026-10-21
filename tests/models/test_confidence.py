import math
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.models import (
    ConfidenceInputs,
    ConfidenceLevel,
    ConfidenceReason,
    ModelInputError,
    assess_confidence,
    lower_level,
)
from app.models.confidence import (
    GOOD_TEAM_MATCHES,
    MIN_TEAM_MATCHES,
    MODERATE_DISAGREEMENT,
    PRELIMINARY_DAYS,
    SANITY_DISAGREEMENT,
)

IDEAL = ConfidenceInputs(
    min_team_matches=40,
    xg_available=True,
    lineups_known=True,
    model_probability=0.55,
    market_probability=0.53,
    days_to_kickoff=0.2,
)


def test_ideal_inputs_give_high_confidence() -> None:
    assessment = assess_confidence(IDEAL)
    assert assessment.level is ConfidenceLevel.HIGH
    assert assessment.reasons == ()
    assert not assessment.sanity_flag
    assert assessment.disagreement == pytest.approx(0.02)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"min_team_matches": GOOD_TEAM_MATCHES - 1}, ConfidenceReason.THIN_DATA),
        ({"xg_available": False, "lineups_known": False}, ConfidenceReason.LIMITED_INFORMATION),
        ({"market_probability": None}, ConfidenceReason.NO_MARKET_REFERENCE),
        ({"market_wide_uncertainty": True}, ConfidenceReason.WIDE_MARKET_UNCERTAINTY),
        (
            {"market_probability": 0.55 - MODERATE_DISAGREEMENT - 0.01},
            ConfidenceReason.MODEL_MARKET_DISAGREEMENT,
        ),
        ({"calibration_bias": -0.06}, ConfidenceReason.CALIBRATION_BIAS),
        ({"days_to_kickoff": PRELIMINARY_DAYS + 0.5}, ConfidenceReason.FAR_FROM_KICKOFF),
    ],
)
def test_each_weakness_lowers_one_level(
    change: dict[str, object], reason: ConfidenceReason
) -> None:
    assessment = assess_confidence(replace(IDEAL, **change))  # type: ignore[arg-type]
    assert assessment.level is ConfidenceLevel.MEDIUM
    assert assessment.reasons == (reason,)


def test_only_one_of_xg_and_lineups_is_enough() -> None:
    assert assess_confidence(replace(IDEAL, xg_available=False)).level is ConfidenceLevel.HIGH
    assert assess_confidence(replace(IDEAL, lineups_known=False)).level is ConfidenceLevel.HIGH


def test_weaknesses_accumulate_down_to_low() -> None:
    weak = replace(IDEAL, min_team_matches=10, days_to_kickoff=5.0, market_wide_uncertainty=True)
    assessment = assess_confidence(weak)
    assert assessment.level is ConfidenceLevel.LOW
    assert len(assessment.reasons) == 3


def test_large_disagreement_is_low_with_sanity_flag() -> None:
    gap = replace(
        IDEAL, model_probability=0.55, market_probability=0.55 - SANITY_DISAGREEMENT - 0.01
    )
    assessment = assess_confidence(gap)
    assert assessment.level is ConfidenceLevel.LOW
    assert assessment.sanity_flag
    assert assessment.reasons == (ConfidenceReason.SANITY_DISAGREEMENT,)


def test_too_little_data_forces_low() -> None:
    assessment = assess_confidence(replace(IDEAL, min_team_matches=MIN_TEAM_MATCHES - 1))
    assert assessment.level is ConfidenceLevel.LOW
    assert assessment.reasons == (ConfidenceReason.INSUFFICIENT_DATA,)
    assert not assessment.sanity_flag


def test_retro_analysis_is_not_downgraded_for_time() -> None:
    assert assess_confidence(replace(IDEAL, days_to_kickoff=-2.0)).level is ConfidenceLevel.HIGH


@pytest.mark.parametrize(
    "change",
    [
        {"min_team_matches": -1},
        {"model_probability": 1.2},
        {"market_probability": -0.1},
        {"days_to_kickoff": math.inf},
        {"calibration_bias": math.nan},
        {"calibration_bias": 1.5},
    ],
)
def test_invalid_inputs_are_rejected(change: dict[str, object]) -> None:
    with pytest.raises(ModelInputError):
        replace(IDEAL, **change)  # type: ignore[arg-type]


def test_lower_level() -> None:
    assert lower_level(ConfidenceLevel.HIGH) is ConfidenceLevel.MEDIUM
    assert lower_level(ConfidenceLevel.HIGH, 5) is ConfidenceLevel.LOW
    assert lower_level(ConfidenceLevel.MEDIUM, 0) is ConfidenceLevel.MEDIUM
    with pytest.raises(ModelInputError):
        lower_level(ConfidenceLevel.LOW, -1)


def test_level_names_match_the_service_contract() -> None:
    # The pipeline maps model levels to the service enum by name; keep them aligned.
    from app.services.interfaces import ConfidenceLevel as ServiceLevel  # noqa: PLC0415

    assert [level.name for level in ConfidenceLevel] == [level.name for level in ServiceLevel]


confidence_inputs = st.builds(
    ConfidenceInputs,
    min_team_matches=st.integers(0, 60),
    xg_available=st.booleans(),
    lineups_known=st.booleans(),
    model_probability=st.floats(0, 1),
    market_probability=st.none() | st.floats(0, 1),
    days_to_kickoff=st.floats(-5, 20),
)


@given(confidence_inputs)
def test_level_never_rises_when_inputs_get_worse(inputs: ConfidenceInputs) -> None:
    order = list(ConfidenceLevel)
    base = order.index(assess_confidence(inputs).level)
    worse = replace(
        inputs,
        days_to_kickoff=inputs.days_to_kickoff + 10,
        min_team_matches=max(inputs.min_team_matches - 10, 0),
    )
    assert order.index(assess_confidence(worse).level) >= base
