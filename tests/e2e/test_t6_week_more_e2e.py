"""T6: the rest of the week: limits, templates, quiet hours, degraded services, restart, users."""

from datetime import time
from decimal import Decimal

import pytest
from aiogram.methods import EditMessageText, SendMessage

from app.bot import texts
from app.bot.callbacks import CardCb
from app.llm.budget import LlmLimits
from app.scheduler import JOB_REFRESH, JOB_SETTLE
from app.services.interfaces import (
    AnalysisQuery,
    BetDraft,
    DateSpec,
    QuietHours,
    ReportRequest,
    TemplateDraft,
    TemplateSchedule,
)
from tests.e2e.bot_client import answers
from tests.e2e.t6_world import OTHER, PERMISSIVE, USER, Week, at, jobs, open_week, tick
from tests.e2e.test_t6_week_e2e import day_one, expected_mark

pytestmark = pytest.mark.slow


@pytest.fixture
async def week(history_template, tmp_path, bot, session):  # type: ignore[no-untyped-def]
    async with open_week(history_template, tmp_path, bot, session) as opened:
        yield opened


def cards_since(w: Week, user_id: int, mark: int) -> list[SendMessage]:
    """Signal-card messages sent to ``user_id`` after request number ``mark``."""
    sent = [m for m in w.session.requests[mark:] if isinstance(m, SendMessage)]
    return [m for m in sent if m.chat_id == user_id and "🎯 Tanlov" in m.text]


def digests_of(w: Week, user_id: int = USER) -> list[SendMessage]:
    return [m for m in w.pushed(user_id) if m.text.startswith("☀️ <b>Kunlik tahlil</b>")]


async def save_daily_template(w: Week, name: str) -> None:
    settings = await w.services.settings.get_settings(USER)
    await w.services.templates.save_template(
        USER,
        TemplateDraft(
            name=name,
            query=AnalysisQuery.from_settings(settings, DateSpec.today()),
            schedule=TemplateSchedule(weekdays=frozenset(range(1, 8)), at=time(9, 30)),
        ),
    )


# --------------------------------------------------------------------------- responsible gaming
async def test_loss_limit_stops_new_signals_and_says_so_once_while_others_are_unaffected(
    week: Week,
) -> None:
    w = week
    day = await day_one(w)
    await w.settings(OTHER, **PERMISSIVE)
    await w.settings(USER, daily_loss_limit=Decimal(30))
    losers = [s for s in day.signals if expected_mark(w, s) == "loss"]
    assert losers, "the seeded day must contain a losing pick"
    for signal in losers:
        await w.services.bets.place_bet(
            USER, BetDraft(signal_id=signal.signal_id, odds_taken=signal.odds, stake=Decimal(50))
        )
    # the process was down on the 22nd: the settlement only runs on the morning of the 24th
    at(w, 24, 3, 0)
    await jobs(w).run(JOB_SETTLE)
    bank = await w.services.bets.bank_summary(USER)
    assert bank.loss_limit_reached
    assert bank.loss_today >= Decimal(50)

    await save_daily_template(w, "Kunlik")
    mark = len(w.session.requests)
    await jobs(w).run(JOB_REFRESH)
    await tick(w, 120)  # 03:30 .. 05:30 UTC: the 09:00 digest, then the 09:30 template tick

    sent = [m for m in w.session.requests[mark:] if isinstance(m, SendMessage)]
    mine = [m for m in sent if m.chat_id == USER]
    assert cards_since(w, USER, mark) == []
    notices = [m for m in mine if "🛑 <b>Zarar limitiga yetildi</b>" in m.text]
    assert len(notices) == 1  # the digest and the template were both blocked: one notice
    assert not any(m.text.startswith("💾 <b>Kunlik</b>") for m in mine)
    # the control user got real signals that same morning, so the silence is the limit's doing
    assert cards_since(w, OTHER, mark)

    # a manual analysis still works for the limited user (with a banner, never a refusal)
    client = w.client(USER)
    await client.send("/tahlil 7kun 30% ev0% 3ta")
    await client.press(texts.BTN_START)
    shown = "\n".join(client.texts.values())
    assert "Zarar limitiga yetgansiz" in shown


# --------------------------------------------------------------------------- templates, quiet hours
async def test_a_scheduled_template_runs_once_on_its_day_and_titles_the_digest(week: Week) -> None:
    w = week
    await w.settings(USER, **{**PERMISSIVE, "min_ev": 0.0})
    await save_daily_template(w, "Dam olish")
    at(w, 24, 3, 30)
    await jobs(w).run(JOB_REFRESH)
    await tick(w, 150)  # until 06:00 UTC = 11:00 local
    titled = [m for m in w.pushed(USER) if m.text.startswith("💾 <b>Dam olish</b>")]
    assert len(titled) == 1
    await tick(w, 240)  # hours later: still one
    assert len([m for m in w.pushed(USER) if m.text.startswith("💾 <b>Dam olish</b>")]) == 1


async def test_quiet_hours_hold_the_digest_until_they_end_then_send_it_once(week: Week) -> None:
    w = week
    await w.settings(
        USER,
        **{
            **PERMISSIVE,
            "send_time": time(1, 0),
            "quiet_hours": QuietHours(start=time(0, 0), end=time(8, 0)),
        },
    )
    await jobs(w).run(JOB_REFRESH)
    at(w, 21, 21, 0)  # 02:00 on the 22nd in Tashkent: the digest is due but it is quiet
    await tick(w, 120)
    assert digests_of(w) == []
    at(w, 22, 2, 0)
    await tick(w, 30)  # 07:30 local, still quiet
    assert digests_of(w) == []
    await tick(w, 30)  # quiet hours end at 08:00 local = 03:00 UTC
    assert len(digests_of(w)) == 1
    await tick(w, 120)
    assert len(digests_of(w)) == 1


# --------------------------------------------------------------------------- degraded services
async def test_gemini_down_the_signals_still_arrive_without_notes(week: Week) -> None:
    w = week
    w.gemini.down = True
    await w.settings(USER, **PERMISSIVE)
    await jobs(w).run(JOB_REFRESH)
    await tick(w, 30)
    cards = [m for m in w.pushed(USER) if "🎯 Tanlov" in m.text]
    assert len(cards) == 10
    assert not any("Salbiy omil" in m.text for m in cards)
    assert w.gemini.requests >= 1  # it was tried, failed, and the numbers went out anyway
    runs = await w.ctx.ledger.last_runs()
    assert all(run.ok for run in runs.values())


async def test_llm_budget_exhausted_midweek_signals_go_on_without_notes(
    history_template,
    tmp_path,
    bot,
    session,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(
        history_template, tmp_path, bot, session, llm_limits=LlmLimits(max_requests_per_day=1)
    ) as w:
        await w.settings(USER, **PERMISSIVE)
        await jobs(w).run(JOB_REFRESH)
        await tick(w, 30)
        first = [m for m in w.pushed(USER) if "🎯 Tanlov" in m.text]
        assert first
        assert all("Salbiy omil" in m.text for m in first)
        # a second LLM answer on the same day is refused by the budget guard: nothing breaks
        client = w.client(USER)
        w.sync(USER)
        before = w.gemini.requests
        _, all_data = client.find_button("Hammasini yangilash")
        await client.press_data(all_data, client.last_message_id)
        assert w.gemini.requests == before
        # the allowance renews with the next UTC day
        at(w, 24, 3, 30)
        await jobs(w).run(JOB_REFRESH)
        await tick(w, 60)
        assert w.gemini.requests == before + 1
        assert len(digests_of(w)) == 2


async def test_api_quota_exhausted_gives_clear_notices_and_nothing_crashes(week: Week) -> None:
    w = week
    await w.settings(USER, **PERMISSIVE)
    await jobs(w).run(JOB_REFRESH)
    await tick(w, 30)
    w.sync(USER)
    client = w.client(USER)
    w.provider.exhausted = True
    recalc_id, recalc_data = client.find_button(texts.BTN_RECALC)
    methods = await client.press_data(recalc_data, recalc_id)
    body = "\n".join(m.text for m in methods if isinstance(m, SendMessage | EditMessageText))
    assert "holatiga" in body  # the numbers of 11:00, said plainly
    sent = await client.send("/tahlil 7kun 50% ev3% 10ta")
    shown_text = "\n".join(m.text for m in sent if isinstance(m, SendMessage))
    assert "API limitidan oshadi" in shown_text
    at(w, 22, 3, 30)
    await jobs(w).run(JOB_REFRESH)  # reported, never raised
    status = await client.send("/holat")
    assert any(isinstance(m, SendMessage) for m in status)


# --------------------------------------------------------------------------- restart, isolation
async def test_restart_in_the_middle_of_the_week_duplicates_nothing(week: Week) -> None:
    w = week
    await w.settings(USER, **PERMISSIVE)
    await jobs(w).run(JOB_REFRESH)
    await tick(w, 30)
    w.sync(USER)
    client = w.client(USER)
    digests_before = len(digests_of(w))
    cards_before = len([m for m in w.pushed(USER) if "🎯 Tanlov" in m.text])
    card_id, bet_data = client.find_button(texts.BTN_BET)
    await client.press_data(bet_data, card_id)
    confirm_id, confirm_data = client.find_button("Yozish")
    await client.press_data(confirm_data, confirm_id)
    await client.send(texts.MENU_ANALYSIS)
    wizard_state = await client.state()
    assert wizard_state is not None
    wizard_id, wizard_data = client.find_button(texts.BTN_TODAY)

    reborn = await w.restart()
    fresh = reborn.client(USER)
    assert await fresh.state() == wizard_state  # the wizard survived in fsm.db
    await tick(reborn, 120)  # the same minute ticks after the restart: nothing is sent again
    assert len(digests_of(reborn)) == digests_before
    assert len([m for m in reborn.pushed(USER) if "🎯 Tanlov" in m.text]) == cards_before
    # a retried callback of the old confirm button: still one bet
    await fresh.press_data(confirm_data, confirm_id)
    assert len(await reborn.services.bets.list_bets(USER)) == 1
    # the wizard message from before the restart still works (its state was persisted) ...
    await fresh.press_data(wizard_data, wizard_id)
    assert await fresh.state() == "AnalysisFlow:sport"
    # ... and once the flow is cancelled the same old button is refused politely
    await fresh.send("/bekor")
    assert await fresh.state() is None
    refused = answers(await fresh.press_data(wizard_data, wizard_id))
    assert [a.text for a in refused] == [texts.STALE_BUTTON]
    assert await fresh.state() is None
    assert await fresh.send(texts.MENU_ANALYSIS)  # the wizard works again from the menu


async def test_two_users_stay_isolated_through_the_whole_week(week: Week) -> None:
    w = week
    await w.settings(USER, **PERMISSIVE)
    await w.settings(OTHER, **{**PERMISSIVE, "bank": Decimal(5000), "unit": Decimal(100)})
    await jobs(w).run(JOB_REFRESH)
    await tick(w, 30)
    w.sync(USER)
    w.sync(OTHER)
    mine = await w.services.signals.list_signals(USER, DateSpec.next_days(3))
    theirs = await w.services.signals.list_signals(OTHER, DateSpec.next_days(3))
    assert {s.signal_id for s in mine}.isdisjoint({s.signal_id for s in theirs})
    # the other user cannot hide, bet on, recalculate or open USER's signal through the bot
    intruder = w.client(OTHER)
    victim = mine[0]
    for action in ("hide", "bet", "recalc", "detail"):
        await intruder.press_data(CardCb(action=action, signal_id=victim.signal_id).pack(), 1)
    assert (await w.services.signals.get_signal(USER, victim.signal_id)).state.value == "active"
    assert await w.services.bets.list_bets(USER) == ()
    assert await w.services.bets.list_bets(OTHER) == ()
    # each user's money is their own
    await w.services.bets.place_bet(USER, BetDraft(signal_id=mine[0].signal_id))
    await w.services.bets.place_bet(OTHER, BetDraft(signal_id=theirs[0].signal_id))
    a = await w.services.bets.bank_summary(USER)
    b = await w.services.bets.bank_summary(OTHER)
    assert (a.unit, b.unit) == (Decimal(10), Decimal(100))
    assert a.open_bets == b.open_bets == 1
    at(w, 22, 3, 0)
    await jobs(w).run(JOB_SETTLE)
    ra = await w.services.reports.report(USER, ReportRequest.all_time())
    rb = await w.services.reports.report(OTHER, ReportRequest.all_time())
    assert (ra.signals, rb.signals) == (len(mine), len(theirs))
