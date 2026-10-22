"""A small deterministic football world for pipeline and real-service tests.

Two leagues of twelve teams (source ``football-data``, like the history importer writes
them), three seasons of double round robins, a round every three days. "Today" is the day of
``START`` (a round is played that day); earlier rounds are finished with scores, later rounds
are scheduled with their results prepared in ``feed`` for the tests to reveal when the clock
passes the matches. Prices are derived from the fitted Dixon-Coles model (Pinnacle with a 3%
margin, 1xBet with a deterministic spread around it), so some selections have positive EV.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import numpy as np

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
    Sport,
    Team,
)
from app.models import fit_dixon_coles, match_records, predict_markets, team_key

SOURCE = "football-data"
START = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
ROUND_DAYS = 3
ROUNDS_PER_SEASON = 22
CURRENT_SEASON = 2026
FIRST_ROUND_OF_TODAY = 14
KICKOFF_HOURS = (10.0, 11.5, 13.0, 14.5, 16.0, 17.5)
PINNACLE_MARGIN = 0.03
LEAGUES = {
    "E0": ("Premier League", "England"),
    "SP1": ("La Liga", "Spain"),
}
TEAM_NAMES = {
    "E0": ["Arsenal", "Chelsea", "Liverpool", "Man City", "Man United", "Tottenham",
           "Newcastle", "Aston Villa", "Brighton", "West Ham", "Fulham", "Everton"],
    "SP1": ["Real Madrid", "Barcelona", "Sevilla", "Valencia", "Betis", "Sociedad",
            "Villarreal", "Girona", "Getafe", "Celta", "Osasuna", "Mallorca"],
}
MARKETS = (
    Market.match_result(),
    Market.double_chance(),
    Market.totals(2.5),
    Market.btts(),
    Market.asian_handicap(-0.5),
)


def _unit(*key: object) -> float:
    """A stable number in [0, 1) from ``key`` (no randomness anywhere)."""
    digest = hashlib.blake2b("|".join(map(str, key)).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def league_of(code: str) -> League:
    """The league ``code`` as the importer stores it."""
    name, country = LEAGUES[code]
    return League(ref=ExternalRef(SOURCE, code), sport=Sport.FOOTBALL, name=name, country=country)


def team_of(name: str) -> Team:
    """A team as the history importer stores it (id = its spelling)."""
    return Team(ref=ExternalRef(SOURCE, name), sport=Sport.FOOTBALL, name=name)


def _rounds(teams: list[str]) -> list[list[tuple[str, str]]]:
    """Double round robin by the circle method: 22 rounds of 6 matches for 12 teams."""
    ring = list(teams)
    first: list[list[tuple[str, str]]] = []
    for round_number in range(len(ring) - 1):
        pairs = [(ring[i], ring[-1 - i]) for i in range(len(ring) // 2)]
        flip = round_number % 2 == 1
        first.append([(b, a) if flip and i == 0 else (a, b) for i, (a, b) in enumerate(pairs)])
        ring = [ring[0], ring[-1], *ring[1:-1]]
    second = [[(b, a) for a, b in matches] for matches in first]
    return first + second


def _strength(code: str, name: str) -> tuple[float, float]:
    return (_unit(code, name, "att") - 0.5) * 0.8, (_unit(code, name, "def") - 0.5) * 0.8


def _result(fixture: Fixture, code: str) -> FixtureResult:
    """The deterministic final score of ``fixture`` from the teams' hidden strengths."""
    att_h, def_h = _strength(code, fixture.home.name)
    att_a, def_a = _strength(code, fixture.away.name)
    seed = int(_unit(fixture.ref, "goals") * 2**32)
    rng = np.random.default_rng(seed)
    home = int(rng.poisson(1.45 * np.exp(att_h - def_a)))
    away = int(rng.poisson(1.15 * np.exp(att_a - def_h)))
    return FixtureResult(
        fixture=fixture.ref, status=ResultStatus.FINISHED, home_score=home, away_score=away
    )


def round_date(season: int, round_number: int) -> date:
    """Calendar day of a round (current season: round 14 is START's day)."""
    base = START.date() - timedelta(days=ROUND_DAYS * FIRST_ROUND_OF_TODAY)
    season_start = base - timedelta(days=365 * (CURRENT_SEASON - season))
    return season_start + timedelta(days=ROUND_DAYS * round_number)


@dataclass(frozen=True, slots=True)
class World:
    """Everything the tests seed and reveal."""

    fixtures: tuple[Fixture, ...]
    results: tuple[FixtureResult, ...]
    feed: dict[ExternalRef, FixtureResult]
    odds: tuple[OddsSnapshot, ...]

    def batch(self) -> HistoryBatch:
        """The seed written to the database."""
        return HistoryBatch(fixtures=self.fixtures, results=self.results, odds=self.odds)

    def history_batch(self) -> HistoryBatch:
        """Only the finished matches and their results (the provider supplies the rest)."""
        past = tuple(f for f in self.fixtures if f.status is FixtureStatus.FINISHED)
        return HistoryBatch(fixtures=past, results=self.results)


def build_world() -> World:
    """Generate fixtures, past results, the hidden future results and all prices."""
    fixtures: list[Fixture] = []
    codes: dict[ExternalRef, str] = {}
    for code, teams in TEAM_NAMES.items():
        league = league_of(code)
        for season in (2024, 2025, CURRENT_SEASON):
            for number, matches in enumerate(_rounds(teams)):
                day = round_date(season, number)
                for slot, (home, away) in enumerate(matches):
                    hour = KICKOFF_HOURS[slot]
                    kickoff = datetime.combine(day, time(), tzinfo=UTC) + timedelta(hours=hour)
                    fixture = Fixture(
                        ref=ExternalRef(SOURCE, f"{code}/{season}/{home}/{away}"),
                        sport=Sport.FOOTBALL,
                        league=league,
                        season=season,
                        kickoff=kickoff,
                        home=team_of(home),
                        away=team_of(away),
                        status=FixtureStatus.SCHEDULED
                        if kickoff > START
                        else FixtureStatus.FINISHED,
                    )
                    fixtures.append(fixture)
                    codes[fixture.ref] = code
    outcomes = {f.ref: _result(f, codes[f.ref]) for f in fixtures}
    past = tuple(outcomes[f.ref] for f in fixtures if f.status is FixtureStatus.FINISHED)
    feed = {f.ref: outcomes[f.ref] for f in fixtures if f.status is FixtureStatus.SCHEDULED}
    return World(
        fixtures=tuple(fixtures),
        results=past,
        feed=feed,
        odds=tuple(_prices(fixtures, past)),
    )


def _prices(fixtures: list[Fixture], past: tuple[FixtureResult, ...]) -> list[OddsSnapshot]:
    finished = [f for f in fixtures if f.status is FixtureStatus.FINISHED]
    snapshots: list[OddsSnapshot] = []
    for code in LEAGUES:
        league_fixtures = [f for f in finished if f.league.ref.external_id == code]
        matches = match_records(league_fixtures, past)
        model = fit_dixon_coles(matches, START.date() + timedelta(days=1))
        priced = [
            f
            for f in fixtures
            if f.league.ref.external_id == code
            and f.season == CURRENT_SEASON
            and f.kickoff.date() >= START.date() - timedelta(days=7)
        ]
        for fixture in priced:
            probabilities = predict_markets(
                model, team_key(fixture.home), team_key(fixture.away), MARKETS
            )
            snapshots.extend(_quotes(fixture, probabilities))
    return snapshots


def _quotes(fixture: Fixture, probabilities: dict) -> list[OddsSnapshot]:  # type: ignore[type-arg]
    past = fixture.kickoff <= START
    kind = OddsKind.OPENING if past else OddsKind.MORNING
    taken_at = fixture.kickoff - timedelta(days=1) if past else START - timedelta(hours=1)
    rows = []
    for market, distribution in probabilities.items():
        for selection in market.selections:
            probability = min(distribution.win_probability(selection) * (1 + PINNACLE_MARGIN), 0.97)
            sharp = max(1.0 / probability, 1.02)
            spread = 0.93 + 0.2 * _unit(fixture.ref, market, selection)
            soft = max(sharp * spread, 1.02)
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

