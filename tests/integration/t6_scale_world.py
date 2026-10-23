"""A production-sized database for the T6 scale checks.

Ten tracked leagues of twenty teams, five full seasons of results (football-data identity) and
about six months of bookmaker snapshots (Pinnacle and 1xBet, 1X2 and Total 2.5). All numbers are
derived from hidden team strengths with plain Poisson arithmetic, no randomness (hash-based).
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from scipy.stats import poisson

from app.db.engine import create_engine, create_session_factory
from app.db.history import upsert_batch
from app.db.schema import Base
from app.domain import (
    BOOKMAKER_1XBET,
    BOOKMAKER_PINNACLE,
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
from app.pipeline.ledger import Ledger
from app.sports.football_leagues import DEFAULT_LEAGUES
from tests.pipeline.world import _rounds, _unit

SOURCE = "football-data"
TEAMS_PER_LEAGUE = 20
SEASONS = (2022, 2023, 2024, 2025, 2026)
NOW = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
ROUND_GAP_DAYS = 7
SEASON_START_MONTH, SEASON_START_DAY = 8, 15
SNAPSHOT_MONTHS = 6
CANDIDATES_PER_LEAGUE = 3
CANDIDATE_FIXTURES = CANDIDATES_PER_LEAGUE * len(DEFAULT_LEAGUES)
LOOKAHEAD_DAYS = 5
MARGIN = 0.03


@dataclass(frozen=True)
class Scale:
    """Counts of what was written."""

    fixtures: int
    finished: int
    odds: int
    upcoming: int


def league_of(code: str) -> League:
    name = {t.football_data_code: t for t in DEFAULT_LEAGUES}[code].football_data_league().name
    return League(ref=ExternalRef(SOURCE, code), sport=Sport.FOOTBALL, name=name, country="X")


def team_of(code: str, index: int) -> Team:
    return Team(
        ref=ExternalRef(SOURCE, f"{code}-T{index:02d}"),
        sport=Sport.FOOTBALL,
        name=f"{code} T{index:02d}",
    )


def _strength(code: str, index: int) -> tuple[float, float]:
    return (_unit(code, index, "att") - 0.5) * 0.8, (_unit(code, index, "def") - 0.5) * 0.8


def _rates(code: str, home: int, away: int) -> tuple[float, float]:
    att_h, def_h = _strength(code, home)
    att_a, def_a = _strength(code, away)
    return 1.45 * math.exp(att_h - def_a), 1.15 * math.exp(att_a - def_h)


def _probabilities(code: str, home: int, away: int) -> dict[tuple[Market, Selection], float]:
    lam, mu = _rates(code, home, away)
    grid = [[poisson.pmf(h, lam) * poisson.pmf(a, mu) for a in range(10)] for h in range(10)]
    home_win = sum(grid[h][a] for h in range(10) for a in range(10) if h > a)
    draw = sum(grid[h][h] for h in range(10))
    over = sum(grid[h][a] for h in range(10) for a in range(10) if h + a > 2)
    total = home_win + draw + (1 - home_win - draw)
    return {
        (Market.match_result(), Selection.HOME): home_win / total,
        (Market.match_result(), Selection.DRAW): draw / total,
        (Market.match_result(), Selection.AWAY): 1 - (home_win + draw) / total,
        (Market.totals(2.5), Selection.OVER): over,
        (Market.totals(2.5), Selection.UNDER): 1 - over,
    }


def _snapshots(
    fixture: Fixture, code: str, home: int, away: int, kinds: tuple[tuple[OddsKind, datetime], ...]
) -> list[OddsSnapshot]:
    rows = []
    for (market, selection), probability in _probabilities(code, home, away).items():
        sharp = max(1 / min(probability * (1 + MARGIN), 0.97), 1.02)
        soft = max(sharp * (0.93 + 0.2 * _unit(fixture.ref, market, selection)), 1.02)
        for kind, taken_at in kinds:
            for bookmaker, odds in ((BOOKMAKER_PINNACLE, sharp), (BOOKMAKER_1XBET, soft)):
                rows.append(
                    OddsSnapshot(
                        fixture=fixture.ref,
                        bookmaker=bookmaker,
                        market=market,
                        selection=selection,
                        odds=round(odds, 3),
                        taken_at=taken_at,
                        kind=kind,
                    )
                )
    return rows


@dataclass(frozen=True)
class Match:
    """A fixture with the team indexes its prices and score are derived from."""

    fixture: Fixture
    home: int
    away: int


def _matches(code: str, season: int) -> list[Match]:
    """Every match of one league season that is already played or due in the lookahead."""
    league = league_of(code)
    teams = [team_of(code, i) for i in range(TEAMS_PER_LEAGUE)]
    start = datetime.combine(date(season, SEASON_START_MONTH, SEASON_START_DAY), time(), tzinfo=UTC)
    horizon = NOW + timedelta(days=LOOKAHEAD_DAYS)
    found = []
    for number, matches in enumerate(_rounds(list(range(TEAMS_PER_LEAGUE)))):
        for slot, (home, away) in enumerate(matches):
            kickoff = start + timedelta(days=ROUND_GAP_DAYS * number, hours=12 + slot % 8)
            if kickoff > horizon:
                continue
            fixture = Fixture(
                ref=ExternalRef(SOURCE, f"{code}/{season}/{home}/{away}"),
                sport=Sport.FOOTBALL,
                league=league,
                season=season,
                kickoff=kickoff,
                home=teams[home],
                away=teams[away],
                status=FixtureStatus.FINISHED if kickoff <= NOW else FixtureStatus.SCHEDULED,
            )
            found.append(Match(fixture, home, away))
    return found


def _history_prices(match: Match, code: str) -> list[OddsSnapshot]:
    kickoff = match.fixture.kickoff
    kinds = (
        (OddsKind.OPENING, kickoff - timedelta(days=2)),
        (OddsKind.MORNING, kickoff - timedelta(hours=8)),
        (OddsKind.PREMATCH, kickoff - timedelta(hours=1)),
        (OddsKind.CLOSING, kickoff - timedelta(minutes=5)),
    )
    return _snapshots(match.fixture, code, match.home, match.away, kinds)


def _played(match: Match, code: str, since: datetime) -> tuple[FixtureResult, list[OddsSnapshot]]:
    """The score of a finished match and, within the snapshot window, its price history."""
    lam, mu = _rates(code, match.home, match.away)
    seed = int(_unit(match.fixture.ref, "goals") * 2**32)
    prices = _history_prices(match, code) if match.fixture.kickoff >= since else []
    return _score(match.fixture, lam, mu, seed), prices


def build_batch() -> tuple[HistoryBatch, Scale]:
    """Five seasons x ten leagues of fixtures/results, six months of snapshots, 30 candidates."""
    fixtures: list[Fixture] = []
    results: list[FixtureResult] = []
    odds: list[OddsSnapshot] = []
    since = NOW - timedelta(days=30 * SNAPSHOT_MONTHS)
    upcoming = 0
    for tracked in DEFAULT_LEAGUES:
        code = tracked.football_data_code
        candidates_left = CANDIDATES_PER_LEAGUE
        matches = [m for season in SEASONS for m in _matches(code, season)]
        for match in matches:
            fixture = match.fixture
            if fixture.status is FixtureStatus.FINISHED:
                result, prices = _played(match, code, since)
                results.append(result)
                odds.extend(prices)
            elif candidates_left > 0:
                morning = ((OddsKind.MORNING, NOW - timedelta(hours=1)),)
                odds.extend(_snapshots(fixture, code, match.home, match.away, morning))
                candidates_left -= 1
                upcoming += 1
            else:
                continue
            fixtures.append(fixture)
    return (
        HistoryBatch(fixtures=tuple(fixtures), results=tuple(results), odds=tuple(odds)),
        Scale(len(fixtures), len(results), len(odds), upcoming),
    )


def _score(fixture: Fixture, lam: float, mu: float, seed: int) -> FixtureResult:
    import numpy as np  # noqa: PLC0415

    rng = np.random.default_rng(seed)
    return FixtureResult(
        fixture=fixture.ref,
        status=ResultStatus.FINISHED,
        home_score=int(rng.poisson(lam)),
        away_score=int(rng.poisson(mu)),
    )


async def build_database(path: Path) -> Scale:
    """Write the batch into a fresh database file (schema from the models + the ledger)."""
    batch, scale = build_batch()
    engine = create_engine(path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await Ledger.ensure_schema(engine)
    sessions = create_session_factory(engine)
    chunk = 4000
    fixtures = list(batch.fixtures)
    for index in range(0, len(fixtures), chunk):
        part = {f.ref for f in fixtures[index : index + chunk]}
        async with sessions.begin() as session:
            await upsert_batch(
                session,
                HistoryBatch(
                    fixtures=tuple(fixtures[index : index + chunk]),
                    results=tuple(r for r in batch.results if r.fixture in part),
                    odds=tuple(o for o in batch.odds if o.fixture in part),
                ),
            )
    await engine.dispose()
    return scale
