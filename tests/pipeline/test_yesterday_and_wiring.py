"""F8.2 "Kecha yakuni" data, and the review wiring end to end (ids, form, experience)."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.db.enums import LessonStatus, Outcome, SignalSource
from app.db.repo import list_fixtures
from app.db.repo.catalog import list_leagues
from app.db.repo.feedback import LessonRecord, save_lesson
from app.domain import Sport
from app.llm.inputs import CandidateInput, ExperienceContext
from app.llm.schemas import LlmReview, ReviewStatus
from app.pipeline.daily import run_due_digests
from app.pipeline.experience import DbExperience
from app.pipeline.review import LlmCandidateReviewer, Review, ReviewCandidate, ReviewKey
from app.services.interfaces import AnalysisQuery, DateSpec, DaySummary
from tests.pipeline.feedback_world import (
    OTHER,
    USER,
    finished_fixture,
    open_world,
    place,
    settle,
)
from tests.pipeline.harness import open_env
from tests.pipeline.world import World

D = Decimal
YESTERDAY_NOON = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


async def test_the_digest_carries_yesterdays_tally_of_the_users_local_day(tmp_path: Path) -> None:
    # Tashkent is UTC+5: its day of Oct 20 runs 2026-10-19 19:00Z to 2026-10-20 19:00Z.
    kick = {
        "won1": datetime(2026, 10, 19, 19, 0, tzinfo=UTC),  # 00:00 on Oct 20: the first minute
        "won2": datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        "lost": datetime(2026, 10, 20, 15, 0, tzinfo=UTC),
        "void": datetime(2026, 10, 20, 16, 0, tzinfo=UTC),
        "open": datetime(2026, 10, 20, 17, 0, tzinfo=UTC),
        "retro": datetime(2026, 10, 20, 12, 30, tzinfo=UTC),
        "late": datetime(2026, 10, 20, 19, 0, tzinfo=UTC),  # 00:00 on Oct 21: today's
        "early": datetime(2026, 10, 19, 18, 59, tzinfo=UTC),  # 23:59 on Oct 19
    }
    fixtures = {name: finished_fixture(name, when) for name, when in kick.items()}
    async with open_world(
        tmp_path / "yesterday.db", *((f, 1, 0) for f in fixtures.values())
    ) as bare:
        placed = {name: await place(bare, f) for name, f in fixtures.items() if name != "retro"}
        retro = await place(bare, fixtures["retro"], recommended=False, source=SignalSource.RETRO)
        foreign = await place(bare, fixtures["won1"], user_id=OTHER)
        await settle(bare, placed["won1"], Outcome.WIN, clv=0.04)
        await settle(bare, placed["won2"], Outcome.WIN, clv=0.02)
        await settle(bare, placed["lost"], Outcome.LOSS)
        await settle(bare, placed["void"], Outcome.VOID)
        await settle(bare, retro, Outcome.WIN, clv=0.5)  # a look-back never counts
        await settle(bare, foreign, Outcome.LOSS, clv=-0.9)  # nor does somebody else's
        await settle(bare, placed["late"], Outcome.LOSS)  # today's local day
        await settle(bare, placed["early"], Outcome.LOSS)  # the day before yesterday

        await run_due_digests(bare.ctx, bare.system.delivery)

    [sent] = [n for n in bare.notifier.sent if n.method == "daily_digest" and n.user_id == USER]
    digest = sent.payload
    assert digest.day == date(2026, 10, 21)  # type: ignore[attr-defined]
    assert digest.yesterday == DaySummary(  # type: ignore[attr-defined]
        day=date(2026, 10, 20),
        signals=5,  # won1, won2, lost, void, open
        won=2,
        lost=1,
        void=1,
        pending=1,
        profit_units=D(1),  # +1 +1 -1 +0, in units
        clv=0.03,  # the mean of the two known values, 0.04 and 0.02
    )


async def test_a_day_without_signals_has_no_tally(tmp_path: Path) -> None:
    fixture = finished_fixture("quiet", YESTERDAY_NOON - timedelta(days=3))
    async with open_world(tmp_path / "quiet.db", (fixture, 1, 0)) as bare:
        await settle(bare, await place(bare, fixture), Outcome.WIN)

        await run_due_digests(bare.ctx, bare.system.delivery)

    [sent] = [n for n in bare.notifier.sent if n.method == "daily_digest"]
    assert sent.payload.yesterday is None  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------- wiring
@dataclass
class RecordingService:
    inputs: list[Sequence[CandidateInput]] = field(default_factory=list)
    contexts: list[ExperienceContext | None] = field(default_factory=list)

    async def review_candidates(
        self, candidates: Sequence[CandidateInput], context: ExperienceContext | None = None
    ) -> tuple[LlmReview, ...]:
        self.inputs.append(candidates)
        self.contexts.append(context)
        return tuple(
            LlmReview(candidate_id=c.candidate_id, status=ReviewStatus.FALLBACK, prompt_version="t")
            for c in candidates
        )


@dataclass
class RecordingReviewer:
    seen: list[ReviewCandidate] = field(default_factory=list)

    async def review(self, candidates: Sequence[ReviewCandidate]) -> Mapping[ReviewKey, Review]:
        self.seen.extend(candidates)
        return {}


PERMISSIVE = {"min_probability": 0.30, "min_ev": 0.0}


async def test_the_reviewer_is_given_the_owner_the_catalog_ids_and_the_form(
    seeded_template: tuple[Path, World], tmp_path: Path
) -> None:
    template, world = seeded_template
    reviewer = RecordingReviewer()
    async with open_env(template, world, tmp_path / "wire.db", reviewer=reviewer) as env:
        result = await env.system.services.analysis.analyze(
            USER, AnalysisQuery(dates=DateSpec.next_days(2), **PERMISSIVE)
        )
        async with env.sessions() as session:
            page = await list_fixtures(
                session, start=env.clock() - timedelta(days=400), end=env.clock()
            )

    assert reviewer.seen
    assert len(reviewer.seen) == len(result.signals)
    history = {r.fixture.ref: r for r in page.items if r.result is not None}
    for candidate in reviewer.seen:
        assert candidate.user_id == USER
        assert candidate.league_id and candidate.home_team_id and candidate.away_team_id
        assert len(candidate.forms) == 2  # the seeded world has long histories for everyone
        for form in candidate.forms:
            assert re.fullmatch(r"[WDL]{5}", form.results)
            assert form.goals_for >= 0
            assert form.goals_against >= 0
    # An independent oracle for one team: replay its last five finished matches.
    first = reviewer.seen[0]
    team = first.forms[0].team
    played = sorted(
        (r for r in history.values() if team in (r.fixture.home.name, r.fixture.away.name)),
        key=lambda r: r.fixture.kickoff,
    )
    played = [r for r in played if r.fixture.kickoff < first.kickoff][-5:]
    letters, scored, conceded = "", 0, 0
    for record in played:
        assert record.result is not None
        assert record.result.home_score is not None
        assert record.result.away_score is not None
        home = record.fixture.home.name == team
        own, other = (
            (record.result.home_score, record.result.away_score)
            if home
            else (record.result.away_score, record.result.home_score)
        )
        letters += "W" if own > other else "D" if own == other else "L"
        scored += own
        conceded += other
    assert (first.forms[0].results, first.forms[0].goals_for, first.forms[0].goals_against) == (
        letters,
        scored,
        conceded,
    )


async def lessons_everywhere(sessions, now: datetime) -> None:  # type: ignore[no-untyped-def]
    """An active lesson for every league and market of the database."""
    async with sessions.begin() as session:
        leagues = await list_leagues(session, Sport.FOOTBALL)
        for league_id, _ in leagues:
            for market in ("1x2", "totals", "double_chance", "btts", "asian_handicap"):
                tags = ("category:missing_info", f"league:{league_id}", f"market:{market}")
                await save_lesson(
                    session,
                    LessonRecord(
                        id=None,
                        text=f"Saboq {league_id}/{market}",
                        tags=tags,
                        confirmations=2,
                        status=LessonStatus.ACTIVE,
                        first_seen=now - timedelta(days=3),
                        last_seen=now - timedelta(days=1),
                    ),
                )


async def test_an_active_lesson_reaches_the_llm_through_a_real_analysis(
    seeded_template: tuple[Path, World], tmp_path: Path
) -> None:
    template, world = seeded_template
    service = RecordingService()
    async with open_env(template, world, tmp_path / "exp.db", reviewer=None) as probe:
        sessions, clock = probe.sessions, probe.clock
        await lessons_everywhere(sessions, clock())
        reviewer = LlmCandidateReviewer(
            service,  # type: ignore[arg-type]
            experience=DbExperience(sessions, clock),
        )
        probe.ctx = replace(probe.ctx, reviewer=reviewer)  # type: ignore[misc]
        # The services were wired over the first context: rebuild them over the new one.
        from app.services.real import create_real_services  # noqa: PLC0415

        system = create_real_services(probe.ctx, probe.notifier)
        result = await system.services.analysis.analyze(
            USER, AnalysisQuery(dates=DateSpec.next_days(2), **PERMISSIVE)
        )

    assert result.signals
    assert service.contexts
    texts = {text for context in service.contexts if context for text in context.lessons}
    assert texts
    for context, inputs in zip(service.contexts, service.inputs, strict=True):
        assert context is not None
        assert len(context.lessons) <= 5
        wanted = {f"{c.market}" for c in inputs}
        for text in context.lessons:  # each lesson belongs to a market of this batch
            assert text.rsplit("/", 1)[1] in wanted
