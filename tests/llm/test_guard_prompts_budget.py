"""Hallucination guard, prompt loading and the budget/journal."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.errors import ConfigurationError
from app.llm.budget import CallOutcome, JournalEntry, LlmBudget, LlmLimits
from app.llm.guard import unknown_names
from app.llm.prompts import load_prompt
from app.llm.schemas import PostmortemCategory, RiskFlag, Verdict

GROUNDING = '{"home":"Arsenal","away":"Chelsea","absences":[{"player":"Palmer"}],"x":"Yevrokubok"}'


@pytest.mark.parametrize(
    "note",
    [
        "Chelsea futbolchisi Palmer o'ynamaydi.",
        "Bu o'yinda chelsea va arsenal uchrashadi.",
        "Xavf bor, chunki Yevrokubok oldida Chelsea'ning tarkibi o'zgaradi.",
        "Palmerning jarohati Chelseaga ta'sir qiladi.",
        "Ma'lumot yetarli emas, EV past bo'lishi mumkin.",
        "Haaland sentence-initial names are not checked.",
    ],
)
def test_grounded_notes_pass(note: str) -> None:
    assert unknown_names(note, GROUNDING) == ()


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("Bu o'yinda Haaland gol uradi.", ("Haaland",)),
        ("Arsenal va Liverpool o'ynaydi.", ("Liverpool",)),
        ("Chelsea uchun Mbappé muhim.", ("Mbappé",)),
        ("Xavf: Manchester City ham bor, Manchester ham.", ("Manchester", "City")),
    ],
)
def test_invented_names_are_reported(note: str, expected: tuple[str, ...]) -> None:
    assert unknown_names(note, GROUNDING) == expected


def test_accents_and_apostrophes_are_normalised() -> None:
    grounding = "Bayern München, O'Neill"

    assert unknown_names("Bu yerda Munchen va ONeill bor.", grounding) == ()


def test_short_known_words_do_not_license_suffix_matches() -> None:
    assert unknown_names("Bu yerda Manchester bor.", "Man") == ("Manchester",)


@given(st.text(max_size=200))
def test_guard_never_crashes_and_ignores_lowercase(text: str) -> None:
    lowered = text.lower()
    assert unknown_names(lowered, "") == ()
    unknown_names(text, GROUNDING)


def test_prompts_are_packaged_and_cached() -> None:
    review = load_prompt("context_check", "v1")

    assert review is load_prompt("context_check", "v1")
    assert review.version_id == "context_check.v1"
    assert "<<<DATA>>>" in review.text
    assert load_prompt("postmortem", "v1").version_id == "postmortem.v1"


def test_review_prompt_lists_every_allowed_value() -> None:
    text = load_prompt("context_check", "v1").text

    for value in [*(v.value for v in Verdict), *(f.value for f in RiskFlag)]:
        assert f"`{value}`" in text


def test_postmortem_prompt_lists_every_category() -> None:
    text = load_prompt("postmortem", "v1").text

    for category in PostmortemCategory:
        assert f"`{category.value}`" in text


@pytest.mark.parametrize(
    ("name", "version"), [("nope", "v1"), ("context_check", "v99"), ("../x", "v1"), ("a", "1")]
)
def test_bad_prompt_references_fail_clearly(name: str, version: str) -> None:
    with pytest.raises(ConfigurationError):
        load_prompt(name, version)


def entry(at: datetime, tokens: int = 10) -> JournalEntry:
    return JournalEntry(
        at=at,
        purpose="context_check",
        prompt_version="context_check.v1",
        model="m",
        input_tokens=tokens,
        output_tokens=0,
        outcome=CallOutcome.OK,
    )


def test_budget_counts_and_resets_at_utc_midnight() -> None:
    now = [datetime(2026, 10, 24, 23, 59, tzinfo=UTC)]
    budget = LlmBudget(LlmLimits(max_requests_per_day=2, max_tokens_per_day=100), lambda: now[0])

    assert budget.allow(60)
    budget.record(entry(now[0], 60))
    assert not budget.allow(60)  # tokens
    assert budget.allow(40)
    budget.record(entry(now[0], 40))
    assert not budget.allow(1)  # requests
    now[0] += timedelta(minutes=2)
    assert budget.allow(60)
    assert budget.snapshot().requests == 0
    assert len(budget.journal()) == 2
    assert budget.now() == now[0]


def test_journal_is_bounded() -> None:
    budget = LlmBudget(LlmLimits(max_requests_per_day=10_000))
    start = datetime(2026, 10, 24, tzinfo=UTC)

    for i in range(600):
        budget.record(entry(start + timedelta(seconds=i), 1))

    journal = budget.journal()
    assert len(journal) == 500
    assert journal[-1].at == start + timedelta(seconds=599)
