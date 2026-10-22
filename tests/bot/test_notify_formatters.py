"""Golden strings and escaping of the notification / recalculation formatters."""

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.bot import texts
from app.bot.notify_formatters import (
    MAX_ALERT_CHARS,
    MAX_BATCH_DIFFS,
    format_admin_alert,
    format_cancelled,
    format_digest_title,
    format_recalc_batch,
    format_recalc_diff,
    format_settled,
    format_update,
    format_watch_event,
    format_yesterday,
)
from app.domain import Sport
from app.services.interfaces import (
    AdminAlert,
    CancelReason,
    ConfidenceLevel,
    DaySummary,
    FixtureOption,
    RecalcBatch,
    RecalcVerdict,
    ResultMark,
    SettledNotice,
    SignalCancelled,
    SignalUpdate,
    UpdateKind,
    WatchlistEvent,
    WatchNotification,
)
from tests.bot.samples import DAY, KICKOFF, NOW, make_diff, make_digest, make_signal, numbers
from tests.e2e.bot_client import telegram_html_problems

TASHKENT = ZoneInfo("Asia/Tashkent")
HOSTILE = "<b>Evil</b> & <script>alert(1)</script> <<<"


def test_diff_matches_the_plan_example() -> None:
    assert format_recalc_diff(make_diff(), TASHKENT) == (
        "🔄 <b>Arsenal — Chelsea</b> yangilandi (14:32)\n"
        "Koeff.: 1.85 → 1.72   EV: +12.9% → +4.9%\n"
        "Holat: ⚠️ Chegaradan pastga tushdi, tavsiya etilmaydi"
    )


def test_diff_lists_only_the_other_numbers_that_moved() -> None:
    after = numbers(
        odds=1.9, probability=0.58, stake_units=Decimal("1"), confidence=ConfidenceLevel.MEDIUM
    )

    text = format_recalc_diff(make_diff(after=after, verdict=RecalcVerdict.STILL_VALID), TASHKENT)

    assert text.splitlines()[2:] == [
        "Ehtimollik: 61% → 58%",
        "Tavsiya: 1.5 → 1 unit",
        "Ishonch: Yuqori → O'rta",
        "Holat: ✅ Kuchda",
    ]


def test_unchanged_recalculation_says_so() -> None:
    diff = make_diff(after=numbers(), verdict=RecalcVerdict.STILL_VALID)

    assert texts.DIFF_UNCHANGED in format_recalc_diff(diff, TASHKENT)


def test_cached_numbers_carry_the_as_of_time() -> None:
    signal = make_signal(data_as_of=NOW - timedelta(hours=5))

    text = format_recalc_diff(make_diff(signal=signal), TASHKENT)

    assert text.endswith("📦 Yangi ma'lumot olinmadi: raqamlar 09:32 holatiga.")


def test_cancelled_verdict_label() -> None:
    diff = make_diff(verdict=RecalcVerdict.CANCELLED)

    assert "Holat: ❌ Signal bekor qilindi" in format_recalc_diff(diff, TASHKENT)


@pytest.mark.parametrize(
    ("kind", "title"),
    [
        (
            UpdateKind.BEFORE_24H,
            "⏰ <b>Arsenal — Chelsea</b> — o'yinga 24 soat qoldi, yangilandi (14:32)",
        ),
        (UpdateKind.PREMATCH, "🔔 <b>Arsenal — Chelsea</b> — o'yin yaqin, yangilandi (14:32)"),
    ],
)
def test_update_titles_by_kind(kind: UpdateKind, title: str) -> None:
    text = format_update(SignalUpdate(kind=kind, diff=make_diff()), TASHKENT)

    assert text.splitlines()[0] == title


def test_update_mentions_confirmed_lineups() -> None:
    update = SignalUpdate(kind=UpdateKind.PREMATCH, diff=make_diff(), lineups_confirmed=True)

    assert format_update(update, TASHKENT).endswith(texts.DIFF_LINEUPS)


def test_cancelled_signal_names_the_reason() -> None:
    cancelled = SignalCancelled(signal=make_signal(), reason=CancelReason.POSTPONED)

    assert format_cancelled(cancelled) == (
        "❌ <b>Bekor: Arsenal — Chelsea</b>\n"
        "Sabab: o'yin qoldirildi.\n"
        "🎯 Tanlov: Arsenal g'alabasi (1)\n"
        "Signal endi tavsiya etilmaydi. Stavka qilgan bo'lsangiz, bukmeker qoidalarini "
        "tekshiring (odatda stavka qaytariladi)."
    )


@pytest.mark.parametrize("reason", list(CancelReason))
def test_every_cancel_reason_has_a_text(reason: CancelReason) -> None:
    assert texts.CANCEL_REASONS[reason.value]


def test_settled_notice_shows_result_and_signed_profit() -> None:
    won = SettledNotice(signal=make_signal(result=ResultMark.WON), profit_units=Decimal("1.275"))
    lost = SettledNotice(signal=make_signal(result=ResultMark.LOST), profit_units=Decimal("-1.5"))

    assert format_settled(won) == (
        "✅ O'tdi — <b>Arsenal — Chelsea</b>\n"
        "🎯 Arsenal g'alabasi (1)   @ 1.85\n"
        "💰 Natija: +1.275 unit"
    )
    assert format_settled(lost).splitlines()[-1] == "💰 Natija: -1.5 unit"


def test_settled_notice_without_profit_or_result() -> None:
    text = format_settled(SettledNotice(signal=make_signal()))

    assert text.startswith("🏁 — <b>Arsenal — Chelsea</b>")
    assert "Natija" not in text


@pytest.mark.parametrize("kind", list(WatchNotification))
def test_watchlist_event_titles(kind: WatchNotification) -> None:
    fixture = FixtureOption(
        fixture_id="fx1",
        sport=Sport.FOOTBALL,
        league="Premier League",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff=KICKOFF,
    )

    text = format_watch_event(WatchlistEvent(kind=kind, fixture=fixture, detail="2:1"), TASHKENT)

    assert text == (
        f"⭐ <b>Sevimli:</b> {texts.WATCH_TITLES[kind.value]}\n"
        "⚽ <b>Arsenal — Chelsea</b>   🕒 20:00  🏆 Premier League\n"
        "2:1"
    )


def test_yesterday_tally_golden() -> None:
    summary = DaySummary(
        day=DAY,
        signals=8,
        won=4,
        lost=2,
        void=1,
        pending=1,
        profit_units=Decimal("2.3"),
        clv=0.012,
    )

    assert format_yesterday(summary) == (
        "📆 <b>Kecha:</b> 8 ta signal · 4 ✅ · 2 ❌ · 1 ↩️ · 1 ⏳ · +2.3 unit · CLV +1.2%"
    )


def test_digest_title_with_and_without_yesterday() -> None:
    digest = make_digest((make_signal(),))

    assert format_digest_title(digest) == (
        "☀️ <b>Kunlik tahlil</b> — Payshanba, 22.10\n"
        "📆 <b>Kecha:</b> 6 ta signal · 4 ✅ · 2 ❌ · +2.3 unit"
    )
    assert format_digest_title(make_digest((), yesterday=None)).count("\n") == 0


def test_batch_summary_lists_changes_and_why_others_were_skipped() -> None:
    batch = RecalcBatch(
        query_id="q1",
        diffs=(make_diff(), make_diff(after=numbers(), verdict=RecalcVerdict.STILL_VALID)),
        skipped_cooldown=(1, 2),
        skipped_started=(3,),
        failed=(4,),
    )

    text = format_recalc_batch(batch, TASHKENT)

    assert text.splitlines()[0] == (
        "🔄 <b>Qayta hisoblash tugadi:</b> yangilandi 2 ta, raqami o'zgargan 1 ta."
    )
    assert text.count("yangilandi (14:32)") == 1
    assert text.splitlines()[-3:] == [
        "⏳ 2 ta signal yaqinda yangilangan (bitta o'yin 10 daqiqada bir marta).",
        "⚽ 1 ta o'yin boshlangan: qayta hisoblanmadi.",
        "⚠️ 1 ta signalni yangilab bo'lmadi. Keyinroq qayta urinib ko'ring.",
    ]


def test_batch_without_changes_and_empty_batch() -> None:
    quiet = RecalcBatch(query_id="q", diffs=(make_diff(after=numbers()),))

    assert texts.RECALC_BATCH_UNCHANGED in format_recalc_batch(quiet, TASHKENT)
    assert format_recalc_batch(RecalcBatch(query_id="q", diffs=()), TASHKENT) == (
        texts.RECALC_BATCH_EMPTY
    )


def test_batch_caps_the_listed_changes() -> None:
    many = tuple(make_diff() for _ in range(MAX_BATCH_DIFFS + 3))

    text = format_recalc_batch(RecalcBatch(query_id="q", diffs=many), TASHKENT)

    assert text.count("yangilandi (14:32)") == MAX_BATCH_DIFFS
    assert "…va yana 3 ta o'zgarish." in text


def test_admin_alert_is_escaped_and_cut() -> None:
    alert = AdminAlert(component="<pipeline>", message=HOSTILE + " x" * 2000, at=NOW)

    text = format_admin_alert(alert, TASHKENT)

    assert text.startswith("🚨 <b>Tizim xatosi</b> · &lt;pipeline&gt; · 22.10 14:32\n")
    assert telegram_html_problems(text) == []
    assert len(text.split("\n", 1)[1]) <= MAX_ALERT_CHARS + 40  # escaping may add entities
    assert "<script>" not in text


def test_hostile_names_never_break_html() -> None:
    signal = make_signal(home_team=HOSTILE, away_team="A&B", league="<i>L</i>")
    diff = make_diff(signal=signal)
    messages = [
        format_recalc_diff(diff, TASHKENT),
        format_update(SignalUpdate(kind=UpdateKind.PREMATCH, diff=diff), TASHKENT),
        format_cancelled(SignalCancelled(signal=signal, reason=CancelReason.ABANDONED)),
        format_settled(SettledNotice(signal=signal)),
        format_recalc_batch(RecalcBatch(query_id="q", diffs=(diff,)), TASHKENT),
    ]

    for message in messages:
        assert telegram_html_problems(message) == [], message
        assert "<script>" not in message


@given(st.text(max_size=300))
def test_any_detail_text_renders_valid_html(detail: str) -> None:
    fixture = FixtureOption(
        fixture_id="fx1",
        sport=Sport.FOOTBALL,
        league=detail,
        home_team=detail,
        away_team="B",
        kickoff=KICKOFF,
    )
    event = WatchlistEvent(kind=WatchNotification.RESULT, fixture=fixture, detail=detail)

    assert telegram_html_problems(format_watch_event(event, TASHKENT)) == []
    alert = AdminAlert(component=detail or "x", message=detail, at=NOW)
    assert telegram_html_problems(format_admin_alert(alert, TASHKENT)) == []
