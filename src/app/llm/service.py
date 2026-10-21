"""The LLM step of the pipeline: review candidates, classify lost bets. Never raises.

Contract (docs/contracts.md section 7): every public method returns a usable value even when
the LLM is disabled, over budget, down, or answers garbage. Failure shows up as
``ReviewStatus.FALLBACK`` (or ``None`` for a post-mortem) with a reason, and the pipeline
sends the signal with numbers only.

Flow of one request: build input within the token budget -> budget guard -> call backend ->
validate against the Pydantic schema -> on invalid output ask once more -> map answers to
candidates -> per-note guards (length, sentence count, names not in the input).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Final

from pydantic import BaseModel, ValidationError

from app.clients.gemini import GeminiError, JsonGenerator, create_gemini_client
from app.config import Settings
from app.errors import ConfigurationError, DataError
from app.llm.budget import CallOutcome, JournalEntry, LlmBudget
from app.llm.builder import (
    DEFAULT_INPUT_TOKEN_BUDGET,
    BuiltPrompt,
    build_postmortem_prompt,
    build_review_prompt,
)
from app.llm.guard import unknown_names
from app.llm.inputs import CandidateInput, ExperienceContext, PostmortemInput
from app.llm.schemas import (
    FLAG_NOTE_TOO_LONG,
    FLAG_UNKNOWN_NAME,
    LlmReview,
    PostmortemOutput,
    PostmortemResult,
    ReviewItem,
    ReviewResponse,
    ReviewStatus,
)
from app.logging import get_logger

logger = get_logger(__name__)

MAX_ATTEMPTS_ON_INVALID_OUTPUT: Final = 2
MAX_NOTE_SENTENCES: Final = 2
MAX_NOTE_CHARS: Final = 400
# Output tokens the budget guard reserves per candidate before the real usage is known.
EXPECTED_OUTPUT_TOKENS_PER_CANDIDATE: Final = 120
EXPECTED_OUTPUT_TOKENS_POSTMORTEM: Final = 150

REASON_DISABLED: Final = "llm_disabled"
REASON_BUDGET: Final = "budget_exhausted"
REASON_UNAVAILABLE: Final = "llm_unavailable"
REASON_INVALID: Final = "invalid_output"
REASON_MISSING: Final = "missing_in_response"
REASON_OMITTED: Final = "omitted_by_token_budget"
REASON_BAD_INPUT: Final = "invalid_input"
REASON_INTERNAL: Final = "internal_error"

_SENTENCE_SPLIT: Final = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE: Final = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class _Outcome[T: BaseModel]:
    """Result of asking the model: a parsed answer or a failure reason, plus the cost."""

    parsed: T | None
    reason: str | None
    model: str | None
    input_tokens: int
    output_tokens: int


class LlmService:
    """Entry point for the pipeline; ``backend=None`` means the LLM is switched off."""

    def __init__(
        self,
        backend: JsonGenerator | None,
        *,
        budget: LlmBudget | None = None,
        input_token_budget: int = DEFAULT_INPUT_TOKEN_BUDGET,
    ) -> None:
        """Wire a model backend (any ``JsonGenerator``), its daily budget and input cap."""
        self._backend = backend
        self._budget = budget or LlmBudget()
        self._input_token_budget = input_token_budget

    @property
    def budget(self) -> LlmBudget:
        """Usage counters and request journal."""
        return self._budget

    async def review_candidates(
        self, candidates: Sequence[CandidateInput], context: ExperienceContext | None = None
    ) -> tuple[LlmReview, ...]:
        """Review candidates in one request; the result matches ``candidates`` one to one."""
        if not candidates:
            return ()
        try:
            return await self._review(candidates, context)
        except DataError as exc:
            logger.warning("llm_review_bad_input", error=str(exc))
            return _all_fallback(candidates, REASON_BAD_INPUT, "unknown")
        except Exception:
            # The LLM is optional enhancement: whatever goes wrong, the signal still ships.
            logger.exception("llm_review_unexpected_error")
            return _all_fallback(candidates, REASON_INTERNAL, "unknown")

    async def postmortem(self, case: PostmortemInput) -> PostmortemResult | None:
        """Classify why a bet lost; ``None`` when the LLM cannot answer (caller retries later)."""
        try:
            built = build_postmortem_prompt(case)
            outcome = await self._ask(
                built, PostmortemOutput, expected_output=EXPECTED_OUTPUT_TOKENS_POSTMORTEM
            )
        except Exception:
            logger.exception("llm_postmortem_unexpected_error")
            return None
        if outcome.parsed is None:
            logger.info("llm_postmortem_skipped", reason=outcome.reason)
            return None
        return PostmortemResult(
            category=outcome.parsed.category,
            reason=_clean_note(outcome.parsed.reason) or "",
            prompt_version=built.prompt.version_id,
            model=outcome.model,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
        )

    async def _review(
        self, candidates: Sequence[CandidateInput], context: ExperienceContext | None
    ) -> tuple[LlmReview, ...]:
        built = build_review_prompt(candidates, context, token_budget=self._input_token_budget)
        version = built.prompt.version_id
        outcome = await self._ask(
            built,
            ReviewResponse,
            expected_output=EXPECTED_OUTPUT_TOKENS_PER_CANDIDATE * len(built.included_ids),
        )
        if outcome.parsed is None:
            return _all_fallback(candidates, outcome.reason or REASON_INVALID, version, outcome)
        answers = _first_answer_per_id(outcome.parsed.reviews)
        reviews = tuple(
            _map_candidate(c.candidate_id, answers, built, version, outcome.model)
            for c in candidates
        )
        reviews = _attribute_tokens(reviews, outcome)
        _log_summary(reviews, outcome)
        return reviews

    async def _ask[T: BaseModel](
        self, built: BuiltPrompt, schema: type[T], *, expected_output: int
    ) -> _Outcome[T]:
        """Call the backend, validating the answer; ask once more if it is invalid."""
        if self._backend is None:
            return _Outcome(None, REASON_DISABLED, None, 0, 0)
        model = self._backend.model
        input_total = output_total = 0
        reason = REASON_INVALID
        for _ in range(MAX_ATTEMPTS_ON_INVALID_OUTPUT):
            if not self._budget.allow(built.estimated_tokens + expected_output):
                reason = REASON_BUDGET
                break
            attempt = await self._attempt(self._backend, built, schema)
            input_total += attempt.input_tokens
            output_total += attempt.output_tokens
            reason = attempt.reason or ""
            if attempt.parsed is not None or attempt.reason != REASON_INVALID:
                return _Outcome(attempt.parsed, attempt.reason, model, input_total, output_total)
        return _Outcome(None, reason, model, input_total, output_total)

    async def _attempt[T: BaseModel](
        self, backend: JsonGenerator, built: BuiltPrompt, schema: type[T]
    ) -> _Outcome[T]:
        """One request. ``reason`` is ``invalid_output`` (worth retrying) or unavailable."""
        started = monotonic()
        try:
            response = await backend.generate_json(
                system_instruction=built.system, contents=built.user, response_schema=schema
            )
        except GeminiError as exc:
            self._journal(built, CallOutcome.ERROR, 0, 0)
            logger.warning("llm_call_failed", reason=type(exc).__name__)
            return _Outcome(None, REASON_UNAVAILABLE, backend.model, 0, 0)
        usage = response.usage
        try:
            parsed = schema.model_validate_json(response.text)
        except ValidationError as exc:
            self._journal(
                built, CallOutcome.INVALID_OUTPUT, usage.input_tokens, usage.output_tokens
            )
            logger.warning(
                "llm_invalid_output",
                errors=exc.error_count(),
                latency_ms=round((monotonic() - started) * 1000),
            )
            return _Outcome(
                None, REASON_INVALID, response.model, usage.input_tokens, usage.output_tokens
            )
        self._journal(built, CallOutcome.OK, usage.input_tokens, usage.output_tokens)
        return _Outcome(parsed, None, response.model, usage.input_tokens, usage.output_tokens)

    def _journal(
        self, built: BuiltPrompt, outcome: CallOutcome, tokens_in: int, tokens_out: int
    ) -> None:
        self._budget.record(
            JournalEntry(
                at=self._budget.now(),
                purpose=built.prompt.name,
                prompt_version=built.prompt.version_id,
                model=self._backend.model if self._backend else None,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                outcome=outcome,
            )
        )


def create_llm_service(settings: Settings, budget: LlmBudget | None = None) -> LlmService:
    """Production wiring; without ``GEMINI_API_KEY`` the service runs switched off."""
    try:
        backend = create_gemini_client(settings)
    except ConfigurationError:
        logger.info("llm_disabled_no_api_key")
        return LlmService(None, budget=budget)
    return LlmService(backend, budget=budget)


def _first_answer_per_id(items: Sequence[ReviewItem]) -> dict[str, ReviewItem]:
    """Duplicate answers for one id: the first wins (deterministic)."""
    answers: dict[str, ReviewItem] = {}
    for item in items:
        answers.setdefault(item.candidate_id, item)
    return answers


def _fallback(candidate_id: str, reason: str, version: str, model: str | None = None) -> LlmReview:
    return LlmReview(
        candidate_id=candidate_id,
        status=ReviewStatus.FALLBACK,
        fallback_reason=reason,
        prompt_version=version,
        model=model,
    )


def _all_fallback(
    candidates: Sequence[CandidateInput],
    reason: str,
    version: str,
    outcome: _Outcome[ReviewResponse] | None = None,
) -> tuple[LlmReview, ...]:
    reviews = tuple(
        _fallback(c.candidate_id, reason, version, outcome.model if outcome else None)
        for c in candidates
    )
    return _attribute_tokens(reviews, outcome) if outcome else reviews


def _attribute_tokens(
    reviews: tuple[LlmReview, ...], outcome: _Outcome[ReviewResponse]
) -> tuple[LlmReview, ...]:
    """Put the request's whole cost on the first review so sums stay true."""
    if not reviews:
        return reviews
    first = reviews[0].model_copy(
        update={"input_tokens": outcome.input_tokens, "output_tokens": outcome.output_tokens}
    )
    return (first, *reviews[1:])


def _clean_note(text: str) -> str:
    """Collapse whitespace and keep at most two sentences."""
    collapsed = _WHITESPACE.sub(" ", text).strip()
    sentences = _SENTENCE_SPLIT.split(collapsed)
    return " ".join(sentences[:MAX_NOTE_SENTENCES])


def _map_candidate(
    candidate_id: str,
    answers: dict[str, ReviewItem],
    built: BuiltPrompt,
    version: str,
    model: str | None,
) -> LlmReview:
    if candidate_id in built.omitted_ids:
        return _fallback(candidate_id, REASON_OMITTED, version, model)
    item = answers.get(candidate_id)
    if item is None:
        return _fallback(candidate_id, REASON_MISSING, version, model)
    note, flags = _guard_note(item.izoh, built.data_text)
    return LlmReview(
        candidate_id=candidate_id,
        status=ReviewStatus.NOTE_DROPPED if flags else ReviewStatus.OK,
        verdict=item.verdict,
        risk_flags=tuple(dict.fromkeys(item.risk_flags)),
        note=note,
        flags=flags,
        prompt_version=version,
        model=model,
    )


def _guard_note(raw: str, grounding: str) -> tuple[str | None, tuple[str, ...]]:
    """Apply the note rules; a violating note is dropped (the verdict stays) and flagged."""
    note = _clean_note(raw)
    if not note:
        return None, ()
    if len(note) > MAX_NOTE_CHARS:
        return None, (FLAG_NOTE_TOO_LONG,)
    invented = unknown_names(note, grounding)
    if invented:
        logger.warning("llm_note_dropped_unknown_names", names=list(invented))
        return None, (FLAG_UNKNOWN_NAME,)
    return note, ()


def _log_summary(reviews: tuple[LlmReview, ...], outcome: _Outcome[ReviewResponse]) -> None:
    counts = {status: sum(r.status == status for r in reviews) for status in ReviewStatus}
    logger.info(
        "llm_review_done",
        candidates=len(reviews),
        ok=counts[ReviewStatus.OK],
        note_dropped=counts[ReviewStatus.NOTE_DROPPED],
        fallback=counts[ReviewStatus.FALLBACK],
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
    )


__all__ = ["LlmService", "create_llm_service"]
