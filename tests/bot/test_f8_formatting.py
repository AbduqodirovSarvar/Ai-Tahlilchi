"""F8.1 / F8.2 / F8.8 rendering: digest variants, the loss-limit banner, the pending list."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.bot import texts
from app.bot.formatters import format_results_summary
from app.bot.keyboards import signal_keyboard
from app.bot.learning_ui import format_journal, journal_keyboard
from app.bot.notify_formatters import format_digest_title, format_limit_digest
from app.bot.result_buttons import markable
from app.bot.result_ui import PENDING_PAGE_SIZE, format_pending, pending_keyboard
from app.services.interfaces import (
    LearningChangeKind,
    LearningChangeView,
    LearningMode,
    LearningPage,
    LearningSummary,
    LimitPeriod,
    LossLimitNotice,
    ResultMark,
    SignalSource,
    SignalState,
)
from tests.bot.fakes import ADMIN_ID
from tests.bot.samples import DAY, KICKOFF, NOW, key, make_digest, make_signal
from tests.bot.test_notifier import Harness
from tests.e2e.bot_client import telegram_html_problems

USER = ADMIN_ID
TASHKENT = ZoneInfo("Asia/Tashkent")
LIMIT = LossLimitNotice(period=LimitPeriod.DAY, limit=Decimal(100_000), loss=Decimal(120_000))
HOSTILE = '<b>Evil</b> & "Name" <script>'


# --------------------------------------------------------------------------- digest title
def test_the_morning_digest_shows_yesterdays_tally() -> None:
    title = format_digest_title(make_digest((make_signal(),)))

    assert "Kunlik tahlil" in title
    assert "<b>Kecha:</b> 6 ta signal · 4 ✅ · 2 ❌ · +2.3 unit" in title


def test_a_template_digest_is_titled_with_its_name_and_has_no_yesterday_block() -> None:
    digest = replace(make_digest((make_signal(),)), template_name=HOSTILE)

    title = format_digest_title(digest)

    assert "&lt;b&gt;Evil&lt;/b&gt;" in title
    assert "Kunlik tahlil" not in title
    assert "Kecha" not in title  # a template digest has no "yesterday" block
    assert telegram_html_problems(title) == []


def test_there_is_no_limit_text_without_a_limit() -> None:
    assert format_limit_digest(make_digest((make_signal(),))) is None


def test_the_limit_digest_names_the_period_limit_and_loss() -> None:
    text = format_limit_digest(replace(make_digest(()), loss_limit=LIMIT))

    assert text is not None
    assert "Zarar limitiga yetildi" in text
    assert "kunlik" in text
    assert "100 000" in text
    assert "120 000" in text
    assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- notifier
@pytest.fixture
def harness() -> Harness:
    return Harness()


async def test_a_blocked_digest_is_one_limit_message_and_not_an_empty_digest(
    harness: Harness,
) -> None:
    digest = replace(make_digest(()), loss_limit=LIMIT)

    ref = await harness.notifier.send_daily_digest(USER, digest, key=key())

    assert ref is not None
    (only,) = harness.texts
    assert "Zarar limitiga yetildi" in only
    assert "Value topilmadi" not in only
    assert "0 ta signal" not in only


async def test_a_template_digest_goes_out_under_the_templates_name(harness: Harness) -> None:
    digest = replace(make_digest((make_signal(),)), template_name="Hafta oxiri")

    await harness.notifier.send_daily_digest(USER, digest, key=key("template"))

    assert harness.texts[0].startswith("💾 <b>Hafta oxiri</b>")
    assert "Kecha" not in harness.texts[0]


async def test_the_morning_digest_still_carries_yesterday(harness: Harness) -> None:
    await harness.notifier.send_daily_digest(USER, make_digest((make_signal(),)), key=key())

    assert "Kecha:" in harness.texts[0]
    assert "6 ta signal" in harness.texts[0]


async def test_the_weekly_summary_is_sent_in_the_users_words(harness: Harness) -> None:
    change = LearningChangeView(
        change_id=3,
        at=NOW,
        kind=LearningChangeKind.SEGMENT_DISABLED,
        subject=HOSTILE,
        reason="roi_negative",
        applied=False,
        rolled_back=False,
        ev_floor=0.05,
        samples=80,
    )
    summary = LearningSummary(week=DAY, mode=LearningMode.SHADOW, changes=(change,))

    ref = await harness.notifier.send_learning_summary(USER, summary, key=key("learning_summary"))

    assert ref is not None
    (text,) = harness.texts
    assert "Model nimani o'rgandi" in text
    assert texts.LEARN_STATE_SHADOW.strip() in text
    assert HOSTILE not in text
    assert telegram_html_problems(text) == []


async def test_a_settled_notice_offers_the_correction_buttons(harness: Harness) -> None:
    from app.services.interfaces import SettledNotice  # noqa: PLC0415

    notice = SettledNotice(signal=make_signal(result=ResultMark.LOST), profit_units=Decimal("-1.5"))

    await harness.notifier.send_result_settled(USER, notice, key=key("settled"))

    (message,) = harness.sent
    data = [b.callback_data for row in message.reply_markup.inline_keyboard for b in row]  # type: ignore[union-attr]
    assert {d.split(":")[1] for d in data if d.startswith("rs:")} == {"mark", "cash", "note"}


# --------------------------------------------------------------------------- the banner
def _result(**overrides: object):  # type: ignore[no-untyped-def]
    return replace(make_digest((make_signal(),)).result, **overrides)


def test_a_manual_analysis_shows_the_loss_limit_banner() -> None:
    text = format_results_summary(_result(loss_limit=LIMIT))

    assert "Zarar limitiga yetgansiz" in text
    assert "baribir bajarildi" in text


def test_a_retro_window_never_shows_the_banner() -> None:
    text = format_results_summary(_result(loss_limit=LIMIT, retro=True))

    assert "Zarar limitiga" not in text


def test_no_limit_no_banner() -> None:
    assert "Zarar limitiga" not in format_results_summary(_result())


# --------------------------------------------------------------------------- mark buttons
def _kinds(markup) -> set[str]:  # type: ignore[no-untyped-def]
    return {
        b.callback_data.split(":")[1]
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("rs:")
    }


def test_marks_appear_once_the_match_has_started() -> None:
    signal = make_signal()
    before, after = KICKOFF - timedelta(hours=1), KICKOFF + timedelta(hours=1)

    assert _kinds(signal_keyboard(signal, before)) == set()
    assert _kinds(signal_keyboard(signal, None)) == set()
    assert _kinds(signal_keyboard(signal, after)) == {"mark", "cash", "note"}


def test_a_settled_card_keeps_the_marks_as_a_correction_even_without_a_clock() -> None:
    assert _kinds(signal_keyboard(make_signal(result=ResultMark.WON))) == {"mark", "cash", "note"}


@pytest.mark.parametrize(
    "signal",
    [
        make_signal(source=SignalSource.RETRO, recommended_at=None, result=ResultMark.WON),
        make_signal(recommended_at=None, result=ResultMark.WON),
        make_signal(state=SignalState.CANCELLED, result=ResultMark.VOID),
    ],
)
def test_retro_unrecommended_and_cancelled_cards_cannot_be_marked(signal) -> None:  # type: ignore[no-untyped-def]
    assert not markable(signal, KICKOFF + timedelta(days=1))
    assert _kinds(signal_keyboard(signal, KICKOFF + timedelta(days=1))) == set()


# --------------------------------------------------------------------------- /natijalar
def test_the_pending_list_escapes_hostile_names_and_pages() -> None:
    signals = [
        make_signal(n, home_team=HOSTILE, away_team="A&B", league="<i>L</i>")
        for n in range(1, 2 * PENDING_PAGE_SIZE + 3)
    ]

    texts_by_page = [format_pending(signals, TASHKENT, page) for page in range(3)]

    for text in texts_by_page:
        assert telegram_html_problems(text) == []
        assert "<script>" not in text
    assert "1. " in texts_by_page[0]
    assert f"{PENDING_PAGE_SIZE + 1}. " in texts_by_page[1]
    markup = pending_keyboard(signals, 1)
    assert markup is not None
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "◀️" in labels
    assert "▶️" in labels


def test_the_empty_pending_list_has_a_message_and_no_keyboard() -> None:
    assert format_pending([], TASHKENT) == texts.RESULTS_PENDING_EMPTY
    assert pending_keyboard([]) is None


# --------------------------------------------------------------------------- learning list
def test_an_empty_journal_page_renders_with_its_mode() -> None:
    page = LearningPage(items=(), page=0, page_count=1, total=0, mode=LearningMode.APPLY)

    text = format_journal(page, TASHKENT)

    assert texts.LEARN_MODES["apply"] in text
    assert texts.LEARN_EMPTY in text
    assert journal_keyboard(page) is None
