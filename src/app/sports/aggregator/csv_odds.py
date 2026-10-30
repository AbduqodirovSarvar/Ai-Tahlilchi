"""football-data.co.uk loader: Bet365 1X2 prices by date and team names.

Two kinds of file share one layout:

* season files (``mmz4281/2526/E0.csv``) hold the matches **already played** of one
  division, with their prices;
* ``fixtures.csv`` holds the **coming** matches of every division (a ``Div`` column),
  published a few days ahead (usually Friday for the weekend, Tuesday for midweek).

Upcoming matches therefore get prices only from ``fixtures.csv``. Files are cached on disk
(:mod:`app.sports.history.download`): a finished season forever, the current one for 12
hours, ``fixtures.csv`` for :data:`UPCOMING_MAX_AGE`. When a refresh fails, a cached copy
is used and the failure logged; without any copy the error is raised.
"""

import asyncio
import csv
import io
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

import httpx

from app.errors import DataError, ExternalServiceError
from app.logging import get_logger
from app.sports.aggregator.models import MatchOdds, OddsRow
from app.sports.history.download import (
    cache_path,
    current_season,
    download_file,
    download_season,
    needs_download,
)
from app.sports.history.football_data import (
    SkippedRow,
    decode_csv,
    parse_date,
    parse_decimal,
    parse_time,
)

logger = get_logger(__name__)

FIXTURES_URL: Final = "https://www.football-data.co.uk/fixtures.csv"
FIXTURES_FILE: Final = "fixtures.csv"
UPCOMING_MAX_AGE: Final = timedelta(hours=3)
BET365_COLUMNS: Final = ("B365H", "B365D", "B365A")
# Logical column -> spellings used across seasons (old files say HT/AT).
_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "date": ("Date",),
    "home": ("HomeTeam", "Home", "HT"),
    "away": ("AwayTeam", "Away", "AT"),
    "division": ("Div",),
    "time": ("Time",),
}
_REQUIRED: Final = ("date", "home", "away")


@dataclass(frozen=True, slots=True)
class ParsedOdds:
    """Rows of one file; rows that could not be read are listed, never fatal."""

    rows: tuple[OddsRow, ...] = ()
    skipped: tuple[SkippedRow, ...] = ()

    def of_division(self, division: str) -> tuple[OddsRow, ...]:
        """Rows of one division (``fixtures.csv`` mixes all of them)."""
        return tuple(row for row in self.rows if row.division == division)


class OddsSource(Protocol):
    """Where the aggregator gets football-data.co.uk rows (a fake in tests)."""

    async def season(self, division: str, season: int) -> ParsedOdds:
        """Played matches of one division season."""
        ...

    async def upcoming(self) -> ParsedOdds:
        """Coming matches of every division."""
        ...


def parse_odds_csv(content: bytes, *, season: int, division: str | None = None) -> ParsedOdds:
    """Read date, teams, kick-off and Bet365 1X2 prices of every row.

    ``season`` (start year) places two-digit years and rejects dates outside it;
    ``division`` is used for rows without a ``Div`` cell.

    Raises:
        DataError: the file is not a CSV or lacks the date or team columns.
    """
    rows: list[OddsRow] = []
    skipped: list[SkippedRow] = []
    for line_number, cells in _read(content):
        try:
            rows.append(_row(cells, season=season, division=division))
        except DataError as exc:
            skipped.append(SkippedRow(line_number, str(exc)))
    return ParsedOdds(tuple(rows), tuple(skipped))


def _read(content: bytes) -> Iterator[tuple[int, dict[str, str]]]:
    """``(line number, {logical column: value})`` of every non-blank row."""
    reader = csv.DictReader(io.StringIO(decode_csv(content), newline=""))
    try:
        present = {name.strip() for name in reader.fieldnames or () if name}
        columns = {
            logical: next(name for name in names if name in present)
            for logical, names in _COLUMNS.items()
            if any(name in present for name in names)
        }
        missing = [logical for logical in _REQUIRED if logical not in columns]
        if missing:
            msg = f"not a football-data file: no column for {', '.join(missing)}"
            raise DataError(msg)
        for raw in reader:
            cells = {
                key.strip(): value.strip()
                for key, value in raw.items()
                if key is not None and isinstance(value, str)
            }
            if any(cells.values()):
                row = {logical: cells.get(name, "") for logical, name in columns.items()}
                row.update({column: cells.get(column, "") for column in BET365_COLUMNS})
                yield reader.line_num, row
    except csv.Error as exc:
        msg = f"malformed CSV near line {reader.line_num}: {exc}"
        raise DataError(msg) from exc


def _row(cells: Mapping[str, str], *, season: int, division: str | None) -> OddsRow:
    row_division = cells.get("division") or division
    if not row_division:
        msg = "row has no division"
        raise DataError(msg)
    home, away = cells["home"], cells["away"]
    if not home or not away:
        msg = "row lacks a team name"
        raise DataError(msg)
    return OddsRow(
        division=row_division,
        day=parse_date(cells["date"], season=season),
        home_team=home,
        away_team=away,
        kickoff=parse_time(cells.get("time", "")),
        odds=bet365_odds(cells),
    )


def bet365_odds(cells: Mapping[str, str]) -> MatchOdds | None:
    """The row's Bet365 prices; ``None`` when any is empty, not a number or not above 1."""
    try:
        home, draw, away = (parse_decimal(cells.get(column, "")) for column in BET365_COLUMNS)
        return MatchOdds(home_win=home, draw=draw, away_win=away)
    except DataError:
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OddsCsvLoader:
    """:class:`OddsSource` over football-data.co.uk with an on-disk cache.

    One lock serialises downloads, so concurrent callers never fetch the same file twice.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        cache_dir: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """``http`` comes from :func:`app.sports.history.download.create_client`."""
        self._http = http
        self._cache_dir = cache_dir
        self._clock = clock
        self._lock = asyncio.Lock()

    async def season(self, division: str, season: int) -> ParsedOdds:
        """Played matches of ``division`` in ``season``; empty when the file does not exist.

        Raises:
            ExternalServiceError: download failed and nothing is cached.
            DataError: the file is not a football-data CSV.
        """
        path = cache_path(self._cache_dir, division, season)
        async with self._lock:
            if needs_download(path, season=season, now=self._clock()):
                await self._refresh(download_season(self._http, division, season, path), path)
        return await self._parse(path, season=season, division=division)

    async def upcoming(self) -> ParsedOdds:
        """Coming matches of every division (``fixtures.csv``).

        Raises:
            ExternalServiceError: download failed and nothing is cached.
            DataError: the file is not a football-data CSV.
        """
        path = self._cache_dir / FIXTURES_FILE
        now = self._clock()
        async with self._lock:
            if _older_than(path, UPCOMING_MAX_AGE, now):
                await self._refresh(download_file(self._http, FIXTURES_URL, path), path)
        return await self._parse(path, season=current_season(now.date()), division=None)

    async def _refresh(self, download: Awaitable[bool], path: Path) -> None:
        try:
            await download
        except ExternalServiceError:
            if not await asyncio.to_thread(path.is_file):
                raise
            logger.warning("odds_csv_refresh_failed_using_cache", path=str(path), exc_info=True)

    async def _parse(self, path: Path, *, season: int, division: str | None) -> ParsedOdds:
        content = await asyncio.to_thread(_read_cached, path)
        if content is None:  # 404: the season has no file (yet)
            return ParsedOdds()
        parsed = await asyncio.to_thread(parse_odds_csv, content, season=season, division=division)
        if parsed.skipped:
            logger.info("odds_csv_rows_skipped", path=str(path), skipped=len(parsed.skipped))
        return parsed


def _read_cached(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _older_than(path: Path, max_age: timedelta, now: datetime) -> bool:
    if not path.is_file():
        return True
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return now - modified > max_age
