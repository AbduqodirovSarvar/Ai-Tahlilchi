"""Hook for the LLM review of candidate signals (plan §4 step 4).

``app.llm`` is built in parallel; the pipeline only knows this small protocol and works
without it: :class:`NoReview` is the default, and a reviewer that fails or times out is
logged and ignored (the plan's fallback: signals go out with numbers only). The reviewer
never changes probabilities or EV - only a verdict, a note and risk flags.
"""

import asyncio
import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from app.db.enums import Confidence, LlmVerdict
from app.domain import Market, Selection
from app.llm.analysis import AnalysisText, MatchAnalysisInput
from app.llm.inputs import (
    MAX_CANDIDATES,
    Absence,
    CandidateInput,
    ExperienceContext,
    PostmortemInput,
    TeamForm,
)
from app.llm.schemas import PostmortemResult, ReviewStatus
from app.llm.schemas import Verdict as LlmVerdictWire
from app.llm.service import LlmService
from app.logging import get_logger

logger = get_logger(__name__)

DEFAULT_REVIEW_TIMEOUT: Final = 30.0
"""Seconds ONE batch (one LLM request of up to ``MAX_CANDIDATES`` candidates) may take."""
DEFAULT_REVIEW_TOTAL_TIMEOUT: Final = 90.0
"""Seconds all the batches of one analysis may take together; the signals of the batches that
did not finish go out unreviewed, the numbers (probability, EV) never depend on a review."""

type ReviewKey = tuple[int, Market, Selection]
"""``(fixture database id, market, selection)`` identifies a candidate within one batch."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewCandidate:
    """The numbers and context a reviewer sees about one candidate signal."""

    fixture_id: int
    league: str
    home_team: str
    away_team: str
    kickoff: datetime
    market: Market
    selection: Selection
    odds: float
    probability: float
    ev: float
    confidence: Confidence
    market_probability: float | None = None
    confidence_reasons: tuple[str, ...] = ()
    lineups_known: bool = False
    user_id: int | None = None
    """Whose analysis it is (the experience block reads this user's notes)."""
    league_id: int | None = None
    home_team_id: int | None = None
    away_team_id: int | None = None
    forms: tuple[TeamForm, ...] = ()
    absences: tuple[Absence, ...] = ()

    @property
    def key(self) -> ReviewKey:
        """Identity of this candidate in the reviewer's answer."""
        return (self.fixture_id, self.market, self.selection)


@dataclass(frozen=True, slots=True, kw_only=True)
class Review:
    """A reviewer's judgement of one candidate."""

    verdict: LlmVerdict
    note: str | None = None
    risk_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """Today's language-model usage against its daily caps (for ``/holat``)."""

    requests_used: int
    requests_limit: int
    tokens_used: int
    tokens_limit: int


class CandidateReviewer(Protocol):
    """Reviews a batch of candidates in one call (one LLM request for all of them)."""

    async def review(self, candidates: Sequence[ReviewCandidate]) -> Mapping[ReviewKey, Review]:
        """Judgements by candidate; candidates it has no opinion on may be left out."""
        ...


class ExperienceSource(Protocol):
    """Builds the "Tajriba konteksti" block of one review batch (F8.7)."""

    async def build(self, candidates: Sequence[ReviewCandidate]) -> ExperienceContext | None:
        """The block for ``candidates`` (only what is relevant to them), ``None`` when empty."""
        ...


class LlmCandidateReviewer:
    """Adapts :class:`app.llm.service.LlmService` to :class:`CandidateReviewer`.

    Candidates go out in batches of at most ``MAX_CANDIDATES`` (one LLM request each). The
    service never raises and answers one review per candidate in order; a ``fallback``
    review means "no opinion" and is left out, so such signals ship with numbers only.
    Verdicts are stored on the signal; ``rad`` makes the pick not a recommendation (decision
    #14) and ``ehtiyot`` keeps it recommended - the pipeline applies that, not this adapter.
    """

    def __init__(
        self,
        service: LlmService,
        context: ExperienceContext | None = None,
        *,
        experience: ExperienceSource | None = None,
    ) -> None:
        """Review through ``service``; the experience block is ``experience``'s (per batch).

        A fixed ``context`` is the fallback when no source is wired or the source has nothing.
        """
        self._service = service
        self._context = context
        self._experience = experience

    def usage(self) -> LlmUsage:
        """Today's requests and tokens against the daily caps."""
        snapshot = self._service.budget.snapshot()
        limits = self._service.budget.limits
        return LlmUsage(
            requests_used=snapshot.requests,
            requests_limit=limits.max_requests_per_day,
            tokens_used=snapshot.tokens,
            tokens_limit=limits.max_tokens_per_day,
        )

    async def review(self, candidates: Sequence[ReviewCandidate]) -> Mapping[ReviewKey, Review]:
        """Verdicts by candidate, from as many LLM requests as the batch size needs."""
        reviews: dict[ReviewKey, Review] = {}
        for batch in itertools.batched(candidates, MAX_CANDIDATES, strict=False):
            inputs = [_to_input(candidate) for candidate in batch]
            answers = await self._service.review_candidates(inputs, await self._context_for(batch))
            for candidate, answer in zip(batch, answers, strict=True):
                if answer.status is ReviewStatus.FALLBACK or answer.verdict is None:
                    continue
                reviews[candidate.key] = Review(
                    verdict=_VERDICTS[answer.verdict],
                    note=answer.note,
                    risk_flags=tuple(flag.value for flag in answer.risk_flags),
                )
        return reviews

    async def _context_for(self, batch: Sequence[ReviewCandidate]) -> ExperienceContext | None:
        """The experience block of ``batch``; a source that fails costs only the block."""
        if self._experience is None:
            return self._context
        try:
            built = await self._experience.build(batch)
        except Exception as error:  # experience is an enhancement: the review goes on without it
            logger.warning("experience_unavailable", error=repr(error))
            return self._context
        return built or self._context


_VERDICTS: Final = {
    LlmVerdictWire.CONFIRM: LlmVerdict.CONFIRM,
    LlmVerdictWire.CAUTION: LlmVerdict.CAUTION,
    LlmVerdictWire.REJECT: LlmVerdict.REJECT,
}
_WIRE_VERDICTS: Final = {stored: wire for wire, stored in _VERDICTS.items()}


def wire_verdict(verdict: LlmVerdict) -> str:
    """The word the LLM prompts use for a stored verdict (``tasdiq``, ``ehtiyot``, ``rad``)."""
    return _WIRE_VERDICTS[verdict].value


def _to_input(candidate: ReviewCandidate) -> CandidateInput:
    market = candidate.market
    line = 0.0 if market.line is None else market.line
    return CandidateInput(
        candidate_id=f"{candidate.fixture_id}:{market.type.value}:{candidate.selection.value}:{line}",
        league=candidate.league,
        home_team=candidate.home_team,
        away_team=candidate.away_team,
        kickoff=candidate.kickoff,
        market=market.type.value,
        selection=candidate.selection.value,
        line=market.line,
        odds=candidate.odds,
        p_model=candidate.probability,
        p_market=candidate.market_probability,
        ev=candidate.ev,
        confidence_level=candidate.confidence.value,
        confidence_reasons=candidate.confidence_reasons[:5],
        lineup_known=candidate.lineups_known,
        forms=candidate.forms,
        absences=candidate.absences,
    )


class NoReview:
    """The default reviewer: no opinion on anything."""

    async def review(self, candidates: Sequence[ReviewCandidate]) -> Mapping[ReviewKey, Review]:  # noqa: ARG002 - protocol signature
        """No reviews."""
        return {}


def llm_usage(reviewer: CandidateReviewer) -> LlmUsage | None:
    """The language-model usage of ``reviewer``, ``None`` when it is not an LLM one."""
    return reviewer.usage() if isinstance(reviewer, LlmCandidateReviewer) else None


async def review_safely(
    reviewer: CandidateReviewer,
    candidates: Sequence[ReviewCandidate],
    *,
    limit_seconds: float = DEFAULT_REVIEW_TIMEOUT,
    total_seconds: float = DEFAULT_REVIEW_TOTAL_TIMEOUT,
) -> Mapping[ReviewKey, Review]:
    """The reviewer's answers, batch by batch; a batch that fails or is too slow costs only itself.

    Every batch of :data:`MAX_CANDIDATES` gets its own ``limit_seconds``, so 20 candidates in two
    batches do not fail together (T6-F-6); ``total_seconds`` caps the whole analysis. Reviews
    already collected are kept when a later batch or the total limit gives out. Cancellation is
    never swallowed: a cancelled analysis stops here too.
    """
    reviews: dict[ReviewKey, Review] = {}
    try:
        async with asyncio.timeout(total_seconds):
            for batch in itertools.batched(candidates, MAX_CANDIDATES, strict=False):
                reviews.update(await _review_batch(reviewer, batch, limit_seconds))
    except TimeoutError:
        logger.warning("review_total_timed_out", kept=len(reviews), total_seconds=total_seconds)
    return reviews


async def _review_batch(
    reviewer: CandidateReviewer, batch: Sequence[ReviewCandidate], limit_seconds: float
) -> Mapping[ReviewKey, Review]:
    """One batch's reviews, or none when it fails or takes longer than ``limit_seconds``."""
    try:
        async with asyncio.timeout(limit_seconds):
            return await reviewer.review(batch)
    except TimeoutError:
        logger.warning("review_timed_out", candidates=len(batch), limit_seconds=limit_seconds)
    except Exception as error:  # an LLM failure must never lose the signals
        logger.warning("review_failed", candidates=len(batch), error=repr(error))
    return {}


class PostmortemAnalyst(Protocol):
    """Classifies why a recommended signal lost (F8.3)."""

    async def classify(self, case: PostmortemInput) -> PostmortemResult | None:
        """The classification, or ``None`` when no answer is available now (try again later)."""
        ...


class NoPostmortem:
    """The default analyst: never answers, so no post-mortem is ever stored."""

    async def classify(self, case: PostmortemInput) -> PostmortemResult | None:  # noqa: ARG002 - protocol signature
        """No answer."""
        return None


class LlmPostmortemAnalyst:
    """Adapts :class:`app.llm.service.LlmService.postmortem` (which never raises) to the port."""

    def __init__(self, service: LlmService) -> None:
        """Classify through ``service`` (the one that also reviews, so the budget is shared)."""
        self._service = service

    async def classify(self, case: PostmortemInput) -> PostmortemResult | None:
        """The service's answer; an unexpected failure is the same as no answer."""
        try:
            return await self._service.postmortem(case)
        except Exception as error:  # the contract says it never raises; a bug must not stop the job
            logger.warning("postmortem_failed", error=repr(error))
            return None


class AnalysisWriter(Protocol):
    """Writes the five sections of a match analysis from the facts it is given."""

    async def write(self, match: MatchAnalysisInput) -> AnalysisText | None:
        """The sections, or ``None`` (the analysis is then worded from the facts)."""
        ...


class NoAnalysisWriter:
    """The default writer: never answers, so every analysis is worded from the facts."""

    async def write(self, match: MatchAnalysisInput) -> AnalysisText | None:  # noqa: ARG002 - protocol signature
        """No answer."""
        return None


class LlmAnalysisWriter:
    """Adapts :meth:`app.llm.service.LlmService.write_analysis` (which never raises)."""

    def __init__(self, service: LlmService) -> None:
        """Write through ``service`` (shared with the reviewer, so is the daily budget)."""
        self._service = service

    async def write(self, match: MatchAnalysisInput) -> AnalysisText | None:
        """The service's sections; an unexpected failure is the same as no answer."""
        try:
            return await self._service.write_analysis(match)
        except Exception as error:  # the contract says it never raises; a bug must not stop it
            logger.warning("analysis_writer_failed", error=repr(error))
            return None
