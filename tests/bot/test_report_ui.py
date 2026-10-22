"""Report text: golden numbers, empty and thin data, hostile slice names."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.bot import texts
from app.bot.draft import StaleDraftError
from app.bot.report_ui import (
    MAX_SEGMENT_ROWS,
    build_request,
    calibration_block,
    format_report,
    period_label,
    report_keyboard,
    segment_label,
)
from app.services.interfaces import (
    CalibrationBucket,
    ReportGroup,
    ReportRequest,
    ReportView,
    RiskProfile,
    SegmentStat,
)
from tests.e2e.bot_client import buttons_of, telegram_html_problems


def make_view(**overrides: object) -> ReportView:
    base = ReportView(
        date_from=date(2026, 10, 15),
        date_to=date(2026, 10, 21),
        signals=12,
        settled=10,
        won=6,
        lost=3,
        void=1,
        profit_units=Decimal("2.35"),
        yield_rate=0.0904,
        roi=0.051,
        clv=0.0123,
        clv_beat_rate=0.6,
        win_rate=0.6667,
        longest_losing_streak=2,
        calibration=(
            CalibrationBucket(lower=0.6, upper=0.7, count=25, predicted=0.65, actual=0.64),
            CalibrationBucket(lower=0.5, upper=0.6, count=4, predicted=0.55, actual=0.5),
        ),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def segment(key: str, **overrides: object) -> SegmentStat:
    base = SegmentStat(
        key=key,
        signals=4,
        profit_units=Decimal("-0.5"),
        yield_rate=-0.12,
        clv=None,
        win_rate=0.5,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_report_golden_summary() -> None:
    text = format_report(make_view(), None)

    lines = text.splitlines()
    assert lines[0] == f"📈 <b>Hisobot</b> — 15.10{texts.RANGE_DASH}21.10"
    assert lines[1] == "📊 Signallar: <b>12</b> · yopilgan 10: ✅ 6 · ❌ 3 · ↩️ 1"
    assert lines[2] == "💰 Foyda: <b>+2.35</b> unit · Yield: +9.0%"
    assert lines[3] == "🎟 ROI (sizning stavkalaringiz): +5.1%"
    assert lines[4] == "📐 CLV: +1.2% · koeffitsienti yopilishdan yaxshi: 60% signalda"
    assert lines[5] == "🎯 Win rate: 67% · eng uzun mag'lubiyat seriyasi: 2"
    assert "💡 60–70% deb berilgan signallar haqiqatda 64% holatda o'tdi (25 ta)." in text
    assert telegram_html_problems(text) == []


def test_missing_values_show_dashes_not_zero() -> None:
    text = format_report(
        make_view(roi=None, clv=None, clv_beat_rate=None, win_rate=None, yield_rate=None), None
    )

    assert texts.REPORT_ROI_NONE in text
    assert "CLV: — · koeffitsienti yopilishdan yaxshi: — signalda" in text
    assert "Yield: —" in text
    assert "Win rate: —" in text


def test_empty_period_is_explained() -> None:
    text = format_report(make_view(signals=0, settled=0, calibration=()), None)

    assert text.endswith(texts.REPORT_EMPTY)


def test_signals_without_a_settlement_say_to_wait() -> None:
    text = format_report(make_view(settled=0, won=0, lost=0, void=0, calibration=()), None)

    assert texts.REPORT_NO_SETTLED in text


def test_thin_calibration_withholds_the_sentence() -> None:
    thin = (CalibrationBucket(lower=0.6, upper=0.7, count=3, predicted=0.65, actual=1.0),)

    lines = calibration_block(thin)

    assert "deb berilgan signallar haqiqatda" not in "\n".join(lines)
    assert texts.REPORT_CALIBRATION_THIN.format(count=3) in lines
    assert calibration_block(()) == []


def test_slice_table_translates_enums_and_escapes_names() -> None:
    view = make_view(
        segments=(segment("football"), segment("La <Liga> & Co", signals=9)),
    )

    sport = format_report(view, ReportGroup.SPORT)
    league = format_report(view, ReportGroup.LEAGUE)

    assert "⚽ Futbol" in sport
    assert "La &lt;Liga&gt; &amp; Co" in league
    assert telegram_html_problems(league) == []
    assert "Kesim bo'yicha" in sport
    assert "Kesim bo'yicha" not in format_report(view, None)


def test_long_slice_lists_are_cut() -> None:
    view = make_view(segments=tuple(segment(f"League {i}") for i in range(MAX_SEGMENT_ROWS + 4)))

    text = format_report(view, ReportGroup.LEAGUE)

    assert texts.REPORT_MORE.format(count=4) in text
    assert len(text) < 4096


@pytest.mark.parametrize(
    ("group", "key", "expected"),
    [
        (ReportGroup.MARKET, "totals", "Total"),
        (ReportGroup.RISK_PROFILE, "balanced", texts.RISK_LABELS[RiskProfile.BALANCED]),
        (ReportGroup.CONFIDENCE, "high", "Yuqori"),
        (ReportGroup.SPORT, "unknown-sport", "unknown-sport"),
        (ReportGroup.MARKET, "mystery", "mystery"),
    ],
)
def test_segment_labels(group: ReportGroup, key: str, expected: str) -> None:
    assert segment_label(group, key) == expected


@given(st.text(max_size=40))
def test_any_league_name_keeps_the_report_valid_html(name: str) -> None:
    text = format_report(make_view(segments=(segment(name or "x"),)), ReportGroup.LEAGUE)

    assert telegram_html_problems(text) == []


def test_build_request() -> None:
    assert build_request("7", None, None) == ReportRequest.recent(7)
    assert build_request("30", ReportGroup.LEAGUE, None) == ReportRequest.recent(
        30, group_by=ReportGroup.LEAGUE
    )
    assert build_request("all", None, None) == ReportRequest.all_time()
    custom = (date(2026, 10, 1), date(2026, 10, 5))
    assert build_request("c", None, custom) == ReportRequest(date_from=custom[0], date_to=custom[1])
    for bad in (("9", None), ("c", None), ("", None)):
        with pytest.raises(StaleDraftError):
            build_request(bad[0], None, bad[1])


def test_period_label_for_all_time() -> None:
    assert period_label(make_view(date_from=None)) == "hammasi (21.10 gacha)"


def test_keyboard_highlights_the_choice_and_fits_callback_data() -> None:
    markup = report_keyboard("30", ReportGroup.MARKET)

    labels = [button.text for button in buttons_of(markup)]
    assert "✅ 30 kun" in labels and "✅ bozor" in labels
    assert "▫️ 7 kun" in labels and "▫️ kesimsiz" in labels
    assert all(len((b.callback_data or "").encode()) <= 64 for b in buttons_of(markup))
