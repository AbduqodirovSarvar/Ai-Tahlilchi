"""🔄 Qayta hisoblash (F6.7) through the real dispatcher: card, bulk, ``/qayta``, errors."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import EditMessageText, SendMessage

from app.bot import texts
from app.bot.callbacks import CardCb, RecalcAllCb, decode_ref, encode_ref
from app.services.errors import (
    BudgetExceededError,
    BusyError,
    CooldownActiveError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.services.interfaces import Services
from tests.e2e.bot_client import Client, answers, sent, shown, telegram_html_problems


async def analyse(admin: Client) -> None:
    """A 7-day analysis with plenty of signals; leaves cards and the summary on screen."""
    await admin.send("/tahlil 7kun 50% ev3% 10ta")
    await admin.press(texts.BTN_START)


# --------------------------------------------------------------------------- single card
async def test_card_recalc_edits_the_card_and_replies_with_the_diff(admin: Client) -> None:
    await analyse(admin)
    card_id, data = admin.find_button(texts.BTN_RECALC)

    methods = await admin.press_data(data, card_id)

    (answer,) = answers(methods)
    assert answer.text == texts.RECALC_STARTED
    (edit,) = [m for m in methods if isinstance(m, EditMessageText)]
    assert edit.message_id == card_id
    assert "🎯 Tanlov" in edit.text  # the card itself, refreshed
    assert edit.reply_markup is not None
    (diff,) = sent(methods)
    assert "yangilandi (11:00)" in diff.text
    assert "Koeff.:" in diff.text
    assert "Holat:" in diff.text
    assert telegram_html_problems(diff.text) == []


async def test_card_recalc_cooldown_is_explained_in_minutes(admin: Client) -> None:
    await analyse(admin)
    card_id, data = admin.find_button(texts.BTN_RECALC)
    await admin.press_data(data, card_id)

    methods = await admin.press_data(data, card_id)

    assert len(answers(methods)) == 1
    assert shown(methods) == ["⏳ 10 daqiqadan keyin qayta urinib ko'ring."]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CooldownActiveError(timedelta(seconds=61)), "⏳ 2 daqiqadan keyin qayta urinib ko'ring."),
        (BusyError(1001), texts.BUSY),
        (
            ServiceUnavailableError("api-football", "timeout"),
            texts.SERVICE_UNAVAILABLE.format(component="api-football"),
        ),
        (BudgetExceededError(12, 3), texts.BUDGET_EXCEEDED.format(needed=12, available=3)),
        (NotFoundError("signal", 9), texts.NOT_FOUND),
        (
            ValidationFailedError("signal_id", "match already started"),
            texts.VALIDATION_BY_FIELD["signal_id"],
        ),
    ],
)
async def test_card_recalc_service_errors_map_to_uzbek_messages(
    admin: Client, services: Services, error: Exception, expected: str
) -> None:
    await analyse(admin)
    card_id, data = admin.find_button(texts.BTN_RECALC)
    services.analysis.recalc_signal = AsyncMock(side_effect=error)  # type: ignore[method-assign]

    methods = await admin.press_data(data, card_id)

    assert len(answers(methods)) == 1
    assert shown(methods) == [expected]


async def test_recalc_of_an_unknown_signal_is_not_found(admin: Client) -> None:
    await analyse(admin)
    card_id, _ = admin.find_button(texts.BTN_RECALC)

    methods = await admin.press_data(CardCb(action="recalc", signal_id=999_999).pack(), card_id)

    assert shown(methods) == [texts.NOT_FOUND]


# --------------------------------------------------------------------------- bulk
async def test_recalc_all_under_the_summary_runs_in_the_background(admin: Client) -> None:
    await analyse(admin)

    methods = await admin.press(texts.BTN_RECALC_ALL)

    assert len(answers(methods)) == 1
    texts_shown = shown(methods)
    assert texts_shown[0] == texts.RECALC_STARTED  # the progress message
    assert texts.RECALC_DONE in texts_shown
    summary = texts_shown[-1]
    assert summary.startswith("🔄 <b>Qayta hisoblash tugadi:</b>")
    assert telegram_html_problems(summary) == []
    assert admin.last_text == summary


async def test_second_recalc_all_finds_everything_in_cooldown(admin: Client) -> None:
    await analyse(admin)
    await admin.press(texts.BTN_RECALC_ALL)

    methods = await admin.press(texts.BTN_RECALC_ALL)

    summary = shown(methods)[-1]
    assert "yangilandi 0 ta" in summary
    assert "yaqinda yangilangan (bitta o'yin 10 daqiqada bir marta)" in summary


async def test_recalc_all_error_is_reported_and_the_user_is_not_stuck(
    admin: Client, services: Services
) -> None:
    await analyse(admin)
    services.analysis.recalc_query = AsyncMock(  # type: ignore[method-assign]
        side_effect=ServiceUnavailableError("api-football")
    )

    methods = await admin.press(texts.BTN_RECALC_ALL)

    assert shown(methods)[-1] == texts.SERVICE_UNAVAILABLE.format(component="api-football")
    assert await admin.state() is None
    assert admin.has_button(texts.BTN_RECALC)  # cards stay usable


async def test_recalc_all_unexpected_failure_gets_the_generic_message(
    admin: Client, services: Services
) -> None:
    await analyse(admin)
    services.analysis.recalc_query = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )

    methods = await admin.press(texts.BTN_RECALC_ALL)

    assert shown(methods)[-1] == texts.UNEXPECTED_ERROR


async def test_recalc_all_of_a_forgotten_query_says_not_found(admin: Client) -> None:
    await analyse(admin)
    message_id, _ = admin.find_button(texts.BTN_RECALC_ALL)
    forgotten = RecalcAllCb(kind="q", ref=encode_ref("q-from-before-the-restart")).pack()

    methods = await admin.press_data(forgotten, message_id)

    assert shown(methods)[-1] == texts.NOT_FOUND


@pytest.mark.parametrize(
    "data",
    [
        RecalcAllCb(kind="q", ref="!!!").pack(),
        RecalcAllCb(kind="d", ref="not-a-date").pack(),
        RecalcAllCb(kind="x", ref="whatever").pack(),
    ],
)
async def test_recalc_all_buttons_with_unusable_references_are_stale(
    admin: Client, data: str
) -> None:
    await analyse(admin)
    message_id, _ = admin.find_button(texts.BTN_RECALC_ALL)

    methods = await admin.press_data(data, message_id)

    (answer,) = answers(methods)
    assert answer.text == texts.STALE_BUTTON
    assert answer.show_alert is True
    assert not [m for m in methods if isinstance(m, SendMessage)]


def test_refs_round_trip_ids_with_the_callback_separator() -> None:
    assert decode_ref(encode_ref("daily:2026-10-22")) == "daily:2026-10-22"


# --------------------------------------------------------------------------- /qayta
async def test_qayta_without_arguments_recalculates_today(admin: Client) -> None:
    await analyse(admin)

    methods = await admin.send("/qayta")

    summary = shown(methods)[-1]
    assert summary.startswith("🔄 <b>Qayta hisoblash tugadi:</b>")


async def test_qayta_with_no_signals_in_the_window_explains(admin: Client) -> None:
    methods = await admin.send("/qayta ertaga")

    assert shown(methods)[-1] == texts.RECALC_BATCH_EMPTY


@pytest.mark.parametrize("argument", ["dam", "3kun", "22.10-24.10"])
async def test_qayta_accepts_date_expressions(admin: Client, argument: str) -> None:
    methods = await admin.send(f"/qayta {argument}")

    summary = shown(methods)[-1]
    assert summary == texts.RECALC_BATCH_EMPTY or summary.startswith(
        "🔄 <b>Qayta hisoblash tugadi:</b>"
    )


@pytest.mark.parametrize("argument", ["31.02", "Arsenal", "futbol", "60%", "10ta", "bugun ertaga"])
async def test_qayta_rejects_things_that_are_not_a_window(admin: Client, argument: str) -> None:
    methods = await admin.send(f"/qayta {argument}")

    (text,) = shown(methods)
    assert "Namuna" in text
    assert "/qayta" in text
    assert telegram_html_problems(text) == []


async def test_qayta_hostile_argument_is_escaped(admin: Client) -> None:
    methods = await admin.send("/qayta <script>")

    (text,) = shown(methods)
    assert "<script>" not in text
    assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- analysis errors
async def test_busy_analysis_start_is_explained_and_confirmation_returns(
    admin: Client, services: Services
) -> None:
    services.analysis.analyze = AsyncMock(side_effect=BusyError(1001))  # type: ignore[method-assign]
    await admin.send("/tahlil 7kun 50% ev3% 10ta")

    methods = await admin.press(texts.BTN_START)

    assert texts.BUSY in shown(methods)
    assert admin.has_button(texts.BTN_START)  # the confirmation is back


async def test_unavailable_provider_during_analysis_is_explained(
    admin: Client, services: Services
) -> None:
    services.analysis.analyze = AsyncMock(  # type: ignore[method-assign]
        side_effect=ServiceUnavailableError("odds-api", "503")
    )
    await admin.send("/tahlil 7kun 50% ev3% 10ta")

    methods = await admin.press(texts.BTN_START)

    assert texts.SERVICE_UNAVAILABLE.format(component="odds-api") in shown(methods)
