"""QA (T5 bot): reports, periods in the user's zone, CSV export and charts.

Periods and counts run through the real services (independent oracle: the local date of each
kickoff computed with ``zoneinfo``). The CSV and chart checks are pure-function tests with
hostile data (spreadsheet formulas, quotes, line breaks, ``$`` in a league name).
"""

import csv
import io
import subprocess
import sys
import threading
import time
import warnings
import zoneinfo
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.bot import charts, texts
from app.bot.callbacks import ReportCb
from app.domain import Market, Selection
from app.pipeline.profile import utc_bounds
from app.services.interfaces import (
    BetView,
    CalibrationBucket,
    ReportRequest,
    ResultMark,
    RiskProfile,
    SegmentStat,
)
from app.services.report_math import SignalOutcome, bets_export_file, export_file
from tests.bot.samples import make_signal
from tests.e2e.bot_client import shown
from tests.e2e.real_world import ADMIN, SECOND, RealBot
from tests.services.contract_suite import retro_signals

# A spreadsheet runs a cell as a formula when it starts with one of these.
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
HOSTILE = (
    '=HYPERLINK("http://evil.example","click")',
    "+cmd|' /C calc'!A0",
    "-2+3",
    "@SUM(A1:A9)",
)


def _rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8"))))


def _outcome(**overrides: object) -> SignalOutcome:
    return SignalOutcome(
        view=make_signal(**overrides),
        profit_units=Decimal(0),
        clv=None,
        risk_profile=RiskProfile.BALANCED,
    )


def _bet(label: str) -> BetView:
    return BetView(
        bet_id=1,
        signal_id=7,
        fixture_label=label,
        market=Market.match_result(),
        selection=Selection.HOME,
        odds_taken=1.9,
        stake=Decimal("50000"),
        placed_at=datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
    )


# --------------------------------------------------------------------------- CSV
@pytest.mark.xfail(
    strict=True,
    reason="QA F-3: provider-supplied names (league, teams, fixture label) go into the CSV "
    "unescaped, so a name starting with = + - @ runs as a formula in Excel/Sheets",
)
@pytest.mark.parametrize("payload", HOSTILE)
def test_signals_csv_neutralises_formula_cells(payload: str) -> None:
    file = export_file([_outcome(league=payload, home_team=payload, away_team=payload)])

    cells = _rows(file.content)[1]
    texty = [cells[3], cells[4], cells[5]]  # league, home, away
    assert not any(cell.startswith(FORMULA_STARTS) for cell in texty)


@pytest.mark.xfail(
    strict=True,
    reason="QA F-3: same as the signals file, for the bets file's fixture column",
)
@pytest.mark.parametrize("payload", HOSTILE)
def test_bets_csv_neutralises_formula_cells(payload: str) -> None:
    file = bets_export_file([_bet(payload)])

    assert not _rows(file.content)[1][2].startswith(FORMULA_STARTS)


def test_csv_keeps_numbers_numeric_and_round_trips_awkward_text() -> None:
    """A fix for the injection must not turn ``-1.5`` or a loss ``-50`` into text."""
    awkward = 'Team, "Quoted"\nNewline'
    market_line = Market.asian_handicap(-1.5) if hasattr(Market, "asian_handicap") else None
    kwargs: dict[str, object] = {"home_team": awkward}
    if market_line is not None:
        kwargs["market"] = market_line
    outcome = SignalOutcome(
        view=make_signal(**kwargs),
        profit_units=Decimal("-50"),
        clv=None,
        risk_profile=RiskProfile.BALANCED,
    )

    rows = _rows(export_file([outcome]).content)

    assert rows[1][4] == awkward  # quoting round-trips
    assert rows[1][-1] == "-50"  # a negative profit stays a plain number
    assert len(rows[0]) == len(rows[1])


def test_csv_is_utf8_with_the_uzbek_apostrophes_intact() -> None:
    file = export_file([_outcome(league="O'zbekiston Superligasi", home_team="Bunyodkor")])

    assert "O'zbekiston Superligasi" in file.content.decode("utf-8")
    assert file.filename == "signals.csv"


# --------------------------------------------------------------------------- periods
def test_utc_day_bounds_partition_time_in_every_zone_through_a_whole_year() -> None:
    """For all IANA zones: a day starts and ends on a local date boundary, no gap, no overlap."""
    first = date(2026, 1, 1)
    problems: list[str] = []
    for name in sorted(zoneinfo.available_timezones()):
        zone = zoneinfo.ZoneInfo(name)
        for offset in range(366):
            day = first + timedelta(days=offset)
            start, end = utc_bounds(zone, day, day)
            next_start, _ = utc_bounds(zone, day + timedelta(days=1), day + timedelta(days=1))
            tiny = timedelta(microseconds=1)
            if not (
                start < end
                and end == next_start
                and start.astimezone(zone).date() == day
                and (end - tiny).astimezone(zone).date() == day
            ):
                problems.append(f"{name} {day}")
    assert problems == []


async def _report_count(rb: RealBot, day: date, user_id: int = ADMIN) -> int:
    request = ReportRequest(date_from=day, date_to=day)
    return (await rb.services.reports.report(user_id, request)).signals


@pytest.mark.slow
@pytest.mark.parametrize(
    "zone",
    [
        "Asia/Tashkent",
        "Pacific/Auckland",  # UTC+13: the local day starts 11:00 UTC the day before
        "America/New_York",  # DST ends 1 Nov 2026
        "Europe/London",  # DST ends 25 Oct 2026, a 25-hour day inside the seeded week
        "Pacific/Kiritimati",  # UTC+14
        "Pacific/Pago_Pago",  # UTC-11
        "Australia/Lord_Howe",  # half-hour DST shift
    ],
)
async def test_report_days_follow_the_local_kickoff_date_in_any_zone(
    real_bot: RealBot, zone: str
) -> None:
    rb = real_bot
    await rb.settings(timezone=zone)
    signals = await rb.signals(distinct=3)
    rb.finish_matches()
    tz = zoneinfo.ZoneInfo(zone)
    local = [signal.kickoff.astimezone(tz).date() for signal in signals]
    first, last = min(local) - timedelta(days=1), max(local) + timedelta(days=1)

    total = 0
    day = first
    while day <= last:
        expected = sum(1 for d in local if d == day)
        assert await _report_count(rb, day) == expected, f"{zone} {day}"
        total += expected
        day += timedelta(days=1)

    assert total == len(signals)
    whole = await rb.services.reports.report(ADMIN, ReportRequest(date_from=first, date_to=last))
    assert whole.signals == len(signals)  # the days add up to the range


@pytest.mark.slow
async def test_the_typed_range_on_the_report_screen_uses_the_users_midnight(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    await rb.settings(timezone="Pacific/Auckland")
    signals = await rb.signals(distinct=3)
    rb.finish_matches()
    tz = zoneinfo.ZoneInfo("Pacific/Auckland")
    target = signals[0].kickoff.astimezone(tz).date()
    expected = sum(1 for s in signals if s.kickoff.astimezone(tz).date() == target)
    admin = rb.client()
    await admin.send(texts.MENU_REPORT)

    await admin.press_data(ReportCb(action="custom", period="c").pack(), admin.last_message_id)
    methods = await admin.send(f"{target:%d.%m.%Y} - {target:%d.%m.%Y}")

    assert f"Signallar: <b>{expected}</b>" in "\n".join(shown(methods))


@pytest.mark.slow
async def test_retro_is_excluded_and_hidden_signals_are_included_in_report_and_export(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    svc = rb.services
    signals = await rb.signals(distinct=2)
    before = await svc.reports.report(ADMIN, ReportRequest.all_time())
    retro = await retro_signals(svc, ADMIN)
    assert retro, "the seeded world has last week's matches"

    await svc.signals.hide_signal(ADMIN, signals[0].signal_id)
    after = await svc.reports.report(ADMIN, ReportRequest.all_time())
    export = _rows((await svc.reports.export(ADMIN)).content)

    assert after.signals == before.signals  # retro added nothing, hiding removed nothing
    ids = {row[0] for row in export[1:]}
    assert str(signals[0].signal_id) in ids
    hidden_column = export[0].index("hidden")
    by_id = {row[0]: row for row in export[1:]}
    assert by_id[str(signals[0].signal_id)][hidden_column] == "True"
    assert not ({str(s.signal_id) for s in retro} & ids)


@pytest.mark.slow
async def test_export_command_sends_two_csv_documents_with_only_my_rows(real_bot: RealBot) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    await rb.settings(bank=Decimal(1000), unit=Decimal(10))
    await rb.bet(signal, 25)

    methods = await rb.client().send("/eksport")
    documents = [m for m in methods if type(m).__name__ == "SendDocument"]

    assert [d.document.filename for d in documents] == ["signals.csv", "bets.csv"]  # type: ignore[union-attr]
    other = await rb.client(SECOND).send("/eksport")
    assert [type(m).__name__ for m in other].count("SendDocument") == 2
    assert not _rows((await rb.services.reports.export_bets(SECOND)).content)[1:]
    assert len(_rows((await rb.services.reports.export_bets(ADMIN)).content)) == 2


# --------------------------------------------------------------------------- charts
def _buckets() -> list[CalibrationBucket]:
    return [
        CalibrationBucket(lower=0.4, upper=0.5, count=12, predicted=0.45, actual=0.5),
        CalibrationBucket(lower=0.5, upper=0.6, count=30, predicted=0.55, actual=0.52),
    ]


def _segments(names: list[str]) -> list[SegmentStat]:
    return [
        SegmentStat(
            key=name, signals=10, profit_units=Decimal(1), yield_rate=0.05, clv=0.01, win_rate=0.5
        )
        for name in names
    ]


def test_charts_import_headless_without_pyplot_or_a_display() -> None:
    code = (
        "import sys, os\n"
        "os.environ.pop('DISPLAY', None); os.environ.pop('MPLBACKEND', None)\n"
        "from app.bot import charts\n"
        "from decimal import Decimal\n"
        "from datetime import date\n"
        "png = charts.bank_chart([(date(2026,10,21), Decimal(900))], Decimal(1000))\n"
        "assert png[:8] == b'\\x89PNG\\r\\n\\x1a\\n'\n"
        "assert 'matplotlib.pyplot' not in sys.modules, 'pyplot (global state) was imported'\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr


def test_every_chart_is_a_png_and_raises_no_font_warning_for_our_own_texts() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a "Glyph missing" UserWarning would fail here
        points = [(date(2026, 10, 20) + timedelta(days=i), Decimal(1000 + i * 7)) for i in range(5)]
        pngs = [
            charts.bank_chart(points, Decimal(1000)),
            charts.calibration_chart(_buckets()),
            charts.segments_chart(_segments(["Premier League", "Süper Lig", "Série A"]), "Yield"),
        ]
    assert all(png.startswith(b"\x89PNG") and len(png) > 2_000 for png in pngs)


@pytest.mark.xfail(
    strict=True,
    reason="QA F-8: a league name with a TeX-like '$..$' goes to matplotlib as mathtext and "
    "raises, so the whole chart (and the button) fails with the generic error",
)
def test_a_league_name_with_dollar_signs_still_draws() -> None:
    charts.segments_chart(_segments(["Cup $\\bad$ final"]), "Yield")


@pytest.mark.parametrize("name", ["中超联赛", "Лига 1", "⚽ Liga", "A" * 300, ""])
def test_unusual_league_names_do_not_crash_the_chart(name: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # missing glyphs are cosmetic; a crash is not
        png = charts.segments_chart(_segments([name or "-"]), "Yield")
    assert png.startswith(b"\x89PNG")


def test_large_data_charts_render_within_a_few_seconds() -> None:
    points = [
        (date(2020, 1, 1) + timedelta(days=i), Decimal(1000 + (i % 40) * 13)) for i in range(3000)
    ]
    names = [f"League {i}" for i in range(60)]

    started = time.perf_counter()
    charts.bank_chart(points, Decimal(1000))
    charts.segments_chart(_segments(names), "Yield")
    elapsed = time.perf_counter() - started

    assert elapsed < 20, f"charts took {elapsed:.1f}s"


@pytest.mark.slow
async def test_chart_buttons_render_off_the_event_loop_thread(
    real_bot: RealBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    rb = real_bot
    seen: list[str] = []
    original = charts.calibration_chart

    def spy(buckets: object) -> bytes:
        seen.append(threading.current_thread().name)
        return original(buckets)  # type: ignore[arg-type]

    monkeypatch.setattr("app.bot.handlers.report.calibration_chart", spy)
    await rb.settings(bank=Decimal(1000), unit=Decimal(10))
    signals = await rb.signals(distinct=3)
    for signal in signals:
        await rb.bet(signal, 10)
    rb.finish_matches()
    for signal in signals:
        await rb.mark(signal, ResultMark.WON)
    admin = rb.client()

    methods = await admin.press_data(ReportCb(action="chart", period="all").pack(), 80)

    assert seen, "three decided signals give a calibration chart"
    assert threading.current_thread().name not in seen
    assert any(type(m).__name__ == "SendPhoto" for m in methods)
