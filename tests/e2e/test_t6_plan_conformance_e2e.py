"""T6: claims of the main plan (section 6) checked against the REAL services, not the fake.

Each test names the plan clause it verifies. The week harness (real pipeline, scripted API, real
Telegram notifier) is used where the claim depends on data; they complement the T5 end-to-end
suites by running the same claims over the production pipeline.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from aiogram.methods import EditMessageText, SendMessage

from app.bot import texts
from app.bot.formatters import format_signal_card
from app.scheduler import JOB_REFRESH
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    WatchKind,
    WatchTarget,
)
from tests.e2e.bot_client import telegram_html_problems
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


async def test_6_2_the_future_is_limited_to_the_horizon(week: Week) -> None:
    client = week.client(USER)
    before = len(week.provider.calls)
    reply = shown(await client.send("/tahlil 20.11-25.11"))
    assert "Oraliq juda uzoqda" in reply
    assert "kun keyingi o'yinlar" in reply
    assert len(week.provider.calls) == before  # nothing was fetched for a refused request


async def test_6_2_matches_more_than_three_days_away_are_marked_preliminary(week: Week) -> None:
    await week.settings(USER, **PERMISSIVE)
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


async def test_6_2_a_short_result_is_stated_not_padded(week: Week) -> None:
    await week.settings(USER, **PERMISSIVE)
    await jobs(week).run(JOB_REFRESH)
    client = week.client(USER)
    await client.send("/tahlil 1kun 30% ev2% 30ta")
    await client.press(texts.BTN_START)
    everything = "\n".join(client.texts.values())
    assert "so'ragansiz" in everything
    assert "30 ta" in everything
    cards = [t for t in client.texts.values() if "🎯 Tanlov" in t]
    assert 0 < len(cards) < 30


async def test_6_4_a_second_recalculation_inside_ten_minutes_is_refused_then_allowed(
    week: Week,
) -> None:
    await week.settings(USER, **PERMISSIVE)
    await jobs(week).run(JOB_REFRESH)
    await tick(week, 30)
    week.sync(USER)
    client = week.client(USER)
    message_id, data = client.find_button(texts.BTN_RECALC)
    first = shown(await client.press_data(data, message_id))
    assert "yangilandi" in first
    second = shown(await client.press_data(data, message_id))
    assert "daqiqadan keyin" in second  # cooldown, with the minutes left
    week.clock.advance(timedelta(minutes=10))
    third = shown(await client.press_data(data, message_id))
    assert "yangilandi" in third


async def test_6_6_the_high_probability_warning_is_shown_once(week: Week) -> None:
    client = week.client(USER)
    await client.send("/start")
    first = shown(await client.send("/tahlil 7kun 90% ev0%"))
    second = shown(await client.send("/tahlil 7kun 90% ev0%"))
    assert "yuqori ehtimollik chegarasi" in first or "yuqori ehtimollik" in first
    assert "yuqori ehtimollik chegarasi" not in second


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


async def test_6_10_the_report_has_a_calibration_table_and_the_export_two_files(
    week: Week,
) -> None:
    from tests.e2e.test_t6_week_e2e import day_one  # noqa: PLC0415

    day = await day_one(week)
    for signal in day.signals[:8]:
        await week.services.bets.place_bet(
            USER,
            __import__("app.services.interfaces", fromlist=["BetDraft"]).BetDraft(
                signal_id=signal.signal_id, stake=Decimal(10)
            ),
        )
    at(week, 22, 3, 0)
    await jobs(week).run("settle")
    client = week.client(USER)
    reply = await client.send("/hisobot")
    text = shown(reply)
    assert "Hisobot" in text
    for problem in telegram_html_problems(text):
        pytest.fail(problem)
    exported = await client.send("/eksport")
    documents = [m for m in exported if type(m).__name__ == "SendDocument"]
    assert len(documents) == 2  # signals.csv and bets.csv
