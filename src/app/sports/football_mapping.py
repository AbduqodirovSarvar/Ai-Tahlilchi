"""API-Football v3 records → domain objects (the only module that knows both).

Pure functions, no I/O. Functions raise :class:`~app.domain.DomainValidationError` (or
return ``None``) for a record that cannot be represented; callers log and skip it.

Conventions decided here (ADR 0007):

- **Status codes:** ``TBD``/``NS`` → scheduled; ``1H HT 2H ET BT P SUSP INT LIVE`` → live;
  ``FT AET PEN`` → finished; ``PST`` → postponed; ``CANC`` → cancelled; ``ABD`` →
  abandoned; ``AWD``/``WO`` (awarded / walkover) → abandoned *without* score: an awarded
  3-0 is not football and must not train the model (bookmakers void such bets).
- **Scores:** ``score.fulltime`` is the result after regular time (extra time and
  penalties are separate), which is what every market settles on; ``goals`` (which include
  extra time) is only a fallback for plain ``FT``.
- **Asian handicap:** API-Football lists each line for both sides, e.g. ``Home -0.5`` and
  ``Away -0.5``. Our reading (:attr:`HandicapConvention.HOME_LINE`, default) is that the
  number is the **home** line for both values, matching the domain convention. This could
  not be verified without a key; ``scripts/probe_api.py`` checks it on real prices (pair
  overround) and :attr:`HandicapConvention.OWN_LINE` exists to switch if it is wrong.
"""

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from app.clients.api_football_models import (
    Bet,
    FixtureItem,
    InjuryItem,
    LineupItem,
    OddsItem,
    ScorePair,
    TeamStatisticsItem,
)
from app.domain import (
    BOOKMAKER_1XBET,
    BOOKMAKER_BET365,
    BOOKMAKER_PINNACLE,
    DomainValidationError,
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    League,
    Market,
    MarketType,
    OddsKind,
    OddsSnapshot,
    ResultStatus,
    Selection,
    Sport,
    Team,
)
from app.sports.football_leagues import API_FOOTBALL_SOURCE

SOURCE: Final = API_FOOTBALL_SOURCE
XG_STATISTIC: Final = "expected_goals"

_STATUS_BY_CODE: Final[Mapping[str, FixtureStatus]] = {
    **dict.fromkeys(("TBD", "NS"), FixtureStatus.SCHEDULED),
    **dict.fromkeys(("1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"), FixtureStatus.LIVE),
    **dict.fromkeys(("FT", "AET", "PEN"), FixtureStatus.FINISHED),
    "PST": FixtureStatus.POSTPONED,
    "CANC": FixtureStatus.CANCELLED,
    **dict.fromkeys(("ABD", "AWD", "WO"), FixtureStatus.ABANDONED),
}
_AWARDED: Final = frozenset({"AWD", "WO"})
_EXTRA_TIME: Final = frozenset({"AET", "PEN"})

_KNOWN_BOOKMAKERS: Final[Mapping[str, str]] = {
    "pinnacle": BOOKMAKER_PINNACLE,
    "bet365": BOOKMAKER_BET365,
    "1xbet": BOOKMAKER_1XBET,
}
_NON_SLUG: Final = re.compile(r"[^a-z0-9]+")
_SIDE_AND_LINE: Final = re.compile(r"^(home|away|over|under)\s+([+-]?\d+(?:\.\d+)?)$")

_FIXED_SELECTIONS: Final[Mapping[str, Mapping[str, Selection]]] = {
    "match winner": {"home": Selection.HOME, "draw": Selection.DRAW, "away": Selection.AWAY},
    "double chance": {
        "home/draw": Selection.HOME_OR_DRAW,
        "home/away": Selection.HOME_OR_AWAY,
        "draw/away": Selection.DRAW_OR_AWAY,
    },
    "both teams score": {"yes": Selection.YES, "no": Selection.NO},
}
_FIXED_MARKETS: Final[Mapping[str, Market]] = {
    "match winner": Market.match_result(),
    "double chance": Market.double_chance(),
    "both teams score": Market.btts(),
}
_TOTALS_BET: Final = "goals over/under"
_HANDICAP_BET: Final = "asian handicap"
SUPPORTED_MARKETS: Final = frozenset(MarketType)


class HandicapConvention(StrEnum):
    """How the number in an Asian handicap value (``Away -0.5``) is to be read."""

    HOME_LINE = "home_line"  # the home team's line, for both sides
    OWN_LINE = "own_line"  # the line of the side named in the value


def fixture_status(code: str) -> FixtureStatus:
    """Domain status of an API-Football status code.

    Raises:
        DomainValidationError: unknown code (a new code must be mapped deliberately).
    """
    status = _STATUS_BY_CODE.get(code.strip().upper())
    if status is None:
        msg = f"unknown API-Football status code {code!r}"
        raise DomainValidationError(msg)
    return status


def ref(external_id: int) -> ExternalRef:
    """An API-Football reference."""
    return ExternalRef(SOURCE, str(external_id))


def map_fixture(item: FixtureItem) -> Fixture:
    """Domain fixture of a ``/fixtures`` record (kick-off converted to UTC)."""
    league = League(
        ref=ref(item.league.id),
        sport=Sport.FOOTBALL,
        name=item.league.name,
        country=item.league.country,
    )
    return Fixture(
        ref=ref(item.fixture.id),
        sport=Sport.FOOTBALL,
        league=league,
        season=item.league.season,
        kickoff=item.fixture.date,
        home=Team(ref=ref(item.teams.home.id), sport=Sport.FOOTBALL, name=item.teams.home.name),
        away=Team(ref=ref(item.teams.away.id), sport=Sport.FOOTBALL, name=item.teams.away.name),
        status=fixture_status(item.fixture.status.short),
    )


def map_result(item: FixtureItem) -> FixtureResult | None:
    """The result of a fixture that is over (or will not be played), else ``None``."""
    code = item.fixture.status.short.strip().upper()
    status = fixture_status(code)
    fixture_ref = ref(item.fixture.id)
    if status in {FixtureStatus.SCHEDULED, FixtureStatus.LIVE}:
        return None
    if status in {FixtureStatus.POSTPONED, FixtureStatus.CANCELLED} or code in _AWARDED:
        return FixtureResult(fixture=fixture_ref, status=ResultStatus(status.value))
    full_time = _regular_time_score(item, code)
    half_time = item.score.halftime
    if not _consistent_half_time(half_time, full_time):
        half_time = ScorePair()
    return FixtureResult(
        fixture=fixture_ref,
        status=ResultStatus(status.value),
        home_score=full_time.home,
        away_score=full_time.away,
        home_ht_score=half_time.home,
        away_ht_score=half_time.away,
    )


def _regular_time_score(item: FixtureItem, code: str) -> ScorePair:
    full_time = item.score.fulltime
    if full_time.home is not None and full_time.away is not None:
        return full_time
    if code in _EXTRA_TIME:  # goals include extra time: never a regular-time score
        return ScorePair()
    return item.goals


def _consistent_half_time(half_time: ScorePair, full_time: ScorePair) -> bool:
    if half_time.home is None or half_time.away is None:
        return False
    if full_time.home is None or full_time.away is None:
        return False
    return half_time.home <= full_time.home and half_time.away <= full_time.away


def bookmaker_slug(name: str) -> str:
    """Domain bookmaker id: ``1xBet`` → ``1xbet``, ``William Hill`` → ``william-hill``."""
    slug = _NON_SLUG.sub("-", name.strip().casefold()).strip("-")
    return _KNOWN_BOOKMAKERS.get(slug, slug)


@dataclass(frozen=True, slots=True)
class OddsMappingStats:
    """What was dropped while mapping odds (for logs and the probe report)."""

    unknown_bets: frozenset[str]
    invalid_prices: int


def map_odds(
    items: Iterable[OddsItem],
    *,
    kind: OddsKind,
    taken_at: datetime,
    convention: HandicapConvention = HandicapConvention.HOME_LINE,
) -> tuple[list[OddsSnapshot], OddsMappingStats]:
    """Snapshots for every supported market of every bookmaker in ``items``.

    The first price of a (fixture, bookmaker, market, selection) wins; unsupported bets
    (correct score, first half...) and invalid prices (including ``null``) are skipped and
    counted. ``taken_at`` is the default capture time; an item that carries its own
    ``fetched_at`` (a price served from cache) keeps the time it was really fetched.
    """
    snapshots: dict[tuple[ExternalRef, str, Market, Selection], OddsSnapshot] = {}
    unknown: set[str] = set()
    invalid = 0
    for fixture_ref, slug, bet, fetched_at in _bets(items):
        quotes, bad = _bet_quotes(bet, convention)
        invalid += bad
        if quotes is None:
            unknown.add(bet.name)
            continue
        for market, selection, price in quotes:
            key = (fixture_ref, slug, market, selection)
            if key in snapshots:
                continue
            try:
                snapshots[key] = OddsSnapshot(
                    fixture=fixture_ref,
                    bookmaker=slug,
                    market=market,
                    selection=selection,
                    odds=price,
                    taken_at=fetched_at or taken_at,
                    kind=kind,
                )
            except DomainValidationError:
                invalid += 1
    return list(snapshots.values()), OddsMappingStats(frozenset(unknown), invalid)


def _bets(items: Iterable[OddsItem]) -> Iterator[tuple[ExternalRef, str, Bet, datetime | None]]:
    for item in items:
        fixture_ref = ref(item.fixture.id)
        for bookmaker in item.bookmakers:
            slug = bookmaker_slug(bookmaker.name)
            for bet in bookmaker.bets:
                yield fixture_ref, slug, bet, item.fetched_at


type _Quote = tuple[Market, Selection, float]


def _bet_quotes(bet: Bet, convention: HandicapConvention) -> tuple[list[_Quote] | None, int]:
    """Quotes of one bet, or ``None`` if the bet type is not supported; plus invalid count."""
    name = bet.name.strip().casefold()
    quotes: list[_Quote] = []
    invalid = 0
    if name not in _FIXED_MARKETS and name not in {_TOTALS_BET, _HANDICAP_BET}:
        return None, 0
    for value in bet.values:
        parsed = _parse_value(name, str(value.value), convention)
        price = _parse_price(value.odd)
        if parsed is None or price is None:
            invalid += 1
            continue
        quotes.append((*parsed, price))
    return quotes, invalid


def _parse_value(
    bet_name: str, value: str, convention: HandicapConvention
) -> tuple[Market, Selection] | None:
    text = " ".join(value.strip().casefold().split())
    fixed = _FIXED_SELECTIONS.get(bet_name)
    if fixed is not None:
        selection = fixed.get(text)
        return None if selection is None else (_FIXED_MARKETS[bet_name], selection)
    match = _SIDE_AND_LINE.match(text)
    if match is None:
        return None
    side, number = match.group(1), float(match.group(2))
    try:
        if bet_name == _TOTALS_BET:
            return _totals_quote(side, number)
        return _handicap_quote(side, number, convention)
    except DomainValidationError:  # off-grid line such as 2.3
        return None


def _totals_quote(side: str, line: float) -> tuple[Market, Selection] | None:
    if side not in {"over", "under"}:
        return None
    return Market.totals(line), Selection(side)


def _handicap_quote(
    side: str, number: float, convention: HandicapConvention
) -> tuple[Market, Selection] | None:
    if side not in {"home", "away"}:
        return None
    home_line = -number if convention is HandicapConvention.OWN_LINE and side == "away" else number
    return Market.asian_handicap(home_line), Selection(side)


def _parse_price(odd: str | float | None) -> float | None:
    if odd is None:
        return None
    try:
        return float(odd)
    except ValueError:
        return None


# --------------------------------------------------------------------------- context data


@dataclass(frozen=True, slots=True)
class PlayerAbsence:
    """A player missing or doubtful for a fixture (``/injuries``)."""

    fixture: ExternalRef
    team: ExternalRef
    player: str
    status: str  # provider wording: "Missing Fixture" or "Questionable"
    reason: str | None

    @property
    def doubtful(self) -> bool:
        """Questionable rather than certainly missing."""
        return self.status.casefold() == "questionable"


@dataclass(frozen=True, slots=True)
class TeamLineup:
    """Confirmed starting eleven of one team (``/fixtures/lineups``)."""

    team: ExternalRef
    formation: str | None
    starters: tuple[str, ...]
    substitutes: tuple[str, ...]


def map_injuries(items: Iterable[InjuryItem]) -> list[PlayerAbsence]:
    """Absences of the ``/injuries`` records."""
    return [
        PlayerAbsence(
            fixture=ref(item.fixture.id),
            team=ref(item.team.id),
            player=item.player.name,
            status=item.player.type or "Missing Fixture",
            reason=item.player.reason,
        )
        for item in items
    ]


def map_lineups(items: Iterable[LineupItem]) -> list[TeamLineup]:
    """Line-ups of the ``/fixtures/lineups`` records (empty until announced)."""
    return [
        TeamLineup(
            team=ref(item.team.id),
            formation=item.formation,
            starters=tuple(entry.player.name for entry in item.start_xi),
            substitutes=tuple(entry.player.name for entry in item.substitutes),
        )
        for item in items
    ]


def expected_goals(items: Iterable[TeamStatisticsItem]) -> dict[ExternalRef, float]:
    """XG per team from ``/fixtures/statistics`` (teams without a usable value are absent)."""
    xg: dict[ExternalRef, float] = {}
    for item in items:
        for statistic in item.statistics:
            if statistic.type.strip().casefold() != XG_STATISTIC or statistic.value is None:
                continue
            try:
                value = float(statistic.value)
            except ValueError:
                continue
            if value >= 0:
                xg[ref(item.team.id)] = value
    return xg
