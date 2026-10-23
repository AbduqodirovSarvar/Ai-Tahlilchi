"""The experience block (F8.7): relevant, bounded, read-only, and wired into the review."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from app.db.enums import LessonStatus, Outcome
from app.db.repo import SegmentRecord, add_signal_note, save_segment_row
from app.db.repo.feedback import LessonRecord, save_lesson, team_records
from app.domain import ExternalRef, Market, MarketType, Selection, Sport
from app.llm.inputs import (
    MAX_LESSONS,
    MAX_STAT_CHARS,
    MAX_USER_NOTE_CHARS,
    CandidateInput,
    ExperienceContext,
)
from app.llm.schemas import LlmReview, ReviewStatus
from app.models.segments import MIN_SEGMENT_SAMPLE
from app.pipeline.experience import (
    EXPERIENCE_WINDOW,
    MAX_USER_NOTES,
    DbExperience,
    lesson_applies,
    segment_line,
    tags_of_candidate,
)
from app.pipeline.review import LlmCandidateReviewer, ReviewCandidate
from tests.integration.t4_support import Bare
from tests.pipeline.feedback_world import (
    NOW,
    OTHER,
    USER,
    open_world,
    place,
    settle,
    versus,
)

LEAGUE_NAME = "Premier League"


def candidate(  # noqa: PLR0913 - the ids and names a candidate carries
    *,
    league_id: int | None = 1,
    home_id: int | None = 10,
    away_id: int | None = 11,
    market: Market | None = None,
    user_id: int | None = USER,
    home: str = "Chelsea",
    away: str = "Arsenal",
) -> ReviewCandidate:
    return ReviewCandidate(
        fixture_id=1,
        league=LEAGUE_NAME,
        home_team=home,
        away_team=away,
        kickoff=NOW + timedelta(days=1),
        market=market or Market.totals(2.5),
        selection=Selection.OVER,
        odds=1.9,
        probability=0.58,
        ev=0.07,
        confidence=_medium(),
        user_id=user_id,
        league_id=league_id,
        home_team_id=home_id,
        away_team_id=away_id,
    )


def _medium():  # type: ignore[no-untyped-def]
    from app.db.enums import Confidence  # noqa: PLC0415

    return Confidence.MEDIUM


def lesson(
    tags: tuple[str, ...], status: LessonStatus = LessonStatus.ACTIVE, confirmations: int = 2
) -> LessonRecord:
    return LessonRecord(
        id=None,
        text=f"Saboq {'/'.join(tags)}",
        tags=tags,
        confirmations=confirmations,
        status=status,
        first_seen=NOW - timedelta(days=5),
        last_seen=NOW - timedelta(days=1),
    )


# ----------------------------------------------------------------------------- pure parts
def test_candidate_tags_cover_teams_league_and_market() -> None:
    assert tags_of_candidate(candidate()) == {"team:10", "team:11", "league:1", "market:totals"}
    assert tags_of_candidate(candidate(league_id=None, home_id=None)) == {
        "team:11",
        "market:totals",
    }


def test_a_lesson_applies_only_when_its_whole_scope_is_touched() -> None:
    tags = tags_of_candidate(candidate())

    assert lesson_applies(lesson(("category:missing_info", "team:10")), tags)
    assert lesson_applies(lesson(("category:missing_info", "league:1", "market:totals")), tags)
    assert not lesson_applies(lesson(("category:missing_info", "team:99")), tags)
    assert not lesson_applies(lesson(("category:missing_info", "league:1", "market:1x2")), tags)
    assert not lesson_applies(lesson(("category:missing_info",)), tags)  # no scope at all


def test_the_segment_line_reads_like_the_plans_example() -> None:
    from app.pipeline.experience import _Segment  # noqa: PLC0415

    line = segment_line("Premier League · Total", _Segment(48, 0.062, 0.03))
    assert line == "Premier League · Total: 48 signal, ROI +6.2%, model 3 p.p. ortiqcha baholaydi"
    under = segment_line("A", _Segment(50, -0.031, -0.025))
    assert under == "A: 50 signal, ROI -3.1%, model 2 p.p. kam baholaydi"
    quiet = segment_line("A", _Segment(50, 0.0, 0.004))
    assert "model" not in quiet  # under one percentage point is noise
    assert len(segment_line("L" * 500, _Segment(50, 0.1, None))) <= MAX_STAT_CHARS


# ----------------------------------------------------------------------------- from the database
async def segment_row(  # noqa: PLR0913, PLR0917 - the columns of one segment
    bare: Bare,
    league_id: int,
    league_ref: ExternalRef,
    bucket: str,
    n: int,
    roi: float,
    bias: float | None,
    market: MarketType = MarketType.TOTALS,
) -> None:
    async with bare.sessions.begin() as session:
        await save_segment_row(
            session,
            SegmentRecord(
                sport=Sport.FOOTBALL,
                league_id=league_id,
                league_ref=league_ref,
                market=market,
                odds_bucket=bucket,
                n=n,
                roi=roi,
                clv=None,
                calib_bias=bias,
                ev_threshold=0.0,
                enabled=True,
                state_since=None,
                hold_until=None,
            ),
            now=NOW,
        )


async def seeded(bare: Bare) -> tuple[int, ExternalRef, ReviewCandidate]:
    """A signal whose fixture gives real catalog ids, and a candidate built from them."""
    signal = await place(bare, versus("Chelsea", "Arsenal"))
    fixture = signal.fixture
    assert fixture.league_id and fixture.home_team_id and fixture.away_team_id
    built = candidate(
        league_id=fixture.league_id,
        home_id=fixture.home_team_id,
        away_id=fixture.away_team_id,
    )
    return fixture.league_id, fixture.fixture.league.ref, built


async def test_segment_rows_of_the_leagues_market_are_averaged_by_sample_size(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with open_world(tmp_path / "seg.db", (versus("Chelsea", "Arsenal"), 1, 0)) as bare:
        league_id, ref, built = await seeded(bare)
        await segment_row(bare, league_id, ref, "1.50-1.80", 30, 0.10, 0.06)
        await segment_row(bare, league_id, ref, "1.80-2.20", 30, 0.02, -0.02)
        await segment_row(bare, league_id, ref, "2.20-3.00", 5, -0.9, 0.5)
        await segment_row(bare, league_id, ref, "x", 400, 0.9, 0.0, market=MarketType.BTTS)

        context = await DbExperience(bare.sessions, bare.clock).build([built])

    assert context is not None
    # ROI = (30*0.10 + 30*0.02 + 5*(-0.9)) / 65; bias = (30*.06 + 30*(-.02) + 5*.5) / 65 = 5.7 pp
    expected_roi = (30 * 0.10 + 30 * 0.02 + 5 * -0.9) / 65
    assert context.segment_stats[0] == (
        f"Premier League · Total: 65 signal, ROI {expected_roi:+.1%}, "
        "model 6 p.p. ortiqcha baholaydi"
    )


async def test_a_small_sample_is_not_reported_the_boundary_is_included(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with open_world(tmp_path / "small.db", (versus("Chelsea", "Arsenal"), 1, 0)) as bare:
        league_id, ref, built = await seeded(bare)
        experience = DbExperience(bare.sessions, bare.clock)
        await segment_row(bare, league_id, ref, "a", MIN_SEGMENT_SAMPLE - 1, 0.1, None)
        below = await experience.build([built])
        await segment_row(bare, league_id, ref, "b", 1, 0.1, None)
        at_bar = await experience.build([built])

    assert below is None
    assert at_bar is not None
    assert at_bar.segment_stats[0].startswith(f"{LEAGUE_NAME} · Total: {MIN_SEGMENT_SAMPLE} signal")


async def test_a_teams_recent_record_counts_each_pick_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    games = [versus("Chelsea", f"Rival{n}", NOW - timedelta(days=10 + n)) for n in range(5)]
    async with open_world(tmp_path / "team.db", *((g, 2, 0) for g in games)) as bare:
        outcomes = [Outcome.WIN, Outcome.WIN, Outcome.LOSS, Outcome.HALF_WIN, Outcome.VOID]
        for game, outcome in zip(games, outcomes, strict=True):
            await settle(bare, await place(bare, game), outcome)
        await settle(bare, await place(bare, games[0], user_id=OTHER), Outcome.WIN)  # same pick
        signal = await place(bare, games[0])
        chelsea = signal.fixture.home_team_id
        assert chelsea is not None
        rival = signal.fixture.away_team_id
        assert rival is not None

        async with bare.sessions() as session:
            records = await team_records(session, [chelsea, rival], since=NOW - EXPERIENCE_WINDOW)

    # Chelsea: win, win, loss, half win (void is neither) = 3 won, 1 lost; Rival0: 1 won (a win
    # for the pick on its fixture) and nothing else.
    assert records[chelsea] == (3, 1)
    assert records[rival] == (1, 0)


async def test_a_team_line_needs_enough_settled_signals(tmp_path) -> None:  # type: ignore[no-untyped-def]
    games = [versus("Chelsea", f"Rival{n}", NOW - timedelta(days=10 + n)) for n in range(3)]
    async with open_world(tmp_path / "line.db", *((g, 2, 0) for g in games)) as bare:
        signals = [await place(bare, g) for g in games]
        for signal, outcome in zip(signals, (Outcome.WIN, Outcome.LOSS, Outcome.WIN), strict=True):
            await settle(bare, signal, outcome)
        fixture = signals[0].fixture
        built = candidate(
            league_id=fixture.league_id,
            home_id=fixture.home_team_id,
            away_id=fixture.away_team_id,
        )
        experience = DbExperience(bare.sessions, bare.clock)

        context = await experience.build([built])
        short = await experience.build([replace_ids(built, home_id=None)])

    assert context is not None
    assert context.segment_stats == ("Chelsea ishtirokidagi signallar: 3 ta, 2 ✅ / 1 ❌",)
    assert short is None  # no team, league or user to be relevant to


def replace_ids(built: ReviewCandidate, *, home_id: int | None) -> ReviewCandidate:
    from dataclasses import replace  # noqa: PLC0415

    return replace(built, home_team_id=home_id, away_team_id=None, league_id=None)


async def test_only_active_relevant_lessons_come_strongest_first_at_most_five(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with open_world(tmp_path / "less.db", (versus("Chelsea", "Arsenal"), 1, 0)) as bare:
        league_id, _, built = await seeded(bare)
        home = f"team:{built.home_team_id}"
        league = f"league:{league_id}"
        async with bare.sessions.begin() as session:
            for confirmations in (2, 3, 4, 5, 6, 7, 8):  # seven relevant, active
                await save_lesson(
                    session,
                    lesson((f"category:c{confirmations}", home), confirmations=confirmations),
                )
            await save_lesson(session, lesson(("category:x", home), LessonStatus.PENDING))
            await save_lesson(session, lesson(("category:y", home), LessonStatus.EXPIRED))
            await save_lesson(session, lesson(("category:z", "team:424242")))  # other team
            await save_lesson(session, lesson(("category:w", league, "market:1x2")))  # other market

        context = await DbExperience(bare.sessions, bare.clock).build([built])

    assert context is not None
    assert len(context.lessons) == MAX_LESSONS == 5
    assert [text.split("/")[0] for text in context.lessons] == [
        f"Saboq category:c{n}" for n in (8, 7, 6, 5, 4)
    ]


async def test_a_lesson_on_the_leagues_market_applies_when_both_match(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with open_world(tmp_path / "lm.db", (versus("Chelsea", "Arsenal"), 1, 0)) as bare:
        league_id, _, built = await seeded(bare)
        async with bare.sessions.begin() as session:
            await save_lesson(
                session, lesson(("category:a", f"league:{league_id}", "market:totals"))
            )

        context = await DbExperience(bare.sessions, bare.clock).build([built])

    assert context is not None
    assert len(context.lessons) == 1


async def test_notes_are_the_users_own_newest_three_about_these_teams(tmp_path) -> None:  # type: ignore[no-untyped-def]
    games = [versus("Chelsea", f"Rival{n}", NOW - timedelta(days=20 + n)) for n in range(5)]
    sevilla = versus("Sevilla", "Betis", NOW - timedelta(days=3))
    async with open_world(tmp_path / "notes.db", *((g, 2, 0) for g in [*games, sevilla])) as bare:
        signals = [await place(bare, g) for g in games]
        mine_other_team = await place(bare, sevilla)
        foreign = await place(bare, games[0], user_id=OTHER)
        written = [
            (signals[0], "Eng eski izoh", NOW - timedelta(days=60)),
            (signals[1], "Ikkinchi izoh", NOW - timedelta(days=30)),
            (signals[2], "Uchinchi izoh", NOW - timedelta(days=20)),
            (signals[3], "To'rtinchi izoh", NOW - timedelta(days=10)),
            (signals[4], "x" * 400, NOW - timedelta(days=2)),
            (signals[0], "Hammasidan eski", NOW - timedelta(days=200)),  # outside the window
            (mine_other_team, "Boshqa jamoa", NOW - timedelta(days=1)),
        ]
        async with bare.sessions.begin() as session:
            for signal, text, at in written:
                await add_signal_note(session, USER, signal.id, text, created_at=at)
            await add_signal_note(session, OTHER, foreign.id, "Begona izoh", created_at=NOW)
        fixture = signals[0].fixture
        built = candidate(
            league_id=fixture.league_id,
            home_id=fixture.home_team_id,
            away_id=fixture.away_team_id,
        )

        context = await DbExperience(bare.sessions, bare.clock).build([built])

    assert context is not None
    assert len(context.user_notes) == MAX_USER_NOTES == 3
    assert (
        context.user_notes[0]
        == f"{NOW - timedelta(days=2):%d.%m}: {'x' * 400}"[:MAX_USER_NOTE_CHARS]
    )
    assert [n.split(": ", 1)[1] for n in context.user_notes[1:]] == [
        "To'rtinchi izoh",
        "Uchinchi izoh",
    ]
    assert all(len(n) <= MAX_USER_NOTE_CHARS for n in context.user_notes)


async def test_nothing_relevant_is_no_block_and_a_stranger_sees_none_of_the_notes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with open_world(tmp_path / "none.db", (versus("Chelsea", "Arsenal"), 1, 0)) as bare:
        _, _, built = await seeded(bare)
        experience = DbExperience(bare.sessions, bare.clock)
        signal = await place(bare, versus("Chelsea", "Arsenal"), user_id=OTHER)
        async with bare.sessions.begin() as session:
            await add_signal_note(session, OTHER, signal.id, "Faqat meniki", created_at=NOW)

        empty = await experience.build([])
        mine = await experience.build([built])
        theirs = await experience.build([replace_user(built, OTHER)])
        nobody = await experience.build([replace_user(built, None)])

    assert empty is None
    assert mine is None
    assert nobody is None
    assert theirs is not None
    assert theirs.user_notes == (f"{NOW:%d.%m}: Faqat meniki",)


def replace_user(built: ReviewCandidate, user_id: int | None) -> ReviewCandidate:
    from dataclasses import replace  # noqa: PLC0415

    return replace(built, user_id=user_id)


async def test_building_the_block_changes_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with open_world(tmp_path / "ro.db", (versus("Chelsea", "Arsenal"), 1, 0)) as bare:
        league_id, ref, built = await seeded(bare)
        await segment_row(bare, league_id, ref, "a", 60, 0.1, 0.05)
        async with bare.sessions.begin() as session:
            await save_lesson(session, lesson(("category:a", f"team:{built.home_team_id}")))
        experience = DbExperience(bare.sessions, bare.clock)

        first = await experience.build([built])
        second = await experience.build([built])

    assert first == second


# ----------------------------------------------------------------------------- the reviewer
@dataclass
class RecordingService:
    """Stands in for ``LlmService``: records what the reviewer hands over."""

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
class FixedExperience:
    context: ExperienceContext | None = None
    error: Exception | None = None
    calls: list[Sequence[ReviewCandidate]] = field(default_factory=list)

    async def build(self, candidates: Sequence[ReviewCandidate]) -> ExperienceContext | None:
        self.calls.append(candidates)
        if self.error is not None:
            raise self.error
        return self.context


BLOCK = ExperienceContext(lessons=("Saboq",))
STATIC = ExperienceContext(segment_stats=("Statik",))


async def test_each_batch_gets_the_block_built_for_it() -> None:
    service = RecordingService()
    source = FixedExperience(BLOCK)
    reviewer = LlmCandidateReviewer(service, STATIC, experience=source)  # type: ignore[arg-type]

    await reviewer.review([candidate()])

    assert service.contexts == [BLOCK]
    assert [c.user_id for c in source.calls[0]] == [USER]


async def test_without_a_source_the_fixed_context_is_used() -> None:
    service = RecordingService()
    reviewer = LlmCandidateReviewer(service, STATIC)  # type: ignore[arg-type]

    await reviewer.review([candidate()])

    assert service.contexts == [STATIC]


async def test_a_failing_or_empty_source_costs_only_the_block() -> None:
    service = RecordingService()
    failing = LlmCandidateReviewer(
        service,
        STATIC,
        experience=FixedExperience(error=RuntimeError("db down")),  # type: ignore[arg-type]
    )
    empty = LlmCandidateReviewer(service, None, experience=FixedExperience(None))  # type: ignore[arg-type]

    await failing.review([candidate()])
    await empty.review([candidate()])

    assert service.contexts == [STATIC, None]
    assert len(service.inputs) == 2  # the review itself always happens


async def test_forms_and_absences_travel_into_the_llm_input() -> None:
    from dataclasses import replace  # noqa: PLC0415

    from app.llm.inputs import Absence, AbsenceStatus, TeamForm  # noqa: PLC0415

    service = RecordingService()
    reviewer = LlmCandidateReviewer(service)  # type: ignore[arg-type]
    forms = (TeamForm(team="Chelsea", results="WWDLW", goals_for=7, goals_against=3),)
    absences = (Absence(team="Chelsea", player="Reece James", status=AbsenceStatus.OUT),)

    await reviewer.review([replace(candidate(), forms=forms, absences=absences)])

    [[sent]] = service.inputs
    assert sent.forms == forms
    assert sent.absences == absences
