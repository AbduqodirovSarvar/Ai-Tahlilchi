"""T6: claims of the main plan (section 6) checked against the REAL services, not the fake.

Each test names the plan clause it verifies. The week harness (real pipeline, scripted API, real
Telegram notifier) is used where the claim depends on data; they complement the T5 end-to-end
suites by running the same claims over the production pipeline.
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from aiogram.methods import EditMessageText, SendMessage

from app.bot import texts
from app.bot.formatters import format_signal_card
from app.pipeline.refresh import refresh
from app.scheduler import JOB_REFRESH
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    WatchKind,
    WatchTarget,
)
from tests.e2e.t6_world import PERMISSIVE, USER, Week, at, jobs, open_week, tick

pytestmark = pytest.mark.slow

STRANGER = 777_000


@pytest.fixture
async def week(history_template, tmp_path, bot, session):  # type: ignore[no-untyped-def]
    async with open_week(history_template, tmp_path, bot, session) as opened:
        yield opened


def shown(methods: list) -> str:  # type: ignore[type-arg]
    return "\n".join(m.text for m in methods if isinstance(m, SendMessage | EditMessageText))


async def test_6_5_a_new_user_gets_the_balanced_defaults_of_the_plan(week: Week) -> None:
    got = await week.services.settings.get_settings(555)
    assert (got.min_probability, got.min_ev) == (0.45, 0.05)  # decision #8: Balanslangan
    assert (got.odds_min, got.odds_max) == (1.50, 3.00)
    assert got.max_signals == 10
    assert got.send_time.strftime("%H:%M") == "09:00"
    assert got.prematch_minutes == 60
    assert got.quiet_hours is not None
    assert got.quiet_hours.start.strftime("%H:%M") == "00:00"
    assert got.quiet_hours.end.strftime("%H:%M") == "08:00"
    assert got.timezone == "Asia/Tashkent"
    assert got.bank is None
    assert got.unit is None


async def refresh_a_week_ahead(week: Week) -> None:
    """Analyses read the database: a refresh reaching a week ahead is what puts far matches in."""
    wide = replace(
        week.ctx, config=replace(week.ctx.config, refresh_days_ahead=7, odds_days_ahead=7)
    )
    await refresh(wide, force=True)


async def test_6_2_matches_more_than_three_days_away_are_marked_preliminary(week: Week) -> None:
    await week.settings(USER, **PERMISSIVE)
    await refresh_a_week_ahead(week)
    result = await week.services.analysis.analyze(
        USER,
        AnalysisQuery(
            dates=DateSpec.next_days(7), min_probability=0.30, min_ev=0.0, max_signals=30
        ),
    )
    soon = [s for s in result.signals if s.kickoff - week.clock() <= timedelta(days=3)]
    far = [s for s in result.signals if s.kickoff - week.clock() > timedelta(days=3)]
    assert soon
    assert far, "the scripted API serves a second matchday six days out"
    assert not any(s.preliminary for s in soon)
    assert all(s.preliminary for s in far)
    # and the card says so
    zone = (await week.services.settings.get_settings(USER)).zone
    assert all(texts.CARD_PRELIMINARY in format_signal_card(s, zone) for s in far)
    assert not any(texts.CARD_PRELIMINARY in format_signal_card(s, zone) for s in soon)


async def test_6_7_a_followed_team_is_always_in_the_digest_even_without_value(
    week: Week,
) -> None:
    await week.settings(USER, **{**PERMISSIVE, "min_probability": 0.95})  # nothing is 95% likely
    await jobs(week).run(JOB_REFRESH)
    catalog = week.services.catalog
    teams = await catalog.search_teams("Arsenal")
    assert teams
    await week.services.watchlist.add_watch(
        USER, WatchTarget(kind=WatchKind.TEAM, entity_id=teams[0].team_id)
    )
    await tick(week, 30)
    text = "\n".join(m.text for m in week.pushed(USER))
    assert "Arsenal" in text, "the followed team's match must be listed although it has no value"
    assert "⭐" in text


async def test_6_13_a_users_note_reaches_the_next_language_model_prompt(week: Week) -> None:
    await week.settings(USER, **PERMISSIVE)
    await jobs(week).run(JOB_REFRESH)
    await tick(week, 30)
    signals = await week.services.signals.list_signals(USER, DateSpec.next_days(3))
    target = signals[0]
    note = "tarkibda asosiy hujumchi yo'q edi"
    await week.services.results.add_note(USER, target.signal_id, note)
    # a later review that involves the same teams sees the note as context
    at(week, 22, 3, 0)
    week.provider.exhausted = False
    await refresh_a_week_ahead(week)  # the next morning's refresh fills the database
    result = await week.services.analysis.analyze(
        USER,
        AnalysisQuery(
            dates=DateSpec.next_days(7), min_probability=0.30, min_ev=0.0, max_signals=30
        ),
    )
    assert result.signals
    prompts = "\n".join(week.gemini.prompts)
    assert note in prompts


async def test_6_12_a_stranger_gets_no_answer_at_all(week: Week) -> None:
    from tests.e2e.bot_client import Client  # noqa: PLC0415

    before = len(week.session.requests)
    stranger = Client(week.dispatcher, week.bot, week.session, STRANGER)
    for entry in ("/start", "/holat", "/bank", texts.MENU_ANALYSIS, "hello"):
        await stranger.send(entry)
    assert len(week.session.requests) == before
