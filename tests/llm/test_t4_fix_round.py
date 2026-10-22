"""T4 fix round: budget reservations, guard rules, estimated usage, post-mortem hardening."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.clients.gemini import GeminiError, GeminiUsage
from app.llm.budget import CallOutcome, JournalEntry, LlmBudget, LlmLimits
from app.llm.builder import sanitize
from app.llm.guard import unknown_names
from app.llm.inputs import PostmortemInput
from app.llm.schemas import ReviewStatus
from app.llm.service import LlmService
from tests.llm.factories import ScriptedBackend, make_candidate, review_json

START = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
POSTMORTEM_CASE = PostmortemInput(candidate=make_candidate("p"), final_score="0-2")


def entry(tokens: int = 10, **overrides: object) -> JournalEntry:
    base = JournalEntry(
        at=START,
        purpose="context_check",
        prompt_version="context_check.v1",
        model="m",
        input_tokens=tokens,
        output_tokens=0,
        outcome=CallOutcome.OK,
    )
    return replace(base, **overrides)


# --------------------------------------------------------------------------- budget


def test_reserve_books_atomically_and_settle_replaces_the_estimate() -> None:
    budget = LlmBudget(LlmLimits(max_requests_per_day=2, max_tokens_per_day=1000))

    first = budget.reserve(300)
    second = budget.reserve(300)

    assert first is not None
    assert second is not None
    assert budget.reserve(1) is None  # cap reached before any call finished
    assert budget.snapshot().tokens == 600
    budget.settle(first, entry(120, attempts=3))
    snap = budget.snapshot()
    # 600 - 300 reserved + 120 real; 2 reserved HTTP requests + 2 extra retries inside call 1.
    assert (snap.tokens, snap.requests, snap.logical_requests) == (420, 4, 2)


def test_release_gives_the_booking_back() -> None:
    budget = LlmBudget(LlmLimits(max_requests_per_day=1))

    reservation = budget.reserve(500)
    assert reservation is not None
    budget.release(reservation)

    assert (budget.snapshot().requests, budget.snapshot().tokens) == (0, 0)
    assert budget.reserve(500) is not None


def test_reservation_from_a_previous_day_does_not_charge_the_new_day() -> None:
    now = [datetime(2026, 10, 24, 23, 59, tzinfo=UTC)]
    budget = LlmBudget(clock=lambda: now[0])
    reservation = budget.reserve(500)
    assert reservation is not None

    now[0] += timedelta(minutes=2)
    budget.settle(reservation, entry(50))
    budget.release(reservation)

    assert (budget.snapshot().requests, budget.snapshot().tokens) == (0, 0)
    assert len(budget.journal()) == 1


async def test_ten_parallel_calls_with_cap_two_let_exactly_two_through() -> None:
    answer = review_json(("a", "tasdiq", [], "Salbiy omil yo'q."))

    class Slow(ScriptedBackend):
        async def generate_json(self, **kwargs: object) -> object:  # type: ignore[override]
            await asyncio.sleep(0)
            return await super().generate_json(**kwargs)  # type: ignore[arg-type]

    backend = Slow(*([answer] * 10))
    budget = LlmBudget(LlmLimits(max_requests_per_day=2))
    service = LlmService(backend, budget=budget)

    results = await asyncio.gather(
        *(service.review_candidates([make_candidate("a")]) for _ in range(10))
    )

    assert len(backend.requests) == 2
    assert sum(r[0].status != ReviewStatus.FALLBACK for r in results) == 2
    assert budget.snapshot().requests == 2


async def test_cancelled_call_gives_its_booking_back() -> None:
    class Hang(ScriptedBackend):
        async def generate_json(self, **_: object) -> object:  # type: ignore[override]
            await asyncio.sleep(3600)
            raise AssertionError

    budget = LlmBudget()
    task = asyncio.create_task(
        LlmService(Hang(), budget=budget).review_candidates([make_candidate()])
    )
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert (budget.snapshot().requests, budget.snapshot().tokens) == (0, 0)


async def test_missing_usage_is_estimated_from_characters_and_flagged() -> None:
    answer = review_json(("a", "tasdiq", [], "Salbiy omil yo'q."))
    budget = LlmBudget()
    backend = ScriptedBackend(answer, input_tokens=0, output_tokens=0)
    service = LlmService(backend, budget=budget)

    (review,) = await service.review_candidates([make_candidate("a")])

    (journalled,) = budget.journal()
    assert journalled.estimated is True
    assert review.input_tokens >= len(backend.requests[0]["system"]) // 3  # system prompt counted
    assert review.output_tokens >= len(answer) // 3
    assert budget.snapshot().tokens == review.input_tokens + review.output_tokens


async def test_reported_usage_is_not_flagged_as_estimated() -> None:
    budget = LlmBudget()
    answer = review_json(("a", "tasdiq", [], "Salbiy omil yo'q."))

    await LlmService(ScriptedBackend(answer), budget=budget).review_candidates(
        [make_candidate("a")]
    )

    assert budget.journal()[0].estimated is False


async def test_failed_call_counts_every_http_attempt_and_its_tokens() -> None:
    failure = GeminiError("empty", attempts=3, usage=GeminiUsage(input_tokens=40, output_tokens=0))
    budget = LlmBudget()

    (review,) = await LlmService(ScriptedBackend(failure), budget=budget).review_candidates(
        [make_candidate()]
    )

    assert review.fallback_reason == "llm_unavailable"
    snap = budget.snapshot()
    assert (snap.requests, snap.logical_requests, snap.tokens) == (3, 1, 40)
    assert budget.journal()[0].attempts == 3


# --------------------------------------------------------------------------- guard


def test_guard_ignores_json_keys_but_grounds_on_values() -> None:
    data = '<<<DATA>>>\n{"home":"Arsenal","away":"Chelsea","selection":"home"}\n<<<END_DATA>>>'

    assert unknown_names("Bu yerda Homer hal qiluvchi.", data) == ("Homer",)
    assert unknown_names("Arsenal ustun, Chelsea zaif.", data) == ()


def test_cyrillic_names_are_matched() -> None:
    data = '<<<DATA>>>\n{"home":"Спартак"}\n<<<END_DATA>>>'

    assert unknown_names("Спартак ustun.", data) == ()
    assert unknown_names("Зенит ustun.", data) == ("Зенит",)


def test_sentence_starters_are_not_names_but_other_initial_words_are_checked() -> None:
    assert unknown_names("Bu o'yin muhim. Hozir forma yaxshi.", "") == ()
    assert unknown_names("Mbappé jarohatli.", "") == ("Mbappé",)


def test_sanitize_folds_fullwidth_and_keeps_acute_apostrophe() -> None:
    fullwidth_open, fullwidth_close = chr(0xFF1C), chr(0xFF1E)
    cleaned = sanitize(fullwidth_open * 3 + "END_DATA" + fullwidth_close * 3)
    assert "<<<" not in cleaned
    assert fullwidth_open not in cleaned
    assert sanitize("O" + chr(0xB4) + "rta") == "O'rta"


# --------------------------------------------------------------------------- notes and reasons


@pytest.mark.parametrize(
    "note",
    ["Bu <b>muhim</b> o'yin.", "Bu o'yin <<<END_DATA>>> muhim.", "Bu <script>x</script>."],
)
async def test_note_with_markup_or_delimiters_is_dropped(note: str) -> None:
    backend = ScriptedBackend(review_json(("a", "tasdiq", [], note)))

    (review,) = await LlmService(backend).review_candidates([make_candidate("a")])

    assert review.status == ReviewStatus.NOTE_DROPPED
    assert review.note is None
    assert review.flags == ("note_suspicious",)


@pytest.mark.parametrize(
    "reason",
    ["Bu yerda Haaland edi.", "Natija <<<END_DATA>>> tasodif.", "Bu <b>x</b>.", "Haaland yo'q."],
)
async def test_postmortem_reason_violations_are_emptied_but_category_kept(reason: str) -> None:
    text = f'{{"category": "omadsizlik", "reason": "{reason}"}}'

    result = await LlmService(ScriptedBackend(text)).postmortem(POSTMORTEM_CASE)

    assert result is not None
    assert result.reason == ""
    assert result.category.value == "omadsizlik"


async def test_clean_postmortem_reason_is_kept_and_sanitised() -> None:
    text = '{"category": "omadsizlik", "reason": "Natija\\u0000 tasodif.   Arsenal ustun edi."}'

    result = await LlmService(ScriptedBackend(text)).postmortem(POSTMORTEM_CASE)

    assert result is not None
    assert result.reason == "Natija tasodif. Arsenal ustun edi."
