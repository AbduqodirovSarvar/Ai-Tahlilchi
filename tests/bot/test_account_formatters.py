"""``/holat`` and bets formatting: golden strings, boundaries and hostile text."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.bot import texts
from app.bot.account_formatters import (
    MAX_LISTED_BETS,
    MAX_STATUS_ERRORS,
    age_label,
    default_stake,
    format_bet_placed,
    format_bet_prompt,
    format_bets,
    format_status,
    stake_label,
)
from app.domain import Market, Selection, Sport
from app.services.interfaces import (
    ApiQuota,
    BankPoint,
    BankView,
    BetView,
    ErrorEntry,
    JobStatus,
    LlmQuota,
    ResultMark,
    SystemStatus,
    UserSettingsView,
)
from tests.bot.samples import NOW, make_signal
from tests.e2e.bot_client import telegram_html_problems

TASHKENT = ZoneInfo("Asia/Tashkent")


def make_status(**overrides: object) -> SystemStatus:
    base = SystemStatus(
        quotas=(
            ApiQuota(
                provider="api-football",
                sport=Sport.FOOTBALL,
                used=66,
                limit=100,
                reserved_manual=12,
                manual_reserve=30,
                manual_used=12,
            ),
        ),
        llm=LlmQuota(requests_used=7, requests_limit=250, tokens_used=48_000, tokens_limit=500_000),
        last_refresh_at=NOW - timedelta(hours=2),
        jobs=(
            JobStatus(name="refresh", last_run_at=NOW - timedelta(hours=2), ok=True),
            JobStatus(name="settle", last_run_at=None, ok=False, detail="<timeout> & retry"),
        ),
        recent_errors=(ErrorEntry(at=NOW, component="gemini", message="429 <rate>"),),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_status_golden() -> None:
    text = format_status(make_status(), TASHKENT, NOW, "real")

    assert text == (
        "ℹ️ <b>Tizim holati</b>\n"
        "🟢 Rejim: <b>haqiqiy ma'lumot</b>\n"
        "\n"
        "📡 <b>API limitlari</b> (bugun):\n"
        "• api-football (⚽ Futbol): ishlatilgan 66/100 · qoldi 34\n"
        "  qo'lda so'rovlar: 12/30 · qo'lda qoldi 18\n"
        "\n"
        "🤖 <b>AI (Gemini)</b> (bugun): so'rovlar 7/250 · tokenlar 48000/500000\n"
        "\n"
        "🔄 Ma'lumot oxirgi yangilangan: <b>22.10 12:32</b> (2 soat oldin)\n"
        "\n"
        "🗓 <b>Oxirgi ishlar:</b>\n"
        "• ✅ refresh — 22.10 12:32\n"
        "• ❌ settle — hali ishlamagan (&lt;timeout&gt; &amp; retry)\n"
        "\n"
        "🧯 <b>Oxirgi xatolar:</b>\n"
        "• 22.10 14:32 · gemini: 429 &lt;rate&gt;\n"
        "\n"
        "Buyruqlar va atamalar: /yordam"
    )
    assert telegram_html_problems(text) == []


def test_status_manual_reserve_never_goes_negative_and_llm_is_optional() -> None:
    quota = ApiQuota(
        provider="p",
        sport=None,
        used=5,
        limit=100,
        reserved_manual=0,
        manual_reserve=3,
        manual_used=9,
    )

    text = format_status(make_status(quotas=(quota,), llm=None), TASHKENT, NOW, "real")

    assert "qo'lda so'rovlar: 9/3 · qo'lda qoldi 0" in text
    assert texts.STATUS_LLM_NONE in text


def test_status_empty_system_and_fake_mode() -> None:
    empty = SystemStatus(quotas=(), last_refresh_at=None, jobs=(), recent_errors=())

    text = format_status(empty, TASHKENT, NOW, "fake")

    assert texts.STATUS_MODE["fake"] in text
    for line in (
        texts.STATUS_QUOTA_NONE,
        texts.STATUS_LLM_NONE,
        texts.STATUS_REFRESH_NEVER,
        texts.STATUS_JOBS_NONE,
        texts.STATUS_ERRORS_NONE,
    ):
        assert line in text


def test_status_with_unknown_backend_escapes_it() -> None:
    text = format_status(make_status(), TASHKENT, NOW, "<x>")

    assert "&lt;x&gt;" in text


def test_status_lists_a_limited_number_of_errors() -> None:
    errors = tuple(
        ErrorEntry(at=NOW, component="job", message=f"e{index}")
        for index in range(MAX_STATUS_ERRORS + 4)
    )

    text = format_status(make_status(recent_errors=errors), TASHKENT, NOW, "real")

    assert text.count("· job:") == MAX_STATUS_ERRORS
    assert "…va yana 4 ta" in text


def test_exhausted_quota_never_shows_negative_remaining() -> None:
    quota = ApiQuota(provider="p", sport=None, used=120, limit=100, reserved_manual=0)

    text = format_status(make_status(quotas=(quota,)), TASHKENT, NOW, "real")

    assert "ishlatilgan 120/100 · qoldi 0" in text
    assert "p:" in text  # no sport suffix for provider-wide quotas


@pytest.mark.parametrize(
    ("delta", "label"),
    [
        (timedelta(seconds=20), "hozirgina"),
        (timedelta(minutes=1), "1 daq. oldin"),
        (timedelta(minutes=59, seconds=59), "59 daq. oldin"),
        (timedelta(hours=1), "1 soat oldin"),
        (timedelta(hours=23, minutes=59), "23 soat oldin"),
        (timedelta(days=3, hours=5), "3 kun oldin"),
        (timedelta(minutes=-5), "hozirgina"),
    ],
)
def test_age_label_boundaries(delta: timedelta, label: str) -> None:
    assert age_label(NOW - delta, NOW) == label


@given(st.text(max_size=200), st.text(max_size=200))
def test_status_with_any_text_is_valid_html(component: str, message: str) -> None:
    status = make_status(
        recent_errors=(ErrorEntry(at=NOW, component=component, message=message),),
        jobs=(JobStatus(name=component, last_run_at=NOW, ok=False, detail=message),),
    )

    assert telegram_html_problems(format_status(status, TASHKENT, NOW, "real")) == []


# --------------------------------------------------------------------------- bets
def settings(**overrides: object) -> UserSettingsView:
    base = UserSettingsView(unit=Decimal("10000"), bank=Decimal("1000000"))
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_default_stake_needs_a_unit_and_an_advised_stake() -> None:
    signal = make_signal(stake_units=Decimal("1.5"))

    assert default_stake(settings(), signal) == Decimal("15000.0")
    assert default_stake(settings(unit=None, bank=None), signal) is None
    assert default_stake(settings(), make_signal(stake_units=Decimal(0))) is None


def test_bet_prompt_golden_with_and_without_unit() -> None:
    signal = make_signal()

    assert format_bet_prompt(signal, settings()) == (
        "✅ <b>Stavka qildim</b>\n"
        "Arsenal — Chelsea\n"
        "🎯 Arsenal g'alabasi (1)\n"
        "Signal koeffitsienti: <b>1.85</b> · tavsiya: <b>15 000 (1.5 unit)</b>\n"
        "\n"
        "Shu qiymatlar bilan yozaymi yoki o'zingiz kiritasizmi?"
    )
    assert stake_label(settings(unit=None, bank=None), signal) == "1.5 unit"


def make_bet(**overrides: object) -> BetView:
    base = BetView(
        bet_id=1,
        signal_id=7,
        fixture_label="Arsenal — Chelsea",
        market=Market.totals(2.5),
        selection=Selection.OVER,
        odds_taken=1.9,
        stake=Decimal("50000"),
        placed_at=NOW,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_bet_placed_golden() -> None:
    assert format_bet_placed(make_bet()) == (
        "🎟 <b>Stavka yozildi</b>\n"
        "Arsenal — Chelsea\n"
        "🎯 Total 2.5 dan ko'p @ <b>1.90</b>\n"
        "💵 Summa: <b>50 000</b>"
    )


def bank(**overrides: object) -> BankView:
    base = BankView(
        balance=Decimal("1020000"),
        unit=Decimal("10000"),
        month_profit=Decimal("20000"),
        open_bets=2,
        history=(BankPoint(day=date(2026, 10, 21), balance=Decimal("1020000")),),
        loss_today=Decimal(0),
        loss_this_week=Decimal(0),
        loss_limit_reached=False,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_bets_view_golden() -> None:
    bets = (make_bet(), make_bet(bet_id=2, market=Market.match_result(), selection=Selection.HOME))

    assert format_bets(bank(), bets, TASHKENT) == (
        "🎟 <b>Mening stavkalarim</b>\n"
        "💰 Balans: <b>1 020 000</b> (boshlang'ich bank + yopilgan stavkalar foydasi)\n"
        "📅 Oy foydasi: 20 000\n"
        "🟡 Ochiq stavkalar: 2 (balansni kamaytirmaydi)\n"
        "\n"
        "• Arsenal — Chelsea — Total 2.5 dan ko'p @ 1.90 · 50 000 · 22.10\n"
        "• Arsenal — Chelsea — uy jamoasi g'alabasi (1) @ 1.90 · 50 000 · 22.10"
    )


def test_bets_view_without_bank_and_without_bets() -> None:
    text = format_bets(bank(balance=None, open_bets=0, loss_limit_reached=True), (), TASHKENT)

    assert texts.BETS_BANK_UNSET in text
    assert "Oy foydasi" not in text
    assert texts.BETS_EMPTY in text
    assert "Zarar limitiga yetildi" in text


def test_bets_view_is_capped() -> None:
    bets = tuple(make_bet(bet_id=index) for index in range(MAX_LISTED_BETS + 3))

    text = format_bets(bank(), bets, TASHKENT)

    assert text.count("• ") == MAX_LISTED_BETS
    assert "…va yana 3 ta" in text


def test_bets_view_escapes_fixture_labels() -> None:
    bet = make_bet(fixture_label="<b>A</b> & B", outcome=ResultMark.WON)

    text = format_bets(bank(), (bet,), TASHKENT)

    assert "&lt;b&gt;A&lt;/b&gt; &amp; B" in text
    assert telegram_html_problems(text) == []


def test_naive_free_dates_use_the_users_zone() -> None:
    late = datetime(2026, 10, 22, 21, 30, tzinfo=UTC)  # already 23.10 in Tashkent

    text = format_bets(bank(), (make_bet(placed_at=late),), TASHKENT)

    assert "· 23.10" in text


def test_status_shows_the_build_when_known() -> None:
    text = format_status(make_status(build="1.0.0-rc1 (abc123)"), TASHKENT, NOW, "real")

    assert "🏷 Versiya: <b>1.0.0-rc1 (abc123)</b>" in text
    assert text.index("Versiya") < text.index("API limitlari")


def test_status_escapes_the_build_and_hides_the_line_when_unknown() -> None:
    assert "Versiya: <b>&lt;x&gt;</b>" in format_status(
        make_status(build="<x>"), TASHKENT, NOW, "real"
    )
    assert "Versiya" not in format_status(make_status(), TASHKENT, NOW, "real")
