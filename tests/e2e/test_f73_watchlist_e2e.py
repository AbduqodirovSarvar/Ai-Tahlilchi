"""⭐ Sevimlilar through the real dispatcher over the fake services (F7.3)."""

import pytest

from app.bot import texts
from app.bot.callbacks import CardCb, WatchCb
from app.bot.draft import StaleDraftError
from app.bot.states import WatchFlow
from app.bot.watch_ui import WatchDraft, entity_key
from app.services.interfaces import (
    DateSpec,
    FixtureListRequest,
    Services,
    WatchKind,
    WatchNotification,
    WatchTarget,
)
from tests.bot.fakes import ADMIN_ID
from tests.e2e.bot_client import Client, answers, shown, telegram_html_problems
from tests.e2e.conftest import SECOND_ADMIN_ID


async def watched_names(services: Services, user_id: int = ADMIN_ID) -> list[str]:
    return [item.name for item in await services.watchlist.list_watchlist(user_id)]


# --------------------------------------------------------------------------- list
async def test_empty_watchlist_explains_what_to_do(admin: Client) -> None:
    methods = await admin.send(texts.MENU_WATCHLIST)

    (text,) = shown(methods)
    assert text == texts.WATCH_EMPTY
    assert "/kuzat Barcelona" in text
    assert admin.has_button(texts.BTN_WATCH_ADD_TEAM)
    assert admin.has_button(texts.BTN_WATCH_ADD_LEAGUE)


async def test_list_shows_entries_with_their_notifications(
    admin: Client, services: Services
) -> None:
    team = (await services.catalog.search_teams("Barcelona"))[0]
    target = WatchTarget(WatchKind.TEAM, team.team_id)
    await services.watchlist.add_watch(ADMIN_ID, target)
    await services.watchlist.set_notifications(
        ADMIN_ID, target, frozenset({WatchNotification.LINEUPS, WatchNotification.RESULT})
    )

    methods = await admin.send(texts.MENU_WATCHLIST)

    (text,) = shown(methods)
    assert "👕 <b>Barcelona</b> — 📋 Tarkib · 🏁 Natija" in text
    assert texts.WATCH_FOOTER in text
    assert admin.has_button("Barcelona")
    assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- /kuzat
async def test_kuzat_follows_a_clear_hit_immediately(admin: Client, services: Services) -> None:
    methods = await admin.send("/kuzat Barcelona")

    text = shown(methods)[0]
    assert "Barcelona</b> sevimlilarga qo'shildi" in text
    assert await watched_names(services) == ["Barcelona"]
    for flag in WatchNotification:
        assert admin.has_button(texts.WATCH_NOTIFY_LABELS[flag])


async def test_kuzat_offers_candidates_for_an_ambiguous_name(
    admin: Client, services: Services
) -> None:
    methods = await admin.send("/kuzat Real")

    assert "topildi" in shown(methods)[0]
    assert await watched_names(services) == []
    await admin.press("Real Betis")
    assert await watched_names(services) == ["Real Betis"]


async def test_kuzat_unknown_team_and_no_argument(admin: Client) -> None:
    unknown = await admin.send("/kuzat Qwertyzz")
    assert "topilmadi" in shown(unknown)[0]

    listing = await admin.send("/kuzat")
    assert shown(listing) == [texts.WATCH_EMPTY]


async def test_kuzat_twice_is_idempotent(admin: Client, services: Services) -> None:
    await admin.send("/kuzat Barcelona")
    await admin.send("/kuzat barcelona")

    assert await watched_names(services) == ["Barcelona"]


async def test_kuzat_with_html_is_escaped(admin: Client) -> None:
    methods = await admin.send("/kuzat <b>x&y")

    (text,) = shown(methods)
    assert "&lt;b&gt;x&amp;y" in text
    assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- buttons
async def test_add_a_team_by_typing_its_name(admin: Client, services: Services) -> None:
    await admin.send(texts.MENU_WATCHLIST)
    await admin.press(texts.BTN_WATCH_ADD_TEAM)
    assert await admin.state() == WatchFlow.team_query.state

    methods = await admin.send("Arsenal")

    assert "Arsenal</b> sevimlilarga qo'shildi" in shown(methods)[0]
    assert await watched_names(services) == ["Arsenal"]
    assert await admin.state() is None


async def test_add_a_league_from_the_list(admin: Client, services: Services) -> None:
    await admin.send(texts.MENU_WATCHLIST)
    await admin.press(texts.BTN_WATCH_ADD_LEAGUE)
    assert texts.WATCH_PICK_LEAGUE in admin.last_text

    await admin.press("Premier League")

    items = await services.watchlist.list_watchlist(ADMIN_ID)
    assert [(i.name, i.target.kind) for i in items] == [("Premier League", WatchKind.LEAGUE)]
    assert "🏆" in admin.last_text


async def test_toggling_notifications_is_saved_and_reversible(
    admin: Client, services: Services
) -> None:
    await admin.send("/kuzat Barcelona")
    label = texts.WATCH_NOTIFY_LABELS[WatchNotification.ODDS_MOVE]

    methods = await admin.press_option(label)
    (item,) = await services.watchlist.list_watchlist(ADMIN_ID)
    assert item.notifications == {WatchNotification.ODDS_MOVE}
    assert answers(methods)[0].text == texts.WATCH_STORED

    await admin.press_option(label)
    (item,) = await services.watchlist.list_watchlist(ADMIN_ID)
    assert item.notifications == frozenset()


async def test_remove_returns_to_the_list(admin: Client, services: Services) -> None:
    await admin.send("/kuzat Barcelona")

    methods = await admin.press(texts.BTN_WATCH_REMOVE)

    assert await watched_names(services) == []
    text = shown(methods)[0]
    assert "olib tashlandi" in text
    assert texts.WATCH_EMPTY in text


async def test_a_button_of_an_entry_removed_elsewhere_reports_it_gone(
    admin: Client, services: Services
) -> None:
    await admin.send("/kuzat Barcelona")
    message_id, data = admin.find_button(texts.BTN_WATCH_REMOVE)
    (item,) = await services.watchlist.list_watchlist(ADMIN_ID)
    await services.watchlist.remove_watch(ADMIN_ID, item.target)

    methods = await admin.press_data(data, message_id)

    assert answers(methods)[0].text == texts.WATCH_GONE
    assert answers(methods)[0].show_alert is True


async def test_a_key_never_hits_another_entry(admin: Client, services: Services) -> None:
    await admin.send("/kuzat Barcelona")
    await admin.send("/kuzat Arsenal")
    message_id, data = admin.find_button(texts.BTN_WATCH_REMOVE)
    removed = WatchCb.unpack(data)
    assert removed.key == entity_key((await services.catalog.search_teams("Arsenal"))[0].team_id)

    await admin.press_data(data, message_id)

    assert await watched_names(services) == ["Barcelona"]


async def test_stale_pick_buttons_are_refused(admin: Client, services: Services) -> None:
    await admin.send("/kuzat Real")
    old_id, data = admin.find_button("Real Betis")
    await admin.send("/kuzat Real")  # a newer pick list replaces the draft

    methods = await admin.press_data(data, old_id)

    assert "Bu tugma eskirgan" in (answers(methods)[0].text or "")
    assert await watched_names(services) == []


async def test_a_pick_button_after_a_restart_is_stale(admin: Client) -> None:
    await admin.send("/kuzat Real")
    message_id, data = admin.find_button("Real Betis")
    await admin.dispatcher.storage.set_data(admin.storage_key(), {})

    methods = await admin.press_data(data, message_id)

    assert "Bu tugma eskirgan" in (answers(methods)[0].text or "")


async def test_every_watch_button_fits_callback_data(admin: Client, services: Services) -> None:
    await admin.send("/kuzat Barcelona")
    await admin.send(texts.MENU_WATCHLIST)
    for message_id in admin.markups:
        for _, data in admin.live_buttons(message_id):
            assert len(data.encode()) <= 64


# --------------------------------------------------------------------------- isolation
async def test_each_user_has_their_own_watchlist(admin: Client, services: Services) -> None:
    other = Client(admin.dispatcher, admin.bot, admin.session, SECOND_ADMIN_ID)
    await admin.send("/kuzat Barcelona")

    methods = await other.send(texts.MENU_WATCHLIST)

    assert shown(methods) == [texts.WATCH_EMPTY]
    assert await watched_names(services, SECOND_ADMIN_ID) == []


# --------------------------------------------------------------------------- from a card
async def card_with_watch_button(admin: Client) -> tuple[int, str]:
    await admin.send("/tahlil 7kun 50% ev3%")
    await admin.press(texts.BTN_START)
    return admin.find_button(texts.BTN_WATCH_CARD)


async def test_card_star_offers_both_teams_and_the_league(
    admin: Client, services: Services
) -> None:
    message_id, data = await card_with_watch_button(admin)

    methods = await admin.press_data(data, message_id)

    (text,) = shown(methods)
    assert "nimani kuzatamiz" in text
    labels = [label for label, _ in admin.live_buttons(admin.last_message_id)]
    assert len(labels) == 3
    assert labels[0].startswith("👕") and labels[2].startswith("🏆")
    assert await watched_names(services) == []


async def test_card_choice_follows_and_a_second_tap_unfollows(
    admin: Client, services: Services
) -> None:
    message_id, data = await card_with_watch_button(admin)
    await admin.press_data(data, message_id)
    prompt_id = admin.last_message_id
    first_choice = admin.live_buttons(prompt_id)[0][1]

    on = await admin.press_data(first_choice, prompt_id)
    assert answers(on)[0].text == texts.WATCH_ADDED_ALERT
    assert len(await watched_names(services)) == 1
    assert admin.live_buttons(prompt_id)[0][0].startswith("👕 ✅")

    off = await admin.press_data(first_choice, prompt_id)
    assert answers(off)[0].text == texts.WATCH_REMOVED_ALERT
    assert await watched_names(services) == []


async def test_card_star_on_a_vanished_signal_explains(admin: Client) -> None:
    message_id, _ = await card_with_watch_button(admin)

    methods = await admin.press_data(CardCb(action="watch", signal_id=99_999).pack(), message_id)

    assert texts.NOT_FOUND in [a.text for a in answers(methods)] + shown(methods)


async def test_a_followed_team_marks_its_signals(admin: Client, services: Services) -> None:
    fixtures = await services.catalog.list_fixtures(
        ADMIN_ID, FixtureListRequest(dates=DateSpec.next_days(3), page_size=20)
    )
    home = fixtures.items[0].home_team
    await admin.send(f"/kuzat {home}")

    await admin.send("/tahlil 3kun 30% ev0%")
    await admin.press(texts.BTN_START)

    assert any(texts.CARD_WATCHLIST in text for text in admin.texts.values())


def test_watch_draft_rejects_corrupt_data() -> None:
    for data in (None, {}, {"kind": "x"}, {**WatchDraft().to_data(), "kind": "weird"}):
        with pytest.raises(StaleDraftError):
            WatchDraft.from_data(data)
