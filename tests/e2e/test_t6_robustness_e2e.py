"""T6 robustness: hostile callback data, corrupted FSM rows, huge inputs, locked databases.

The dispatcher is the production one; the fast tests run over the fake services, the ones that
need the database run over the real services (``Week``).
"""

import contextlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.exc import OperationalError

from app.bot import callbacks, texts
from app.bot.callbacks import CardCb
from app.services.interfaces import DateSpec, Services
from tests.e2e.bot_client import Client, answers
from tests.e2e.t6_world import PERMISSIVE, USER, jobs, open_week, tick

PREFIXES = tuple(
    sorted(
        {
            cls.__prefix__
            for cls in vars(callbacks).values()
            if isinstance(cls, type) and issubclass(cls, CallbackData) and cls is not CallbackData
        }
    )
)
HOSTILE = [
    "",
    "x",
    ":",
    "::::::",
    "sc:bet:",
    "sc:bet:abc",
    "sc:bet:-1",
    "sc:bet:" + "9" * 40,
    "sc:nope:1",
    "sc:bet:1:2:3:4:5",
    "a" * 64,
    "\x00\x00",
    "💥" * 16,
    "ra:d:not-a-date",
    "ra:d:2026-13-45",
    "ln:undo:x:y",
    "rs:mark:1:z:l:0",
    "wd:" + "9" * 30,
    "<b>:</b>",
    "' OR 1=1 --",
]
UNEXPECTED = texts.UNEXPECTED_ERROR


def unexpected(methods: list) -> bool:  # type: ignore[type-arg]
    shown = [m.text for m in methods if isinstance(m, SendMessage | EditMessageText)]
    shown += [a.text or "" for a in answers(methods)]
    return any(UNEXPECTED in text for text in shown)


@pytest.mark.parametrize("data", HOSTILE)
async def test_hostile_callback_data_is_answered_and_never_an_unexpected_error(
    admin: Client, data: str
) -> None:
    await admin.send("/start")
    methods = await admin.press_data(data, 5)
    assert any(isinstance(m, AnswerCallbackQuery) for m in methods) or methods == []
    assert not unexpected(methods), data


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("tail", ["", ":", ":0", ":-5", ":x:y:z", ":" + "7" * 25, ":\x00", ":💥"])
async def test_every_callback_family_survives_garbage_fields(
    admin: Client, prefix: str, tail: str
) -> None:
    methods = await admin.press_data(prefix + tail, 7)
    assert not unexpected(methods), (prefix, tail)


@settings(max_examples=150)
@given(text=st.text(max_size=64).filter(lambda t: len(t.encode()) <= 64))
async def test_any_callback_text_gets_a_graceful_answer(text: str) -> None:
    from aiogram import Bot  # noqa: PLC0415

    from tests.bot.fakes import ADMIN_ID, RecordingSession  # noqa: PLC0415
    from tests.e2e.bot_client import make_dispatcher  # noqa: PLC0415

    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)
    client = Client(make_dispatcher(frozenset({ADMIN_ID})), bot, session, ADMIN_ID)
    methods = await client.press_data(text, 9) if text else []
    assert not unexpected(methods)


@pytest.mark.parametrize(
    "data", [CardCb(action="hide", signal_id=1).pack(), "wd:today", "ra:d:2026-10-21"]
)
async def test_a_tap_on_a_message_telegram_no_longer_shows_is_answered(
    admin: Client, data: str
) -> None:
    """Very old messages arrive as ``InaccessibleMessage`` (or none at all)."""
    from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Update  # noqa: PLC0415

    from tests.bot.fakes import feed  # noqa: PLC0415

    # (a callback without any message is an inline-mode update: dropped by design, decision #4)
    for message in (
        InaccessibleMessage(chat=Chat(id=admin.user_id, type="private"), message_id=3),
    ):
        update_id = 880_000 + len(admin.texts)
        callback = CallbackQuery(
            id=f"old{update_id}",
            from_user=admin._user(),
            chat_instance="ci",
            message=message,
            data=data,
        )
        update = Update.model_validate(
            Update(update_id=update_id, callback_query=callback).model_dump(),
            context={"bot": admin.bot},
        )
        before = len(admin.session.requests)
        await feed(admin.dispatcher, admin.bot, update)
        methods = admin.session.requests[before:]
        assert any(isinstance(m, AnswerCallbackQuery) for m in methods)
        assert not unexpected(methods)


# --------------------------------------------------------------------------- huge inputs
HUGE = [
    "x" * 5_000,
    "Arsenal, " * 3_000,
    "\n".join(f"line {i}" for i in range(10_000)),
    "💥" * 4_000,
    "<b>" * 2_000,
    "%" * 5_000,
    "9" * 5_000,
    "25.10-31.10 " + "futbol " * 2_000,
]


@pytest.mark.parametrize("text", HUGE, ids=[f"huge{i}" for i in range(len(HUGE))])
async def test_huge_text_in_every_free_text_entry_is_refused_politely(
    admin: Client, text: str
) -> None:
    for entry in ("/tahlil ", "/kuzat ", "/qayta ", "", "/natijalar ", "/bank ", "/hisobot "):
        methods = await admin.send(entry + text)
        assert not unexpected(methods), (entry, text[:20])
        for method in methods:
            if isinstance(method, SendMessage | EditMessageText):
                assert len(method.text) <= 4096


async def test_huge_text_into_the_wizard_prompts_and_the_note_prompt(admin: Client) -> None:
    from tests.e2e.flows import reach  # noqa: PLC0415

    for screen in ("probability_input", "ev_input", "count_input", "template_name", "teams"):
        await reach(admin, screen)
        methods = await admin.send("z" * 6_000)
        assert not unexpected(methods), screen
        await admin.send("/bekor")


async def test_ten_thousand_ids_in_one_service_call_do_not_hang_or_crash(
    services: Services,
) -> None:
    ids = list(range(1, 10_001))
    got = await services.signals.get_signals(1001, ids)
    assert len(got) <= 10_000


# --------------------------------------------------------------------------- corrupted FSM
def corrupt(path: Path, state: str | None, data: str) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE fsm SET state = ?, data = ?", (state, data))


@pytest.mark.parametrize(
    ("state", "data"),
    [
        ("AnalysisFlow:nonexistent", "{}"),
        ("garbage", "{}"),
        ("AnalysisFlow:confirm", "{not json"),
        ("AnalysisFlow:confirm", "[1, 2, 3]"),
        ("AnalysisFlow:confirm", json.dumps({"draft": "not-a-dict"})),
        ("AnalysisFlow:confirm", json.dumps({"draft": {"date_mode": 12345, "probability": "x"}})),
        ("CustomFlow:running", "{}"),
        (None, "null"),
        ("AnalysisFlow:fixtures", json.dumps({"fixture_ids": "oops", "page": "NaN"})),
    ],
)
async def test_a_corrupted_wizard_row_is_survived_and_the_user_can_start_over(  # noqa: PLR0913, PLR0917
    history_template,
    tmp_path,
    bot,
    session,
    state,
    data,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, bot, session) as w:
        client = w.client(USER)
        await client.send(texts.MENU_ANALYSIS)  # creates the FSM row
        await w.storage.close()  # flush, then damage the file like a bad disk would
        corrupt(tmp_path / "fsm.db", state, data)
        for entry in ("/start", texts.MENU_ANALYSIS, "/bekor", "/holat", "hello"):
            methods = await client.send(entry)
            assert not unexpected(methods), (state, data, entry)
            assert any(isinstance(m, SendMessage | EditMessageText) for m in methods), entry
        # a tap on the old keyboard of the damaged flow is answered, not ignored
        stale = await client.press_data("wx:edit", client.last_message_id)
        assert not unexpected(stale)


# --------------------------------------------------------------------------- locked databases
async def test_a_locked_database_fails_the_job_cleanly_and_the_next_tick_recovers(
    history_template,
    tmp_path,
    bot,
    session,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, bot, session) as w:
        await w.settings(USER, **PERMISSIVE)
        await jobs(w).run("refresh")
        reborn = await w.restart(busy_timeout_ms=200)  # do not wait 5 s per statement
        blocker = sqlite3.connect(tmp_path / "week.db", timeout=0.1, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")  # another process holds the write lock
            with contextlib.suppress(sqlite3.OperationalError, OperationalError):
                await tick(reborn, 30)  # see the xfail below: today the error escapes the wrapper
            assert [m for m in reborn.pushed(USER) if m.text.startswith("☀️")] == []
            bet_attempt = await reborn.client(USER).send("/natijalar")
            assert not [
                m for m in bet_attempt if isinstance(m, SendMessage) and "Traceback" in m.text
            ]
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        # the lock is gone: the ledger is not stuck, the next ticks deliver exactly one digest
        await tick(reborn, 60 * 4)
        digests = [m for m in reborn.pushed(USER) if m.text.startswith("☀️")]
        assert len(digests) == 1
        views = await reborn.services.signals.list_signals(USER, DateSpec.next_days(3))
        assert len(views) == 10


async def test_a_locked_database_during_a_bet_tap_gives_a_message_and_a_retry_works(
    history_template,
    tmp_path,
    bot,
    session,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, bot, session) as w:
        await w.settings(USER, **PERMISSIVE)
        await jobs(w).run("refresh")
        await tick(w, 30)
        w.sync(USER)
        reborn = await w.restart(busy_timeout_ms=200)
        client = reborn.client(USER)
        card_id, bet_data = client.find_button(texts.BTN_BET)
        await client.press_data(bet_data, card_id)
        confirm_id, confirm_data = client.find_button("Yozish")
        blocker = sqlite3.connect(tmp_path / "week.db", timeout=0.1, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            locked = await client.press_data(confirm_data, confirm_id)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        assert answers(locked)  # the spinner stops ...
        seen = [m.text for m in locked if isinstance(m, SendMessage | EditMessageText)]
        seen += [a.text or "" for a in answers(locked)]
        assert any("xatolik" in t or "Kutilmagan" in t or "qayta urinib" in t for t in seen), seen
        assert await reborn.services.bets.list_bets(USER) == ()  # nothing half-written
        await client.press_data(confirm_data, confirm_id)  # the user taps again
        assert len(await reborn.services.bets.list_bets(USER)) == 1


async def test_a_locked_fsm_database_does_not_kill_the_bot(
    history_template,
    tmp_path,
    bot,
    session,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, bot, session) as w:
        client = w.client(USER)
        await client.send("/start")
        blocker = sqlite3.connect(tmp_path / "fsm.db", timeout=0.1, isolation_level=None)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            outcome: object
            try:
                outcome = await client.send(texts.MENU_ANALYSIS)
            except Exception as error:
                outcome = error
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        # whatever happened while locked, afterwards the same user is served normally
        again = await client.send(texts.MENU_ANALYSIS)
        assert any(isinstance(m, SendMessage | EditMessageText) for m in again), outcome


@pytest.mark.xfail(
    strict=True,
    reason="T6 finding: PipelineJobs.run writes its failure record with the same locked "
    "database, so the OperationalError escapes the wrapper that promises to never raise "
    "(src/app/scheduler.py PipelineJobs.run -> ledger.record_run/record_error/alert_throttled)",
)
async def test_a_job_wrapper_never_raises_even_when_the_database_is_locked(
    history_template,
    tmp_path,
    bot,
    session,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, bot, session) as w:
        await w.settings(USER, **PERMISSIVE)
        reborn = await w.restart(busy_timeout_ms=100)
        blocker = sqlite3.connect(tmp_path / "week.db", timeout=0.1, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            await jobs(reborn).run("daily-tick")
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
