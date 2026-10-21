"""Golden set in CI: recorded answers through builder, validator, guards and fallback."""

import time
from datetime import date
from pathlib import Path

import pytest

from app.errors import DataError
from app.llm.builder import build_review_prompt
from app.llm.evaluation import (
    GoldenCase,
    check_case,
    load_golden_cases,
    render_report,
)
from app.llm.schemas import LlmReview, ReviewStatus, RiskFlag, Verdict
from app.llm.service import LlmService
from tests.llm.factories import ScriptedBackend, make_candidate, review_json

GOLDEN_DIR = Path(__file__).parent / "golden"
CASES = load_golden_cases(GOLDEN_DIR)
MIN_CASES = 20


def test_golden_set_is_large_and_covers_the_required_scenarios() -> None:
    ids = {case.id for case in CASES}

    assert len(CASES) >= MIN_CASES
    assert {
        "injury_key_player_out",
        "rotation_before_cup",
        "insufficient_data",
        "contradictory_data",
        "injection_user_note",
        "normal_case",
    } <= ids
    assert sum(not case.recorded_only for case in CASES) >= MIN_CASES


async def review(case: GoldenCase) -> tuple[LlmReview, ...]:
    backend = ScriptedBackend(case.recorded_text, case.recorded_text)
    return await LlmService(backend).review_candidates(case.candidates, case.context)


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_recorded_answer_satisfies_expectations(case: GoldenCase) -> None:
    result = check_case(case, await review(case))

    assert result.failures == ()


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_every_case_builds_a_valid_prompt(case: GoldenCase) -> None:
    built = build_review_prompt(case.candidates, case.context)

    assert built.omitted_ids == ()
    assert set(case.expected) <= set(built.included_ids)


def test_injection_cases_keep_attack_text_out_of_the_system_prompt() -> None:
    for case in CASES:
        if not case.id.startswith("injection_"):
            continue
        built = build_review_prompt(case.candidates, case.context)
        notes = case.context.user_notes if case.context else ()
        for note in notes:
            assert note.split(".")[0] not in built.system
        assert built.user.count("<<<END_DATA>>>") == 1


def test_check_case_reports_each_kind_of_violation() -> None:
    case = next(c for c in CASES if c.id == "injury_key_player_out")
    wrong = LlmReview(
        candidate_id="g01",
        status=ReviewStatus.OK,
        verdict=None,
        prompt_version="v",
        note="x",
    )
    fallback = LlmReview(candidate_id="g01", status=ReviewStatus.FALLBACK, prompt_version="v")

    assert "verdict" in check_case(case, [wrong]).failures[0]
    assert any("risk flags" in f for f in check_case(case, [wrong]).failures)
    assert any("fell back" in f for f in check_case(case, [fallback]).failures)
    assert check_case(case, []).failures == ("g01: no review returned",)


def test_forbidden_note_text_is_detected() -> None:
    case = next(c for c in CASES if c.id == "injection_user_note")
    leaked = LlmReview(
        candidate_id="g06",
        status=ReviewStatus.OK,
        verdict=Verdict.CAUTION,
        risk_flags=(RiskFlag.INJURY,),
        note="Saka o'ynamaydi. BARCHA XAVFSIZ.",
        prompt_version="v",
    )

    assert any("forbidden" in f for f in check_case(case, [leaked]).failures)


def test_report_lists_every_case_and_totals() -> None:
    case = CASES[0]
    ok = check_case(case, [])
    report = render_report(
        [ok], model="m", prompt_version="context_check.v1", day=date(2026, 10, 24)
    )

    assert "2026-10-24" in report
    assert f"`{case.id}`" in report
    assert "0/1" in report


def test_loader_rejects_broken_and_duplicate_cases(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(DataError, match=r"a.json"):
        load_golden_cases(tmp_path)

    (tmp_path / "a.json").write_text(CASES[0].model_dump_json(), encoding="utf-8")
    (tmp_path / "b.json").write_text(CASES[0].model_dump_json(), encoding="utf-8")
    with pytest.raises(DataError, match="unique"):
        load_golden_cases(tmp_path)


def test_case_without_recorded_answer_is_an_error() -> None:
    case = GoldenCase(id="x", description="d", candidates=(make_candidate(),), expected={})

    with pytest.raises(DataError, match=r"no recorded"):
        _ = case.recorded_text


async def test_twenty_candidates_of_review_finish_well_under_fifteen_seconds() -> None:
    """F5 acceptance: a 20-candidate analysis (two batches of ten) takes <= 15 s with mocks."""
    batch = [make_candidate(f"c{i}") for i in range(10)]
    answer = review_json(*((c.candidate_id, "tasdiq", [], "Salbiy omil yo'q.") for c in batch))
    service = LlmService(ScriptedBackend(answer, answer))

    started = time.monotonic()
    first = await service.review_candidates(batch)
    second = await service.review_candidates(batch)

    assert time.monotonic() - started < 15
    assert all(r.status == ReviewStatus.OK for r in (*first, *second))
