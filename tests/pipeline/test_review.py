"""The LLM hook: works without an LLM, never loses signals, maps the service's answers."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

from app.db.enums import Confidence, LlmVerdict
from app.domain import Market, Selection
from app.llm.inputs import MAX_CANDIDATES, CandidateInput, ExperienceContext
from app.llm.schemas import LlmReview, ReviewStatus, RiskFlag, Verdict
from app.llm.service import LlmService
from app.pipeline.review import (
    LlmCandidateReviewer,
    NoReview,
    Review,
    ReviewCandidate,
    ReviewKey,
    review_safely,
)

KICKOFF = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)


def candidate(number: int = 1) -> ReviewCandidate:
    return ReviewCandidate(
        fixture_id=number,
        league="Premier League",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff=KICKOFF,
        market=Market.match_result(),
        selection=Selection.HOME,
        odds=2.1,
        probability=0.55,
        ev=0.155,
        confidence=Confidence.MEDIUM,
        market_probability=0.5,
        confidence_reasons=("thin_data",),
    )


class SlowReviewer:
    async def review(self, candidates: Sequence[ReviewCandidate]) -> dict[ReviewKey, Review]:  # noqa: ARG002
        await asyncio.sleep(5)
        return {}


class ScriptedBatches:
    """Answers each batch after its own delay; remembers how often it was asked."""

    def __init__(self, delays: Sequence[float]) -> None:
        self.delays = list(delays)
        self.calls = 0

    async def review(self, candidates: Sequence[ReviewCandidate]) -> dict[ReviewKey, Review]:
        delay = self.delays[self.calls]
        self.calls += 1
        await asyncio.sleep(delay)
        return {c.key: Review(verdict=LlmVerdict.CONFIRM, note=None) for c in candidates}


class BrokenReviewer:
    async def review(self, candidates: Sequence[ReviewCandidate]) -> dict[ReviewKey, Review]:
        raise RuntimeError(len(candidates))


class ScriptedService:
    """Stands in for LlmService: confirms odd fixtures, rejects even ones, skips every third."""

    def __init__(self) -> None:
        self.batches: list[int] = []

    async def review_candidates(
        self,
        candidates: Sequence[CandidateInput],
        context: ExperienceContext | None = None,  # noqa: ARG002
    ) -> tuple[LlmReview, ...]:
        self.batches.append(len(candidates))
        reviews = []
        for number, item in enumerate(candidates):
            if number % 3 == 2:
                reviews.append(
                    LlmReview(
                        candidate_id=item.candidate_id,
                        status=ReviewStatus.FALLBACK,
                        prompt_version="t",
                    )
                )
                continue
            reviews.append(
                LlmReview(
                    candidate_id=item.candidate_id,
                    status=ReviewStatus.OK,
                    verdict=Verdict.CONFIRM if number % 3 == 0 else Verdict.REJECT,
                    risk_flags=(RiskFlag.ROTATION,),
                    note="Izoh",
                    prompt_version="t",
                )
            )
        return tuple(reviews)


async def test_no_review_has_no_opinion() -> None:
    assert await NoReview().review([candidate()]) == {}


async def test_nothing_to_review_is_not_a_call() -> None:
    assert await review_safely(BrokenReviewer(), []) == {}


async def test_a_slow_reviewer_times_out_without_losing_anything() -> None:
    assert await review_safely(SlowReviewer(), [candidate()], limit_seconds=0.01) == {}


async def test_a_slow_second_batch_does_not_take_the_first_batchs_reviews_with_it() -> None:
    reviewer = ScriptedBatches([0.0, 5.0])
    candidates = [candidate(number) for number in range(MAX_CANDIDATES + 2)]

    reviews = await review_safely(reviewer, candidates, limit_seconds=0.05, total_seconds=10)

    assert reviewer.calls == 2
    assert {key[0] for key in reviews} == set(range(MAX_CANDIDATES))  # the first batch only


async def test_each_batch_gets_its_own_time_limit() -> None:
    reviewer = ScriptedBatches([0.04, 0.04])  # together slower than one limit, each within it
    candidates = [candidate(number) for number in range(MAX_CANDIDATES + 2)]

    reviews = await review_safely(reviewer, candidates, limit_seconds=0.06, total_seconds=10)

    assert len(reviews) == len(candidates)


async def test_the_total_limit_stops_the_remaining_batches_and_keeps_what_was_collected() -> None:
    reviewer = ScriptedBatches([0.0, 0.5, 0.5])
    candidates = [candidate(number) for number in range(2 * MAX_CANDIDATES + 1)]

    reviews = await review_safely(reviewer, candidates, limit_seconds=2, total_seconds=0.2)

    assert {key[0] for key in reviews} == set(range(MAX_CANDIDATES))
    assert reviewer.calls == 2  # the third batch was never started


async def test_a_failing_batch_costs_only_itself() -> None:
    class FailsOnce:
        calls = 0

        async def review(self, candidates: Sequence[ReviewCandidate]) -> dict[ReviewKey, Review]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("first batch")
            return {c.key: Review(verdict=LlmVerdict.CAUTION, note=None) for c in candidates}

    candidates = [candidate(number) for number in range(MAX_CANDIDATES + 1)]

    reviews = await review_safely(FailsOnce(), candidates)

    assert list(reviews) == [candidates[-1].key]


async def test_a_broken_reviewer_is_ignored() -> None:
    assert await review_safely(BrokenReviewer(), [candidate()]) == {}


async def test_service_answers_are_mapped_in_batches() -> None:
    service = ScriptedService()
    reviewer = LlmCandidateReviewer(service, None)  # type: ignore[arg-type]
    candidates = [candidate(number) for number in range(MAX_CANDIDATES + 2)]

    reviews = await reviewer.review(candidates)

    assert service.batches == [MAX_CANDIDATES, 2]
    first = reviews[candidates[0].key]
    assert first == Review(verdict=LlmVerdict.CONFIRM, note="Izoh", risk_flags=("rotatsiya",))
    assert reviews[candidates[1].key].verdict is LlmVerdict.REJECT
    assert candidates[2].key not in reviews  # fallback = no opinion


async def test_a_switched_off_llm_service_gives_no_reviews() -> None:
    reviewer = LlmCandidateReviewer(LlmService(None))
    assert await reviewer.review([candidate()]) == {}
