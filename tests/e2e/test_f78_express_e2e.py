"""🧩 Ekspress tuzuvchi through the real dispatcher over the fake services (F7.8)."""

from unittest.mock import AsyncMock

import pytest

from app.bot import texts
from app.bot.callbacks import ExpressCb
from app.bot.draft import StaleDraftError
from app.bot.express_ui import ExpressDraft, ExpressOption, format_express, pick_keyboard
from app.bot.states import ExpressFlow
from app.domain import Market, Selection
from app.services.errors import ValidationFailedError
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    ExpressLeg,
    ExpressView,
    Services,
)
from tests.bot.fakes import ADMIN_ID
from tests.e2e.bot_client import Client, answers, buttons_of, shown, telegram_html_problems
from tests.e2e.conftest import SECOND_ADMIN_ID


async def recommend(services: Services, *, with_twins: bool = False) -> None:
    """Many recommended signals over the next three days."""
    await services.analysis.analyze(
        ADMIN_ID,
        AnalysisQuery(
            dates=DateSpec.next_days(3),
            min_probability=0.3,
            min_ev=0.0,
            include_no_value=with_twins,
            markets=frozenset(texts.MARKET_LABELS),
        ),
    )


def toggles(admin: Client) -> list[tuple[str, str]]:
    return [
        (label, data)
        for label, data in admin.live_buttons(admin.last_message_id)
        if ExpressCb.unpack(data).action == "toggle"
    ]


# --------------------------------------------------------------------------- picker
async def test_without_signals_the_picker_explains(admin: Client) -> None:
    methods = await admin.send("/ekspress")

    assert shown(methods) == [texts.EXPRESS_EMPTY]
    assert await admin.state() is None


async def test_picker_lists_recommended_signals_with_odds(
    admin: Client, services: Services
) -> None:
    await recommend(services)

    methods = await admin.send("/ekspress")

    (text,) = shown(methods)
    assert "Tanlangan: 0" in text
    assert len(toggles(admin)) >= 2
    assert all("@" in label for label, _ in toggles(admin))
    assert await admin.state() == ExpressFlow.pick.state
    assert not admin.has_button("Hisoblash")


async def test_button_under_todays_signals_opens_the_picker(
    admin: Client, services: Services
) -> None:
    await recommend(services)
    await admin.send(texts.MENU_TODAY)

    methods = await admin.press(texts.BTN_EXPRESS)

    assert "Ekspress tuzuvchi" in shown(methods)[0]


async def test_calculate_appears_only_for_two_to_four_picks(
    admin: Client, services: Services
) -> None:
    await recommend(services)
    await admin.send("/ekspress")
    message_id = admin.last_message_id
    seen = []
    for index in range(5):
        await admin.press_data(ExpressCb(action="toggle", index=index).pack(), message_id)
        seen.append(admin.has_button("Hisoblash"))

    assert seen[0] is False
    assert seen[1] is True


async def test_two_picks_of_one_match_are_refused_early(admin: Client, services: Services) -> None:
    await recommend(services, with_twins=True)
    await admin.send("/ekspress")
    message_id = admin.last_message_id
    draft = ExpressDraft.from_data(
        (await admin.dispatcher.storage.get_data(admin.storage_key()))["express"]
    )
    by_fixture: dict[str, list[int]] = {}
    for index, option in enumerate(draft.options):
        by_fixture.setdefault(option.fixture_id, []).append(index)
    twins = next((ids for ids in by_fixture.values() if len(ids) >= 2), None)
    if twins is None:
        pytest.skip("the sample world offered no two selections of one match")
    await admin.press_data(ExpressCb(action="toggle", index=twins[0]).pack(), message_id)

    methods = await admin.press_data(ExpressCb(action="toggle", index=twins[1]).pack(), message_id)

    assert answers(methods)[0].text == texts.EXPRESS_SAME_MATCH
    assert answers(methods)[0].show_alert is True


async def test_a_fifth_leg_is_refused(admin: Client, services: Services) -> None:
    await recommend(services)
    await admin.send("/ekspress")
    message_id = admin.last_message_id
    draft = ExpressDraft.from_data(
        (await admin.dispatcher.storage.get_data(admin.storage_key()))["express"]
    )
    distinct = {}
    for index, option in enumerate(draft.options):
        distinct.setdefault(option.fixture_id, index)
    if len(distinct) < 5:
        pytest.skip("the sample world offered fewer than five matches")
    for index in list(distinct.values())[:4]:
        await admin.press_data(ExpressCb(action="toggle", index=index).pack(), message_id)

    methods = await admin.press_data(
        ExpressCb(action="toggle", index=list(distinct.values())[4]).pack(), message_id
    )

    assert answers(methods)[0].text == texts.EXPRESS_LIMIT.format(max=4)


# --------------------------------------------------------------------------- result
async def pick_distinct(admin: Client, count: int) -> list[int]:
    message_id = admin.last_message_id
    draft = ExpressDraft.from_data(
        (await admin.dispatcher.storage.get_data(admin.storage_key()))["express"]
    )
    seen: set[str] = set()
    chosen: list[int] = []
    for index, option in enumerate(draft.options):
        if option.fixture_id not in seen and len(chosen) < count:
            seen.add(option.fixture_id)
            chosen.append(option.signal_id)
            await admin.press_data(ExpressCb(action="toggle", index=index).pack(), message_id)
    assert len(chosen) == count
    return chosen


async def test_result_shows_the_combined_numbers_and_the_warning(
    admin: Client, services: Services
) -> None:
    await recommend(services)
    await admin.send("/ekspress")
    chosen = await pick_distinct(admin, 3)
    views = await services.signals.get_signals(ADMIN_ID, chosen)

    methods = await admin.press("Hisoblash")

    (text,) = shown(methods)
    probability = views[0].probability * views[1].probability * views[2].probability
    odds = views[0].odds * views[1].odds * views[2].odds
    assert f"Umumiy koeffitsient: <b>{odds:.2f}</b>" in text
    assert f"Umumiy ehtimollik: <b>{probability * 100:.0f}%</b>" in text
    assert "3 ta tanlov" in text
    assert "3 ta 70% lik tanlovdan tuzilgan ekspressning o'tish ehtimolligi atigi 34%" in text
    assert "Sizning ekspressingiz: 3 ta tanlov" in text
    assert telegram_html_problems(text) == []


async def test_back_returns_to_the_picker_with_the_same_choice(
    admin: Client, services: Services
) -> None:
    await recommend(services)
    await admin.send("/ekspress")
    await pick_distinct(admin, 2)
    await admin.press("Hisoblash")

    methods = await admin.press(texts.BTN_EXPRESS_BACK)

    assert "Tanlangan: 2" in shown(methods)[0]
    assert admin.has_button("Hisoblash")


async def test_close_leaves_the_flow(admin: Client, services: Services) -> None:
    await recommend(services)
    await admin.send("/ekspress")

    methods = await admin.press(texts.BTN_EXPRESS_CLOSE)

    assert shown(methods) == [texts.CANCELLED]
    assert await admin.state() is None


async def test_a_service_refusal_is_explained(admin: Client, services: Services) -> None:
    await recommend(services)
    await admin.send("/ekspress")
    await pick_distinct(admin, 2)
    services.express.evaluate = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValidationFailedError("signal_ids", "can no longer be played")
    )

    methods = await admin.press("Hisoblash")

    assert answers(methods)[0].text == texts.EXPRESS_REFUSED
    assert admin.has_button("Hisoblash")  # the picker stays


# --------------------------------------------------------------------------- stale and isolation
async def test_old_picker_buttons_are_stale(admin: Client, services: Services) -> None:
    await recommend(services)
    await admin.send("/ekspress")
    old_id = admin.last_message_id
    await admin.send("/ekspress")  # a newer picker replaces the draft

    methods = await admin.press_data(ExpressCb(action="toggle", index=0).pack(), old_id)

    assert "Bu tugma eskirgan" in (answers(methods)[0].text or "")


async def test_picker_after_a_restart_is_stale(admin: Client, services: Services) -> None:
    await recommend(services)
    await admin.send("/ekspress")
    message_id = admin.last_message_id
    await admin.dispatcher.storage.set_state(admin.storage_key(), None)

    methods = await admin.press_data(ExpressCb(action="toggle", index=0).pack(), message_id)

    assert texts.STALE_BUTTON in [a.text for a in answers(methods)]


async def test_another_users_signals_are_not_offered(admin: Client, services: Services) -> None:
    await recommend(services)
    other = Client(admin.dispatcher, admin.bot, admin.session, SECOND_ADMIN_ID)

    methods = await other.send("/ekspress")

    assert shown(methods) == [texts.EXPRESS_EMPTY]


# --------------------------------------------------------------------------- pure
def make_view(*probabilities: float, odds_each: float = 1.5) -> ExpressView:
    legs = tuple(
        ExpressLeg(
            signal_id=i,
            fixture_label=f"A{i} <b> — B{i}",
            market=Market.match_result(),
            selection=Selection.HOME,
            odds=odds_each,
            probability=p,
        )
        for i, p in enumerate(probabilities, start=1)
    )
    probability = 1.0
    odds = 1.0
    for leg in legs:
        probability *= leg.probability
        odds *= leg.odds
    return ExpressView(
        legs=legs,
        combined_odds=odds,
        combined_probability=probability,
        ev=probability * odds - 1,
    )


def test_format_flags_a_negative_ev_and_escapes_names() -> None:
    text = format_express(make_view(0.7, 0.7, 0.7, odds_each=1.3))

    assert texts.EXPRESS_NEGATIVE in text
    assert "A1 &lt;b&gt;" in text
    assert "34%" in text
    assert f"70%{texts.RANGE_DASH}70%" in text
    assert telegram_html_problems(text) == []


def test_a_positive_express_has_no_negative_flag() -> None:
    assert texts.EXPRESS_NEGATIVE not in format_express(make_view(0.95, 0.95))


def test_draft_round_trip_and_corruption() -> None:
    draft = ExpressDraft(
        message_id=5,
        options=(ExpressOption(1, "a", "f1"), ExpressOption(2, "b", "f2")),
        selected=(2,),
    )

    assert ExpressDraft.from_data(draft.to_data()) == draft
    for bad in (None, {}, {**draft.to_data(), "selected": [9]}, {**draft.to_data(), "options": 3}):
        with pytest.raises(StaleDraftError):
            ExpressDraft.from_data(bad)
    with pytest.raises(StaleDraftError):
        draft.toggled(7)


def test_picker_keyboard_fits_callback_data() -> None:
    options = tuple(ExpressOption(i, f"opt {i}", f"f{i}") for i in range(12))
    draft = ExpressDraft(message_id=1, options=options, selected=(0, 1))

    for button in buttons_of(pick_keyboard(draft)):
        assert len((button.callback_data or "").encode()) <= 64
