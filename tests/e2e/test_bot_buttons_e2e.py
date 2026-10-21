"""QA: every reachable button is answered exactly once and fits 64 bytes (F6.10).

A crawler drives the real dispatcher to each wizard screen, the settings editors and the
result cards, then taps *every* button there (FSM restored between taps). Each tap must
produce exactly one ``answerCallbackQuery``, never the generic "unexpected error", and only
valid Telegram HTML.
"""

import pytest

from app.bot import texts
from tests.e2e.bot_client import (
    CALLBACK_DATA_LIMIT,
    Client,
    Snapshot,
    answers,
    shown,
    telegram_html_problems,
)
from tests.e2e.flows import WIZARD_SCREENS, reach


async def crawl(client: Client, message_id: int) -> list[str]:
    """Tap every button of ``message_id`` from the same starting point; return all data."""
    start = Snapshot.take(client)
    buttons = client.live_buttons(message_id)
    assert buttons, "screen without buttons"
    for label, data in buttons:
        start.restore(client)
        methods = await client.press_data(data, message_id)
        replies = answers(methods)
        assert len(replies) == 1, (label, data, replies)
        for text in shown(methods):
            assert text != texts.UNEXPECTED_ERROR, (label, data)
            assert telegram_html_problems(text) == [], (label, text)
    start.restore(client)
    return [data for _, data in buttons]


def assert_fits(datas: list[str]) -> None:
    for data in datas:
        assert len(data.encode()) <= CALLBACK_DATA_LIMIT, data


@pytest.mark.parametrize("screen", WIZARD_SCREENS)
async def test_every_wizard_button_answers_once(admin: Client, screen: str) -> None:
    await reach(admin, screen)

    datas = await crawl(admin, admin.last_message_id)

    assert_fits(datas)


async def test_calendar_end_picking_buttons_answer_once(admin: Client) -> None:
    await reach(admin, "calendar")
    await admin.press("21")  # start = today; now every later day ends the range

    assert_fits(await crawl(admin, admin.last_message_id))


async def test_fixture_picker_second_page_buttons_answer_once(admin: Client) -> None:
    await reach(admin, "fixtures")
    await admin.press(texts.BTN_NEXT_PAGE)

    assert_fits(await crawl(admin, admin.last_message_id))


async def test_result_card_and_more_buttons_answer_once(admin: Client) -> None:
    await admin.send("/tahlil 7kun 50% ev3% 10ta")
    await admin.press(texts.BTN_START)
    card_ids = [
        message_id
        for message_id, markup in admin.markups.items()
        if markup is not None
        and any(texts.BTN_HIDE in label for label, _ in admin.live_buttons(message_id))
    ]
    more_id, _ = admin.find_button("Yana")
    assert card_ids

    datas: list[str] = []
    for message_id in [*card_ids[:2], more_id]:
        datas += await crawl(admin, message_id)

    assert_fits(datas)


DOUBLE_ANSWER = pytest.mark.xfail(
    strict=True,
    reason="F-4: settings multi-select toggle answers the callback twice "
    "(handlers/settings.py on_settings: '✅ Saqlandi' then a bare answer)",
)
SETTINGS_FIELDS = [
    pytest.param(field, marks=DOUBLE_ANSWER) if field in {"sports", "leagues", "markets"} else field
    for field in texts.SETTINGS_BUTTONS
]


@pytest.mark.parametrize("field", SETTINGS_FIELDS)
async def test_every_settings_editor_button_answers_once(admin: Client, field: str) -> None:
    await admin.send(texts.MENU_SETTINGS)
    overview = admin.last_message_id
    await admin.press_option(texts.SETTINGS_BUTTONS[field])

    datas = await crawl(admin, overview)

    assert_fits(datas)


async def test_settings_overview_buttons_answer_once(admin: Client) -> None:
    await admin.send(texts.MENU_SETTINGS)

    assert_fits(await crawl(admin, admin.last_message_id))
