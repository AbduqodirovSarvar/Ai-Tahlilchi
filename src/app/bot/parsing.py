"""Pure parsers for everything a user can type: numbers, dates, ranges and ``/tahlil``.

Every parser either returns a value or raises :class:`InputError` whose ``message`` is a
ready Uzbek (HTML-safe) explanation of what to type instead. Nothing here touches Telegram
or services, so it is unit- and property-tested in isolation.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.bot import texts
from app.bot.formatters import escape
from app.domain import Sport
from app.services.interfaces import (
    MAX_FUTURE_DAYS,
    MAX_ODDS_LIMIT,
    MAX_RETRO_LOOKBACK_DAYS,
    MAX_SCOPE_IDS,
    MAX_WINDOW_DAYS,
    MIN_ODDS_LIMIT,
    QUERY_MAX_SIGNALS_UPPER,
    DateSpec,
    horizon_end,
)

PERCENT: Final = 100
RETRO_WEEK_DAYS: Final = 7
MAX_QUICK_LENGTH: Final = 300
"""Longest ``/tahlil`` argument we try to understand (a real query is far shorter)."""
_HALF_YEAR_DAYS: Final = 183
_MONTHS_PER_YEAR: Final = 12
_CENTURY: Final = 2000
_TWO_DIGIT_YEAR_LENGTH: Final = 2

_DATE: Final = r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?"
_DASH: Final = r"\s*[-\u2013\u2014]\s*"
"""Hyphen, en dash or em dash (phones autocorrect ``-`` into dashes)."""
_DATE_RE: Final = re.compile(rf"^{_DATE}$")
_RANGE_RE: Final = re.compile(rf"^{_DATE}{_DASH}{_DATE}$")
_TIME_RE: Final = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_DASH_RE: Final = re.compile(_DASH)

# /tahlil tokens, matched on whole words (case-insensitive) and removed from the text;
# what remains (split by commas) are team names.
_QUICK_RANGE_RE: Final = re.compile(rf"(?<![\w.]){_DATE}{_DASH}{_DATE}(?![\w.])")
_QUICK_DATE_RE: Final = re.compile(rf"(?<![\w.]){_DATE}(?![\w.])")
_QUICK_EV_RE: Final = re.compile(r"(?<!\w)ev\s*(?:>=|≥|=)?\s*(\d+(?:[.,]\d+)?)\s*%?(?!\w)", re.I)
_QUICK_PERCENT_RE: Final = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*%")
_QUICK_COUNT_RE: Final = re.compile(r"(?<![\w.,])(\d+)\s*ta(?!\w)", re.I)
_QUICK_DAYS_RE: Final = re.compile(r"(?<![\w.,])(\d+)\s*kun(?!\w)", re.I)
_QUICK_NUMBER_RE: Final = re.compile(r"(?<![\w.,%])(\d+(?:[.,]\d+)?)(?![\w.,%])")
_QUICK_KEYWORDS: Final[dict[str, str]] = {
    "bugun": "today",
    "kecha": "yesterday",
    "o'tgan hafta": "last_week",
    "otgan hafta": "last_week",
    "ertaga": "tomorrow",
    "dam olish kunlari": "weekend",
    "dam olish": "weekend",
    "dam": "weekend",
    "weekend": "weekend",
}
SPORT_ALIASES: Final[dict[str, Sport | None]] = {
    "futbol": Sport.FOOTBALL,
    "football": Sport.FOOTBALL,
    "soccer": Sport.FOOTBALL,
    "футбол": Sport.FOOTBALL,
    "xokkey": Sport.HOCKEY,
    "hokkey": Sport.HOCKEY,
    "hockey": Sport.HOCKEY,
    "хоккей": Sport.HOCKEY,
    "basketbol": Sport.BASKETBALL,
    "basketball": Sport.BASKETBALL,
    "баскетбол": Sport.BASKETBALL,
    "voleybol": Sport.VOLLEYBALL,
    "volleyball": Sport.VOLLEYBALL,
    "волейбол": Sport.VOLLEYBALL,
    "tennis": Sport.TENNIS,
    "теннис": Sport.TENNIS,
    "hammasi": None,
    "barchasi": None,
}
"""Sport names users type; ``None`` means every sport."""


class InputError(ValueError):
    """A typed value is invalid; ``message`` tells the user what to type instead."""

    def __init__(self, message: str) -> None:
        """Keep the user-facing (already escaped) explanation."""
        super().__init__(message)
        self.message = message


def _quoted(value: str) -> str:
    """User input echoed back inside a message (escaped, shortened)."""
    shortened = value if len(value) <= 40 else value[:39] + "…"  # noqa: PLR2004 - display width
    return escape(shortened)


# --------------------------------------------------------------------------- numbers
def parse_number(text: str, example: str = "60") -> float:
    """A decimal number; accepts ``,`` as the decimal separator and a trailing ``%``."""
    cleaned = text.strip().removesuffix("%").strip().replace(",", ".")
    if not cleaned:
        raise InputError(texts.PARSE_EMPTY)
    try:
        value = float(cleaned)
    except ValueError:
        raise InputError(
            texts.PARSE_NOT_NUMBER.format(value=_quoted(text), example=example)
        ) from None
    if not math.isfinite(value):
        raise InputError(texts.PARSE_NOT_NUMBER.format(value=_quoted(text), example=example))
    return value


def parse_percent(text: str, low: float, high: float) -> float:
    """A percentage typed as ``60``/``60%``/``62,5`` within ``[low, high]`` (fractions)."""
    value = parse_number(text) / PERCENT
    if not low <= value <= high:
        raise InputError(
            texts.PARSE_PERCENT_RANGE.format(
                value=_quoted(text), low=_percent_number(low), high=_percent_number(high)
            )
        )
    return round(value, 4)


def _percent_number(fraction: float) -> str:
    return f"{fraction * PERCENT:g}"


def parse_count(text: str, low: int, high: int) -> int:
    """A whole number within ``[low, high]``."""
    value = parse_number(text, example=str(high // 2 or 1))
    if value != int(value) or not low <= value <= high:
        raise InputError(texts.PARSE_COUNT_RANGE.format(value=_quoted(text), low=low, high=high))
    return int(value)


def parse_money(text: str) -> Decimal:
    """A positive amount; spaces and ``_`` as thousands separators are allowed."""
    cleaned = re.sub(r"[\s_']", "", text).replace(",", ".")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise InputError(texts.PARSE_BAD_MONEY.format(value=_quoted(text))) from None
    if not amount.is_finite() or amount <= 0:
        raise InputError(texts.PARSE_BAD_MONEY.format(value=_quoted(text)))
    return amount


def parse_bet_input(text: str) -> tuple[float, Decimal | None]:
    """``1.90`` or ``1.90 50000`` → ``(1.9, None)`` / ``(1.9, Decimal("50000"))``.

    The first word is the odds the user really got; the rest, if any, the stake (spaces
    inside it are thousands separators).
    """
    parts = text.split(maxsplit=1)
    if not parts:
        raise InputError(texts.PARSE_EMPTY)
    try:
        value = parse_number(parts[0], example="1.90")
    except InputError:
        raise InputError(texts.BET_BAD_INPUT.format(value=_quoted(text))) from None
    if not MIN_ODDS_LIMIT <= value <= MAX_ODDS_LIMIT:
        raise InputError(texts.BET_BAD_ODDS.format(low=MIN_ODDS_LIMIT, high=f"{MAX_ODDS_LIMIT:g}"))
    stake = parse_money(parts[1]) if len(parts) > 1 else None
    return value, stake


def parse_odds_range(text: str) -> tuple[float, float]:
    """``1.40-3.00`` → ``(1.4, 3.0)`` within the contract's odds limits."""
    error = InputError(
        texts.PARSE_BAD_ODDS_RANGE.format(
            value=_quoted(text), low=MIN_ODDS_LIMIT, high=f"{MAX_ODDS_LIMIT:g}"
        )
    )
    parts = _DASH_RE.split(text.strip().replace(",", "."))
    if len(parts) != 2:  # noqa: PLR2004 - a range has two ends
        raise error
    try:
        low, high = float(parts[0]), float(parts[1])
    except ValueError:
        raise error from None
    if not MIN_ODDS_LIMIT <= low < high <= MAX_ODDS_LIMIT:
        raise error
    return low, high


# --------------------------------------------------------------------------- time
def parse_time(text: str) -> time:
    """``09:00`` / ``9.30`` → local time of day."""
    match = _TIME_RE.match(text.strip())
    if match is None:
        raise InputError(texts.PARSE_BAD_TIME.format(value=_quoted(text)))
    try:
        return time(int(match.group(1)), int(match.group(2)))
    except ValueError:
        raise InputError(texts.PARSE_BAD_TIME.format(value=_quoted(text))) from None


def parse_time_range(text: str) -> tuple[time, time]:
    """``23:00-07:00`` → (start, end); may wrap midnight, ends must differ."""
    parts = _DASH_RE.split(text.strip())
    error = InputError(texts.PARSE_BAD_TIME_RANGE.format(value=_quoted(text)))
    if len(parts) != 2:  # noqa: PLR2004 - a range has two ends
        raise error
    try:
        start, end = parse_time(parts[0]), parse_time(parts[1])
    except InputError:
        raise error from None
    if start == end:
        raise error
    return start, end


def parse_timezone(text: str) -> str:
    """A known IANA time zone name such as ``Asia/Tashkent``."""
    name = text.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise InputError(texts.PARSE_BAD_TIMEZONE.format(value=_quoted(text))) from None
    return name


# --------------------------------------------------------------------------- dates
def _build_date(parts: Sequence[str | None], today: date, raw: str) -> date:
    """Day/month/optional year → date; a missing year is the one closest to ``today``."""
    day_text, month_text, year_text = parts
    error = InputError(texts.PARSE_BAD_DATE.format(value=_quoted(raw)))
    if day_text is None or month_text is None:
        raise error
    day, month = int(day_text), int(month_text)
    try:
        if year_text is not None:
            year = int(year_text)
            year = year + _CENTURY if len(year_text) == _TWO_DIGIT_YEAR_LENGTH else year
            return date(year, month, day)
        candidates = []
        for year in (today.year - 1, today.year, today.year + 1):
            try:
                candidates.append(date(year, month, day))
            except ValueError:
                continue
    except ValueError:
        raise error from None
    if not candidates or not 1 <= month <= _MONTHS_PER_YEAR:
        raise error
    return min(candidates, key=lambda candidate: abs((candidate - today).days))


def parse_date(text: str, today: date) -> date:
    """``25.10`` or ``25.10.2026`` (also ``/`` and two-digit years)."""
    match = _DATE_RE.match(text.strip())
    if match is None:
        raise InputError(texts.PARSE_BAD_DATE.format(value=_quoted(text)))
    return _build_date(match.groups(), today, text.strip())


def parse_date_range(text: str, today: date) -> tuple[date, date]:
    """``25.10 - 31.10``, ``25.10.2026-31.10.2026`` or a single date (one-day window).

    A year-less end before its start rolls into the next year (``28.12-03.01``), but only
    when that gives a window the service accepts: ``25.10-24.10`` is a typo, not a year.
    """
    stripped = text.strip()
    match = _RANGE_RE.match(stripped)
    if match is None:
        if _DATE_RE.match(stripped):
            single = parse_date(stripped, today)
            return single, single
        raise InputError(texts.PARSE_BAD_RANGE.format(value=_quoted(text)))
    groups = match.groups()
    start = _build_date(groups[:3], today, stripped)
    end = _build_date(groups[3:], today, stripped)
    if end < start and groups[5] is None:
        rolled = _next_year(end)
        if (rolled - start).days + 1 <= MAX_WINDOW_DAYS:
            end = rolled
    return start, end


def _next_year(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29.02 into a non-leap year
        return day.replace(year=day.year + 1, day=28)


def check_window(start: date, end: date, today: date) -> DateSpec:
    """Validate a typed/picked range like the service would, with a friendly message."""
    if end < start:
        raise InputError(texts.PARSE_REVERSED_RANGE)
    if (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise InputError(texts.PARSE_WINDOW_TOO_LONG.format(max_window=MAX_WINDOW_DAYS))
    if start < today - timedelta(days=MAX_RETRO_LOOKBACK_DAYS):
        raise InputError(texts.PARSE_TOO_OLD.format(max_lookback=MAX_RETRO_LOOKBACK_DAYS))
    if end > horizon_end(today):
        raise InputError(texts.PARSE_TOO_FAR.format(max_future=MAX_FUTURE_DAYS))
    return DateSpec.between(start, end)


# --------------------------------------------------------------------------- /tahlil
@dataclass(frozen=True, slots=True, kw_only=True)
class QuickQuery:
    """What a ``/tahlil`` line specified; ``None`` fields fall back to the user's settings."""

    dates: DateSpec | None = None
    sports: frozenset[Sport] | None = None
    min_probability: float | None = None
    min_ev: float | None = None
    max_signals: int | None = None
    team_names: tuple[str, ...] = field(default=())


@dataclass(slots=True)
class _QuickBuilder:
    """Mutable accumulator while tokens are consumed."""

    today: date
    prob_bounds: tuple[float, float]
    ev_bounds: tuple[float, float]
    dates: DateSpec | None = None
    sports: set[Sport] = field(default_factory=set)
    min_probability: float | None = None
    min_ev: float | None = None
    max_signals: int | None = None

    def set_dates(self, spec: DateSpec, raw: str) -> None:
        if self.dates is not None:
            raise _duplicate(raw, texts.QUICK_WHAT_DATE)
        self.dates = spec


def _duplicate(raw: str, what: str) -> InputError:
    return InputError(texts.QUICK_DUPLICATE.format(value=_quoted(raw), what=what))


def _consume(pattern: re.Pattern[str], text: str) -> tuple[list[re.Match[str]], str]:
    """All matches of ``pattern`` and the text with them blanked out (keeps commas apart)."""
    matches = list(pattern.finditer(text))
    return matches, pattern.sub(" ", text)


def _take_ranges(builder: _QuickBuilder, text: str) -> str:
    matches, rest = _consume(_QUICK_RANGE_RE, text)
    for match in matches:
        start, end = parse_date_range(match.group(0), builder.today)
        builder.set_dates(check_window(start, end, builder.today), match.group(0))
    matches, rest = _consume(_QUICK_DATE_RE, rest)
    for match in matches:
        day = parse_date(match.group(0), builder.today)
        builder.set_dates(check_window(day, day, builder.today), match.group(0))
    return rest


def keyword_spec(mode: str, today: date) -> DateSpec:
    """The dates a ``/tahlil`` keyword stands for (``kecha``, ``o'tgan hafta`` are retro)."""
    yesterday = today - timedelta(days=1)
    match mode:
        case "today":
            return DateSpec.today()
        case "tomorrow":
            return DateSpec.tomorrow()
        case "yesterday":
            return DateSpec.between(yesterday, yesterday)
        case "last_week":
            return DateSpec.between(today - timedelta(days=RETRO_WEEK_DAYS), yesterday)
        case _:
            return DateSpec.weekend()


def _take_keywords(builder: _QuickBuilder, text: str) -> str:
    matches, rest = _consume(_QUICK_DAYS_RE, text)
    for match in matches:
        days = parse_count(match.group(1), 1, MAX_FUTURE_DAYS)
        builder.set_dates(DateSpec.next_days(days), match.group(0))
    for keyword, mode in _QUICK_KEYWORDS.items():
        pattern = re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.I)
        matches, rest = _consume(pattern, rest)
        for match in matches:
            builder.set_dates(keyword_spec(mode, builder.today), match.group(0))
    for alias, sport in SPORT_ALIASES.items():
        pattern = re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.I)
        matches, rest = _consume(pattern, rest)
        if matches:
            builder.sports.update(Sport if sport is None else {sport})
    return rest


def _take_numbers(builder: _QuickBuilder, text: str) -> str:
    matches, rest = _consume(_QUICK_EV_RE, text)
    for match in matches:
        if builder.min_ev is not None:
            raise _duplicate(match.group(0), texts.QUICK_WHAT_EV)
        builder.min_ev = parse_percent(match.group(1), *builder.ev_bounds)
    matches, rest = _consume(_QUICK_PERCENT_RE, rest)
    for match in matches:
        if builder.min_probability is not None:
            raise _duplicate(match.group(0), texts.QUICK_WHAT_PROBABILITY)
        builder.min_probability = parse_percent(match.group(1), *builder.prob_bounds)
    matches, rest = _consume(_QUICK_COUNT_RE, rest)
    for match in matches:
        if builder.max_signals is not None:
            raise _duplicate(match.group(0), texts.QUICK_WHAT_COUNT)
        builder.max_signals = parse_count(match.group(1), 1, QUERY_MAX_SIGNALS_UPPER)
    stray = _stray_number(rest)
    if stray is not None:
        raise InputError(texts.QUICK_AMBIGUOUS_NUMBER.format(value=_quoted(stray)))
    return rest


def _stray_number(text: str) -> str | None:
    """A bare number no token claimed, e.g. ``60`` in ``futbol 60``.

    A number in a comma segment that also has letters belongs to a team name
    (``Schalke 04``, ``Bayer 04 Leverkusen``), so only letterless segments are checked.
    """
    for segment in text.split(","):
        if any(char.isalpha() for char in segment):
            continue
        found = _QUICK_NUMBER_RE.search(segment)
        if found is not None:
            return found.group(1)
    return None


def _team_names(text: str) -> tuple[str, ...]:
    names: list[str] = []
    for part in text.split(","):
        name = " ".join(part.split())
        if name and name.casefold() not in {existing.casefold() for existing in names}:
            names.append(name)
    if len(names) > MAX_SCOPE_IDS:
        raise InputError(texts.QUICK_TOO_MANY_TEAMS.format(max_scope=MAX_SCOPE_IDS))
    return tuple(names)


def parse_quick_query(
    text: str,
    today: date,
    *,
    probability_bounds: tuple[float, float],
    ev_bounds: tuple[float, float],
) -> QuickQuery:
    """Parse the argument of ``/tahlil 25.10-31.10 futbol 60% Arsenal,Inter`` (any order).

    Recognised parts: a date range or date, ``bugun``/``ertaga``/``dam``/``Nkun``, sport
    names (Uzbek, English, Cyrillic), ``60%`` (minimal probability), ``ev5%``, ``10ta``;
    whatever is left, split by commas, are team names.

    Raises:
        InputError: a part is invalid, duplicated or ambiguous (e.g. a bare ``60``).
    """
    if len(text) > MAX_QUICK_LENGTH:
        raise InputError(texts.QUICK_TOO_LONG.format(max_length=MAX_QUICK_LENGTH))
    builder = _QuickBuilder(today=today, prob_bounds=probability_bounds, ev_bounds=ev_bounds)
    rest = _take_ranges(builder, text)
    rest = _take_keywords(builder, rest)
    rest = _take_numbers(builder, rest)
    return QuickQuery(
        dates=builder.dates,
        sports=frozenset(builder.sports) or None,
        min_probability=builder.min_probability,
        min_ev=builder.min_ev,
        max_signals=builder.max_signals,
        team_names=_team_names(rest),
    )


# --------------------------------------------------------------------------- custom analysis
_FIXTURE_SEPARATOR_RE: Final = re.compile(r"\s+(?:[-\u2013\u2014]|vs\.?|v)\s+", re.IGNORECASE)
"""``Arsenal - Chelsea`` / ``Arsenal vs Chelsea``: the separator has spaces around it, so a
hyphen inside a name (``Saint-Etienne``) is not a separator."""
_NAME_FORBIDDEN_RE: Final = re.compile(r"[\d%,]")
MIN_NAME_LETTERS: Final = 2


def parse_fixture_text(text: str) -> tuple[str, str] | None:
    """Split ``Arsenal - Chelsea`` into its two team names, or ``None`` if it is not one.

    Dates (``25.10 - 31.10``), percentages and comma lists are other ``/tahlil`` syntax, so
    any digit, ``%`` or comma makes this return ``None``.
    """
    cleaned = " ".join(text.split())
    if not cleaned or len(cleaned) > MAX_QUICK_LENGTH or _NAME_FORBIDDEN_RE.search(cleaned):
        return None
    parts = _FIXTURE_SEPARATOR_RE.split(cleaned)
    if len(parts) != 2:  # noqa: PLR2004 - exactly two sides
        return None
    home, away = (part.strip() for part in parts)
    if sum(char.isalpha() for char in home) < MIN_NAME_LETTERS:
        return None
    if sum(char.isalpha() for char in away) < MIN_NAME_LETTERS:
        return None
    return home, away
