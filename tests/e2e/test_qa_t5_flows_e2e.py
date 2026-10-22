"""QA (T5 bot): FSM flows — running guards, restarts, stale buttons, back/cancel, concurrency.

Fake services (fast) except where a persistent FSM database is the point.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher

from app.bot import texts
from app.bot.callbacks import (
    BankCb,
    CardCb,
    CustomCb,
    ExpressCb,
    ReportCb,
    TplCb,
    WatchCb,
)
from app.bot.dispatcher import build_dispatcher
from app.bot.middleware import RateLimitMiddleware
from app.bot.storage import SqliteStorage
from app.services.fake import create_fake_services
from app.services.interfaces import AnalysisQuery, DateSpec, Services
from tests.bot.fakes import ADMIN_ID, FIXED_NOW, RecordingSession, fixed_clock
from tests.e2e.bot_client import Client, YieldingSession, answers, make_dispatcher, shown
from tests.e2e.flows import to_confirm
from tests.e2e.test_bot_concurrency_e2e import BlockedAnalyze

assert FIXED_NOW  # the fixed Wednesday 21.10.2026 11:00 Tashkent the fakes run on


@pytest.fixture
def slow_admin() -> tuple[Client, Services]:
    """An admin whose every Telegram call yields to the event loop (like real I/O)."""
    session = YieldingSession()
    bot = Bot(token="42:TEST", session=session)
    dispatcher: Dispatcher = make_dispatcher(frozenset({ADMIN_ID}))
    services: Services = dispatcher["services"]
    return Client(dispatcher, bot, session, ADMIN_ID), services


async def seed_signals(services: Services) -> None:
    """Store some signals so the express picker has something to offer."""
    await services.analysis.analyze(
        ADMIN_ID,
        AnalysisQuery(dates=DateSpec.next_days(3), min_probability=0.3, min_ev=0.0),
    )


# --------------------------------------------------------------------------- running guard
OTHER_ENTRIES = [
    texts.MENU_WATCHLIST,
    texts.MENU_TEMPLATES,
    texts.MENU_REPORT,
    texts.MENU_BETS,
    texts.MENU_SETTINGS,
    texts.MENU_TODAY,
    "/ekspress",
    "/kuzat",
    "/bank",
    "/hisobot",
    "/holat",
]


CLEARS_RUNNING = {texts.MENU_WATCHLIST, texts.MENU_SETTINGS, "/kuzat"}
"""Entries that reset the FSM state and so drop the 'running' guard (QA F-5)."""


def _entries(clearing: bool) -> list[object]:
    return [
        pytest.param(
            entry,
            marks=pytest.mark.xfail(
                strict=True, reason="QA F-5: this entry resets the FSM state of a running analysis"
            ),
        )
        if clearing and entry in CLEARS_RUNNING
        else entry
        for entry in OTHER_ENTRIES
    ]


@pytest.mark.parametrize("entry", _entries(clearing=True))
async def test_opening_another_screen_does_not_end_the_running_analysis_guard(
    slow_admin: tuple[Client, Services], entry: str
) -> None:
    """A second ▶️ while the first run lives would double the provider-request spend."""
    admin, services = slow_admin
    blocked = BlockedAnalyze(services)
    await to_confirm(admin)
    confirm_message, start = admin.find_button(texts.BTN_START)

    run = asyncio.create_task(admin.press_data(start, confirm_message))
    await asyncio.wait_for(blocked.started.wait(), 5)
    try:
        await admin.send(entry)
        state_after = await admin.state()
        again = await admin.press_data(start, confirm_message)
    finally:
        blocked.release.set()
        await run

    assert blocked.calls == 1, f"{entry} let a second analysis start"
    assert state_after == "AnalysisFlow:running"
    assert answers(again)[0].text == texts.ALREADY_RUNNING


@pytest.mark.parametrize("entry", _entries(clearing=True))
async def test_opening_another_screen_keeps_the_custom_analysis_running_state(
    slow_admin: tuple[Client, Services], entry: str
) -> None:
    admin, services = slow_admin
    blocked = BlockedAnalyze(services)
    await admin.send(texts.MENU_CUSTOM)
    await admin.press(texts.BTN_CUSTOM_TODAY)
    await admin.press_option(admin.live_buttons(admin.last_message_id)[0][0].lstrip("▫️✅ "))
    run_message, run_data = admin.find_button(texts.BTN_CUSTOM_RUN.split("{")[0].strip())

    task = asyncio.create_task(admin.press_data(run_data, run_message))
    await asyncio.wait_for(blocked.started.wait(), 5)
    try:
        await admin.send(entry)
        state_after = await admin.state()
        busy = await admin.send(texts.MENU_CUSTOM)
    finally:
        blocked.release.set()
        await task

    assert state_after == "CustomFlow:running"
    assert texts.CUSTOM_ALREADY_RUNNING in "\n".join(shown(busy))
    assert blocked.calls == 1


# --------------------------------------------------------------------------- restart
@pytest.fixture
async def persistent() -> AsyncIterator[Callable[[Path], SqliteStorage]]:
    """Factory of SQLite FSM storages; every one is closed even when the test fails."""
    opened: list[SqliteStorage] = []

    def make(path: Path) -> SqliteStorage:
        storage = SqliteStorage(path)
        opened.append(storage)
        return storage

    yield make
    for storage in opened:
        await storage.close()


def _dispatcher(services: Services, storage: SqliteStorage) -> Dispatcher:
    return build_dispatcher(
        frozenset({ADMIN_ID}),
        services,
        storage=storage,
        clock=fixed_clock,
        rate_limit=RateLimitMiddleware(capacity=10_000),
    )


async def test_a_flow_survives_a_restart_with_the_persistent_storage(
    tmp_path: Path,
    bot: Bot,
    session: RecordingSession,
    persistent: Callable[[Path], SqliteStorage],
) -> None:
    services = create_fake_services(clock=fixed_clock)
    await seed_signals(services)
    first = Client(_dispatcher(services, persistent(tmp_path / "fsm.db")), bot, session, ADMIN_ID)
    await first.send(texts.MENU_TEMPLATES)
    await first.send("/ekspress")
    picker = first.last_message_id
    options = (await first.dispatcher.storage.get_data(first.storage_key()))["express"]["options"]
    other = next(i for i, row in enumerate(options) if row[2] != options[0][2])
    await first.press_data(ExpressCb(action="toggle", index=0).pack(), picker)
    await first.dispatcher.storage.close()

    # "restart": brand new dispatcher and storage object over the same file
    second = Client(_dispatcher(services, persistent(tmp_path / "fsm.db")), bot, session, ADMIN_ID)
    methods = await second.press_data(ExpressCb(action="toggle", index=other).pack(), picker)

    assert not any(a.show_alert for a in answers(methods))
    data = await second.dispatcher.storage.get_data(second.storage_key())
    assert len(data["express"]["selected"]) == 2
    await second.dispatcher.storage.close()


@pytest.mark.xfail(
    strict=True,
    reason="QA F-1: the persistent FSM keeps 'running' after a restart although the job died; "
    "🔍 Maxsus tahlil then answers 'already running' for good (nothing clears it but "
    "another flow's reset)",
)
async def test_a_dead_custom_run_does_not_lock_the_menu_after_a_restart(
    tmp_path: Path,
    bot: Bot,
    session: RecordingSession,
    persistent: Callable[[Path], SqliteStorage],
) -> None:
    services = create_fake_services(clock=fixed_clock)
    storage = persistent(tmp_path / "fsm.db")
    first = Client(_dispatcher(services, storage), bot, session, ADMIN_ID)
    await first.send(texts.MENU_CUSTOM)
    await storage.set_state(first.storage_key(), "CustomFlow:running")  # the job died with us
    await storage.close()

    second = Client(_dispatcher(services, persistent(tmp_path / "fsm.db")), bot, session, ADMIN_ID)
    reply = "\n".join(shown(await second.send(texts.MENU_CUSTOM)))

    assert texts.CUSTOM_ALREADY_RUNNING not in reply
    await second.dispatcher.storage.close()


async def test_a_dead_wizard_run_is_recoverable_from_the_menu(
    tmp_path: Path,
    bot: Bot,
    session: RecordingSession,
    persistent: Callable[[Path], SqliteStorage],
) -> None:
    services = create_fake_services(clock=fixed_clock)
    storage = persistent(tmp_path / "fsm.db")
    first = Client(_dispatcher(services, storage), bot, session, ADMIN_ID)
    await to_confirm(first)
    await storage.set_state(first.storage_key(), "AnalysisFlow:running")
    await storage.close()

    second = Client(_dispatcher(services, persistent(tmp_path / "fsm.db")), bot, session, ADMIN_ID)
    message_id, start = first.find_button(texts.BTN_START)
    old = await second.press_data(start, message_id)
    fresh = await second.send(texts.MENU_ANALYSIS)

    assert answers(old)[0].text == texts.ALREADY_RUNNING  # the stale ▶️ is blocked (safe)
    assert "Sana oralig'i" in "\n".join(shown(fresh))
    assert await second.state() == "AnalysisFlow:dates"
    await second.dispatcher.storage.close()


STALE_BUTTONS = {
    "custom": CustomCb(action="toggle", index=0).pack(),
    "custom-run": CustomCb(action="run").pack(),
    "express": ExpressCb(action="toggle", index=0).pack(),
    "express-calc": ExpressCb(action="calc").pack(),
    "watch-pick": WatchCb(action="add", kind="t", key="0").pack(),
    "template-day": TplCb(action="day", id=1, value=2).pack(),
    "template-save": TplCb(action="save", id=1).pack(),
}


@pytest.mark.parametrize("name", sorted(STALE_BUTTONS))
async def test_buttons_of_a_lost_flow_say_they_are_stale_instead_of_acting(
    admin: Client, name: str
) -> None:
    methods = await admin.press_data(STALE_BUTTONS[name], message_id=42)

    (answer,) = answers(methods)
    assert answer.show_alert
    assert "eskirgan" in (answer.text or "")


# --------------------------------------------------------------------------- back / cancel
async def test_cancel_and_close_end_each_new_flow_and_clear_its_state(
    admin: Client, services: Services
) -> None:
    await seed_signals(services)
    await admin.send(texts.MENU_CUSTOM)
    assert (await admin.state()) == "CustomFlow:query"
    await admin.press(texts.BTN_CANCEL)
    assert await admin.state() is None

    await admin.send("/ekspress")
    assert await admin.state() == "ExpressFlow:pick"
    await admin.press(texts.BTN_EXPRESS_CLOSE)
    assert await admin.state() is None

    await admin.send("/kuzat")
    await admin.press(texts.BTN_WATCH_ADD_TEAM)
    assert await admin.state() == "WatchFlow:team_query"
    await admin.press(texts.BTN_WATCH_BACK)
    assert await admin.state() is None

    await admin.send(texts.MENU_REPORT)
    await admin.press(texts.BTN_REPORT_PERIODS["c"].strip("📅 "))
    assert await admin.state() == "ReportFlow:range_input"
    await admin.press(texts.BTN_BACK)
    assert await admin.state() is None


async def test_typing_while_a_flow_waits_for_input_does_not_leak_into_the_text_search(
    admin: Client,
) -> None:
    await admin.send(texts.MENU_REPORT)
    await admin.press(texts.BTN_REPORT_PERIODS["c"].strip("📅 "))

    reply = "\n".join(shown(await admin.send("Arsenal")))

    assert "Arsenal" in reply or reply  # an input error, not a team list
    assert texts.CUSTOM_TEAMS_FOUND.split("{")[0] not in reply
    assert await admin.state() == "ReportFlow:range_input"


# --------------------------------------------------------------------------- concurrency
async def test_simultaneous_express_toggles_are_both_kept(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, services = slow_admin
    await seed_signals(services)
    await admin.send("/ekspress")
    picker = admin.last_message_id
    data = await admin.dispatcher.storage.get_data(admin.storage_key())
    options = data["express"]["options"]
    # two options of different matches, so the early guard lets both through
    first = 0
    second = next(i for i, row in enumerate(options) if row[2] != options[0][2])

    await asyncio.gather(
        admin.press_data(ExpressCb(action="toggle", index=first).pack(), picker),
        admin.press_data(ExpressCb(action="toggle", index=second).pack(), picker),
    )

    stored = (await admin.dispatcher.storage.get_data(admin.storage_key()))["express"]
    assert len(stored["selected"]) == 2


async def test_simultaneous_custom_toggles_are_both_kept(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, _ = slow_admin
    await admin.send(texts.MENU_CUSTOM)
    await admin.press(texts.BTN_CUSTOM_TODAY)
    picker = admin.last_message_id

    await asyncio.gather(
        admin.press_data(CustomCb(action="toggle", index=0).pack(), picker),
        admin.press_data(CustomCb(action="toggle", index=1).pack(), picker),
    )

    stored = (await admin.dispatcher.storage.get_data(admin.storage_key()))["custom"]
    assert len(stored["selected"]) == 2


async def test_double_tap_on_remove_watch_removes_once_and_reports_gone(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, services = slow_admin
    await admin.send("/kuzat Arsenal")
    remove_message, remove = admin.find_button(texts.BTN_WATCH_REMOVE)

    first, second = await asyncio.gather(
        admin.press_data(remove, remove_message), admin.press_data(remove, remove_message)
    )

    assert not await services.watchlist.list_watchlist(ADMIN_ID)
    assert texts.UNEXPECTED_ERROR not in "\n".join(shown(first + second))
    assert len(answers(first)) == len(answers(second)) == 1


async def test_double_tap_on_cashout_prompt_and_unknown_actions_never_crash(admin: Client) -> None:
    for data in (
        BankCb(action="mark", id=0, value="zzz").pack(),
        BankCb(action="mark", id=2**62, value="c").pack(),
        BankCb(action="nope").pack(),
        CardCb(action="nope", signal_id=1).pack(),
        ReportCb(action="show", period="bogus", group="nonsense").pack(),
        ReportCb(action="chart", period="c", group="").pack(),
    ):
        methods = await admin.press_data(data, message_id=77)
        assert methods, data
        assert all(type(m).__name__ in {"AnswerCallbackQuery", "SendMessage"} for m in methods)
        assert texts.UNEXPECTED_ERROR not in "\n".join(shown(methods)), data
