"""``/tahlil`` command and signal-card buttons through the real dispatcher."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.bot import texts
from app.bot.callbacks import CardCb, ResultsCb
from app.services.errors import CooldownActiveError, NotFoundError
from app.services.interfaces import SignalState
from tests.bot.fakes import ADMIN_ID, BotHarness, answers, sent_texts


async def test_quick_command_jumps_to_confirmation(harness: BotHarness) -> None:
    methods = await harness.send("/tahlil 22.10-28.10 futbol 60% Arsenal,Inter")

    text = sent_texts(methods)[-1]
    assert "22.10–28.10" in text
    assert "≥ 60%" in text
    assert "Arsenal, Inter" in text
    harness.find_button(texts.BTN_START)


async def test_quick_command_without_arguments_opens_wizard(harness: BotHarness) -> None:
    methods = await harness.send("/tahlil")
    assert "Sana oralig'i" in sent_texts(methods)[0]


async def test_quick_missing_parts_take_settings_defaults(harness: BotHarness) -> None:
    methods = await harness.send("/tahlil ertaga")
    text = sent_texts(methods)[-1]
    assert "22.10" in text
    assert "≥ 45%" in text  # default minimum probability
    assert "EV ≥ 5%" in text


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("31.02 futbol", "bunday sana yo'q"),
        ("150%", "30–95%"),
        ("60", "foizmi yoki sonmi"),
        ("bugun ertaga", "ikki marta"),
        ("Real", "noaniq"),
        ("Qwertyzz", "topilmadi"),
    ],
)
async def test_quick_command_errors_explain_and_show_example(
    harness: BotHarness, argument: str, expected: str
) -> None:
    methods = await harness.send(f"/tahlil {argument}")

    text = sent_texts(methods)[0]
    assert expected in text
    assert "Namuna" in text


async def test_quick_command_high_probability_warning(harness: BotHarness) -> None:
    methods = await harness.send("/tahlil bugun 85%")
    assert "Diqqat" in sent_texts(methods)[-1]


async def run_and_get_card(harness: BotHarness) -> tuple[int, int]:
    await harness.send("/tahlil 7kun 50% ev3%")
    await harness.press(texts.BTN_START)
    message_id, data = harness.find_button(texts.BTN_HIDE)
    return message_id, CardCb.unpack(data).signal_id


async def test_hide_button_hides_signal_and_edits_card(harness: BotHarness) -> None:
    message_id, signal_id = await run_and_get_card(harness)

    methods = await harness.press_data(
        CardCb(action="hide", signal_id=signal_id).pack(), message_id
    )

    assert answers(methods)[0].text == texts.HIDDEN_ALERT
    assert sent_texts(methods)[0].startswith("🙈 Yashirildi")
    signal = await harness.services.signals.get_signal(ADMIN_ID, signal_id)
    assert signal.state is SignalState.HIDDEN


@pytest.mark.parametrize("action", ["detail"])
async def test_later_wave_card_buttons_are_placeholders(harness: BotHarness, action: str) -> None:
    message_id, signal_id = await run_and_get_card(harness)

    methods = await harness.press_data(
        CardCb(action=action, signal_id=signal_id).pack(), message_id
    )

    assert len(answers(methods)) == 1
    assert answers(methods)[0].text == texts.PLACEHOLDER_ALERT


async def test_cooldown_error_is_mapped_to_minutes(harness: BotHarness) -> None:
    message_id, signal_id = await run_and_get_card(harness)
    harness.services.signals.hide_signal = AsyncMock(  # type: ignore[method-assign]
        side_effect=CooldownActiveError(timedelta(minutes=7, seconds=10))
    )

    methods = await harness.press_data(
        CardCb(action="hide", signal_id=signal_id).pack(), message_id
    )

    assert answers(methods)[0].text == "⏳ 8 daqiqadan keyin qayta urinib ko'ring."
    assert answers(methods)[0].show_alert is True


async def test_unknown_signal_is_not_found(harness: BotHarness) -> None:
    message_id, _ = await run_and_get_card(harness)
    harness.services.signals.hide_signal = AsyncMock(  # type: ignore[method-assign]
        side_effect=NotFoundError("signal", 999)
    )

    methods = await harness.press_data(CardCb(action="hide", signal_id=999).pack(), message_id)

    assert "Topilmadi" in (answers(methods)[0].text or "")


async def test_results_page_after_restart_is_stale(harness: BotHarness) -> None:
    await harness.send("/tahlil 7kun 50% ev3%")
    await harness.press(texts.BTN_START)
    message_id, data = harness.find_button("Yana")
    harness.restart()

    methods = await harness.press_data(data, message_id)

    assert "eskirgan" in (answers(methods)[0].text or "")


async def test_results_page_skips_vanished_signals(harness: BotHarness) -> None:
    await harness.send("/tahlil 7kun 50% ev3%")
    await harness.press(texts.BTN_START)
    message_id, _ = harness.find_button("Yana")
    harness.services.signals.get_signal = AsyncMock(  # type: ignore[method-assign]
        side_effect=NotFoundError("signal", 1)
    )

    methods = await harness.press_data(ResultsCb(page=1).pack(), message_id)

    assert len(answers(methods)) == 1
    assert not [text for text in sent_texts(methods) if text.startswith("⚽")]
