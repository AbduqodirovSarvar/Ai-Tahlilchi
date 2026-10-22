"""⚡ Bugungi signallar (F6.8) and /holat (F6.9) through the real dispatcher."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.bot import texts
from app.bot.callbacks import ResultsCb
from app.bot.handlers.analysis_run import RESULTS_PAGE_SIZE
from app.services.errors import NotFoundError, ServiceUnavailableError
from app.services.interfaces import (
    DateSpec,
    ErrorEntry,
    Services,
    SignalView,
    SystemStatus,
)
from tests.bot.fakes import FIXED_NOW
from tests.e2e.bot_client import Client, answers, shown, telegram_html_problems


# --------------------------------------------------------------------------- today
async def test_today_without_signals_shows_the_empty_state(admin: Client) -> None:
    methods = await admin.send(texts.MENU_TODAY)

    assert shown(methods) == [texts.TODAY_EMPTY]


async def test_today_lists_recommended_signals_with_buttons(admin: Client) -> None:
    await admin.send("/tahlil 7kun 50% ev3% 10ta")
    await admin.press(texts.BTN_START)

    methods = await admin.send(texts.MENU_TODAY)

    header, *rest = shown(methods)
    assert header == texts.TODAY_HEADER.format(count=1)
    assert any("🎯 Tanlov" in text for text in rest)
    assert admin.has_button(texts.BTN_RECALC_ALL)
    assert admin.has_button(texts.BTN_BET)


async def test_today_command_is_an_alias(admin: Client) -> None:
    await admin.send("/tahlil 7kun 50% ev3% 10ta")
    await admin.press(texts.BTN_START)

    methods = await admin.send("/bugun")

    assert shown(methods)[0] == texts.TODAY_HEADER.format(count=1)


async def test_today_hidden_signals_are_not_listed(admin: Client, services: Services) -> None:
    await admin.send("/tahlil 7kun 50% ev3% 10ta")
    await admin.press(texts.BTN_START)
    (signal,) = await services.signals.list_signals(admin.user_id, DateSpec.today())
    await services.signals.hide_signal(admin.user_id, signal.signal_id)

    methods = await admin.send(texts.MENU_TODAY)

    assert shown(methods) == [texts.TODAY_EMPTY]


async def test_paging_uses_one_batch_call_not_a_loop(admin: Client, services: Services) -> None:
    real_batch = services.signals.get_signals
    batch_calls: list[tuple[int, ...]] = []

    async def spy(user_id: int, ids: tuple[int, ...]) -> tuple[SignalView, ...]:
        batch_calls.append(tuple(ids))
        return await real_batch(user_id, ids)

    services.signals.get_signals = spy  # type: ignore[method-assign]
    services.signals.get_signal = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("per-signal lookups are not allowed")
    )
    await admin.send("/tahlil 7kun 50% ev3% 10ta")
    await admin.press(texts.BTN_START)

    methods = await admin.press("Yana")

    assert len(answers(methods)) == 1
    assert len(batch_calls) == 1
    assert len(batch_calls[0]) == 10
    assert len([t for t in shown(methods) if "🎯 Tanlov" in t]) == min(
        RESULTS_PAGE_SIZE, 10 - RESULTS_PAGE_SIZE
    )


async def test_paging_skips_signals_that_vanished(admin: Client, services: Services) -> None:
    real_batch = services.signals.get_signals

    async def lossy(user_id: int, ids: tuple[int, ...]) -> tuple[SignalView, ...]:
        return await real_batch(user_id, ids[:-2])

    services.signals.get_signals = lossy  # type: ignore[method-assign]
    await admin.send("/tahlil 7kun 50% ev3% 10ta")
    await admin.press(texts.BTN_START)

    methods = await admin.press("Yana")

    assert len(answers(methods)) == 1
    assert len([t for t in shown(methods) if "🎯 Tanlov" in t]) == RESULTS_PAGE_SIZE - 2


async def test_a_stale_page_button_of_an_older_result_is_refused(admin: Client) -> None:
    await admin.send("/tahlil 7kun 50% ev3% 10ta")
    await admin.press(texts.BTN_START)
    message_id, _ = admin.find_button("Yana")

    methods = await admin.press_data(ResultsCb(page=1, token=999).pack(), message_id)

    assert answers(methods)[0].text == texts.STALE_BUTTON


# --------------------------------------------------------------------------- /holat
async def test_status_command_shows_mode_quota_freshness_and_jobs(admin: Client) -> None:
    methods = await admin.send("/holat")

    (text,) = shown(methods)
    assert "namunaviy ma'lumot" in text
    assert "ishlatilgan 40/100 · qoldi 60 · qo'lda: 30" in text
    assert "Ma'lumot oxirgi yangilangan: <b>21.10 08:30</b> (2 soat oldin)" in text
    assert "✅ refresh" in text
    assert texts.STATUS_ERRORS_NONE in text
    assert telegram_html_problems(text) == []


async def test_status_menu_item_shows_the_same_and_points_to_help(admin: Client) -> None:
    command = shown(await admin.send("/holat"))
    menu = shown(await admin.send(texts.MENU_HELP))

    assert menu == command
    assert "/yordam" in menu[0]


async def test_status_in_real_mode_and_with_hostile_errors(
    admin: Client, services: Services
) -> None:
    admin.dispatcher["backend"] = "real"
    real_status = await services.status.system_status()

    async def hostile() -> SystemStatus:
        return SystemStatus(
            quotas=real_status.quotas,
            last_refresh_at=FIXED_NOW - timedelta(days=2),
            jobs=real_status.jobs,
            recent_errors=(
                ErrorEntry(at=FIXED_NOW, component="<gemini>", message="<script>x</script> & 429"),
            ),
        )

    services.status.system_status = hostile  # type: ignore[method-assign]

    methods = await admin.send("/holat")

    (text,) = shown(methods)
    assert texts.STATUS_MODE["real"] in text
    assert "(2 kun oldin)" in text
    assert "&lt;script&gt;x&lt;/script&gt; &amp; 429" in text
    assert telegram_html_problems(text) == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ServiceUnavailableError("database"),
            texts.SERVICE_UNAVAILABLE.format(component="database"),
        ),
        (NotFoundError("status", 1), texts.NOT_FOUND),
    ],
)
async def test_status_failures_are_explained(
    admin: Client, services: Services, error: Exception, expected: str
) -> None:
    services.status.system_status = AsyncMock(side_effect=error)  # type: ignore[method-assign]

    methods = await admin.send("/holat")

    assert shown(methods) == [expected]


async def test_help_no_longer_promises_holat_later(admin: Client) -> None:
    methods = await admin.send("/yordam")

    (text,) = shown(methods)
    assert "/holat" in text
    assert "/qayta" in text
    assert "tez orada" not in text
