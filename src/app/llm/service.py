"""The LLM step of the pipeline: review candidates, classify lost bets. Never raises.

Contract (docs/contracts.md section 7): every public method returns a usable value even when
the LLM is disabled, over budget, down, or answers garbage. Failure shows up as
``ReviewStatus.FALLBACK`` (or ``None`` for a post-mortem) with a reason, and the pipeline
sends the signal with numbers only.

Flow of one request: build input within the token budget -> budget guard -> call backend ->
validate against the Pydantic schema -> on invalid output ask once more -> map answers to
candidates -> per-note guards (length, sentence count, markup, names not in the input).

Budget: each backend call books its estimated cost first (``LlmBudget.reserve``) and is
settled with the real usage afterwards; when the provider reports no usage, the cost is
estimated from characters and journalled with ``estimated=True``.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Final

from pydantic import BaseModel, ValidationError

from app.clients.gemini import GeminiError, GeminiUsage, JsonGenerator, create_gemini_client
from app.config import Settings
from app.domain import CallRecorder
from app.errors import ConfigurationError, DataError
from app.llm.budget import CallOutcome, JournalEntry, LlmBudget, Reservation
from app.llm.builder import (
    DATA_CLOSE,
    DATA_OPEN,
    DEFAULT_INPUT_TOKEN_BUDGET,
    BuiltPrompt,
    build_postmortem_prompt,
    build_review_prompt,
    estimate_tokens,
    sanitize,
)
from app.llm.guard import unknown_names
from app.llm.inputs import CandidateInput, ExperienceContext, PostmortemInput
from app.llm.picks import (
    MAX_MATCHES_PER_REQUEST,
    MAX_PICKS_PER_MATCH,
    MatchPicksInput,
    PickChoice,
    PicksResponse,
    build_picks_prompt,
)
from app.llm.schemas import (
    FLAG_NOTE_SUSPICIOUS,
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
# Cap on a note the model writes (inputs.MAX_USER_NOTE_CHARS caps notes users write).
MAX_NOTE_CHARS: Final = 400
# Output tokens the budget guard reserves per candidate before the real usage is known.
EXPECTED_OUTPUT_TOKENS_PER_CANDIDATE: Final = 120
EXPECTED_OUTPUT_TOKENS_POSTMORTEM: Final = 150
EXPECTED_OUTPUT_TOKENS_PER_MATCH: Final = 160

# Task names the provider call log shows for each request (instead of the schema class name).
OPERATION_REVIEW: Final = "llm_review"
OPERATION_POSTMORTEM: Final = "post_mortem"
OPERATION_PICKS: Final = "match_picks"

REASON_DISABLED: Final = "llm_disabled"
REASON_BUDGET: Final = "budget_exhausted"
REASON_UNAVAILABLE: Final = "llm_unavailable"
REASON_INVALID: Final = "invalid_output"
REASON_MISSING: Final = "missing_in_response"
REASON_OMITTED: Final = "omitted_by_token_budget"
REASON_BAD_INPUT: Final = "invalid_input"
REASON_INTERNAL: Final = "internal_error"

# Markup or data-block delimiters in a note: the model echoed an instruction or an injection.
_SUSPICIOUS_MARKUP: Final = re.compile(r"</?[A-Za-z][^>]*>|<<<|>>>")
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
            # Unexpected by definition (known failures are handled above): keep the traceback.
            logger.warning("llm_review_unexpected_error", exc_info=True)
            return _all_fallback(candidates, REASON_INTERNAL, "unknown")

    async def postmortem(self, case: PostmortemInput) -> PostmortemResult | None:
        """Classify why a bet lost; ``None`` when the LLM cannot answer (caller retries later)."""
        try:
            built = build_postmortem_prompt(case)
            outcome = await self._ask(
                built,
                PostmortemOutput,
                expected_output=EXPECTED_OUTPUT_TOKENS_POSTMORTEM,
                operation=OPERATION_POSTMORTEM,
            )
        except Exception:
            logger.warning("llm_postmortem_unexpected_error", exc_info=True)
            return None
        if outcome.parsed is None:
            logger.info("llm_postmortem_skipped", reason=outcome.reason)
            return None
        return PostmortemResult(
            category=outcome.parsed.category,
            reason=_guard_reason(outcome.parsed.reason, built.data_text),
            prompt_version=built.prompt.version_id,
            model=outcome.model,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
        )

    async def write_picks(
        self, matches: Sequence[MatchPicksInput]
    ) -> dict[str, tuple[PickChoice, ...]]:
        """``match_id -> 1..2 chosen options`` in batches; never raises.

        A match missing from the answer (LLM off, over budget, down, or a bad answer) is the
        caller's to decide without the LLM. Only option ids of that match are accepted.
        """
        chosen: dict[str, tuple[PickChoice, ...]] = {}
        for start in range(0, len(matches), MAX_MATCHES_PER_REQUEST):
            batch = tuple(matches[start : start + MAX_MATCHES_PER_REQUEST])
            try:
                chosen.update(await self._picks(batch))
            except Exception:
                # Optional enhancement: the picks still go out, chosen by rule instead.
                logger.warning("llm_picks_unexpected_error", exc_info=True)
        return chosen

    async def _picks(self, batch: tuple[MatchPicksInput, ...]) -> dict[str, tuple[PickChoice, ...]]:
        built = build_picks_prompt(batch)
        outcome = await self._ask(
            built,
            PicksResponse,
            expected_output=EXPECTED_OUTPUT_TOKENS_PER_MATCH * len(batch),
            operation=OPERATION_PICKS,
        )
        if outcome.parsed is None:
            logger.info("llm_picks_skipped", reason=outcome.reason, matches=len(batch))
            return {}
        allowed = {match.match_id: {o.option_id for o in match.options} for match in batch}
        chosen: dict[str, tuple[PickChoice, ...]] = {}
        for answer in outcome.parsed.matches:
            options = allowed.get(answer.match_id)
            if options is None or answer.match_id in chosen:
                continue
            picks: dict[str, PickChoice] = {}
            for item in answer.picks:
                if item.option_id in options and item.option_id not in picks:
                    reason, _flags = _guard_note(item.reason, built.data_text)
                    picks[item.option_id] = PickChoice(item.option_id, reason)
            if picks:
                chosen[answer.match_id] = tuple(picks.values())[:MAX_PICKS_PER_MATCH]
        logger.info("llm_picks_done", asked=len(batch), answered=len(chosen))
        return chosen

    async def _review(
        self, candidates: Sequence[CandidateInput], context: ExperienceContext | None
    ) -> tuple[LlmReview, ...]:
        built = build_review_prompt(candidates, context, token_budget=self._input_token_budget)
        version = built.prompt.version_id
        outcome = await self._ask(
            built,
            ReviewResponse,
            expected_output=EXPECTED_OUTPUT_TOKENS_PER_CANDIDATE * len(built.included_ids),
            operation=OPERATION_REVIEW,
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
        self, built: BuiltPrompt, schema: type[T], *, expected_output: int, operation: str
    ) -> _Outcome[T]:
        """Call the backend, validating the answer; ask once more if it is invalid."""
        if self._backend is None:
            return _Outcome(None, REASON_DISABLED, None, 0, 0)
        model = self._backend.model
        estimate = built.estimated_tokens + expected_output
        input_total = output_total = 0
        reason = REASON_INVALID
        for _ in range(MAX_ATTEMPTS_ON_INVALID_OUTPUT):
            reservation = self._budget.reserve(estimate)
            if reservation is None:
                reason = REASON_BUDGET
                break
            attempt = await self._attempt(self._backend, built, schema, reservation, operation)
            input_total += attempt.input_tokens
            output_total += attempt.output_tokens
            reason = attempt.reason or ""
            if attempt.parsed is not None or attempt.reason != REASON_INVALID:
                return _Outcome(attempt.parsed, attempt.reason, model, input_total, output_total)
        return _Outcome(None, reason, model, input_total, output_total)

    async def _attempt[T: BaseModel](
        self,
        backend: JsonGenerator,
        built: BuiltPrompt,
        schema: type[T],
        reservation: Reservation,
        operation: str,
    ) -> _Outcome[T]:
        """One request. ``reason`` is ``invalid_output`` (worth retrying) or unavailable."""
        started = monotonic()
        try:
            response = await backend.generate_json(
                system_instruction=built.system,
                contents=built.user,
                response_schema=schema,
                operation=operation,
            )
        except GeminiError as exc:
            usage = exc.usage
            spent = (usage.input_tokens, usage.output_tokens, False) if usage else (0, 0, False)
            self._settle(reservation, built, CallOutcome.ERROR, spent, exc.attempts)
            logger.warning("llm_call_failed", reason=type(exc).__name__, attempts=exc.attempts)
            return _Outcome(None, REASON_UNAVAILABLE, backend.model, spent[0], spent[1])
        except BaseException:
            # Cancelled or crashed mid-call: the true cost is unknown, so give the booking back.
            self._budget.release(reservation)
            raise
        cost = _effective_usage(built, response.text, response.usage)
        try:
            parsed = schema.model_validate_json(response.text)
        except ValidationError as exc:
            self._settle(reservation, built, CallOutcome.INVALID_OUTPUT, cost, response.attempts)
            logger.warning(
                "llm_invalid_output",
                errors=exc.error_count(),
                latency_ms=round((monotonic() - started) * 1000),
            )
            return _Outcome(None, REASON_INVALID, response.model, cost[0], cost[1])
        self._settle(reservation, built, CallOutcome.OK, cost, response.attempts)
        return _Outcome(parsed, None, response.model, cost[0], cost[1])

    def _settle(
        self,
        reservation: Reservation,
        built: BuiltPrompt,
        outcome: CallOutcome,
        cost: tuple[int, int, bool],
        attempts: int,
    ) -> None:
        tokens_in, tokens_out, estimated = cost
        self._budget.settle(
            reservation,
            JournalEntry(
                at=self._budget.now(),
                purpose=built.prompt.name,
                prompt_version=built.prompt.version_id,
                model=self._backend.model if self._backend else None,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                outcome=outcome,
                attempts=max(attempts, 1),
                estimated=estimated,
            ),
        )


def _effective_usage(built: BuiltPrompt, answer: str, usage: GeminiUsage) -> tuple[int, int, bool]:
    """Reported tokens, or a character-based estimate when the provider reported none.

    A real call always has input tokens, so a zero total means "not reported"; counting it
    as free would leave the daily token cap dead.
    """
    if usage.total_tokens > 0:
        return usage.input_tokens, usage.output_tokens, False
    return (
        estimate_tokens(built.system) + estimate_tokens(built.user),
        estimate_tokens(answer),
        True,
    )


def create_llm_service(
    settings: Settings, budget: LlmBudget | None = None, recorder: CallRecorder | None = None
) -> LlmService:
    """Production wiring; without ``GEMINI_API_KEY`` the service runs switched off.

    ``recorder`` receives every call to the model (the administrator's call log).
    """
    try:
        backend = create_gemini_client(settings, recorder)
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


def _note_violation(note: str, grounding: str) -> str | None:
    """The flag for the first rule a cleaned, non-empty note breaks, if any."""
    if len(note) > MAX_NOTE_CHARS:
        return FLAG_NOTE_TOO_LONG
    if _SUSPICIOUS_MARKUP.search(note) or DATA_OPEN in note or DATA_CLOSE in note:
        return FLAG_NOTE_SUSPICIOUS
    invented = unknown_names(note, grounding)
    if invented:
        logger.warning("llm_note_dropped_unknown_names", names=list(invented))
        return FLAG_UNKNOWN_NAME
    return None


def _guard_note(raw: str, grounding: str) -> tuple[str | None, tuple[str, ...]]:
    """Apply the note rules; a violating note is dropped (the verdict stays) and flagged."""
    note = _clean_note(raw)
    if not note:
        return None, ()
    flag = _note_violation(note, grounding)
    return (None, (flag,)) if flag else (note, ())


def _guard_reason(raw: str, grounding: str) -> str:
    """Post-mortem reason under the note rules; F8 stores it as a lesson and feeds it back.

    A violating reason becomes ``""``: the category is still useful, an untrusted lesson is not.
    """
    note = _clean_note(sanitize(raw))
    if not note or _note_violation(note, grounding):
        return ""
    return note


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
