"""QA findings pinned as strict xfails (see docs/qa/T3-bot.md); fixing one turns it XPASS."""

from typing import Any

import pytest

from app.bot import texts
from app.bot.wizard import MAX_TEAM_NAMES_PER_MESSAGE
from app.domain import Sport
from app.services.interfaces import MAX_SCOPE_IDS, AnalysisQuery, AnalysisResult, Services
from tests.e2e.bot_client import Client, answers, shown
from tests.e2e.flows import FOOTBALL


class ResultSpy:
    def __init__(self, services: Services) -> None:
        self.results: list[AnalysisResult] = []
        self._real = services.analysis.analyze
        services.analysis.analyze = self  # type: ignore[method-assign]

    async def __call__(self, user_id: int, query: AnalysisQuery, **kwargs: Any) -> AnalysisResult:
        result = await self._real(user_id, query, **kwargs)
        self.results.append(result)
        return result


@pytest.mark.xfail(
    strict=True,
    reason="F-5: ➡️ Yana carries only the page number; after a newer analysis an old "
    "button pages through the *newer* result set",
)
async def test_old_more_button_does_not_page_through_a_newer_result(
    admin: Client, services: Services
) -> None:
    spy = ResultSpy(services)
    await admin.send("/tahlil 7kun futbol 30% ev0% 100ta")
    await admin.press(texts.BTN_START)
    old_more = admin.find_button("Yana")
    await admin.send("/tahlil 7kun xokkey 30% ev0% 100ta")
    await admin.press(texts.BTN_START)
    assert len(spy.results[0].signals) > 5
    assert len(spy.results[1].signals) > 5

    methods = await admin.press_data(old_more[1], old_more[0])

    stale = answers(methods)[0].text == texts.STALE_BUTTON
    cards = [t for t in shown(methods) if t.startswith(("⚽", "🏒"))]
    # Either the old button is refused, or it continues the *old* (football) result.
    assert stale or (cards and all(card.startswith("⚽") for card in cards))


async def test_reversed_typed_range_says_end_before_start(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)

    methods = await admin.send("25.10 - 24.10")

    assert shown(methods) == [texts.PARSE_REVERSED_RANGE]


async def test_typed_fractional_percent_is_shown_as_typed(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    await admin.press(texts.BTN_TODAY)
    await admin.press(FOOTBALL)
    await admin.press(texts.BTN_CUSTOM)
    await admin.send("62,5")
    await admin.press(texts.BTN_CUSTOM)
    await admin.send("4,5")
    for label in (texts.BTN_SCOPE_ALL, texts.BTN_ALL, texts.BTN_DONE, "EV bo'yicha"):
        await admin.press(label)

    assert "≥ 62.5% · EV ≥ 4.5%" in admin.last_text


async def test_team_step_bounds_catalog_searches(admin: Client, services: Services) -> None:
    calls = 0
    real = services.catalog.search_teams

    async def counting(text: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return await real(text, **kwargs)

    services.catalog.search_teams = counting  # type: ignore[method-assign]
    await admin.send(texts.MENU_ANALYSIS)
    for label in (texts.BTN_TODAY, texts.SPORT_LABELS[Sport.FOOTBALL], "50%", "5%"):
        await admin.press(label)
    await admin.press(texts.BTN_SCOPE_TEAMS)

    await admin.send(", ".join(f"Team{index:03d}" for index in range(300)))

    assert calls <= MAX_SCOPE_IDS


async def test_team_step_rejects_a_message_with_too_many_names(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    for label in (texts.BTN_TODAY, texts.SPORT_LABELS[Sport.FOOTBALL], "50%", "5%"):
        await admin.press(label)
    await admin.press(texts.BTN_SCOPE_TEAMS)

    methods = await admin.send(", ".join(f"Team{index:03d}" for index in range(300)))

    assert shown(methods) == [
        texts.WIZARD_TEAMS_TOO_MANY_NAMES.format(max_names=MAX_TEAM_NAMES_PER_MESSAGE)
    ]
    assert await admin.state() == "AnalysisFlow:teams"


async def test_team_step_searches_each_distinct_name_once(
    admin: Client, services: Services
) -> None:
    searched: list[str] = []
    real = services.catalog.search_teams

    async def recording(text: str, **kwargs: Any) -> Any:
        searched.append(text)
        return await real(text, **kwargs)

    services.catalog.search_teams = recording  # type: ignore[method-assign]
    await admin.send(texts.MENU_ANALYSIS)
    for label in (texts.BTN_TODAY, texts.SPORT_LABELS[Sport.FOOTBALL], "50%", "5%"):
        await admin.press(label)
    await admin.press(texts.BTN_SCOPE_TEAMS)

    await admin.send("Arsenal, arsenal ,  ARSENAL, Inter,, Inter")

    assert searched == ["Arsenal", "Inter"]
