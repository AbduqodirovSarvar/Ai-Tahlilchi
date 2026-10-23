"""F8.1 through the real dispatcher over the REAL services: marking results by hand.

Covers the card buttons, ``/natijalar`` (one-tap marking), cash-out, notes, the precedence of
a manual mark over the API result, per-user isolation of every new callback, stale buttons and
hostile text.
"""

from decimal import Decimal

import pytest

from app.bot import texts
from app.bot.callbacks import CardCb, ResultCb
from app.bot.formatters import escape
from app.bot.states import ResultFlow
from app.db.repo import list_signal_notes
from app.services.errors import NotFoundError
from app.services.interfaces import MAX_NOTE_LENGTH, ResultMark
from tests.e2e.bot_client import Client, answers, buttons_of, shown, telegram_html_problems
from tests.e2e.real_world import ADMIN, SECOND, RealBot

pytestmark = pytest.mark.slow

LIST_MESSAGE = 70


def _data(admin: Client, label: str, message_id: int) -> str:
    """The callback data of the button of message ``message_id`` whose text contains ``label``."""
    for button in buttons_of(admin.markups.get(message_id)):
        if label in button.text and button.callback_data:
            return str(button.callback_data)
    msg = f"no {label!r} button on message {message_id}"
    raise AssertionError(msg)


async def _pending_list(admin: Client) -> int:
    """Send ``/natijalar``; returns the message id of the list."""
    await admin.send("/natijalar")
    return admin.last_message_id


# --------------------------------------------------------------------------- the list
async def test_natijalar_lists_only_signals_whose_match_is_over(real_bot: RealBot) -> None:
    rb = real_bot
    await rb.signals()
    admin = rb.client()

    before = shown(await admin.send("/natijalar"))
    rb.finish_matches()
    after = shown(await admin.send("/natijalar"))

    assert before == [texts.RESULTS_PENDING_EMPTY]
    assert "kutilayotgan" in after[0]
    assert all(telegram_html_problems(text) == [] for text in after)


async def test_one_tap_marks_the_signal_and_the_list_shrinks(real_bot: RealBot) -> None:
    rb = real_bot
    first, second = (await rb.signals(distinct=2))[:2]
    bet = await rb.bet(first, 10, odds=2.0)
    rb.finish_matches()
    admin = rb.client()
    message_id = await _pending_list(admin)
    listing = await rb.services.results.pending_results(ADMIN)
    assert {first.signal_id, second.signal_id} <= {s.signal_id for s in listing}

    mark = ResultCb(action="mark", signal_id=first.signal_id, value="w", origin="l", page=0).pack()
    methods = await admin.press_data(mark, message_id)

    assert texts.RESULT_MARKED.format(result=texts.RESULT_LABELS[ResultMark.WON]) in "".join(
        shown(methods)
    )
    pending_ids = {s.signal_id for s in await rb.services.results.pending_results(ADMIN)}
    assert first.signal_id not in pending_ids
    assert second.signal_id in pending_ids
    settled = await rb.services.bets.get_bet(ADMIN, bet.bet_id)
    assert settled.outcome is ResultMark.WON
    assert settled.profit == Decimal(10)  # the user's bet follows the signal's mark


@pytest.mark.parametrize(
    ("code", "mark", "profit"),
    [
        ("w", ResultMark.WON, Decimal(10)),
        ("l", ResultMark.LOST, Decimal(-10)),
        ("v", ResultMark.VOID, Decimal(0)),
    ],
)
async def test_each_mark_button_sets_its_result(
    real_bot: RealBot, code: str, mark: ResultMark, profit: Decimal
) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    bet = await rb.bet(signal, 10, odds=2.0)
    rb.finish_matches()
    admin = rb.client()
    message_id = await _pending_list(admin)

    await admin.press_data(
        ResultCb(action="mark", signal_id=signal.signal_id, value=code, origin="l").pack(),
        message_id,
    )

    assert (await rb.services.signals.get_signal(ADMIN, signal.signal_id)).result is mark
    assert (await rb.services.bets.get_bet(ADMIN, bet.bet_id)).profit == profit


async def test_the_list_is_paged_and_every_signal_is_reachable_with_short_callbacks(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    signals = await rb.signals(distinct=7)  # more than one page
    rb.finish_matches()
    pending = await rb.services.results.pending_results(ADMIN)
    admin = rb.client()
    message_id = await _pending_list(admin)

    reachable: set[int] = set()
    for _ in range(20):
        buttons = admin.live_buttons(message_id)
        for _, data in buttons:
            assert len(data.encode()) <= 64
        reachable |= {int(d.split(":")[2]) for _, d in buttons if d.startswith("rs:mark")}
        forward = [d for t, d in buttons if t == "▶️"]
        if not forward:
            break
        await admin.press_data(forward[0], message_id)

    assert reachable == {s.signal_id for s in pending}
    assert len(pending) >= len(signals)


# --------------------------------------------------------------------------- card buttons
async def test_a_started_card_carries_the_four_marks_and_a_note_button(real_bot: RealBot) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    admin = rb.client()

    await admin.press_data(CardCb(action="card", signal_id=signal.signal_id).pack(), 80)
    before = [d for _, d in admin.live_buttons(80)]
    rb.finish_matches()
    await admin.press_data(CardCb(action="card", signal_id=signal.signal_id).pack(), 81)
    after = [d for _, d in admin.live_buttons(81)]

    assert not any(d.startswith("rs:") for d in before)  # a future match has no result buttons
    assert {d.split(":")[1] for d in after if d.startswith("rs:")} == {"mark", "cash", "note"}
    assert len([d for d in after if d.startswith("rs:mark")]) == 3


async def test_marking_from_the_card_redraws_the_card_with_the_result(real_bot: RealBot) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    rb.finish_matches()
    admin = rb.client()
    await admin.press_data(CardCb(action="card", signal_id=signal.signal_id).pack(), 82)

    methods = await admin.press_data(_data(admin, "O'tmadi", 82), 82)

    text = "".join(shown(methods))
    assert texts.RESULT_LABELS[ResultMark.LOST] in text
    assert (await rb.services.signals.get_signal(ADMIN, signal.signal_id)).result is (
        ResultMark.LOST
    )
    # a settled card keeps the buttons: the mark can be corrected again
    assert any(d.startswith("rs:mark") for _, d in admin.live_buttons(82))


async def test_a_manual_mark_beats_the_api_result_and_survives_the_next_settlement(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    rb.finish_matches()
    await rb.env.feed(rb.env.clock())  # the API results arrive and settle the signal
    api = (await rb.services.signals.get_signal(ADMIN, signal.signal_id)).result
    assert api is not None
    wrong = ResultMark.LOST if api is ResultMark.WON else ResultMark.WON
    admin = rb.client()
    await admin.press_data(CardCb(action="card", signal_id=signal.signal_id).pack(), 83)
    code = "w" if wrong is ResultMark.WON else "l"

    await admin.press_data(
        ResultCb(action="mark", signal_id=signal.signal_id, value=code).pack(), 83
    )
    await rb.env.feed(rb.env.clock())  # another settlement run must not undo the person's mark

    assert (await rb.services.signals.get_signal(ADMIN, signal.signal_id)).result is wrong


# --------------------------------------------------------------------------- cash-out
async def test_cashout_asks_for_units_validates_and_marks(real_bot: RealBot) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    rb.finish_matches()
    admin = rb.client()
    message_id = await _pending_list(admin)

    prompt = shown(
        await admin.press_data(
            ResultCb(action="cash", signal_id=signal.signal_id, origin="l").pack(), message_id
        )
    )
    assert "Cashout" in prompt[0]
    assert await admin.state() == ResultFlow.cashout_input.state
    assert "abc" in shown(await admin.send("abc"))[0]  # not a number: asked again
    assert shown(await admin.send("500")) == [texts.RESULT_CASHOUT_RANGE]
    assert await admin.state() == ResultFlow.cashout_input.state

    methods = await admin.send("0.8")

    assert await admin.state() is None
    marked = await rb.services.signals.get_signal(ADMIN, signal.signal_id)
    assert marked.result is ResultMark.CASHOUT
    assert texts.RESULT_MARKED.format(result=texts.RESULT_LABELS[ResultMark.CASHOUT]) in "".join(
        shown(methods)
    )


async def test_cancelling_the_cashout_input_forgets_it(real_bot: RealBot) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    rb.finish_matches()
    admin = rb.client()
    message_id = await _pending_list(admin)
    await admin.press_data(
        ResultCb(action="cash", signal_id=signal.signal_id, origin="l").pack(), message_id
    )

    await admin.send("/bekor")
    await admin.send("0.8")  # a late line is nobody's cash-out any more

    assert await admin.state() is None
    assert (await rb.services.signals.get_signal(ADMIN, signal.signal_id)).result is None


# --------------------------------------------------------------------------- notes
async def test_a_note_is_stored_plain_and_shown_escaped(real_bot: RealBot) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    admin = rb.client()
    await admin.press_data(CardCb(action="card", signal_id=signal.signal_id).pack(), 84)
    hostile = "<b>asosiy</b> hujumchi & <script>alert(1)</script> yo'q"

    await admin.press_data(ResultCb(action="note", signal_id=signal.signal_id).pack(), 84)
    assert await admin.state() == ResultFlow.note_input.state
    methods = await admin.send(hostile)

    async with rb.env.sessions() as session:
        notes = await list_signal_notes(session, ADMIN, signal.signal_id)
    assert [n.text for n in notes] == [hostile]  # stored as typed
    reply = "".join(shown(methods))
    assert escape(hostile) in reply
    assert "<script>" not in reply
    assert telegram_html_problems(reply) == []
    assert await admin.state() is None


@pytest.mark.parametrize("typed", ["   ", "x" * (MAX_NOTE_LENGTH + 1)])
async def test_a_bad_note_is_refused_and_the_question_stays_open(
    real_bot: RealBot, typed: str
) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    admin = rb.client()
    await admin.press_data(ResultCb(action="note", signal_id=signal.signal_id).pack(), 85)

    reply = shown(await admin.send(typed))

    assert reply
    assert await admin.state() == ResultFlow.note_input.state
    async with rb.env.sessions() as session:
        assert await list_signal_notes(session, ADMIN, signal.signal_id) == []


async def test_a_note_of_exactly_the_limit_is_accepted(real_bot: RealBot) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    admin = rb.client()
    await admin.press_data(ResultCb(action="note", signal_id=signal.signal_id).pack(), 86)

    await admin.send("y" * MAX_NOTE_LENGTH)

    async with rb.env.sessions() as session:
        assert len(await list_signal_notes(session, ADMIN, signal.signal_id)) == 1


# --------------------------------------------------------------------------- isolation, stale
async def test_forged_result_callbacks_of_another_user_change_nothing(real_bot: RealBot) -> None:
    rb = real_bot
    signal = (await rb.signals(user_id=ADMIN))[0]
    rb.finish_matches()
    intruder = rb.client(SECOND)
    forged = [
        ResultCb(action="mark", signal_id=signal.signal_id, value="w", origin="l"),
        ResultCb(action="mark", signal_id=signal.signal_id, value="l"),
        ResultCb(action="cash", signal_id=signal.signal_id),
        ResultCb(action="note", signal_id=signal.signal_id),
    ]

    for callback in forged:
        await intruder.press_data(callback.pack(), 90)

    assert await intruder.state() is None  # no input state was opened for a foreign signal
    assert (await rb.services.signals.get_signal(ADMIN, signal.signal_id)).result is None
    with pytest.raises(NotFoundError):
        await rb.services.signals.get_signal(SECOND, signal.signal_id)
    shown_to_intruder = shown(await intruder.send("/natijalar"))
    assert shown_to_intruder == [texts.RESULTS_PENDING_EMPTY]


async def test_a_typed_cashout_after_a_forged_press_marks_nothing(real_bot: RealBot) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    rb.finish_matches()
    intruder = rb.client(SECOND)
    await intruder.press_data(ResultCb(action="cash", signal_id=signal.signal_id).pack(), 91)

    await intruder.send("0.8")

    assert (await rb.services.signals.get_signal(ADMIN, signal.signal_id)).result is None


async def test_a_mark_button_before_the_match_is_refused_with_the_reason(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    admin = rb.client()

    methods = await admin.press_data(
        ResultCb(action="mark", signal_id=signal.signal_id, value="w").pack(), 92
    )

    assert [a.text for a in answers(methods)] == [texts.RESULT_MARK_NOT_STARTED]
    assert (await rb.services.signals.get_signal(ADMIN, signal.signal_id)).result is None


async def test_buttons_of_vanished_or_unknown_signals_and_codes_do_not_crash(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    admin = rb.client()

    unknown_signal = await admin.press_data(
        ResultCb(action="mark", signal_id=424_242, value="w").pack(), 93
    )
    unknown_code = await admin.press_data(
        ResultCb(action="mark", signal_id=1, value="zz").pack(), 93
    )
    stale_page = await admin.press_data(ResultCb(action="list", page=99).pack(), 93)

    assert shown(unknown_signal) or answers(unknown_signal)
    assert shown(unknown_code) or answers(unknown_code)
    assert shown(stale_page) == [texts.RESULTS_PENDING_EMPTY]


async def test_pressing_the_cashout_button_while_an_analysis_runs_does_not_clobber_the_guard(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    admin = rb.client()
    await rb.dispatcher.storage.set_state(admin.storage_key(), "AnalysisFlow:running")

    methods = await admin.press_data(ResultCb(action="cash", signal_id=signal.signal_id).pack(), 94)

    assert [a.text for a in answers(methods)] == [texts.ALREADY_RUNNING]
    assert await admin.state() == "AnalysisFlow:running"
