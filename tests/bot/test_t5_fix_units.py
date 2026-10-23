"""Unit tests of the T5 bot fix round: paging, callbacks, money, edits, storage recovery, CSV."""

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, Message
from hypothesis import given
from hypothesis import strategies as st

from app.bot import callbacks, texts
from app.bot.formatters import money
from app.bot.messaging import edit_html
from app.bot.paging import PAGE_SIZE, pager_row, paginate
from app.bot.recovery import recover_running_analyses
from app.bot.storage import FSM_DB_NAME, SqliteStorage
from app.bot.watch_ui import entity_key, find_item
from app.domain import Sport
from app.services.errors import ValidationFailedError
from app.services.interfaces import (
    ResultMark,
    ResultMarkRequest,
    WatchItem,
    WatchKind,
    WatchTarget,
)
from app.services.report_math import CSV_ENCODING, bank_totals, export_file, safe_cell
from tests.bot.fakes import RecordingSession

BIG = 2**63 - 1


# --------------------------------------------------------------------------- paging
@given(st.lists(st.integers(), max_size=200), st.integers(min_value=-5, max_value=40))
def test_pages_cover_every_item_exactly_once(items: list[int], page: int) -> None:
    count = paginate(items, 0).count
    pages = [paginate(items, number) for number in range(count)]

    assert [item for p in pages for item in p.items] == items  # nothing lost, nothing repeated
    assert all(len(p.items) <= PAGE_SIZE for p in pages)
    clamped = paginate(items, page)
    assert 0 <= clamped.number < max(clamped.count, 1)
    assert clamped.items == pages[clamped.number].items


def test_an_empty_list_has_one_empty_page_and_no_pager() -> None:
    page = paginate([], 3)

    assert (page.items, page.number, page.count) == ((), 0, 1)
    assert pager_row(page, lambda n: callbacks.LearnCb(action="list", page=n)) == []


def test_the_pager_leads_to_the_neighbouring_pages_only() -> None:
    page = paginate(list(range(25)), 1)  # 3 pages, the middle one

    row = pager_row(page, lambda n: callbacks.LearnCb(action="list", page=n))

    assert [b.text for b in row] == ["◀️", "2/3", "▶️"]
    assert [b.callback_data for b in row] == [
        callbacks.LearnCb(action="list", page=n).pack() for n in (0, 1, 2)
    ]


# --------------------------------------------------------------------------- callback size
@given(
    action=st.sampled_from(["mark", "cash", "note", "list", "card"]),
    value=st.sampled_from(["w", "l", "v", "c", ""]),
    origin=st.sampled_from(["l", ""]),
    signal_id=st.integers(min_value=0, max_value=BIG),
    page=st.integers(min_value=0, max_value=BIG),
)
def test_result_callbacks_fit_telegrams_limit(
    action: str, value: str, origin: str, signal_id: int, page: int
) -> None:
    packed = callbacks.ResultCb(
        action=action, signal_id=signal_id, value=value, page=page, origin=origin
    ).pack()

    assert len(packed.encode()) <= 64
    assert callbacks.ResultCb.unpack(packed).signal_id == signal_id


@given(
    action=st.sampled_from(["list", "ask", "undo"]),
    change_id=st.integers(min_value=0, max_value=BIG),
    page=st.integers(min_value=0, max_value=BIG),
)
def test_learning_callbacks_fit_telegrams_limit(action: str, change_id: int, page: int) -> None:
    assert len(callbacks.LearnCb(action=action, id=change_id, page=page).pack().encode()) <= 64


@given(
    entity=st.text(max_size=40),
    page=st.integers(min_value=0, max_value=BIG),
    flag=st.sampled_from(["l", "o", "r", ""]),
)
def test_watch_callbacks_with_a_page_fit_telegrams_limit(entity: str, page: int, flag: str) -> None:
    packed = callbacks.WatchCb(
        action="notify", kind="t", key=entity_key(entity), flag=flag, page=page
    ).pack()

    assert len(packed.encode()) <= 64


@given(st.integers(min_value=0, max_value=BIG), st.integers(min_value=0, max_value=BIG))
def test_template_callbacks_with_a_page_fit_telegrams_limit(template_id: int, page: int) -> None:
    for action in ("open", "delete", "delete_yes", "list"):
        assert len(callbacks.TplCb(action=action, id=template_id, value=page).pack().encode()) <= 64


@given(st.text(max_size=200), st.text(max_size=200))
def test_watch_keys_of_different_ids_differ(first: str, second: str) -> None:
    if first != second:
        assert entity_key(first) != entity_key(second)  # 48 bits: no accidental collisions here


def test_a_digest_that_still_matches_two_entries_is_treated_as_unknown() -> None:
    def item(entity: str) -> WatchItem:
        return WatchItem(
            target=WatchTarget(kind=WatchKind.TEAM, entity_id=entity),
            name=entity,
            sport=Sport.FOOTBALL,
            notifications=frozenset(),
        )

    twins = [item("team:1"), item("team:1")]  # defensive: a duplicate must not pick "the first"

    assert find_item(twins, "t", entity_key("team:1")) is None
    assert find_item([item("team:1")], "t", entity_key("team:1")) is not None


# --------------------------------------------------------------------------- money
@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("-0.004"), "0"),
        (Decimal("0.004"), "0"),
        (Decimal("-0.006"), "-0.01"),
        (Decimal("-0.00"), "0"),
        (Decimal("1234567.5"), "1 234 567.50"),
        (Decimal("-50"), "-50"),
        (Decimal("100"), "100"),
    ],
)
def test_money_never_prints_minus_zero(value: Decimal, text: str) -> None:
    assert money(value) == text


@given(st.decimals(min_value=Decimal("-1e9"), max_value=Decimal("1e9"), places=6))
def test_money_text_never_starts_with_a_minus_for_a_zero_amount(value: Decimal) -> None:
    rendered = money(value)

    assert not rendered.startswith("-0") or rendered.startswith("-0.")
    assert rendered != "-0"


# --------------------------------------------------------------------------- result requests
def test_cashout_by_units_or_by_payout_but_not_both_or_neither() -> None:
    assert ResultMarkRequest(
        signal_id=1, mark=ResultMark.CASHOUT, cashout_profit_units=Decimal("0.8")
    )
    assert ResultMarkRequest(
        signal_id=1, mark=ResultMark.CASHOUT, cashout_bet_id=5, cashout_payout=Decimal(200)
    )
    for bad in (
        {"cashout_profit_units": Decimal(1), "cashout_bet_id": 5, "cashout_payout": Decimal(2)},
        {},  # a cash-out with no amount at all
        {"cashout_bet_id": 5},  # a bet without a payout
        {"cashout_payout": Decimal(5)},  # a payout without its bet
        {"cashout_bet_id": 5, "cashout_payout": Decimal(-1)},
        {"cashout_bet_id": 5, "cashout_payout": Decimal("NaN")},
        {"cashout_bet_id": 5, "cashout_payout": Decimal("Infinity")},
    ):
        with pytest.raises(ValidationFailedError):
            ResultMarkRequest(signal_id=1, mark=ResultMark.CASHOUT, **bad)  # type: ignore[arg-type]


def test_a_payout_of_zero_is_a_legal_total_loss_and_other_marks_take_no_amount() -> None:
    assert ResultMarkRequest(
        signal_id=1, mark=ResultMark.CASHOUT, cashout_bet_id=5, cashout_payout=Decimal(0)
    )
    with pytest.raises(ValidationFailedError):
        ResultMarkRequest(
            signal_id=1, mark=ResultMark.WON, cashout_bet_id=5, cashout_payout=Decimal(1)
        )
    with pytest.raises(ValidationFailedError):
        ResultMarkRequest(signal_id=1, mark=ResultMark.HALF_WON)  # not a manual mark


@given(
    st.lists(
        st.tuples(
            st.sampled_from([None, *ResultMark]),
            st.decimals(min_value=1, max_value=1000, places=2),
            st.decimals(min_value=-1000, max_value=1000, places=2),
        ),
        max_size=60,
    )
)
def test_bank_totals_add_up(rows: list[tuple[ResultMark | None, Decimal, Decimal]]) -> None:
    totals = bank_totals(rows)

    settled = [row for row in rows if row[0] is not None]
    assert totals.settled == len(settled)
    assert totals.won + totals.lost + totals.void + totals.cashout <= totals.settled
    assert totals.profit == sum((profit for _, _, profit in settled), Decimal(0))
    assert totals.staked == sum(
        (stake for mark, stake, _ in settled if mark is not ResultMark.VOID), Decimal(0)
    )


# --------------------------------------------------------------------------- CSV safety
HOSTILE_CELLS = [
    '=HYPERLINK("http://evil","x")',
    "+cmd|' /C calc'!A0",
    "-2+3",
    "@SUM(1+1)",
    "\t=1+1",
    "\r=1+1",
]


@pytest.mark.parametrize("cell", HOSTILE_CELLS)
def test_formula_cells_are_neutralised(cell: str) -> None:
    safe = safe_cell(cell)

    assert safe.startswith("'")
    assert safe[1:] == cell


@given(st.text(max_size=40).filter(lambda t: not t.startswith(("=", "+", "-", "@", "\t", "\r"))))
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert safe_cell(text) == text


@given(st.text(max_size=40))
def test_a_safe_cell_never_starts_with_a_formula_character(text: str) -> None:
    assert not safe_cell(text).startswith(("=", "+", "-", "@", "\t", "\r"))


def test_the_export_has_a_bom_and_hostile_names_survive_a_csv_round_trip() -> None:
    from app.services.interfaces import RiskProfile  # noqa: PLC0415
    from app.services.report_math import SignalOutcome  # noqa: PLC0415
    from tests.bot.samples import make_signal as sample  # noqa: PLC0415

    view = sample(league='=HYPERLINK("x")', home_team="@home", away_team="+away")
    file = export_file(
        [
            SignalOutcome(
                view=view, profit_units=Decimal("-50"), clv=None, risk_profile=RiskProfile.BALANCED
            )
        ]
    )

    assert file.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM for Excel
    rows = list(csv.reader(io.StringIO(file.content.decode(CSV_ENCODING))))
    header, row = rows
    cells = dict(zip(header, row, strict=True))
    assert cells["league"] == '\'=HYPERLINK("x")'
    assert (cells["home"], cells["away"]) == ("'@home", "'+away")
    assert cells["profit_units"] == "-50"  # numbers are never touched


# --------------------------------------------------------------------------- edit_html
def _message(bot: Bot) -> Message:
    return Message(
        message_id=5, date=datetime.now(UTC), chat=Chat(id=1, type="private"), text="x"
    ).as_(bot)


async def test_a_long_edit_is_never_silently_cut(bot: Bot, session: RecordingSession) -> None:
    text = "satr\n" * 3000  # 15000 characters

    await edit_html(_message(bot), text)

    edits = [m for m in session.requests if isinstance(m, EditMessageText)]
    sent = [m for m in session.requests if isinstance(m, SendMessage)]
    assert len(edits) == 1
    assert len(edits) + len(sent) >= 4
    rebuilt = "".join(m.text for m in [*edits, *sent])
    assert rebuilt.replace("\n", "") == text.replace("\n", "")  # every character arrived
    assert all(len(m.text) <= 4096 for m in [*edits, *sent])


async def test_the_keyboard_goes_on_the_last_part_of_a_long_edit(
    bot: Bot, session: RecordingSession
) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: PLC0415

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="a", callback_data="b")]]
    )

    await edit_html(_message(bot), "satr\n" * 3000, markup)

    edits = [m for m in session.requests if isinstance(m, EditMessageText)]
    sent = [m for m in session.requests if isinstance(m, SendMessage)]
    assert edits[0].reply_markup is None
    assert sent[-1].reply_markup == markup
    assert all(m.reply_markup is None for m in sent[:-1])


async def test_a_short_edit_is_a_single_edit_with_the_keyboard(
    bot: Bot, session: RecordingSession
) -> None:
    await edit_html(_message(bot), "qisqa")

    assert [type(m) for m in session.requests] == [EditMessageText]


# --------------------------------------------------------------------------- restart recovery
def _key(chat: int, user: int) -> StorageKey:
    return StorageKey(bot_id=7, chat_id=chat, user_id=user)


async def test_opening_the_storage_clears_dead_running_states_and_reports_the_chats(
    tmp_path: Path,
) -> None:
    path = tmp_path / FSM_DB_NAME
    before = SqliteStorage(path)
    await before.set_state(_key(10, 10), "CustomFlow:running")
    await before.set_data(_key(10, 10), {"keep": 1})
    await before.set_state(_key(11, 11), "AnalysisFlow:running")
    await before.set_state(_key(12, 12), "AnalysisFlow:confirm")  # a wizard step survives
    await before.close()

    after = SqliteStorage(path)
    try:
        assert await after.get_state(_key(10, 10)) is None
        assert await after.get_data(_key(10, 10)) == {"keep": 1}  # data stays, only the guard goes
        assert await after.get_state(_key(11, 11)) is None
        assert await after.get_state(_key(12, 12)) == "AnalysisFlow:confirm"
        assert sorted(await after.take_recovered()) == [10, 11]
        assert await after.take_recovered() == []  # told once
    finally:
        await after.close()


async def test_a_running_state_set_after_opening_is_not_cleared_by_the_same_process(
    tmp_path: Path,
) -> None:
    store = SqliteStorage(tmp_path / FSM_DB_NAME)
    try:
        await store.set_state(_key(10, 10), "CustomFlow:running")  # opens, then sets
        assert await store.get_state(_key(10, 10)) == "CustomFlow:running"
        assert await store.take_recovered() == []
    finally:
        await store.close()


async def test_users_with_a_dead_run_are_told_and_a_blocked_user_does_not_stop_the_rest(
    tmp_path: Path, bot: Bot, session: RecordingSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / FSM_DB_NAME
    first = SqliteStorage(path)
    for chat in (10, 11, 12):
        await first.set_state(_key(chat, chat), "CustomFlow:running")
    await first.close()
    storage = SqliteStorage(path)
    original = Bot.send_message

    async def flaky(self: Bot, chat_id: int | str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if chat_id == 11:
            raise TelegramForbiddenError(SendMessage(chat_id=11, text="x"), "blocked")
        return await original(self, chat_id, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Bot, "send_message", flaky)
    try:
        told = await recover_running_analyses(bot, storage)
    finally:
        await storage.close()

    assert told == 2
    assert sorted(m.chat_id for m in session.requests if isinstance(m, SendMessage)) == [10, 12]
    assert all(
        m.text == texts.RESTART_RECOVERED for m in session.requests if isinstance(m, SendMessage)
    )


async def test_in_memory_storage_needs_no_recovery(bot: Bot) -> None:
    from aiogram.fsm.storage.memory import MemoryStorage  # noqa: PLC0415

    assert await recover_running_analyses(bot, MemoryStorage()) == 0


async def test_a_first_start_creates_no_fsm_file_just_to_look_for_dead_runs(
    tmp_path: Path,
) -> None:
    store = SqliteStorage(tmp_path / FSM_DB_NAME)

    assert await store.take_recovered() == []
    assert not (tmp_path / FSM_DB_NAME).exists()
