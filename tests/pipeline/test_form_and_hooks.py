"""Team form for the LLM input, the review hooks, the new config knobs and the new jobs."""

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.db.enums import LlmVerdict
from app.db.repo import list_fixtures
from app.errors import ConfigurationError
from app.llm.inputs import PostmortemInput
from app.llm.schemas import PostmortemResult
from app.pipeline.config import PipelineConfig
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import NullDelivery
from app.pipeline.form import FORM_LENGTH, forms_for, result_letter, team_form
from app.pipeline.review import (
    LlmPostmortemAnalyst,
    NoPostmortem,
    wire_verdict,
)
from app.scheduler import (
    FEEDBACK_AT,
    JOB_FEEDBACK,
    JOB_LESSONS,
    JOB_NAMES,
    JOB_TEMPLATE_TICK,
    LESSONS_AT,
    PipelineJobs,
    PipelineScheduler,
)
from tests.pipeline.feedback_world import NOW, open_world, versus

TASHKENT = ZoneInfo("Asia/Tashkent")


# ----------------------------------------------------------------------------- form
@given(st.integers(0, 9), st.integers(0, 9))
def test_a_result_letter_follows_the_score(goals_for: int, goals_against: int) -> None:
    letter = result_letter(goals_for, goals_against)

    assert letter == (
        "W" if goals_for > goals_against else "D" if goals_for == goals_against else "L"
    )


async def test_form_is_the_last_five_before_the_match_oldest_first(tmp_path: Path) -> None:
    day = timedelta(days=1)
    start = NOW - 30 * day
    history = [  # (opponent, venue, goals for, goals against), oldest first
        ("O1", "H", 2, 0),  # W   dropped: only the last five count
        ("O2", "A", 1, 1),  # D
        ("O3", "A", 0, 3),  # L
        ("O4", "H", 1, 0),  # W
        ("O5", "A", 2, 1),  # W   (O5 plays away: Hull's goals are the second number reversed)
        ("O6", "H", 0, 0),  # D
    ]
    fixtures = []
    for index, (rival, venue, scored, conceded) in enumerate(history):
        home, away = ("Hull", rival) if venue == "H" else (rival, "Hull")
        # the score is written from the home side's view
        home_goals, away_goals = (scored, conceded) if venue == "H" else (conceded, scored)
        fixtures.append((versus(home, away, start + index * day), home_goals, away_goals))
    after = versus("Hull", "Late", NOW + day, finished=False)
    leaked = (versus("Hull", "Leak", NOW + 2 * day), 9, 0)  # finished *after* the target
    target = versus("Hull", "Target", NOW + day)
    async with open_world(
        tmp_path / "form.db", *fixtures, leaked, upcoming=(after, target)
    ) as bare:
        async with bare.sessions() as session:
            page = await list_fixtures(session, start=NOW, end=NOW + 5 * day)
        records = [r for r in page.items if r.fixture.away.name in {"Target", "Late"}]
        target_record = next(r for r in records if r.fixture.away.name == "Target")

        forms = await forms_for(bare.ctx, [target_record])

    [hull] = forms[target_record.id]  # "Target" has no history: left out, not guessed
    # last five of the six: D L W W D  -> goals for 1+0+1+2+0 = 4, against 1+3+0+1+0 = 5
    assert (hull.team, hull.results, hull.goals_for, hull.goals_against) == ("Hull", "DLWWD", 4, 5)
    assert len(hull.results) == FORM_LENGTH


async def test_a_team_without_history_is_left_out(tmp_path: Path) -> None:
    target = versus("Newcomer", "Stranger", NOW + timedelta(days=1), finished=False)
    async with open_world(tmp_path / "new.db", upcoming=(target,)) as bare:
        async with bare.sessions() as session:
            page = await list_fixtures(session, start=NOW, end=NOW + timedelta(days=3))

        forms = await forms_for(bare.ctx, list(page.items))

    assert forms[page.items[0].id] == ()


def test_a_team_form_needs_matches() -> None:
    assert team_form(versus("A", "B").home, []) is None


# ----------------------------------------------------------------------------- hooks
def test_the_stored_verdicts_have_prompt_words() -> None:
    assert [wire_verdict(v) for v in LlmVerdict] == ["tasdiq", "ehtiyot", "rad"]


@dataclass
class FakePostmortemService:
    answer: PostmortemResult | None = None
    error: Exception | None = None
    cases: list[PostmortemInput] = field(default_factory=list)

    async def postmortem(self, case: PostmortemInput) -> PostmortemResult | None:
        self.cases.append(case)
        if self.error is not None:
            raise self.error
        return self.answer


async def test_the_default_analyst_never_answers() -> None:
    assert await NoPostmortem().classify(_case()) is None


async def test_the_llm_analyst_passes_the_service_answer_through() -> None:
    from tests.pipeline.doubles import verdict  # noqa: PLC0415

    service = FakePostmortemService(answer=verdict())

    answer = await LlmPostmortemAnalyst(service).classify(_case())  # type: ignore[arg-type]

    assert answer == verdict()
    assert len(service.cases) == 1


async def test_an_unexpected_failure_is_no_answer() -> None:
    service = FakePostmortemService(error=RuntimeError("bug"))

    assert await LlmPostmortemAnalyst(service).classify(_case()) is None  # type: ignore[arg-type]


def _case() -> PostmortemInput:
    from tests.llm.factories import make_candidate  # noqa: PLC0415

    return PostmortemInput(candidate=make_candidate(), final_score="0-1")


# ----------------------------------------------------------------------------- config
def test_the_postmortem_knobs_have_sensible_defaults_and_are_validated() -> None:
    config = PipelineConfig()
    assert config.postmortems_per_run == 10
    assert config.postmortem_lookback == timedelta(days=30)
    with pytest.raises(ConfigurationError):
        PipelineConfig(postmortems_per_run=0)
    with pytest.raises(ConfigurationError):
        PipelineConfig(postmortem_lookback=timedelta(0))


# ----------------------------------------------------------------------------- scheduler
async def test_the_feedback_jobs_are_registered_at_their_times(tmp_path: Path) -> None:
    scheduler = PipelineScheduler(
        PipelineJobs.__new__(PipelineJobs), db_path=tmp_path / "jobs.db", timezone=TASHKENT
    )
    scheduler.start()
    try:
        runs = scheduler.next_runs()
        assert set(runs) == set(JOB_NAMES)
        feedback, lessons = runs[JOB_FEEDBACK], runs[JOB_LESSONS]
        assert feedback is not None
        assert lessons is not None
        assert (feedback.astimezone(TASHKENT).hour, feedback.astimezone(TASHKENT).minute) == (
            FEEDBACK_AT.hour,
            FEEDBACK_AT.minute,
        )
        local = lessons.astimezone(TASHKENT)
        assert (local.weekday(), local.hour, local.minute) == (
            0,
            LESSONS_AT.hour,
            LESSONS_AT.minute,
        )
        assert runs[JOB_TEMPLATE_TICK] is not None
    finally:
        await scheduler.stop()


async def test_a_restart_keeps_every_job_once(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    names: list[set[str]] = []
    for _ in range(3):
        scheduler = PipelineScheduler(
            PipelineJobs.__new__(PipelineJobs), db_path=path, timezone=TASHKENT
        )
        scheduler.start()
        names.append(set(scheduler.next_runs()))
        await scheduler.stop()

    assert names == [set(JOB_NAMES)] * 3
    assert len(JOB_NAMES) == len(set(JOB_NAMES)) == 8


@dataclass
class Calls:
    names: list[str] = field(default_factory=list)


async def test_feedback_explains_then_rebuilds_lessons_only_with_news(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.pipeline.postmortem import PostmortemReport  # noqa: PLC0415

    calls = Calls()
    reports = [PostmortemReport(explained=1), PostmortemReport(), PostmortemReport(copied=2)]

    async def fake_postmortems(ctx: PipelineContext) -> PostmortemReport:  # noqa: ARG001
        calls.names.append("postmortems")
        return reports.pop(0)

    async def fake_lessons(ctx: PipelineContext) -> None:  # noqa: ARG001
        calls.names.append("lessons")

    monkeypatch.setattr("app.scheduler.run_postmortems", fake_postmortems)
    monkeypatch.setattr("app.scheduler.run_lessons", fake_lessons)
    async with open_world(tmp_path / "jobs.db") as bare:
        jobs = PipelineJobs(bare.ctx, NullDelivery())
        for _ in range(3):
            await jobs.run(JOB_FEEDBACK)
        await jobs.run(JOB_LESSONS)

    assert calls.names == [
        "postmortems", "lessons",  # news: explained one
        "postmortems",  # nothing new: no rebuild
        "postmortems", "lessons",  # copied twins count as news
        "lessons",  # the weekly maintenance always rebuilds
    ]  # fmt: skip


async def test_the_template_tick_runs_the_templates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[PipelineContext] = []

    async def fake_templates(ctx: PipelineContext, delivery: object) -> None:  # noqa: ARG001
        seen.append(ctx)

    monkeypatch.setattr("app.scheduler.run_due_templates", fake_templates)
    async with open_world(tmp_path / "tick.db") as bare:
        await PipelineJobs(bare.ctx, NullDelivery()).run(JOB_TEMPLATE_TICK)

    assert seen == [bare.ctx]


async def test_a_failing_feedback_job_is_recorded_and_never_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def broken(ctx: PipelineContext) -> None:  # noqa: ARG001
        msg = "database is locked"
        raise RuntimeError(msg)

    monkeypatch.setattr("app.scheduler.run_postmortems", broken)
    async with open_world(tmp_path / "fail.db") as bare:
        await PipelineJobs(bare.ctx, NullDelivery()).run(JOB_FEEDBACK)
        runs = await bare.ctx.ledger.last_runs()
        errors = await bare.ctx.ledger.recent_errors()

    assert not runs[JOB_FEEDBACK].ok
    assert any("database is locked" in message for _, _, message in errors)
