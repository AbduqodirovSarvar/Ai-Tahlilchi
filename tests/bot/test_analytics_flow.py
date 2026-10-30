"""The analytics bot end to end: menu -> match list -> analysis -> refresh / back."""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.bot import texts
from app.bot.callbacks import GameCb
from app.services.errors import NotFoundError, ServiceUnavailableError
from tests.bot.fakes import BotHarness, answers, sent_texts

TODAY = date(2026, 10, 21)  # fixed_clock, Tashkent


def buttons(harness: BotHarness, message_id: int) -> list[str]:
    markup = harness.markups[message_id]
    assert markup is not None
    return [button.text for row in markup.inline_keyboard for button in row]


async def test_today_lists_one_button_per_match(harness: BotHarness) -> None:
    [text] = sent_texts(await harness.send(texts.MENU_GAMES_TODAY))
    assert text.startswith("📅 <b>21.10.2026, Chorshanba</b> — 2 ta o'yin")
    message_id, _ = harness.find_button("Arsenal FC vs Chelsea FC")
    assert buttons(harness, message_id) == [
        "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Arsenal FC vs Chelsea FC | 19:00",
        "🇪🇸 Real Madrid CF vs FC Barcelona | 21:30",
    ]


async def test_tomorrow_shows_the_next_day(harness: BotHarness) -> None:
    [text] = sent_texts(await harness.send(texts.MENU_GAMES_TOMORROW))
    assert "22.10.2026" in text


async def test_a_match_opens_its_analysis_with_two_buttons(harness: BotHarness) -> None:
    await harness.send(texts.MENU_GAMES_TODAY)
    methods = await harness.press("Arsenal FC vs Chelsea FC")
    progress, analysis = sent_texts(methods)
    assert progress == texts.ANALYSIS_PROGRESS
    assert analysis.startswith("⚽ <b>Arsenal FC — Chelsea FC</b>")
    assert texts.ANALYSIS_SOURCE_AI in analysis
    message_id, _ = harness.find_button(texts.BTN_REFRESH)
    assert buttons(harness, message_id) == [texts.BTN_REFRESH, texts.BTN_BACK_TO_LIST]

    refreshed = sent_texts(await harness.press(texts.BTN_REFRESH))
    assert refreshed[-1].startswith("⚽ <b>Arsenal FC — Chelsea FC</b>")

    back = sent_texts(await harness.press(texts.BTN_BACK_TO_LIST))
    assert back == [harness.last_text]
    assert "2 ta o'yin" in back[0]


async def test_the_second_sample_is_worded_from_facts(harness: BotHarness) -> None:
    await harness.send(texts.MENU_GAMES_TODAY)
    await harness.press("Real Madrid CF")
    assert texts.ANALYSIS_SOURCE_FACTS in harness.last_text


async def test_top_leagues_lead_to_a_league_and_back(harness: BotHarness) -> None:
    [menu] = sent_texts(await harness.send(texts.MENU_TOP_LEAGUES))
    assert menu == texts.LEAGUES_TITLE.format(days=7)
    [league] = sent_texts(await harness.press("La Liga"))
    assert "Keyingi 7 kun: 7 ta o'yin" in league
    await harness.press("Real Madrid CF vs FC Barcelona | 21.10")
    assert harness.last_text.startswith("⚽ <b>Real Madrid CF — FC Barcelona</b>")
    [again] = sent_texts(await harness.press(texts.BTN_BACK_TO_LIST))
    assert again == league
    [leagues] = sent_texts(await harness.press(texts.BTN_BACK_TO_LEAGUES))
    assert leagues == menu


async def test_a_forged_league_or_day_does_not_break(harness: BotHarness) -> None:
    [menu] = sent_texts(await harness.send(texts.MENU_TOP_LEAGUES))
    message_id = max(harness.markups)
    methods = await harness.press_data(GameCb(action="league", c="ZZ").pack(), message_id)
    assert sent_texts(methods) == [menu]
    methods = await harness.press_data(GameCb(action="day", d=0).pack(), message_id)
    assert "21.10.2026" in sent_texts(methods)[0]


async def test_help_and_commands(harness: BotHarness) -> None:
    assert sent_texts(await harness.send(texts.MENU_ABOUT)) == [texts.ABOUT]
    [text] = sent_texts(await harness.send("/oyinlar"))
    assert "21.10.2026" in text


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NotFoundError("match", 1), "topilmadi"),
        (ServiceUnavailableError("football-data", "down"), "football-data"),
    ],
)
async def test_failures_reach_the_user(
    harness: BotHarness, error: Exception, expected: str
) -> None:
    await harness.send(texts.MENU_GAMES_TODAY)
    harness.services.matches.analyse_match = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    methods = await harness.press("Arsenal FC vs Chelsea FC")
    shown = " ".join(sent_texts(methods) + [a.text or "" for a in answers(methods)])
    assert expected in shown.lower() or expected in shown
