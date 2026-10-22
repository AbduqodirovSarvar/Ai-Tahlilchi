"""QA T4-llm: never-raises fuzz, answer/id mismatches, budget concurrency, secrecy, postmortem."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from app.clients.gemini import GeminiError, GeminiResponse, GeminiUsage
from app.llm.budget import LlmBudget, LlmLimits
from app.llm.inputs import ExperienceContext, PostmortemInput
from app.llm.schemas import (
    LlmReview,
    ReviewResponse,
    ReviewStatus,
    RiskFlag,
    Verdict,
)
from app.llm.service import LlmService
from app.logging import configure_logging
from tests.llm.factories import ScriptedBackend, make_candidate, review_json


def ids_of(reviews: tuple[LlmReview, ...]) -> list[str]:
    return [r.candidate_id for r in reviews]


# --------------------------------------------------------------------------- id mismatches


@pytest.mark.parametrize(
    ("answer_ids", "expected_status"),
    [
        ([], [ReviewStatus.FALLBACK] * 3),  # empty list
        (["a", "b"], [ReviewStatus.OK, ReviewStatus.OK, ReviewStatus.FALLBACK]),  # missing
        (["c", "b", "a"], [ReviewStatus.OK] * 3),  # reordered
        (["a", "a", "b", "c"], [ReviewStatus.OK] * 3),  # duplicated answer
        (["a", "b", "c", "zzz", "yyy"], [ReviewStatus.OK] * 3),  # extra ids
        (["A", "B", "C"], [ReviewStatus.FALLBACK] * 3),  # wrong case
        ([" a", "b ", "c"], [ReviewStatus.FALLBACK, ReviewStatus.FALLBACK, ReviewStatus.OK]),
    ],
)
async def test_output_is_one_to_one_in_input_order_whatever_the_model_answers(
    answer_ids: list[str], expected_status: list[ReviewStatus]
) -> None:
    text = review_json(*((i, "tasdiq", [], "Salbiy omil yo'q.") for i in answer_ids))
    service = LlmService(ScriptedBackend(text))

    reviews = await service.review_candidates([make_candidate(i) for i in "abc"])

    assert ids_of(reviews) == ["a", "b", "c"]
    assert [r.status for r in reviews] == expected_status


async def test_first_duplicate_answer_wins() -> None:
    text = review_json(
        ("a", "rad", ["jarohat"], "Palmer o'ynamaydi."), ("a", "tasdiq", [], "Hammasi joyida.")
    )

    (review,) = await LlmService(ScriptedBackend(text)).review_candidates([make_candidate("a")])

    assert review.verdict == Verdict.REJECT


async def test_duplicate_input_ids_fall_back_for_every_candidate_and_keep_order() -> None:
    backend = ScriptedBackend()

    reviews = await LlmService(backend).review_candidates(
        [make_candidate("a"), make_candidate("b"), make_candidate("a")]
    )

    assert ids_of(reviews) == ["a", "b", "a"]
    assert {r.fallback_reason for r in reviews} == {"invalid_input"}
    assert backend.requests == []


async def test_more_than_ten_candidates_keep_order_and_extras_fall_back() -> None:
    candidates = [make_candidate(f"c{i:02d}") for i in range(13)]
    text = review_json(*((c.candidate_id, "tasdiq", [], "Salbiy omil yo'q.") for c in candidates))

    reviews = await LlmService(ScriptedBackend(text)).review_candidates(candidates)

    assert ids_of(reviews) == [c.candidate_id for c in candidates]
    assert [r.status for r in reviews[:10]] == [ReviewStatus.OK] * 10
    assert {r.fallback_reason for r in reviews[10:]} == {"omitted_by_token_budget"}


# ----------------------------------------------------------- numbers are untouchable


def test_wire_schema_has_no_numeric_field_the_model_could_use_to_change_numbers() -> None:
    schema = json.dumps(ReviewResponse.model_json_schema())

    for forbidden in ("p_model", "p_market", "ev", "odds", "probability", "stake"):
        assert f'"{forbidden}"' not in schema
    assert '"type": "number"' not in schema
    assert '"type": "integer"' not in schema


async def test_numbers_smuggled_into_the_answer_are_ignored() -> None:
    text = json.dumps(
        {
            "p_model": 0.99,
            "reviews": [
                {
                    "candidate_id": "a",
                    "verdict": "tasdiq",
                    "risk_flags": [],
                    "izoh": "Salbiy omil yo'q.",
                    "p_model": 0.99,
                    "ev": 9.9,
                    "odds": 50,
                }
            ],
        }
    )

    (review,) = await LlmService(ScriptedBackend(text)).review_candidates([make_candidate("a")])

    assert review.status == ReviewStatus.OK
    assert not {"p_model", "ev", "odds"} & set(LlmReview.model_fields)
    assert "0.99" not in review.model_dump_json()


@pytest.mark.parametrize(
    "verdict", ["TASDIQ", "Tasdiq", "tasdiq ", "tasdiq\u200b", "yes", "", None]
)
async def test_verdict_must_match_the_enum_exactly(verdict: str | None) -> None:
    text = json.dumps(
        {"reviews": [{"candidate_id": "a", "verdict": verdict, "risk_flags": [], "izoh": "x"}]}
    )
    backend = ScriptedBackend(text, text)

    (review,) = await LlmService(backend).review_candidates([make_candidate("a")])

    assert review.status == ReviewStatus.FALLBACK
    assert review.verdict is None
    assert review.fallback_reason == "invalid_output"
    assert len(backend.requests) == 2


async def test_duplicate_risk_flags_are_collapsed_and_unknown_ones_rejected() -> None:
    dup = review_json(("a", "ehtiyot", ["jarohat", "jarohat", "derbi"], "Qiyin o'yin."))
    (review,) = await LlmService(ScriptedBackend(dup)).review_candidates([make_candidate("a")])
    assert review.risk_flags == (RiskFlag.INJURY, RiskFlag.DERBY)


async def test_huge_note_is_dropped_but_verdict_kept() -> None:
    text = review_json(("a", "rad", ["jarohat"], "Palmer o'ynamaydi. " * 100_000))

    (review,) = await LlmService(ScriptedBackend(text)).review_candidates([make_candidate("a")])

    assert review.verdict == Verdict.REJECT
    assert review.note is None or len(review.note) <= 400


# --------------------------------------------------------------------------- never raises


class BoomError(Exception):
    pass


EXCEPTIONS = (
    RuntimeError("x"),
    ValueError("x"),
    KeyError("x"),
    OSError("x"),
    TimeoutError(),
    UnicodeError("x"),
    RecursionError(),
    MemoryError(),
    ZeroDivisionError(),
    BoomError(),
    GeminiError("down"),
    json.JSONDecodeError("m", "d", 0),
)


@pytest.mark.parametrize("error", EXCEPTIONS, ids=lambda e: type(e).__name__)
async def test_any_backend_exception_becomes_fallback_never_an_exception(
    error: BaseException,
) -> None:
    service = LlmService(ScriptedBackend(error, error))
    candidates = [make_candidate("a"), make_candidate("b")]

    reviews = await service.review_candidates(candidates)
    result = await service.postmortem(
        PostmortemInput(candidate=make_candidate("p"), final_score="0-1")
    )

    assert ids_of(reviews) == ["a", "b"]
    assert all(r.status == ReviewStatus.FALLBACK for r in reviews)
    assert result is None


async def test_cancellation_is_not_swallowed() -> None:
    service = LlmService(ScriptedBackend(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await service.review_candidates([make_candidate("a")])


class FuzzBackend:
    """Backend whose every call either raises or returns arbitrary text."""

    model = "fuzz"

    def __init__(self, behaviours: list[object]) -> None:
        self.behaviours = behaviours

    async def generate_json(
        self, *, system_instruction: str, contents: str, response_schema: type[BaseModel]
    ) -> GeminiResponse:
        del system_instruction, contents, response_schema
        item = self.behaviours.pop(0) if self.behaviours else RuntimeError("exhausted")
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, tuple)
        text, tokens_in, tokens_out = item
        return GeminiResponse(
            text=text, model="fuzz", usage=GeminiUsage(tokens_in, tokens_out), attempts=1
        )


id_text = st.one_of(st.sampled_from(["a", "b", "c", "A", "", " a", "zzz"]), st.text(max_size=8))
review_item = st.fixed_dictionaries(
    {
        "candidate_id": st.one_of(id_text, st.integers(), st.none()),
        "verdict": st.one_of(st.sampled_from(["tasdiq", "ehtiyot", "rad", "RAD", "x"]), st.none()),
        "risk_flags": st.one_of(
            st.lists(st.sampled_from([f.value for f in RiskFlag] + ["yangi"]), max_size=4),
            st.none(),
        ),
        "izoh": st.one_of(st.text(max_size=300), st.none(), st.integers()),
    }
)
random_json = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=10)),
    lambda children: st.one_of(
        st.lists(children, max_size=3), st.dictionaries(st.text(max_size=5), children, max_size=3)
    ),
    max_leaves=8,
)
answer_text = st.one_of(
    st.builds(lambda items: json.dumps({"reviews": items}), st.lists(review_item, max_size=6)),
    st.builds(json.dumps, random_json),
    st.text(max_size=60),
    st.just(""),
)
behaviour = st.one_of(
    st.tuples(answer_text, st.integers(0, 10**6), st.integers(0, 10**6)),
    st.sampled_from(
        [RuntimeError("x"), ValueError("x"), OSError("x"), TimeoutError(), GeminiError("d")]
    ),
)


@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    behaviours=st.lists(behaviour, min_size=1, max_size=3),
    ids=st.lists(st.sampled_from(["a", "b", "c", "d"]), max_size=14),
)
def test_fuzz_review_never_raises_and_stays_one_to_one(
    behaviours: list[object], ids: list[str]
) -> None:
    candidates = [make_candidate(candidate_id) for candidate_id in ids]
    service = LlmService(FuzzBackend(list(behaviours)))

    # capture_logs: skips slow traceback rendering of the many injected failures.
    with structlog.testing.capture_logs():
        reviews = asyncio.run(service.review_candidates(candidates))

    assert ids_of(reviews) == ids
    for review in reviews:
        if review.status == ReviewStatus.FALLBACK:
            assert review.verdict is None
            assert review.note is None
            assert review.fallback_reason
        else:
            assert isinstance(review.verdict, Verdict)
            assert review.fallback_reason is None
        if review.note is not None:
            assert len(review.note) <= 400
    assert sum(r.input_tokens for r in reviews) <= 2 * 10**6 + 1


@settings(max_examples=100, deadline=None)
@given(behaviours=st.lists(behaviour, min_size=1, max_size=3))
def test_fuzz_postmortem_never_raises(behaviours: list[object]) -> None:
    service = LlmService(FuzzBackend(list(behaviours)))
    case = PostmortemInput(candidate=make_candidate("p"), final_score="1-2")

    with structlog.testing.capture_logs():
        result = asyncio.run(service.postmortem(case))

    assert result is None or result.reason is not None


# --------------------------------------------------------------------------- budget


async def test_concurrent_calls_are_all_journaled_and_counted() -> None:
    answer = review_json(("a", "tasdiq", [], "Salbiy omil yo'q."))

    class Slow(ScriptedBackend):
        async def generate_json(self, **kwargs: object) -> GeminiResponse:  # type: ignore[override]
            await asyncio.sleep(0)
            return await super().generate_json(**kwargs)  # type: ignore[arg-type]

    budget = LlmBudget(LlmLimits(max_requests_per_day=100, max_tokens_per_day=10**6))
    service = LlmService(Slow(*([answer] * 25)), budget=budget)

    results = await asyncio.gather(
        *(service.review_candidates([make_candidate("a")]) for _ in range(25))
    )

    assert all(r[0].status == ReviewStatus.OK for r in results)
    assert budget.snapshot().requests == 25
    assert budget.snapshot().tokens == 25 * 150
    assert len(budget.journal()) == 25


@pytest.mark.xfail(
    strict=True,
    reason="QA T4-llm F-3: allow() and record() are separated by an await, so N concurrent "
    "calls all pass the check; the daily cap is overshot (service.py:167 / budget.py:329).",
)
async def test_concurrent_calls_cannot_overshoot_the_request_cap() -> None:
    answer = review_json(("a", "tasdiq", [], "Salbiy omil yo'q."))

    class Slow(ScriptedBackend):
        async def generate_json(self, **kwargs: object) -> GeminiResponse:  # type: ignore[override]
            await asyncio.sleep(0)
            return await super().generate_json(**kwargs)  # type: ignore[arg-type]

    backend = Slow(*([answer] * 10))
    service = LlmService(backend, budget=LlmBudget(LlmLimits(max_requests_per_day=2)))

    await asyncio.gather(*(service.review_candidates([make_candidate("a")]) for _ in range(10)))

    assert len(backend.requests) <= 2


@pytest.mark.xfail(
    strict=True,
    reason="QA T4-llm F-8: when the SDK returns no usage metadata the call is counted as 0 "
    "tokens, so the daily TOKEN cap can never trip (gemini.py:202, budget.py:349).",
)
async def test_missing_usage_metadata_still_consumes_token_budget() -> None:
    answer = review_json(("a", "tasdiq", [], "Salbiy omil yo'q."))
    budget = LlmBudget()
    service = LlmService(ScriptedBackend(answer, input_tokens=0, output_tokens=0), budget=budget)

    await service.review_candidates([make_candidate("a")])

    assert budget.snapshot().tokens > 0


async def test_exhausted_budget_skips_the_backend_and_falls_back() -> None:
    backend = ScriptedBackend()
    service = LlmService(backend, budget=LlmBudget(LlmLimits(max_requests_per_day=0)))

    (review,) = await service.review_candidates([make_candidate("a")])

    assert review.fallback_reason == "budget_exhausted"
    assert backend.requests == []


async def test_invalid_output_retry_is_stopped_by_the_budget_between_attempts() -> None:
    backend = ScriptedBackend("not json", "not json")
    service = LlmService(backend, budget=LlmBudget(LlmLimits(max_requests_per_day=1)))

    (review,) = await service.review_candidates([make_candidate("a")])

    assert len(backend.requests) == 1
    assert review.status == ReviewStatus.FALLBACK


async def test_budget_day_rolls_over_between_two_calls_at_utc_midnight() -> None:
    clock = [datetime(2026, 10, 24, 23, 59, 59, tzinfo=UTC)]
    answer = review_json(("a", "tasdiq", [], "Salbiy omil yo'q."))
    budget = LlmBudget(LlmLimits(max_requests_per_day=1), lambda: clock[0])
    service = LlmService(ScriptedBackend(answer, answer), budget=budget)

    first = await service.review_candidates([make_candidate("a")])
    blocked = await service.review_candidates([make_candidate("a")])
    clock[0] += timedelta(seconds=1)
    after_midnight = await service.review_candidates([make_candidate("a")])

    assert first[0].status == ReviewStatus.OK
    assert blocked[0].fallback_reason == "budget_exhausted"
    assert after_midnight[0].status == ReviewStatus.OK
    assert budget.snapshot().day.isoformat() == "2026-10-25"


# --------------------------------------------------------------------------- secrecy


SECRET = "AIza-qa-secret-key-9f8e7d6c5b4a"


async def test_secret_in_an_unexpected_exception_never_reaches_rendered_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", app_env="prod", secret_values=[SECRET])
    service = LlmService(ScriptedBackend(RuntimeError(f"bad url ?key={SECRET}")))

    await service.review_candidates([make_candidate("a")])

    captured = capsys.readouterr()
    assert "llm_review_unexpected_error" in captured.out
    assert SECRET not in captured.out + captured.err


async def test_prompt_text_is_not_logged_at_info_or_warning() -> None:
    note = "UNIQUE-USER-NOTE-TEXT"
    backend = ScriptedBackend("not json", "not json")
    with structlog.testing.capture_logs() as logs:
        await LlmService(backend).review_candidates(
            [make_candidate("a")], ExperienceContext(user_notes=(note,))
        )

    assert note not in str(logs)
    assert "Arsenal" not in str(logs)


# --------------------------------------------------------------------------- postmortem guards


POSTMORTEM_CASE = PostmortemInput(candidate=make_candidate("p"), final_score="0-2")


@pytest.mark.xfail(
    strict=True,
    reason="QA T4-llm F-9: postmortem reason skips the hallucination and length guards "
    "that review notes get (service.py:129); F8 stores it as a lesson that is fed back "
    "into later prompts.",
)
async def test_postmortem_reason_with_invented_name_is_not_stored() -> None:
    text = json.dumps(
        {"category": "ma'lumot yetishmadi", "reason": "Bu yerda Haaland jarohatlangan edi."}
    )

    result = await LlmService(ScriptedBackend(text)).postmortem(POSTMORTEM_CASE)

    assert result is None or "Haaland" not in result.reason


@pytest.mark.xfail(
    strict=True,
    reason="QA T4-llm F-9: postmortem reason has no 400-char cap (service.py:129).",
)
async def test_postmortem_reason_is_length_capped() -> None:
    text = json.dumps({"category": "omadsizlik", "reason": "Natija tasodif. " + "x" * 5000})

    result = await LlmService(ScriptedBackend(text)).postmortem(POSTMORTEM_CASE)

    assert result is None or len(result.reason) <= 400


async def test_postmortem_category_is_enum_validated() -> None:
    bad = json.dumps({"category": "boshqa", "reason": "x"})

    result = await LlmService(ScriptedBackend(bad, bad)).postmortem(POSTMORTEM_CASE)

    assert result is None
