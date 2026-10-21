"""Parser for football-data.co.uk season CSVs (results + odds) into domain objects.

One file holds one league season (``mmz4281/2425/E0.csv``). Columns vary between leagues
and seasons, so every odds group is optional; only date, teams and full-time goals are
required. Rows that cannot be parsed are reported in :attr:`ParsedSeason.skipped` and never
abort the file. Column meanings: https://www.football-data.co.uk/notes.txt
"""

import csv
import io
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from app.domain import (
    BOOKMAKER_BET365,
    BOOKMAKER_MARKET_AVERAGE,
    BOOKMAKER_MARKET_MAXIMUM,
    BOOKMAKER_PINNACLE,
    DomainValidationError,
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    League,
    Market,
    OddsKind,
    OddsSnapshot,
    ResultStatus,
    Selection,
    Sport,
    Team,
)
from app.errors import DataError

SOURCE: Final = "football-data"
# Dates and kick-off times in the files are UK local time.
SOURCE_TIMEZONE: Final = ZoneInfo("Europe/London")
# Used when a row has no Time column/value; noon keeps the UTC date equal to the local one.
DEFAULT_KICKOFF_TIME: Final = time(12, 0)
# The non-closing ("early") prices are collected 1-3 days before kick-off at an
# unrecorded moment; this lead only keeps them ordered before the closing price.
EARLY_ODDS_LEAD: Final = timedelta(days=1)
TOTALS_LINE: Final = 2.5
TWO_DIGIT_YEAR_BASE: Final = 2000
_TWO_DIGIT_YEAR_LENGTH: Final = 2


@dataclass(frozen=True, slots=True)
class LeagueInfo:
    """Display name and country of a football-data division code."""

    name: str
    country: str


LEAGUES: Final[Mapping[str, LeagueInfo]] = {
    "E0": LeagueInfo("Premier League", "England"),
    "E1": LeagueInfo("Championship", "England"),
    "SP1": LeagueInfo("La Liga", "Spain"),
    "D1": LeagueInfo("Bundesliga", "Germany"),
    "I1": LeagueInfo("Serie A", "Italy"),
    "F1": LeagueInfo("Ligue 1", "France"),
    "N1": LeagueInfo("Eredivisie", "Netherlands"),
    "P1": LeagueInfo("Primeira Liga", "Portugal"),
    "B1": LeagueInfo("Pro League", "Belgium"),
    "T1": LeagueInfo("Super Lig", "Turkey"),
}


@dataclass(frozen=True, slots=True)
class PriceGroup:
    """Columns that together quote one complete market of one bookmaker at one moment."""

    bookmaker: str
    kind: OddsKind
    prices: tuple[tuple[Selection, str], ...]
    market: Market | None = None
    line_column: str | None = None  # Asian handicap: column holding the home line


def _match_result(bookmaker: str, kind: OddsKind, prefix: str) -> PriceGroup:
    return PriceGroup(
        bookmaker,
        kind,
        (
            (Selection.HOME, f"{prefix}H"),
            (Selection.DRAW, f"{prefix}D"),
            (Selection.AWAY, f"{prefix}A"),
        ),
        market=Market.match_result(),
    )


def _totals(bookmaker: str, kind: OddsKind, prefix: str) -> PriceGroup:
    return PriceGroup(
        bookmaker,
        kind,
        ((Selection.OVER, f"{prefix}>2.5"), (Selection.UNDER, f"{prefix}<2.5")),
        market=Market.totals(TOTALS_LINE),
    )


def _handicap(bookmaker: str, kind: OddsKind, prefix: str, line_column: str) -> PriceGroup:
    return PriceGroup(
        bookmaker,
        kind,
        ((Selection.HOME, f"{prefix}H"), (Selection.AWAY, f"{prefix}A")),
        line_column=line_column,
    )


_EARLY, _CLOSING = OddsKind.OPENING, OddsKind.CLOSING
PRICE_GROUPS: Final[tuple[PriceGroup, ...]] = (
    _match_result(BOOKMAKER_PINNACLE, _EARLY, "PS"),
    _match_result(BOOKMAKER_PINNACLE, _CLOSING, "PSC"),
    _match_result(BOOKMAKER_MARKET_AVERAGE, _EARLY, "Avg"),
    _match_result(BOOKMAKER_MARKET_AVERAGE, _CLOSING, "AvgC"),
    _match_result(BOOKMAKER_MARKET_MAXIMUM, _EARLY, "Max"),
    _match_result(BOOKMAKER_MARKET_MAXIMUM, _CLOSING, "MaxC"),
    _match_result(BOOKMAKER_BET365, _EARLY, "B365"),
    _match_result(BOOKMAKER_BET365, _CLOSING, "B365C"),
    _totals(BOOKMAKER_PINNACLE, _EARLY, "P"),
    _totals(BOOKMAKER_PINNACLE, _CLOSING, "PC"),
    _totals(BOOKMAKER_MARKET_AVERAGE, _EARLY, "Avg"),
    _totals(BOOKMAKER_MARKET_AVERAGE, _CLOSING, "AvgC"),
    _totals(BOOKMAKER_MARKET_MAXIMUM, _EARLY, "Max"),
    _totals(BOOKMAKER_MARKET_MAXIMUM, _CLOSING, "MaxC"),
    _totals(BOOKMAKER_BET365, _EARLY, "B365"),
    _totals(BOOKMAKER_BET365, _CLOSING, "B365C"),
    _handicap(BOOKMAKER_PINNACLE, _EARLY, "PAH", "AHh"),
    _handicap(BOOKMAKER_PINNACLE, _CLOSING, "PCAH", "AHCh"),
    _handicap(BOOKMAKER_MARKET_AVERAGE, _EARLY, "AvgAH", "AHh"),
    _handicap(BOOKMAKER_MARKET_AVERAGE, _CLOSING, "AvgCAH", "AHCh"),
    _handicap(BOOKMAKER_MARKET_MAXIMUM, _EARLY, "MaxAH", "AHh"),
    _handicap(BOOKMAKER_MARKET_MAXIMUM, _CLOSING, "MaxCAH", "AHCh"),
    _handicap(BOOKMAKER_BET365, _EARLY, "B365AH", "AHh"),
    _handicap(BOOKMAKER_BET365, _CLOSING, "B365CAH", "AHCh"),
)

# Alternative header spellings used by older files, in order of preference.
_COLUMN_ALIASES: Final[Mapping[str, tuple[str, ...]]] = {
    "home": ("HomeTeam", "HT"),
    "away": ("AwayTeam", "AT"),
    "home_goals": ("FTHG", "HG"),
    "away_goals": ("FTAG", "AG"),
    "home_ht_goals": ("HTHG",),
    "away_ht_goals": ("HTAG",),
    "date": ("Date",),
    "time": ("Time",),
    "division": ("Div",),
}
_REQUIRED: Final = ("date", "home", "away", "home_goals", "away_goals")
_DATE_PATTERN: Final = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})")
_TIME_PATTERN: Final = re.compile(r"(\d{1,2})[:.](\d{2})")


@dataclass(frozen=True, slots=True)
class SkippedRow:
    """A data row that was not imported, with the reason (1-based line number)."""

    line: int
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ParsedSeason:
    """Everything usable in one file plus an account of what was left out."""

    batch: HistoryBatch
    rows: int
    skipped: tuple[SkippedRow, ...] = ()
    incomplete_price_groups: int = 0
    missing_kickoff_time: int = 0


@dataclass(slots=True)
class _Accumulator:
    fixtures: dict[ExternalRef, Fixture] = field(default_factory=dict)
    results: list[FixtureResult] = field(default_factory=list)
    odds: list[OddsSnapshot] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)
    incomplete_price_groups: int = 0
    missing_kickoff_time: int = 0
    rows: int = 0


class RowError(DataError):
    """One CSV row cannot be turned into a fixture."""


def season_code(season: int) -> str:
    """football-data's season folder: 2024 → ``"2425"``."""
    return f"{season % 100:02d}{(season + 1) % 100:02d}"


def league_for(code: str) -> League:
    """Domain league for a supported division code.

    Raises:
        DataError: the code is not in :data:`LEAGUES`.
    """
    info = LEAGUES.get(code)
    if info is None:
        msg = f"unsupported football-data division {code!r}; known: {', '.join(LEAGUES)}"
        raise DataError(msg)
    return League(
        ref=ExternalRef(SOURCE, code), sport=Sport.FOOTBALL, name=info.name, country=info.country
    )


def decode_csv(content: bytes) -> str:
    """Decode a file that may be UTF-8 (with or without BOM) or Windows-1252/Latin-1."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")  # total: every byte maps to a character


def parse_season_csv(content: bytes, *, league: League, season: int) -> ParsedSeason:
    """Parse one season file of ``league``.

    Raises:
        DataError: the file is empty, not a CSV, or lacks a required column.
    """
    accumulator = _Accumulator()
    for line_number, row, columns in _read_rows(content):
        accumulator.rows += 1
        try:
            _parse_row(row, columns, league=league, season=season, into=accumulator)
        except (RowError, DomainValidationError) as exc:
            accumulator.skipped.append(SkippedRow(line_number, str(exc)))
    return ParsedSeason(
        batch=HistoryBatch(
            fixtures=tuple(accumulator.fixtures.values()),
            results=tuple(accumulator.results),
            odds=tuple(accumulator.odds),
        ),
        rows=accumulator.rows,
        skipped=tuple(accumulator.skipped),
        incomplete_price_groups=accumulator.incomplete_price_groups,
        missing_kickoff_time=accumulator.missing_kickoff_time,
    )


def _read_rows(content: bytes) -> Iterator[tuple[int, dict[str, str], dict[str, str]]]:
    """Yield ``(line number, cleaned non-blank row, column map)`` for each data row.

    Raises:
        DataError: the header is unusable or the CSV structure itself is broken (the
            reader cannot resynchronise after that, so the file is rejected as a whole).
    """
    reader = csv.DictReader(io.StringIO(decode_csv(content), newline=""))
    try:
        columns = _resolve_columns(reader.fieldnames)
        for raw_row in reader:
            row = _clean_row(raw_row)
            if any(row.values()):
                yield reader.line_num, row, columns
    except csv.Error as exc:
        msg = f"malformed CSV near line {reader.line_num}: {exc}"
        raise DataError(msg) from exc


def _resolve_columns(fieldnames: Sequence[str] | None) -> dict[str, str]:
    """Map logical names to the header spelling present in this file."""
    present = {name.strip() for name in fieldnames or () if name}
    columns = {
        logical: next(alias for alias in aliases if alias in present)
        for logical, aliases in _COLUMN_ALIASES.items()
        if any(alias in present for alias in aliases)
    }
    missing = [logical for logical in _REQUIRED if logical not in columns]
    if missing:
        msg = f"not a football-data results file: missing columns for {', '.join(missing)}"
        raise DataError(msg)
    return columns


def _clean_row(raw_row: Mapping[str | None, object]) -> dict[str, str]:
    """Strip keys and values; drop overflow cells (key None) and missing cells (value None)."""
    return {
        key.strip(): value.strip()
        for key, value in raw_row.items()
        if key is not None and isinstance(value, str)
    }


def _parse_row(
    row: Mapping[str, str],
    columns: Mapping[str, str],
    *,
    league: League,
    season: int,
    into: _Accumulator,
) -> None:
    division = row.get(columns.get("division", ""), "")
    if division and division != league.ref.external_id:
        msg = f"row belongs to division {division!r}, not {league.ref.external_id!r}"
        raise RowError(msg)
    fixture = _fixture(row, columns, league=league, season=season, into=into)
    if fixture.ref in into.fixtures:
        msg = f"duplicate fixture {fixture.ref.external_id}"
        raise RowError(msg)
    result = _result(row, columns, fixture.ref)
    odds = list(_odds(row, fixture, into))
    into.fixtures[fixture.ref] = fixture
    into.results.append(result)
    into.odds.extend(odds)


def _fixture(
    row: Mapping[str, str],
    columns: Mapping[str, str],
    *,
    league: League,
    season: int,
    into: _Accumulator,
) -> Fixture:
    match_date = parse_date(row.get(columns["date"], ""))
    kickoff_time = parse_time(row.get(columns.get("time", ""), ""))
    if kickoff_time is None:
        into.missing_kickoff_time += 1
        kickoff_time = DEFAULT_KICKOFF_TIME
    home = _team(row.get(columns["home"], ""), league)
    away = _team(row.get(columns["away"], ""), league)
    external_id = "/".join(
        (league.ref.external_id, str(season), match_date.isoformat(), home.name, away.name)
    )
    return Fixture(
        ref=ExternalRef(SOURCE, external_id),
        sport=Sport.FOOTBALL,
        league=league,
        season=season,
        kickoff=datetime.combine(match_date, kickoff_time, tzinfo=SOURCE_TIMEZONE),
        home=home,
        away=away,
        status=FixtureStatus.FINISHED,
    )


def _team(name: str, league: League) -> Team:
    if not name:
        msg = "team name is empty"
        raise RowError(msg)
    return Team(
        ref=ExternalRef(SOURCE, name), sport=Sport.FOOTBALL, name=name, country=league.country
    )


def _result(row: Mapping[str, str], columns: Mapping[str, str], ref: ExternalRef) -> FixtureResult:
    home_goals = parse_goals(row.get(columns["home_goals"], ""))
    away_goals = parse_goals(row.get(columns["away_goals"], ""))
    if home_goals is None or away_goals is None:
        msg = "full-time score is missing"
        raise RowError(msg)
    return FixtureResult(
        fixture=ref,
        status=ResultStatus.FINISHED,
        home_score=home_goals,
        away_score=away_goals,
        home_ht_score=parse_goals(row.get(columns.get("home_ht_goals", ""), "")),
        away_ht_score=parse_goals(row.get(columns.get("away_ht_goals", ""), "")),
    )


def _odds(row: Mapping[str, str], fixture: Fixture, into: _Accumulator) -> Iterator[OddsSnapshot]:
    for group in PRICE_GROUPS:
        cells = [row.get(column, "") for _, column in group.prices]
        if group.line_column is not None:
            cells.append(row.get(group.line_column, ""))
        if not any(cells):
            continue  # this file does not quote the group at all
        try:
            yield from _group_snapshots(row, group, fixture)
        except (RowError, DomainValidationError):
            into.incomplete_price_groups += 1


def _group_snapshots(
    row: Mapping[str, str], group: PriceGroup, fixture: Fixture
) -> list[OddsSnapshot]:
    """All prices of ``group``, or an error if any one is missing or invalid."""
    market = group.market
    if group.line_column is not None:
        market = Market.asian_handicap(parse_decimal(row.get(group.line_column, "")))
    if market is None:  # pragma: no cover - every group defines a market or a line column
        msg = "price group defines neither a market nor a line column"
        raise RowError(msg)
    taken_at = (
        fixture.kickoff if group.kind is OddsKind.CLOSING else fixture.kickoff - EARLY_ODDS_LEAD
    )
    return [
        OddsSnapshot(
            fixture=fixture.ref,
            bookmaker=group.bookmaker,
            market=market,
            selection=selection,
            odds=parse_decimal(row.get(column, "")),
            taken_at=taken_at,
            kind=group.kind,
        )
        for selection, column in group.prices
    ]


def parse_date(text: str) -> date:
    """Parse ``dd/mm/yy`` or ``dd/mm/yyyy``.

    Raises:
        RowError: the text is not such a date.
    """
    match = _DATE_PATTERN.fullmatch(text)
    if match is None:
        msg = f"unparseable date {text!r}"
        raise RowError(msg)
    day, month, year = (int(part) for part in match.groups())
    if len(match.group(3)) == _TWO_DIGIT_YEAR_LENGTH:
        year += TWO_DIGIT_YEAR_BASE
    try:
        return date(year, month, day)
    except ValueError as exc:
        msg = f"invalid date {text!r}"
        raise RowError(msg) from exc


def parse_time(text: str) -> time | None:
    """Parse ``HH:MM``; ``None`` when absent or unparseable (the caller picks a default)."""
    match = _TIME_PATTERN.fullmatch(text)
    if match is None:
        return None
    hour, minute = (int(part) for part in match.groups())
    try:
        return time(hour, minute)
    except ValueError:
        return None


def parse_goals(text: str) -> int | None:
    """Parse a goal count; ``None`` when empty.

    Raises:
        RowError: the text is not a non-negative integer (``"2.0"`` is accepted).
    """
    if not text:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        msg = f"goal count {text!r} is not a number"
        raise RowError(msg) from exc
    if value < 0 or not value.is_integer():
        msg = f"goal count {text!r} is not a non-negative integer"
        raise RowError(msg)
    return int(value)


def parse_decimal(text: str) -> float:
    """Parse a price or line.

    Raises:
        RowError: the text is empty or not a number.
    """
    try:
        return float(text)
    except ValueError as exc:
        msg = f"expected a number, got {text!r}"
        raise RowError(msg) from exc
