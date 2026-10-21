"""Pure text formatting: signal cards, day groups, summaries and Telegram-size splitting.

Every value that may come from a user or an external source (team, league, note) is
HTML-escaped here; templates in :mod:`app.bot.texts` are trusted HTML.
"""

import html
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from itertools import groupby
from typing import Final
from zoneinfo import ZoneInfo

from app.bot import texts
from app.domain import MarketType, Selection, Sport
from app.services.errors import BudgetExceededError, CooldownActiveError, ValidationFailedError
from app.services.interfaces import (
    MAX_FUTURE_DAYS,
    MAX_ODDS_LIMIT,
    MAX_SCOPE_IDS,
    MAX_TEMPLATE_NAME_LENGTH,
    MAX_WINDOW_DAYS,
    MIN_EV_LOWER,
    MIN_EV_UPPER,
    MIN_ODDS_LIMIT,
    MIN_PROBABILITY_LOWER,
    MIN_PROBABILITY_UPPER,
    MIN_SEARCH_LENGTH,
    AnalysisResult,
    DateWindow,
    SignalState,
    SignalView,
    UserSettingsView,
    Verdict,
)

TELEGRAM_TEXT_LIMIT: Final = 4096
MAX_NOTE_CHARS: Final = 600
"""LLM notes are cut so one card always fits a message comfortably."""
PERCENT: Final = 100
SECONDS_PER_MINUTE: Final = 60


def escape(text: str) -> str:
    """Make untrusted text safe for ``parse_mode=HTML`` (apostrophes stay readable)."""
    return html.escape(text, quote=False)


# --------------------------------------------------------------------------- numbers
def percent(fraction: float, *, signed: bool = False, decimals: int = 0) -> str:
    """``0.613`` → ``61%``; signed: ``+12.9%`` (EV)."""
    value = fraction * PERCENT
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def odds(value: float) -> str:
    """Decimal odds with two decimals."""
    return f"{value:.2f}"


def units(value: Decimal) -> str:
    """``Decimal('1.50')`` → ``1.5``."""
    return f"{value.normalize():f}"


def money(value: Decimal | None) -> str:
    """Amount with thousands separated by spaces, or "kiritilmagan"."""
    if value is None:
        return texts.NOT_SET
    return f"{value:,.2f}".replace(",", " ").removesuffix(".00")


def clock(value: time) -> str:
    """``09:00``."""
    return value.strftime("%H:%M")


def short_date(day: date) -> str:
    """``25.10``."""
    return day.strftime("%d.%m")


def weekday_name(day: date) -> str:
    """Uzbek weekday name."""
    return texts.WEEKDAYS[day.weekday()]


def window_label(window: DateWindow) -> str:
    """``25.10-31.10`` (typographic dash) or ``25.10`` for a single day."""
    if window.date_from == window.date_to:
        return texts.WINDOW_SINGLE.format(day=short_date(window.date_from))
    return texts.WINDOW_RANGE.format(
        start=short_date(window.date_from), end=short_date(window.date_to)
    )


# --------------------------------------------------------------------------- labels
def sport_label(sport: Sport) -> str:
    """``⚽ Futbol``."""
    return texts.SPORT_LABELS[sport]


def sports_label(sports: Iterable[Sport]) -> str:
    """Sports in enum order; every sport → "🌐 Hammasi"."""
    chosen = set(sports)
    if chosen == set(Sport):
        return texts.ALL_SPORTS
    return ", ".join(texts.SPORT_LABELS[sport] for sport in Sport if sport in chosen)


def markets_label(markets: Iterable[MarketType]) -> str:
    """Market names in enum order."""
    chosen = set(markets)
    return ", ".join(texts.MARKET_LABELS[market] for market in MarketType if market in chosen)


def _line(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return ""
    return f"{value:+g}" if signed else f"{value:g}"


def selection_label(signal: SignalView) -> str:
    """Human description of the backed selection (``Total 2.5 dan ko'p``)."""
    home, away = escape(signal.home_team), escape(signal.away_team)
    market, selection = signal.market, signal.selection
    match market.type:
        case MarketType.MATCH_RESULT:
            by_selection = {
                Selection.HOME: texts.SELECTION_HOME_WIN.format(home=home),
                Selection.DRAW: texts.SELECTION_DRAW,
                Selection.AWAY: texts.SELECTION_AWAY_WIN.format(away=away),
            }
            return by_selection[selection]
        case MarketType.DOUBLE_CHANCE:
            return texts.SELECTION_DOUBLE[selection]
        case MarketType.TOTALS:
            template = (
                texts.SELECTION_OVER if selection is Selection.OVER else texts.SELECTION_UNDER
            )
            return template.format(line=_line(market.line))
        case MarketType.BTTS:
            return (
                texts.SELECTION_BTTS_YES if selection is Selection.YES else texts.SELECTION_BTTS_NO
            )
        case MarketType.ASIAN_HANDICAP:
            line = market.line or 0.0
            is_home = selection is Selection.HOME
            return texts.SELECTION_HANDICAP.format(
                team=home if is_home else away, line=_line(line if is_home else -line, signed=True)
            )


# --------------------------------------------------------------------------- signal card
def _flag_lines(signal: SignalView, zone: ZoneInfo) -> list[str]:
    return [*_value_flags(signal), *_state_flags(signal, zone)]


def _value_flags(signal: SignalView) -> list[str]:
    """Flags about the signal's value and origin."""
    lines: list[str] = []
    if signal.preliminary:
        lines.append(texts.CARD_PRELIMINARY)
    if signal.negative_ev:
        lines.append(texts.CARD_NEGATIVE_EV)
    elif signal.no_value:
        lines.append(texts.CARD_NO_VALUE)
    if signal.watchlist:
        lines.append(texts.CARD_WATCHLIST)
    if signal.experimental:
        lines.append(texts.CARD_EXPERIMENTAL)
    if signal.llm_verdict is Verdict.CAUTION:
        lines.append(texts.CARD_CAUTION)
    return lines


def _state_flags(signal: SignalView, zone: ZoneInfo) -> list[str]:
    """Risk flags, cached data, cancellation and the settled result."""
    lines: list[str] = []
    if signal.risk_flags:
        flags = ", ".join(escape(flag) for flag in signal.risk_flags)
        lines.append(texts.CARD_RISK_FLAGS.format(flags=flags))
    if signal.data_as_of is not None:
        as_of = clock(signal.data_as_of.astimezone(zone).time())
        lines.append(texts.CARD_DATA_AS_OF.format(time=as_of))
    if signal.state is SignalState.CANCELLED:
        lines.append(texts.CARD_CANCELLED)
    if signal.result is not None:
        lines.append(texts.CARD_RESULT.format(result=texts.RESULT_LABELS[signal.result]))
    return lines


def _note(text: str | None) -> str | None:
    if text is None or not text.strip():
        return None
    cleaned = " ".join(text.split())
    if len(cleaned) > MAX_NOTE_CHARS:
        cleaned = cleaned[: MAX_NOTE_CHARS - 1] + "…"
    return texts.CARD_NOTE.format(note=escape(cleaned))


def format_signal_card(signal: SignalView, zone: ZoneInfo) -> str:
    """The §6.2 signal card (HTML), with every flag that applies to the signal."""
    local_kickoff = signal.kickoff.astimezone(zone)
    lines = [
        texts.CARD_HEADER.format(
            icon=texts.SPORT_ICONS[signal.sport],
            fixture=escape(signal.fixture_label),
            time=clock(local_kickoff.time()),
            league=escape(signal.league),
        ),
        texts.CARD_PICK.format(selection=selection_label(signal), odds=odds(signal.odds)),
        texts.CARD_PROBABILITY.format(
            probability=percent(signal.probability), fair_odds=odds(signal.fair_odds)
        ),
        texts.CARD_EV.format(
            ev=percent(signal.ev, signed=True, decimals=1),
            confidence=texts.CONFIDENCE_LABELS[signal.confidence],
        ),
        texts.CARD_STAKE.format(stake=units(signal.stake_units))
        if signal.stake_units > 0
        else texts.CARD_NO_STAKE,
    ]
    note = _note(signal.llm_note)
    if note is not None:
        lines.append(note)
    lines.extend(_flag_lines(signal, zone))
    return "\n".join(lines)


def format_hidden_card(signal: SignalView) -> str:
    """What remains of a card after "🙈 Yashirish"."""
    return texts.CARD_HIDDEN.format(fixture=escape(signal.fixture_label))


# --------------------------------------------------------------------------- results
@dataclass(frozen=True, slots=True)
class DayGroup:
    """Signals of one local calendar day, in the order the service sorted them."""

    day: date
    signals: tuple[SignalView, ...]


def group_by_day(signals: Iterable[SignalView], zone: ZoneInfo) -> tuple[DayGroup, ...]:
    """Group by local kickoff date (days ascending, service order kept inside a day)."""

    def local_day(signal: SignalView) -> date:
        return signal.kickoff.astimezone(zone).date()

    ordered = sorted(signals, key=local_day)  # stable: keeps the service's order per day
    return tuple(
        DayGroup(day=day, signals=tuple(items)) for day, items in groupby(ordered, key=local_day)
    )


def day_header(group: DayGroup, *, continued: bool = False) -> str:
    """``📅 Shanba, 25.10 — 3 ta signal``."""
    template = texts.DAY_HEADER_CONTINUED if continued else texts.DAY_HEADER
    return template.format(
        weekday=weekday_name(group.day), day=short_date(group.day), count=len(group.signals)
    )


def format_results_summary(result: AnalysisResult) -> str:
    """Header of an analysis result, with shortfall/failure/retro notes or the empty state."""
    window = window_label(result.window)
    if not result.signals:
        return texts.RESULTS_EMPTY.format(window=window, fixtures=result.fixtures_analyzed)
    lines = [
        texts.RESULTS_HEADER.format(
            window=window, count=len(result.signals), fixtures=result.fixtures_analyzed
        )
    ]
    if result.retro:
        lines.append(texts.RESULTS_RETRO)
    if result.shortfall and result.requested is not None:
        lines.append(
            texts.RESULTS_SHORTFALL.format(matched=result.matched, requested=result.requested)
        )
    if result.failed_fixtures:
        lines.append(texts.RESULTS_FAILED.format(count=result.failed_fixtures))
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PageItem:
    """One message of a result page: a day header (``signal`` is ``None``) or a card."""

    text: str
    signal: SignalView | None = None


def result_page(
    signals: Sequence[SignalView], zone: ZoneInfo, page: int, page_size: int
) -> tuple[PageItem, ...]:
    """Messages of result page ``page``: day headers interleaved with cards.

    A day that started on an earlier page gets a "(davomi)" header.
    """
    groups = group_by_day(signals, zone)
    flat = [(group, signal) for group in groups for signal in group.signals]
    chunk = flat[page * page_size : (page + 1) * page_size]
    items: list[PageItem] = []
    previous_day: date | None = None
    for position, (group, signal) in enumerate(chunk):
        if group.day != previous_day:
            continued = position == 0 and group.signals[0] is not signal
            items.append(PageItem(text=day_header(group, continued=continued)))
            previous_day = group.day
        items.append(PageItem(text=format_signal_card(signal, zone), signal=signal))
    return tuple(items)


def ordered_for_display(signals: Sequence[SignalView], zone: ZoneInfo) -> tuple[SignalView, ...]:
    """Signals in display order (by day, then the service order)."""
    return tuple(signal for group in group_by_day(signals, zone) for signal in group.signals)


# --------------------------------------------------------------------------- settings
def settings_values(settings: UserSettingsView, league_names: dict[str, str]) -> dict[str, str]:
    """Display value of every settings row (keys of :data:`texts.SETTINGS_ROWS`)."""
    leagues = (
        ", ".join(
            escape(league_names.get(league, league)) for league in sorted(settings.league_ids)
        )
        if settings.league_ids
        else texts.DEFAULT_LEAGUES
    )
    quiet = settings.quiet_hours
    return {
        "sports": sports_label(settings.sports),
        "leagues": leagues,
        "markets": markets_label(settings.markets),
        "max_signals": str(settings.max_signals),
        "min_probability": percent(settings.min_probability),
        "min_ev": percent(settings.min_ev, decimals=0),
        "odds": f"{odds(settings.odds_min)}{texts.RANGE_DASH}{odds(settings.odds_max)}",
        "risk": texts.RISK_LABELS[settings.risk_profile],
        "send_time": clock(settings.send_time),
        "prematch": texts.OFF
        if settings.prematch_minutes is None
        else texts.PREMATCH_MINUTES.format(minutes=settings.prematch_minutes),
        "quiet": texts.OFF
        if quiet is None
        else f"{clock(quiet.start)}{texts.RANGE_DASH}{clock(quiet.end)}",
        "timezone": escape(settings.timezone),
        "bank": money(settings.bank),
        "unit": money(settings.unit),
        "daily_loss": money(settings.daily_loss_limit),
        "weekly_loss": money(settings.weekly_loss_limit),
    }


def format_settings(settings: UserSettingsView, league_names: dict[str, str]) -> str:
    """The ⚙️ settings overview."""
    values = settings_values(settings, league_names)
    rows = [template.format(value=values[key]) for key, template in texts.SETTINGS_ROWS.items()]
    return texts.SETTINGS_TITLE + "\n\n" + "\n".join(rows)


# --------------------------------------------------------------------------- errors
def cooldown_message(error: CooldownActiveError) -> str:
    """``⏳ N daqiqadan keyin...`` (rounded up, at least one minute)."""
    seconds = max(error.retry_after.total_seconds(), 1.0)
    minutes = int(-(-seconds // SECONDS_PER_MINUTE))
    return texts.COOLDOWN.format(minutes=minutes)


def budget_message(error: BudgetExceededError) -> str:
    """How many API requests are missing and what to do about it."""
    return texts.BUDGET_EXCEEDED.format(needed=error.needed, available=error.available)


def validation_message(error: ValidationFailedError) -> str:
    """User message for the offending field (generic for fields the bot cannot fix)."""
    template = texts.VALIDATION_BY_FIELD.get(error.field)
    if template is None:
        return texts.VALIDATION_GENERIC
    return template.format(
        max_window=MAX_WINDOW_DAYS,
        max_future=MAX_FUTURE_DAYS,
        max_scope=MAX_SCOPE_IDS,
        low=_bounds_low(error.field),
        high=_bounds_high(error.field),
        max_name=MAX_TEMPLATE_NAME_LENGTH,
        min_search=MIN_SEARCH_LENGTH,
    )


_BOUNDS: Final[dict[str, tuple[str, str]]] = {
    "min_probability": (
        f"{MIN_PROBABILITY_LOWER * PERCENT:g}",
        f"{MIN_PROBABILITY_UPPER * PERCENT:g}",
    ),
    "min_ev": (f"{MIN_EV_LOWER * PERCENT:g}", f"{MIN_EV_UPPER * PERCENT:g}"),
    "odds_min": (f"{MIN_ODDS_LIMIT:g}", f"{MAX_ODDS_LIMIT:g}"),
}


def _bounds_low(field: str) -> str:
    return _BOUNDS.get(field, ("", ""))[0]


def _bounds_high(field: str) -> str:
    return _BOUNDS.get(field, ("", ""))[1]


# --------------------------------------------------------------------------- splitting
def split_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split into chunks of at most ``limit`` characters, preferring line boundaries.

    Our HTML never spans lines, so cutting between lines keeps tags balanced. A single
    over-long line is cut hard (only possible for pathological input).
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        rest = line
        while len(rest) > limit:
            chunks.append(rest[:limit])
            rest = rest[limit:]
        current = rest
    if current:
        chunks.append(current)
    return chunks


def local_today(now: datetime, zone: ZoneInfo) -> date:
    """The user's current calendar date."""
    return now.astimezone(zone).date()
