"""T5 follow-ups from the pipeline round: skipped-fixture notes, odds drift, blocked users."""

import dataclasses
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot import texts
from app.bot.formatters import format_results_summary, format_signal_card
from app.bot.notifier import BLOCKED_TTL_SECONDS, TelegramNotifier
from app.bot.notify_formatters import format_recalc_diff
from app.services.interfaces import (
    AnalysisResult,
    CancelReason,
    DateWindow,
    MessageRef,
    RecalcVerdict,
    SignalCancelled,
    UndeliverableAware,
)
from tests.bot.samples import DAY, key, make_diff, make_digest, make_signal
from tests.bot.test_notifier import METHOD, Harness, forbidden
from tests.e2e.bot_client import telegram_html_problems

TASHKENT = ZoneInfo("Asia/Tashkent")


def make_result(**overrides: object) -> AnalysisResult:
    base = AnalysisResult(
        query_id="q1",
        window=DateWindow(DAY, DAY),
        signals=(make_signal(),),
        fixtures_analyzed=9,
        matched=1,
        requested=None,
        api_requests_used=0,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- summary notes
def test_summary_without_skips_has_no_price_notes() -> None:
    text = format_results_summary(make_result())

    assert "1xBet" not in text
    assert "eski ma'lumot" not in text


def test_summary_reports_fixtures_without_a_bookmaker_price() -> None:
    text = format_results_summary(make_result(skipped_no_price=3))

    assert "3 ta o'yin: koeffitsient yo'q" in text


def test_summary_reports_stale_prices_and_stale_data() -> None:
    text = format_results_summary(make_result(skipped_stale_price=2, data_stale=True))

    assert "2 ta o'yin: koeffitsient eskirgan" in text
    assert "Ma'lumot yangilanmadi — eski ma'lumot" in text
    assert telegram_html_problems(text) == []


def test_empty_result_still_explains_the_skipped_fixtures() -> None:
    text = format_results_summary(make_result(signals=(), matched=0, skipped_no_price=4))

    assert "Value topilmadi" in text
    assert "4 ta o'yin: koeffitsient yo'q" in text


def test_digest_header_carries_the_price_notes() -> None:
    digest = make_digest((make_signal(),))
    result = dataclasses.replace(digest.result, skipped_no_price=1, data_stale=True)

    text = format_results_summary(result)

    assert "koeffitsient yo'q" in text
    assert "eski ma'lumot" in text


# --------------------------------------------------------------------------- card odds
def test_card_shows_recommended_and_current_odds_once_they_differ() -> None:
    card = format_signal_card(make_signal(odds=1.72, recommended_odds=1.85), TASHKENT)

    assert "📌 Tavsiya koeff.: 1.85 → hozir 1.72" in card


@pytest.mark.parametrize("recommended", [None, 1.85, 1.8500001])
def test_card_hides_the_odds_note_when_nothing_moved(recommended: float | None) -> None:
    card = format_signal_card(make_signal(odds=1.85, recommended_odds=recommended), TASHKENT)

    assert "Tavsiya koeff." not in card


def test_card_fair_odds_use_the_zero_ev_price_when_known() -> None:
    card = format_signal_card(make_signal(zero_ev_odds=1.64), TASHKENT)

    assert "adolatli koeff. 1.64" in card


# --------------------------------------------------------------------------- verdict labels
def test_every_recalc_verdict_has_a_label() -> None:
    assert set(texts.VERDICT_LABELS) == {verdict.value for verdict in RecalcVerdict}


def test_no_price_verdict_is_shown_in_the_diff_message() -> None:
    text = format_recalc_diff(make_diff(verdict=RecalcVerdict.NO_PRICE), TASHKENT)

    assert "Bukmekerda yangi narx yo'q, raqamlar tasdiqlanmadi" in text
    assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- blocked users
USER = 1001


async def send(harness: Harness) -> MessageRef | None:
    cancelled = SignalCancelled(signal=make_signal(), reason=CancelReason.POSTPONED)
    return await harness.notifier.send_signal_cancelled(USER, cancelled, key=key())


def test_telegram_notifier_implements_the_undeliverable_protocol() -> None:
    assert isinstance(Harness().notifier, UndeliverableAware)


async def test_a_blocked_user_becomes_undeliverable_and_is_skipped() -> None:
    harness = Harness([forbidden()])
    notifier = harness.notifier
    assert not notifier.is_undeliverable(USER)

    assert await send(harness) is None

    assert notifier.is_undeliverable(USER)
    before = len(harness.session.requests)
    assert await send(harness) is None
    assert len(harness.session.requests) == before  # no API call against a blocked chat
    assert not notifier.is_undeliverable(USER + 1)  # per user


async def test_other_failures_do_not_mark_the_user_undeliverable() -> None:
    harness = Harness([TelegramBadRequest(METHOD, "Bad Request: chat not found")])

    assert await send(harness) is None
    assert not harness.notifier.is_undeliverable(USER)


async def test_the_block_expires_so_an_unblocked_user_is_tried_again() -> None:
    clock = [0.0]
    harness = Harness([forbidden()])
    notifier = TelegramNotifier(
        admin_ids=frozenset(), default_zone=TASHKENT, monotonic=lambda: clock[0]
    )
    notifier.bind(harness.bot)
    harness.notifier = notifier

    assert await send(harness) is None
    assert notifier.is_undeliverable(USER)
    clock[0] = BLOCKED_TTL_SECONDS - 1
    assert notifier.is_undeliverable(USER)
    clock[0] = BLOCKED_TTL_SECONDS
    assert not notifier.is_undeliverable(USER)
    assert await send(harness) == MessageRef(USER, 2)  # delivered again
