"""The Telegram ``Notifier``: rendering, replies, error handling and splitting.

A real ``Bot`` over a recording session; failures are scripted per request.
"""

from dataclasses import replace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

from app.bot.callbacks import CardCb, RecalcAllCb
from app.bot.notifier import MAX_RETRY_AFTER_SECONDS, TelegramNotifier
from app.domain import Sport
from app.services.fake import create_fake_services
from app.services.interfaces import (
    AdminAlert,
    CancelReason,
    DailyDigest,
    DigestResume,
    FixtureOption,
    MessageRef,
    SettledNotice,
    SignalCancelled,
    SignalState,
    SignalUpdate,
    UpdateKind,
    WatchlistEvent,
    WatchNotification,
)
from tests.bot.fakes import ADMIN_ID, ScriptedSession, fixed_clock
from tests.bot.samples import KICKOFF, NOW, key, make_diff, make_digest, make_signal
from tests.e2e.bot_client import telegram_html_problems

USER = 1001
TASHKENT = ZoneInfo("Asia/Tashkent")
METHOD = SendMessage(chat_id=USER, text="x")


class Harness:
    """A notifier bound to a bot whose requests fail as scripted."""

    def __init__(self, script: list[Exception | None] | None = None) -> None:
        self.session = ScriptedSession(script or [])
        self.bot = Bot(token="42:TEST", session=self.session)
        self.sleeps: list[float] = []
        self.notifier = TelegramNotifier(
            admin_ids=frozenset({ADMIN_ID, 1002}), default_zone=TASHKENT, sleep=self._sleep
        )
        self.notifier.bind(self.bot)

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    @property
    def sent(self) -> list[SendMessage]:
        return [m for m in self.session.requests if isinstance(m, SendMessage)]

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]


@pytest.fixture
def harness() -> Harness:
    return Harness()


def forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(METHOD, "Forbidden: bot was blocked by the user")


def retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(METHOD, "Too Many Requests", seconds)


def markup_of(message: SendMessage) -> InlineKeyboardMarkup:
    assert isinstance(message.reply_markup, InlineKeyboardMarkup)
    return message.reply_markup


def callback_data(message: SendMessage) -> list[str]:
    return [b.callback_data or "" for row in markup_of(message).inline_keyboard for b in row]


# --------------------------------------------------------------------------- digest
async def test_digest_sends_header_then_cards_with_buttons(harness: Harness) -> None:
    first = make_signal(1)
    second = make_signal(2, home_team="Inter", away_team="Lazio", watchlist=True)
    digest = make_digest((first, second), requested=3)

    ref = await harness.notifier.send_daily_digest(USER, digest, key=key())

    assert ref == MessageRef(USER, 1)
    header, *rest = harness.sent
    assert header.text == (
        "☀️ <b>Kunlik tahlil</b> — Payshanba, 22.10\n"
        "📆 <b>Kecha:</b> 6 ta signal · 4 ✅ · 2 ❌ · +2.3 unit\n"
        "📊 <b>Natijalar</b>: 22.10 · 2 ta signal · 12 ta o'yin\n"
        "ℹ️ Shartlarga 2 ta o'yin mos keldi (siz 3 ta so'ragansiz). "
        "Ro'yxat shartga mos kelmagan o'yinlar bilan to'ldirilmaydi."
    )
    assert callback_data(header) == [RecalcAllCb(kind="d", ref="2026-10-22").pack()]
    assert [m.text.splitlines()[0] for m in rest] == [
        "📅 <b>Payshanba, 22.10</b> — 2 ta signal",
        "⚽ <b>Arsenal — Chelsea</b>   🕒 20:00  🏆 Premier League",
        "⚽ <b>Inter — Lazio</b>   🕒 20:00  🏆 Premier League",
    ]
    assert CardCb(action="recalc", signal_id=1).pack() in callback_data(rest[1])
    assert all(m.parse_mode == "HTML" and m.chat_id == USER for m in harness.sent)


async def test_digest_without_value_says_so_and_has_no_buttons(harness: Harness) -> None:
    ref = await harness.notifier.send_daily_digest(USER, make_digest((), yesterday=None), key=key())

    assert ref is not None
    (only,) = harness.sent
    assert "Value topilmadi" in only.text
    assert only.reply_markup is None


async def test_digest_stops_when_the_user_blocks_midway(harness: Harness) -> None:
    harness.session.script.extend([None, forbidden()])

    ref = await harness.notifier.send_daily_digest(
        USER, make_digest((make_signal(1), make_signal(2))), key=key()
    )

    assert ref == MessageRef(USER, 1)  # the header went out
    assert len(harness.sent) == 2  # the day header failed; no cards were tried after it


class MemoryProgress:
    """A digest progress kept in memory (the pipeline keeps it in the ledger)."""

    def __init__(self, header: MessageRef | None = None, handled: frozenset[int] = frozenset()):
        self.header = header
        self.handled = set(handled)
        self.header_marks = 0

    async def resume(self) -> DigestResume:
        return DigestResume(self.header, frozenset(self.handled))

    async def header_sent(self, header: MessageRef) -> None:
        self.header = header
        self.header_marks += 1

    async def part_handled(self, position: int) -> None:
        self.handled.add(position)


def with_progress(digest: DailyDigest, progress: MemoryProgress) -> DailyDigest:
    return replace(digest, progress=progress)


def bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(METHOD, "Bad Request: can't parse entities")


async def test_a_rejected_card_is_skipped_and_marked_handled(harness: Harness) -> None:
    progress = MemoryProgress()
    harness.session.script.extend([None, None, bad_request()])  # header, day header, card 1
    digest = with_progress(make_digest((make_signal(1), make_signal(2))), progress)

    ref = await harness.notifier.send_daily_digest(USER, digest, key=key())

    assert ref == MessageRef(USER, 1)  # delivered: a rejected card is never retried
    assert len(harness.sent) == 4  # header, day header, rejected card, second card
    assert progress.header == MessageRef(USER, 1)
    assert progress.handled == {0, 1, 2}  # positions: day header, card 1 (rejected), card 2


@pytest.mark.parametrize("failure", [retry_after(MAX_RETRY_AFTER_SECONDS + 1), "network"])
async def test_a_card_that_fails_for_now_is_left_out_of_the_progress(
    harness: Harness, failure: object
) -> None:
    error = failure if isinstance(failure, Exception) else TelegramNetworkError(METHOD, "timeout")
    harness.session.script.extend([None, None, error])  # type: ignore[list-item]
    progress = MemoryProgress()
    digest = with_progress(make_digest((make_signal(1), make_signal(2))), progress)

    ref = await harness.notifier.send_daily_digest(USER, digest, key=key())

    assert ref is None  # "not delivered": the pipeline retries later
    assert len(harness.sent) == 4  # the second card was still tried
    assert progress.handled == {0, 2}  # the failed card (position 1) is missing
    assert progress.header == MessageRef(USER, 1)


async def test_a_retry_sends_only_what_the_progress_says_is_missing(harness: Harness) -> None:
    progress = MemoryProgress(header=MessageRef(USER, 77), handled=frozenset({0, 2}))
    digest = with_progress(make_digest((make_signal(1), make_signal(2))), progress)

    ref = await harness.notifier.send_daily_digest(USER, digest, key=key())

    assert ref == MessageRef(USER, 77)  # the stored header reference, nothing re-sent for it
    assert [m.text.splitlines()[0] for m in harness.sent] == [
        "⚽ <b>Arsenal — Chelsea</b>   🕒 20:00  🏆 Premier League"
    ]
    assert progress.handled == {0, 1, 2}
    assert progress.header_marks == 0


async def test_a_digest_without_progress_behaves_as_before(harness: Harness) -> None:
    ref = await harness.notifier.send_daily_digest(USER, make_digest((make_signal(1),)), key=key())

    assert ref == MessageRef(USER, 1)
    assert len(harness.sent) == 3


# --------------------------------------------------------------------------- updates
async def test_update_is_a_reply_to_the_earlier_message(harness: Harness) -> None:
    update = SignalUpdate(kind=UpdateKind.BEFORE_24H, diff=make_diff())

    ref = await harness.notifier.send_signal_update(
        USER, update, key=key("update"), reply_to=MessageRef(USER, 55)
    )

    assert ref is not None
    (message,) = harness.sent
    assert message.text.splitlines()[1:] == [
        "Koeff.: 1.85 → 1.72   EV: +12.9% → +4.9%",
        "Holat: ⚠️ Chegaradan pastga tushdi, tavsiya etilmaydi",
    ]
    assert message.reply_parameters is not None
    assert message.reply_parameters.message_id == 55
    assert message.reply_parameters.allow_sending_without_reply is True


async def test_update_without_reference_is_a_plain_message(harness: Harness) -> None:
    update = SignalUpdate(kind=UpdateKind.PREMATCH, diff=make_diff())

    await harness.notifier.send_signal_update(USER, update, key=key("update"))

    assert harness.sent[0].reply_parameters is None


async def test_update_of_a_cancelled_signal_offers_no_recalculation(harness: Harness) -> None:
    signal = make_signal(state=SignalState.CANCELLED)
    update = SignalUpdate(kind=UpdateKind.PREMATCH, diff=make_diff(signal=signal))

    await harness.notifier.send_signal_update(USER, update, key=key("update"))

    assert CardCb(action="recalc", signal_id=7).pack() not in callback_data(harness.sent[0])


async def test_cancellation_and_settlement_and_watch_event(harness: Harness) -> None:
    cancelled = SignalCancelled(signal=make_signal(), reason=CancelReason.CANCELLED)
    settled = SettledNotice(signal=make_signal(), profit_units=None)
    fixture = FixtureOption(
        fixture_id="fx1",
        sport=Sport.FOOTBALL,
        league="PL",
        home_team="A",
        away_team="B",
        kickoff=KICKOFF,
    )
    event = WatchlistEvent(kind=WatchNotification.LINEUPS, fixture=fixture, signal=make_signal(3))

    refs = [
        await harness.notifier.send_signal_cancelled(
            USER, cancelled, key=key("c"), reply_to=MessageRef(USER, 9)
        ),
        await harness.notifier.send_result_settled(USER, settled, key=key("s")),
        await harness.notifier.send_watchlist_event(USER, event, key=key("w")),
    ]

    assert all(ref is not None for ref in refs)
    assert harness.sent[0].text.startswith("❌ <b>Bekor: Arsenal — Chelsea</b>")
    assert harness.sent[0].reply_parameters is not None
    assert harness.sent[1].text.startswith("🏁 — ")
    assert harness.sent[2].text.startswith("⭐ <b>Sevimli:</b> 📋 Tarkiblar e'lon qilindi")
    assert "🎯 Tanlov" in harness.sent[2].text  # the signal card follows the event
    assert CardCb(action="bet", signal_id=3).pack() in callback_data(harness.sent[2])


async def test_a_repeated_key_is_tolerated(harness: Harness) -> None:
    update = SignalUpdate(kind=UpdateKind.PREMATCH, diff=make_diff())

    first = await harness.notifier.send_signal_update(USER, update, key=key("u"))
    second = await harness.notifier.send_signal_update(USER, update, key=key("u"))

    assert first is not None
    assert second is not None


# --------------------------------------------------------------------------- failures
async def test_blocked_user_gets_none_and_nothing_raises() -> None:
    harness = Harness([forbidden()])

    ref = await harness.notifier.send_signal_cancelled(
        USER, SignalCancelled(signal=make_signal(), reason=CancelReason.POSTPONED), key=key()
    )

    assert ref is None
    assert len(harness.sent) == 1  # no retry against a blocked chat


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(METHOD, "Bad Request: chat not found"),
        TelegramNetworkError(METHOD, "timeout"),
    ],
)
async def test_other_telegram_errors_give_none(error: Exception) -> None:
    harness = Harness([error])

    ref = await harness.notifier.send_result_settled(
        USER, SettledNotice(signal=make_signal()), key=key()
    )

    assert ref is None


async def test_flood_control_is_retried_exactly_once() -> None:
    harness = Harness([retry_after(3)])

    ref = await harness.notifier.send_result_settled(
        USER, SettledNotice(signal=make_signal()), key=key()
    )

    assert ref is not None
    assert harness.sleeps == [3.0]
    assert len(harness.sent) == 2


async def test_persistent_flood_control_gives_up_after_one_retry() -> None:
    harness = Harness([retry_after(2), retry_after(2)])

    ref = await harness.notifier.send_result_settled(
        USER, SettledNotice(signal=make_signal()), key=key()
    )

    assert ref is None
    assert harness.sleeps == [2.0]
    assert len(harness.sent) == 2


async def test_a_very_long_flood_wait_is_not_slept_through() -> None:
    harness = Harness([retry_after(MAX_RETRY_AFTER_SECONDS + 1)])

    ref = await harness.notifier.send_result_settled(
        USER, SettledNotice(signal=make_signal()), key=key()
    )

    assert ref is None
    assert harness.sleeps == []


async def test_without_a_bot_nothing_is_sent_and_nothing_raises() -> None:
    notifier = TelegramNotifier(admin_ids=frozenset(), default_zone=TASHKENT)

    ref = await notifier.send_result_settled(USER, SettledNotice(signal=make_signal()), key=key())

    assert ref is None


async def test_a_rendering_bug_is_reported_as_undelivered(harness: Harness) -> None:
    broken = SettledNotice(signal=make_signal(), profit_units=None)
    object.__setattr__(broken, "signal", None)  # simulate corrupt data from the pipeline

    ref = await harness.notifier.send_result_settled(USER, broken, key=key())

    assert ref is None
    assert harness.sent == []


# --------------------------------------------------------------------------- splitting, escaping
async def test_long_message_is_split_without_breaking_html(harness: Harness) -> None:
    hostile = "<b>x</b> & " * 900  # ~9 900 characters once escaped
    fixture = FixtureOption(
        fixture_id="fx1",
        sport=Sport.FOOTBALL,
        league="PL",
        home_team="A",
        away_team="B",
        kickoff=KICKOFF,
    )
    event = WatchlistEvent(kind=WatchNotification.RESULT, fixture=fixture, detail=hostile)

    ref = await harness.notifier.send_watchlist_event(USER, event, key=key("w"))

    assert ref == MessageRef(USER, 1)  # the first part
    assert len(harness.sent) >= 3
    for text in harness.texts:
        assert len(text) <= 4096
        assert telegram_html_problems(text) == [], text[:80]
    assert "<script" not in "".join(harness.texts)


async def test_markup_is_only_on_the_last_part(harness: Harness) -> None:
    note = "Gap. " * 1500
    signal = make_signal(llm_note=note)
    event = WatchlistEvent(
        kind=WatchNotification.RESULT,
        fixture=FixtureOption(
            fixture_id="fx1",
            sport=Sport.FOOTBALL,
            league="PL",
            home_team="A",
            away_team="B",
            kickoff=KICKOFF,
        ),
        detail="x\n" * 3000,
        signal=signal,
    )

    await harness.notifier.send_watchlist_event(USER, event, key=key("w"))

    *head, last = harness.sent
    assert len(head) >= 1
    assert all(m.reply_markup is None for m in head)
    assert last.reply_markup is not None


async def test_later_part_failure_keeps_the_first_reference() -> None:
    harness = Harness([None, forbidden()])
    fixture = FixtureOption(
        fixture_id="fx1",
        sport=Sport.FOOTBALL,
        league="PL",
        home_team="A",
        away_team="B",
        kickoff=KICKOFF,
    )
    event = WatchlistEvent(kind=WatchNotification.RESULT, fixture=fixture, detail="x\n" * 4000)

    ref = await harness.notifier.send_watchlist_event(USER, event, key=key("w"))

    assert ref == MessageRef(USER, 1)


async def test_hostile_llm_note_and_names_are_escaped_in_the_digest(harness: Harness) -> None:
    hostile = "<b>Evil</b> & <script>alert(1)</script> <<<"
    signal = make_signal(llm_note=hostile, home_team=hostile, away_team="A&B", league="<i>L</i>")

    await harness.notifier.send_daily_digest(USER, make_digest((signal,)), key=key())

    for text in harness.texts:
        assert telegram_html_problems(text) == [], text
        assert "<script>" not in text
    card = harness.texts[-1]
    assert "&lt;script&gt;" in card


# --------------------------------------------------------------------------- admin and zone
async def test_admin_alert_reaches_every_admin_even_if_one_blocked() -> None:
    harness = Harness([forbidden()])

    await harness.notifier.alert_admin(AdminAlert(component="refresh", message="<boom>", at=NOW))

    assert [m.chat_id for m in harness.sent] == [ADMIN_ID, 1002]
    assert harness.sent[1].text == ("🚨 <b>Tizim xatosi</b> · refresh · 22.10 14:32\n&lt;boom&gt;")


async def test_times_follow_the_users_own_time_zone(harness: Harness) -> None:
    services = create_fake_services(clock=fixed_clock)
    settings = await services.settings.get_settings(USER)
    await services.settings.update_settings(USER, replace(settings, timezone="Europe/London"))
    harness.notifier.attach_settings(services.settings)

    await harness.notifier.send_signal_update(
        USER, SignalUpdate(kind=UpdateKind.PREMATCH, diff=make_diff()), key=key("u")
    )

    assert "(10:32)" in harness.texts[0]  # 09:32 UTC is 10:32 in London (BST)


async def test_unavailable_settings_fall_back_to_the_default_zone(harness: Harness) -> None:
    class Broken:
        async def get_settings(self, _user_id: int) -> Any:
            raise RuntimeError("database is locked")

    harness.notifier.attach_settings(Broken())  # type: ignore[arg-type]

    ref = await harness.notifier.send_signal_update(
        USER, SignalUpdate(kind=UpdateKind.PREMATCH, diff=make_diff()), key=key("u")
    )

    assert ref is not None
    assert "(14:32)" in harness.texts[0]


async def test_on_startup_binds_the_bot() -> None:
    notifier = TelegramNotifier(admin_ids=frozenset(), default_zone=TASHKENT)
    harness = Harness()

    await notifier.on_startup(harness.bot)
    ref = await notifier.send_result_settled(USER, SettledNotice(signal=make_signal()), key=key())

    assert ref is not None
    assert len(harness.sent) == 1
