"""🔍 Maxsus tahlil through the real dispatcher over the fake services (F7.1)."""

import asyncio
from dataclasses import replace

import pytest

from app.bot import texts
from app.bot.callbacks import CustomCb
from app.bot.custom_ui import MAX_CUSTOM_FIXTURES, CustomDraft, CustomOption
from app.bot.draft import StaleDraftError
from app.bot.states import CustomFlow
from app.services.errors import BudgetExceededError
from app.services.interfaces import DateSpec, FixtureListRequest, FixtureOption, Services
from tests.bot.fakes import ADMIN_ID
from tests.e2e.bot_client import Client, answers, shown, telegram_html_problems
from tests.e2e.test_bot_concurrency_e2e import BlockedAnalyze


async def a_fixture(services: Services) -> FixtureOption:
    """A fixture of the next two weeks (the sample world has one per league and day)."""
    page = await services.catalog.list_fixtures(
        ADMIN_ID, FixtureListRequest(dates=DateSpec.next_days(14), page_size=20)
    )
    return page.items[0]


async def make_nothing_pass(services: Services) -> None:
    """Thresholds no sample selection can reach (so every pick is "no value")."""
    settings = await services.settings.get_settings(ADMIN_ID)
    await services.settings.update_settings(
        ADMIN_ID, replace(settings, min_probability=0.95, min_ev=0.2)
    )


# --------------------------------------------------------------------------- start
async def test_menu_opens_the_prompt_with_day_lists(admin: Client) -> None:
    methods = await admin.send(texts.MENU_CUSTOM)

    (prompt,) = shown(methods)
    assert prompt == texts.CUSTOM_PROMPT
    assert admin.has_button(texts.BTN_CUSTOM_TODAY)
    assert admin.has_button(texts.BTN_CUSTOM_TOMORROW)
    assert await admin.state() == CustomFlow.query.state


async def test_cancel_from_the_prompt_leaves_the_flow(admin: Client) -> None:
    await admin.send(texts.MENU_CUSTOM)

    methods = await admin.press(texts.BTN_CANCEL)

    assert shown(methods) == [texts.CANCELLED]
    assert await admin.state() is None


# --------------------------------------------------------------------------- team search
async def test_ambiguous_team_name_lists_candidates_then_its_three_fixtures(
    admin: Client,
) -> None:
    await admin.send(texts.MENU_CUSTOM)

    methods = await admin.send("Real")

    assert "Real" in shown(methods)[0]
    for team in ("Real Madrid", "Real Sociedad", "Real Betis"):
        assert admin.has_button(team)
    methods = await admin.press("Real Madrid")
    text = shown(methods)[0]
    assert "Real Madrid" in text
    assert "Tanlangan: 1" in text  # the nearest fixture is pre-checked
    toggles = [label for label, _ in admin.live_buttons(admin.last_message_id) if "—" in label]
    assert 1 <= len(toggles) <= 3  # the next three fixtures at most
    assert admin.has_button(texts.BTN_CUSTOM_RUN.format(count=1))


async def test_cyrillic_and_typos_find_the_team(admin: Client) -> None:
    await admin.send(texts.MENU_CUSTOM)

    methods = await admin.send("arsenl")

    assert "Arsenal" in shown(methods)[0]


async def test_unknown_team_explains_and_keeps_waiting(admin: Client) -> None:
    await admin.send(texts.MENU_CUSTOM)

    methods = await admin.send("Qwertyzz Xyzzy")

    assert "topilmadi" in shown(methods)[0]
    assert await admin.state() == CustomFlow.query.state


async def test_too_short_text_is_explained(admin: Client) -> None:
    await admin.send(texts.MENU_CUSTOM)

    methods = await admin.send("a")

    assert "kamida 2" in shown(methods)[0]


async def test_html_in_a_query_is_escaped(admin: Client) -> None:
    await admin.send(texts.MENU_CUSTOM)

    methods = await admin.send("<b>x&y</b>")

    (text,) = shown(methods)
    assert "&lt;b&gt;x&amp;y&lt;/b&gt;" in text
    assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- the analysis
async def test_selected_fixture_shows_every_market_with_probabilities(
    admin: Client, services: Services
) -> None:
    fixture = await a_fixture(services)
    await admin.send(texts.MENU_CUSTOM)
    await admin.send(fixture.home_team)
    if not admin.has_button(texts.BTN_CUSTOM_RUN.format(count=1)):
        await admin.press(fixture.home_team)
    methods = await admin.press(texts.BTN_CUSTOM_RUN.format(count=1))

    blocks = shown(methods)
    assert any(texts.CUSTOM_RESULT_HEADER.split("{")[0] in text for text in blocks)
    fixture_block = next(text for text in blocks if "EV" in text and "@" in text)
    assert fixture_block.count("EV") >= 4  # all markets, not only the best selection
    assert all(telegram_html_problems(text) == [] for text in blocks)
    assert await admin.state() is None  # the flow is over


async def test_a_fixture_without_value_is_flagged_and_gets_no_card(
    admin: Client, services: Services
) -> None:
    await make_nothing_pass(services)
    fixture = await a_fixture(services)
    await admin.send(texts.MENU_CUSTOM)
    await admin.send(f"{fixture.home_team} - {fixture.away_team}")
    await admin.press_option(f"{fixture.home_team} — {fixture.away_team}")
    methods = await admin.press(texts.BTN_CUSTOM_RUN.format(count=1))

    blocks = shown(methods)
    assert any(texts.CUSTOM_NO_VALUE_NOTE in text for text in blocks)
    assert not any(texts.CUSTOM_LEGEND in text for text in blocks)
    assert not any(texts.BTN_BET in label for label, _ in admin.live_buttons(admin.last_message_id))


async def test_value_selections_get_full_cards_with_buttons(
    admin: Client, services: Services
) -> None:
    settings = await services.settings.get_settings(ADMIN_ID)
    await services.settings.update_settings(
        ADMIN_ID, replace(settings, min_probability=0.30, min_ev=0.0, odds_min=1.05, odds_max=50.0)
    )
    fixtures = await services.catalog.list_fixtures(
        ADMIN_ID, FixtureListRequest(dates=DateSpec.next_days(3), page_size=20)
    )
    await admin.send(texts.MENU_CUSTOM)
    await admin.press(texts.BTN_CUSTOM_TODAY)
    for label, _ in admin.live_buttons(admin.last_message_id)[:3]:
        await admin.press_option(label.removeprefix("▫️ "))
    methods = await admin.press("Tahlil qilish")

    blocks = shown(methods)
    assert fixtures.items
    assert any(texts.CUSTOM_LEGEND in text for text in blocks)
    assert admin.has_button(texts.BTN_BET)  # the card of a recommended selection


async def test_nothing_checked_cannot_run(admin: Client) -> None:
    await admin.send(texts.MENU_CUSTOM)
    await admin.press(texts.BTN_CUSTOM_TODAY)
    message_id = admin.last_message_id

    methods = await admin.press_data(CustomCb(action="run").pack(), message_id)

    assert answers(methods)[0].text == texts.CUSTOM_SELECT_ONE


async def test_selection_is_limited(admin: Client) -> None:
    await admin.send(texts.MENU_CUSTOM)
    await admin.press(texts.BTN_CUSTOM_TODAY)
    message_id = admin.last_message_id
    key = admin.storage_key()
    storage = admin.dispatcher.storage
    data = await storage.get_data(key)
    options = tuple(CustomOption(f"f{i}", f"opt {i}", "football", f"opt {i}") for i in range(12))
    draft = CustomDraft(
        message_id=message_id,
        kind="fixtures",
        options=options,
        selected=tuple(o.option_id for o in options[:MAX_CUSTOM_FIXTURES]),
    )
    await storage.set_data(key, {**data, "custom": draft.to_data()})

    methods = await admin.press_data(CustomCb(action="toggle", index=11).pack(), message_id)

    assert answers(methods)[0].text == texts.CUSTOM_LIMIT.format(max=MAX_CUSTOM_FIXTURES)
    assert answers(methods)[0].show_alert is True


# --------------------------------------------------------------------------- failures
async def test_failed_analysis_explains_and_lets_the_user_retry(
    admin: Client, services: Services
) -> None:
    fixture = await a_fixture(services)
    await admin.send(texts.MENU_CUSTOM)
    await admin.send(f"{fixture.home_team} - {fixture.away_team}")
    await admin.press_option(f"{fixture.home_team} — {fixture.away_team}")
    blocked = BlockedAnalyze(services, failure=BudgetExceededError(needed=9, available=2))
    blocked.release.set()

    methods = await admin.press(texts.BTN_CUSTOM_RUN.format(count=1))

    assert any("API limiti" in text for text in shown(methods))
    assert await admin.state() == CustomFlow.pick.state
    assert admin.has_button(texts.BTN_CUSTOM_RUN.format(count=1))


async def test_a_second_tap_while_running_is_refused(admin: Client, services: Services) -> None:
    fixture = await a_fixture(services)
    await admin.send(texts.MENU_CUSTOM)
    await admin.send(f"{fixture.home_team} - {fixture.away_team}")
    await admin.press_option(f"{fixture.home_team} — {fixture.away_team}")
    blocked = BlockedAnalyze(services)
    message_id, data = admin.find_button(texts.BTN_CUSTOM_RUN.format(count=1))

    first = asyncio.create_task(admin.press_data(data, message_id))
    await asyncio.wait_for(blocked.started.wait(), timeout=5)
    second = await admin.press_data(data, message_id)
    blocked.release.set()
    await first

    assert answers(second)[0].text == texts.CUSTOM_ALREADY_RUNNING
    assert blocked.calls == 1


# --------------------------------------------------------------------------- stale buttons
async def test_buttons_of_an_older_screen_are_stale(admin: Client) -> None:
    await admin.send(texts.MENU_CUSTOM)
    old_id = admin.last_message_id
    await admin.send("Real")

    methods = await admin.press_data(CustomCb(action="today").pack(), old_id)

    assert "Bu tugma eskirgan" in (answers(methods)[0].text or "")
    assert answers(methods)[0].show_alert is True


async def test_buttons_after_a_restart_are_stale(admin: Client) -> None:
    await admin.send(texts.MENU_CUSTOM)
    message_id, data = admin.find_button(texts.BTN_CUSTOM_TODAY)
    await admin.dispatcher.storage.set_data(admin.storage_key(), {})
    await admin.dispatcher.storage.set_state(admin.storage_key(), None)

    methods = await admin.press_data(data, message_id)

    assert answers(methods)[0].text == texts.STALE_BUTTON


def test_a_corrupt_draft_is_stale() -> None:
    with pytest.raises(StaleDraftError):
        CustomDraft.from_data({"kind": "teams"})
    with pytest.raises(StaleDraftError):
        CustomDraft.from_data("nope")


# --------------------------------------------------------------------------- /tahlil and plain text
async def test_tahlil_with_two_team_names_is_a_custom_analysis(
    admin: Client, services: Services
) -> None:
    fixture = await a_fixture(services)

    methods = await admin.send(f"/tahlil {fixture.home_team} - {fixture.away_team}")

    text = shown(methods)[0]
    assert fixture.home_team in text
    assert "Tanlangan: 0" in text
    assert await admin.state() == CustomFlow.pick.state


async def test_tahlil_with_dates_still_opens_the_confirmation(admin: Client) -> None:
    methods = await admin.send("/tahlil 22.10 - 24.10 futbol")

    assert (
        texts.BTN_START in " ".join(label for label, _ in admin.live_buttons(admin.last_message_id))
        or "So'rov" in shown(methods)[-1]
    )


async def test_plain_text_with_two_teams_starts_a_search(admin: Client, services: Services) -> None:
    fixture = await a_fixture(services)

    methods = await admin.send(f"{fixture.home_team} {fixture.away_team}")

    assert fixture.home_team in shown(methods)[0]
    assert admin.has_button(texts.BTN_CUSTOM_AGAIN)


async def test_plain_chatter_still_gets_the_hint(admin: Client) -> None:
    assert shown(await admin.send("salom")) == [texts.UNKNOWN_TEXT]
    assert shown(await admin.send("x")) == [texts.UNKNOWN_TEXT]


async def test_other_users_flows_are_independent(admin: Client) -> None:
    from tests.e2e.conftest import SECOND_ADMIN_ID  # noqa: PLC0415

    other = Client(admin.dispatcher, admin.bot, admin.session, SECOND_ADMIN_ID)
    await admin.send(texts.MENU_CUSTOM)
    await other.send(texts.MENU_CUSTOM)
    await admin.send("Real")

    assert await other.state() == CustomFlow.query.state
    assert await admin.state() == CustomFlow.pick.state
