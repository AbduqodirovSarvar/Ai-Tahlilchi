"""Post-mortems of lost signals (F8.3): idempotent, budget aware, never in settlement's way."""

from dataclasses import replace
from datetime import timedelta

import pytest

from app.db.enums import LlmVerdict, Outcome, PostmortemCategory, ResultSource, SignalSource
from app.db.repo.feedback import list_postmortems
from app.domain import Selection
from app.llm.schemas import PostmortemCategory as LlmCategory
from app.pipeline.config import PipelineConfig
from app.pipeline.postmortem import (
    MAX_SILENT_CALLS,
    MAX_STORED_REASON_CHARS,
    build_case,
    run_postmortems,
    tags_of,
)
from app.pipeline.review import NoPostmortem
from tests.integration.t4_support import Bare
from tests.pipeline.doubles import Crash, ScriptedAnalyst, verdict
from tests.pipeline.feedback_world import (
    KICKOFF,
    NOW,
    OTHER,
    SETTLED,
    USER,
    finished_fixture,
    open_world,
    place,
    settle,
)

LONG_AGO = NOW - timedelta(days=400)


async def stored(bare: Bare) -> list:  # type: ignore[type-arg]
    async with bare.sessions() as session:
        return await list_postmortems(session, since=LONG_AGO)


async def test_a_lost_signal_is_explained_and_stored_with_its_tags(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture = finished_fixture("A")
    analyst = ScriptedAnalyst(
        default=verdict(LlmCategory.MISSING_INFO, "Asosiy hujumchi yo'q edi.")
    )
    async with open_world(tmp_path / "pm.db", (fixture, 0, 2), analyst=analyst) as bare:
        signal = await settle(bare, await place(bare, fixture))

        report = await run_postmortems(bare.ctx)
        [postmortem] = await stored(bare)

    assert (report.explained, report.copied, report.skipped) == (1, 0, 0)
    assert postmortem.signal_id == signal.id
    assert postmortem.category is PostmortemCategory.MISSING_INFO
    assert postmortem.reason == "Asosiy hujumchi yo'q edi."
    assert postmortem.created_at == NOW
    home, away, league = (
        signal.fixture.home_team_id,
        signal.fixture.away_team_id,
        signal.fixture.league_id,
    )
    assert postmortem.tags == (f"team:{home}", f"team:{away}", f"league:{league}", "market:1x2")
    assert tags_of(signal) == postmortem.tags


async def test_the_case_carries_what_was_known_and_what_happened(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture = finished_fixture("B")
    analyst = ScriptedAnalyst(default=verdict())
    async with open_world(tmp_path / "case.db", (fixture, 0, 2), analyst=analyst) as bare:
        await settle(
            bare,
            await place(bare, fixture, verdict=LlmVerdict.CAUTION, note="Tarkib noaniq.", odds=2.1),
        )

        await run_postmortems(bare.ctx)

    [case] = analyst.cases
    candidate = case.candidate
    assert (candidate.p_model, candidate.odds, candidate.ev) == (0.55, 2.1, 0.1)
    assert (candidate.market, candidate.selection, candidate.line) == ("1x2", "home", None)
    assert (candidate.home_team, candidate.away_team) == ("B-H", "B-A")
    assert candidate.confidence_level == "medium"
    assert case.final_score == "0-2"
    assert case.original_verdict == "ehtiyot"
    assert case.original_note == "Tarkib noaniq."
    assert case.xg_summary is None


async def test_expected_goals_and_form_reach_the_case(tmp_path) -> None:  # type: ignore[no-untyped-def]
    earlier = finished_fixture("OLD", KICKOFF - timedelta(days=3))
    fixture = finished_fixture("C")
    async with open_world(
        tmp_path / "xg.db", (earlier, 1, 0), (fixture, 0, 2), analyst=ScriptedAnalyst()
    ) as bare:
        signal = await settle(bare, await place(bare, fixture))
        with_xg = replace(signal, fixture=replace(signal.fixture, home_xg=1.234, away_xg=0.5))

        case = await build_case(bare.ctx, with_xg)

    assert case.xg_summary == "xG 1.23-0.50"
    assert case.candidate.forms == ()  # "OLD" is another pairing: neither team played before


async def test_the_original_note_is_cut_to_the_input_limit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture = finished_fixture("D")
    async with open_world(tmp_path / "note.db", (fixture, 0, 2)) as bare:
        signal = await settle(bare, await place(bare, fixture, note="n" * 400))

        case = await build_case(bare.ctx, signal)

    assert case.original_note == "n" * 240


async def test_a_second_run_explains_nothing_again(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture = finished_fixture("E")
    analyst = ScriptedAnalyst(default=verdict())
    async with open_world(tmp_path / "idem.db", (fixture, 0, 2), analyst=analyst) as bare:
        await settle(bare, await place(bare, fixture))

        await run_postmortems(bare.ctx)
        again = await run_postmortems(bare.ctx)
        rows = await stored(bare)

    assert len(analyst.cases) == 1
    assert (again.explained, again.copied, again.skipped) == (0, 0, 0)
    assert len(rows) == 1


async def test_only_recommended_non_retro_full_losses_are_explained(tmp_path) -> None:  # type: ignore[no-untyped-def]
    names = "FGHIJK"
    fixtures = [finished_fixture(n) for n in names]
    analyst = ScriptedAnalyst(default=verdict())
    async with open_world(
        tmp_path / "filter.db", *((f, 0, 2) for f in fixtures), analyst=analyst
    ) as bare:
        lost = await settle(bare, await place(bare, fixtures[0]))
        await settle(bare, await place(bare, fixtures[1]), Outcome.WIN)
        await settle(bare, await place(bare, fixtures[2]), Outcome.VOID)
        await settle(bare, await place(bare, fixtures[3]), Outcome.HALF_LOSS)
        await settle(
            bare,
            await place(bare, fixtures[4], recommended=False, source=SignalSource.RETRO),
        )
        await settle(bare, await place(bare, fixtures[5], recommended=False))
        await place(bare, fixtures[0], user_id=OTHER, selection=Selection.AWAY)  # unsettled

        report = await run_postmortems(bare.ctx)
        [only] = await stored(bare)

    assert only.signal_id == lost.id
    assert report.explained == 1
    assert len(analyst.cases) == 1


async def test_a_manual_loss_counts_like_an_automatic_one(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture = finished_fixture("M")
    async with open_world(
        tmp_path / "manual.db", (fixture, 0, 2), analyst=ScriptedAnalyst(default=verdict())
    ) as bare:
        await settle(bare, await place(bare, fixture), source=ResultSource.MANUAL)

        report = await run_postmortems(bare.ctx)

    assert report.explained == 1


async def test_without_an_llm_nothing_is_stored_and_the_loss_is_explained_later(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture = finished_fixture("N")
    async with open_world(tmp_path / "off.db", (fixture, 0, 2)) as bare:
        assert isinstance(bare.ctx.postmortem, NoPostmortem)
        await settle(bare, await place(bare, fixture))

        silent = await run_postmortems(bare.ctx)
        assert await stored(bare) == []
        bare.ctx = replace(bare.ctx, postmortem=ScriptedAnalyst(default=verdict()))
        later = await run_postmortems(bare.ctx)
        rows = await stored(bare)

    assert (silent.explained, silent.skipped) == (0, 1)
    assert later.explained == 1
    assert len(rows) == 1


async def test_the_same_pick_of_two_users_is_explained_once_and_stored_twice(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture = finished_fixture("P")
    analyst = ScriptedAnalyst(default=verdict(LlmCategory.MODEL_ERROR, "Model xato baholagan."))
    async with open_world(tmp_path / "twins.db", (fixture, 0, 2), analyst=analyst) as bare:
        mine = await settle(bare, await place(bare, fixture, user_id=USER))
        theirs = await settle(bare, await place(bare, fixture, user_id=OTHER))

        report = await run_postmortems(bare.ctx)
        rows = await stored(bare)

    assert len(analyst.cases) == 1
    assert report.explained == 2
    assert {row.signal_id for row in rows} == {mine.id, theirs.id}
    assert {row.fixture_id for row in rows} == {mine.fixture.id}  # one fixture: one confirmation
    assert {row.category for row in rows} == {PostmortemCategory.MODEL_ERROR}


async def test_a_late_twin_gets_the_stored_explanation_without_a_new_call(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture = finished_fixture("Q")
    analyst = ScriptedAnalyst(default=verdict(LlmCategory.BAD_LUCK, "Omadsizlik."))
    async with open_world(tmp_path / "late.db", (fixture, 0, 2), analyst=analyst) as bare:
        await settle(bare, await place(bare, fixture, user_id=USER))
        await run_postmortems(bare.ctx)
        late = await settle(bare, await place(bare, fixture, user_id=OTHER))

        report = await run_postmortems(bare.ctx)
        rows = await stored(bare)

    assert len(analyst.cases) == 1
    assert (report.explained, report.copied) == (0, 1)
    assert {row.signal_id for row in rows} >= {late.id}
    assert len(rows) == 2


async def test_at_most_n_calls_per_run_newest_losses_first(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixtures = [finished_fixture(n) for n in "RST"]
    analyst = ScriptedAnalyst(default=verdict())
    config = PipelineConfig(postmortems_per_run=2)
    async with open_world(
        tmp_path / "budget.db", *((f, 0, 2) for f in fixtures), analyst=analyst
    ) as bare:
        bare.ctx = replace(bare.ctx, config=config)
        signals = [
            await settle(bare, await place(bare, f), at=SETTLED + timedelta(hours=i))
            for i, f in enumerate(fixtures)
        ]

        first = await run_postmortems(bare.ctx)
        explained_first = {row.signal_id for row in await stored(bare)}
        second = await run_postmortems(bare.ctx)
        explained_all = {row.signal_id for row in await stored(bare)}

    assert (first.explained, first.skipped) == (2, 1)
    assert explained_first == {signals[2].id, signals[1].id}  # the newest two
    assert second.explained == 1
    assert explained_all == {s.id for s in signals}
    assert len(analyst.cases) == 3


async def test_a_run_stops_asking_after_repeated_silence(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixtures = [finished_fixture(f"Z{n}") for n in range(5)]
    analyst = ScriptedAnalyst(default=None)
    async with open_world(
        tmp_path / "silent.db", *((f, 0, 2) for f in fixtures), analyst=analyst
    ) as bare:
        for fixture in fixtures:
            await settle(bare, await place(bare, fixture))

        report = await run_postmortems(bare.ctx)

    assert len(analyst.cases) == MAX_SILENT_CALLS
    assert (report.explained, report.skipped) == (0, 5)


async def test_losses_older_than_the_lookback_are_left_alone(tmp_path) -> None:  # type: ignore[no-untyped-def]
    inside, outside = finished_fixture("IN"), finished_fixture("OUT")
    analyst = ScriptedAnalyst(default=verdict())
    config = PipelineConfig()
    async with open_world(
        tmp_path / "old.db", (inside, 0, 2), (outside, 0, 2), analyst=analyst
    ) as bare:
        edge = NOW - config.postmortem_lookback  # exactly at the boundary: still included
        kept = await settle(bare, await place(bare, inside), at=edge)
        await settle(bare, await place(bare, outside), at=edge - timedelta(seconds=1))

        await run_postmortems(bare.ctx)
        rows = await stored(bare)

    assert [row.signal_id for row in rows] == [kept.id]


async def test_a_crash_between_two_explanations_loses_nothing_and_repeats_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    first_fixture, second_fixture = finished_fixture("U1"), finished_fixture("U2")
    analyst = ScriptedAnalyst(script=[verdict(), Crash("killed")], default=verdict())
    async with open_world(
        tmp_path / "crash.db", (first_fixture, 0, 2), (second_fixture, 0, 2), analyst=analyst
    ) as bare:
        older = await settle(bare, await place(bare, first_fixture), at=SETTLED)
        newer = await settle(
            bare, await place(bare, second_fixture), at=SETTLED + timedelta(hours=1)
        )

        with pytest.raises(Crash):
            await run_postmortems(bare.ctx)
        after_crash = {row.signal_id for row in await stored(bare)}
        rerun = await run_postmortems(bare.ctx)
        final = [row.signal_id for row in await stored(bare)]

    assert after_crash == {newer.id}  # newest first: it was stored before the kill
    assert rerun.explained == 1
    assert sorted(final) == sorted([older.id, newer.id])  # no duplicate rows
    assert len(analyst.cases) == 3  # the interrupted pick was asked about again, nothing else


async def test_a_case_that_cannot_be_built_is_counted_not_asked(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture = finished_fixture("BIG")
    analyst = ScriptedAnalyst(default=verdict())
    async with open_world(tmp_path / "big.db", (fixture, 100, 0), analyst=analyst) as bare:
        await settle(bare, await place(bare, fixture))

        report = await run_postmortems(bare.ctx)

    assert report.invalid == 1
    assert analyst.cases == []


async def test_a_guarded_empty_reason_is_stored_empty_and_a_long_one_is_cut(tmp_path) -> None:  # type: ignore[no-untyped-def]
    short, long = finished_fixture("S1"), finished_fixture("L1")
    analyst = ScriptedAnalyst(script=[verdict(reason=""), verdict(reason="x" * 500)])
    async with open_world(
        tmp_path / "reason.db", (short, 0, 2), (long, 0, 2), analyst=analyst
    ) as bare:
        first = await settle(bare, await place(bare, short), at=SETTLED)
        second = await settle(bare, await place(bare, long), at=SETTLED + timedelta(hours=1))

        await run_postmortems(bare.ctx)
        rows = {row.signal_id: row for row in await stored(bare)}

    assert rows[second.id].reason == ""  # newest first: the first answer belongs to it
    assert len(rows[first.id].reason) == MAX_STORED_REASON_CHARS


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (LlmCategory.BAD_LUCK, PostmortemCategory.BAD_LUCK),
        (LlmCategory.MISSING_INFO, PostmortemCategory.MISSING_INFO),
        (LlmCategory.MODEL_ERROR, PostmortemCategory.MODEL_ERROR),
    ],
)
async def test_every_llm_category_maps_to_a_stored_one(  # type: ignore[no-untyped-def]
    tmp_path, answer: LlmCategory, expected: PostmortemCategory
) -> None:
    fixture = finished_fixture("CAT")
    async with open_world(
        tmp_path / "cat.db", (fixture, 0, 2), analyst=ScriptedAnalyst(default=verdict(answer))
    ) as bare:
        await settle(bare, await place(bare, fixture))

        await run_postmortems(bare.ctx)
        [row] = await stored(bare)

    assert row.category is expected


async def test_the_run_is_recorded_for_the_status_page(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture = finished_fixture("LOG")
    async with open_world(
        tmp_path / "run.db", (fixture, 0, 2), analyst=ScriptedAnalyst(default=verdict())
    ) as bare:
        await settle(bare, await place(bare, fixture))

        await run_postmortems(bare.ctx)
        runs = await bare.ctx.ledger.last_runs()

    assert runs["postmortem"].ok
    assert "explained=1" in (runs["postmortem"].detail or "")
