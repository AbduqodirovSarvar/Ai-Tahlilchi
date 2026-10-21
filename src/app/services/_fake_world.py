"""Deterministic sample world behind the in-memory fake services (private to ``fake``).

Every number is derived from a hash of stable keys, so the same clock always yields the
same leagues, fixtures, odds and scores: tests can rely on it and nothing is random. The
numbers are only *plausible* (odds with a bookmaker margin, EV between about -7% and +15%);
they do not come from the real models, which the fake must not import.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from app.domain import Market, MarketType, Selection, Sport
from app.services.interfaces import (
    MAX_FUTURE_DAYS,
    Absence,
    FixtureDetail,
    FixtureOption,
    FormResult,
    HeadToHead,
    LeagueOption,
    MarketQuote,
    OddsMove,
    ResultMark,
    ScoreProbability,
    TeamOption,
    TeamSide,
    TeamStats,
)

PAST_DAYS: Final = 7
"""Days of finished fixtures before "today", for retro analyses and results."""
MATCH_DURATION: Final = timedelta(hours=2)
"""After this the fake treats a fixture as finished."""
UNREPORTED_SHARE: Final = 0.15
"""Share of finished fixtures whose result the "API" never delivered (manual marking)."""
ABSENCE_SHARE: Final = 0.5
"""Share of fixtures without a reported absence."""
FOOTBALL_TOTAL_LINE: Final = 2.5
HOCKEY_TOTAL_LINE: Final = 5.5
HANDICAP_LINE: Final = -0.5
MIN_FAKE_ODDS: Final = 1.02
ODDS_PRECISION: Final = 2
_HASH_BYTES: Final = 8
_HASH_SPACE: Final = float(2 ** (8 * _HASH_BYTES))
_FORM_LENGTH: Final = 5
_H2H_LENGTH: Final = 3
_TOP_SCORES: Final = ((1, 0), (1, 1), (2, 1), (0, 0), (2, 0), (0, 1))
_LLM_NOTES: Final = (
    "Ikki jamoaning so'nggi 5 o'yinidagi xG o'rtachasi yuqori.",
    "Mehmon jamoa himoyachisi jarohatlangan.",
    "Uy jamoasi shu mavsumda uyda mag'lub bo'lmagan.",
    "Rotatsiya xavfi bor: hafta o'rtasida kubok o'yini.",
)
_ABSENCE_REASONS: Final = ("jarohat", "diskvalifikatsiya")


def unit_interval(*parts: object) -> float:
    """Stable pseudo-random number in ``[0, 1)`` for the given keys."""
    key = "|".join(str(part) for part in parts).encode()
    digest = hashlib.blake2b(key, digest_size=_HASH_BYTES).digest()
    return int.from_bytes(digest) / _HASH_SPACE


def pick[T](options: tuple[T, ...], *parts: object) -> T:
    """Stable choice from ``options`` for the given keys."""
    return options[int(unit_interval(*parts) * len(options))]


@dataclass(frozen=True, slots=True)
class _LeagueSeed:
    league_id: str
    sport: Sport
    name: str
    country: str
    local_hour: int
    teams: tuple[str, ...]


_SEEDS: Final = (
    _LeagueSeed(
        "af:l39",
        Sport.FOOTBALL,
        "Premier League",
        "England",
        20,
        ("Arsenal", "Chelsea", "Liverpool", "Manchester City", "Tottenham", "Newcastle"),
    ),
    _LeagueSeed(
        "af:l140",
        Sport.FOOTBALL,
        "La Liga",
        "Spain",
        22,
        ("Real Madrid", "Barcelona", "Atletico Madrid", "Real Sociedad", "Real Betis", "Sevilla"),
    ),
    _LeagueSeed(
        "af:l135",
        Sport.FOOTBALL,
        "Serie A",
        "Italy",
        21,
        ("Inter", "Milan", "Juventus", "Napoli", "Roma", "Lazio"),
    ),
    _LeagueSeed(
        "af:l78",
        Sport.FOOTBALL,
        "Bundesliga",
        "Germany",
        19,
        ("Bayern München", "Borussia Dortmund", "RB Leipzig", "Bayer Leverkusen"),
    ),
    _LeagueSeed(
        "ah:l35",
        Sport.HOCKEY,
        "KHL",
        "Russia",
        17,
        ("CSKA Moskva", "SKA Sankt-Peterburg", "Ak Bars", "Metallurg Magnitogorsk"),
    ),
)
EXPERIMENTAL_SPORTS: Final = frozenset({Sport.HOCKEY})
"""Sports without a backtest yet: their signals carry the "🧪 Test rejimi" flag."""


@dataclass(frozen=True, slots=True, kw_only=True)
class FakeQuote:
    """Model probability and bookmaker price of one selection."""

    market: Market
    selection: Selection
    probability: float
    odds: float

    @property
    def ev(self) -> float:
        """Expected value per unit staked."""
        return self.probability * self.odds - 1.0


@dataclass(frozen=True, slots=True, kw_only=True)
class FakeFixture:
    """A sample fixture with its quotes and its (eventual) full-time score."""

    fixture_id: str
    sport: Sport
    league: LeagueOption
    home: TeamOption
    away: TeamOption
    kickoff: datetime
    quotes: tuple[FakeQuote, ...]
    home_score: int
    away_score: int
    result_reported: bool

    def started(self, now: datetime) -> bool:
        """True from kickoff on."""
        return self.kickoff <= now

    def finished(self, now: datetime) -> bool:
        """True once the match is over."""
        return self.kickoff + MATCH_DURATION <= now

    def in_play(self, now: datetime) -> bool:
        """True while the match runs: neither analysable nor settled."""
        return self.started(now) and not self.finished(now)

    def option(self) -> FixtureOption:
        """Picker view of the fixture."""
        return FixtureOption(
            fixture_id=self.fixture_id,
            sport=self.sport,
            league=self.league.name,
            home_team=self.home.name,
            away_team=self.away.name,
            kickoff=self.kickoff,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class World:
    """Leagues, teams and fixtures of the sample world (fixtures ordered by kickoff)."""

    leagues: tuple[LeagueOption, ...]
    teams: tuple[TeamOption, ...]
    fixtures: tuple[FakeFixture, ...]


def build_world(today: date, zone: ZoneInfo) -> World:
    """Sample world around ``today`` (local date): past week plus the analysis horizon."""
    leagues = tuple(_league(seed) for seed in _SEEDS)
    teams_by_league = {seed.league_id: _teams(seed) for seed in _SEEDS}
    fixtures = [
        _fixture(
            seed, league, teams_by_league[seed.league_id], today + timedelta(days=offset), zone
        )
        for offset in range(-PAST_DAYS, MAX_FUTURE_DAYS + 1)
        for seed, league in zip(_SEEDS, leagues, strict=True)
    ]
    fixtures.sort(key=lambda fixture: (fixture.kickoff, fixture.fixture_id))
    teams = tuple(team for seed in _SEEDS for team in teams_by_league[seed.league_id])
    return World(leagues=leagues, teams=teams, fixtures=tuple(fixtures))


def _league(seed: _LeagueSeed) -> LeagueOption:
    return LeagueOption(
        league_id=seed.league_id, sport=seed.sport, name=seed.name, country=seed.country
    )


def _teams(seed: _LeagueSeed) -> tuple[TeamOption, ...]:
    return tuple(
        TeamOption(
            team_id=f"{seed.league_id}t{index}", sport=seed.sport, name=name, league=seed.name
        )
        for index, name in enumerate(seed.teams)
    )


def _fixture(
    seed: _LeagueSeed,
    league: LeagueOption,
    teams: tuple[TeamOption, ...],
    day: date,
    zone: ZoneInfo,
) -> FakeFixture:
    ordinal = day.toordinal()
    home = teams[ordinal % len(teams)]
    away = teams[(ordinal + 1 + ordinal % (len(teams) - 1)) % len(teams)]
    fixture_id = f"{seed.league_id}f{day:%y%m%d}"
    kickoff = datetime.combine(day, time(seed.local_hour), tzinfo=zone).astimezone(UTC)
    return FakeFixture(
        fixture_id=fixture_id,
        sport=seed.sport,
        league=league,
        home=home,
        away=away,
        kickoff=kickoff,
        quotes=_quotes(fixture_id, seed.sport),
        home_score=int(unit_interval(fixture_id, "home-goals") * 4),
        away_score=int(unit_interval(fixture_id, "away-goals") * 3),
        result_reported=unit_interval(fixture_id, "reported") >= UNREPORTED_SHARE,
    )


def _quotes(fixture_id: str, sport: Sport) -> tuple[FakeQuote, ...]:
    """Coherent probabilities per market (1X2 sums to 1) priced with a varying edge."""
    home = 0.30 + 0.30 * unit_interval(fixture_id, "p-home")
    draw = 0.20 + 0.08 * unit_interval(fixture_id, "p-draw")
    away = 1.0 - home - draw
    over = 0.38 + 0.26 * unit_interval(fixture_id, "p-over")
    probabilities: list[tuple[Market, Selection, float]] = [
        (Market.match_result(), Selection.HOME, home),
        (Market.match_result(), Selection.DRAW, draw),
        (Market.match_result(), Selection.AWAY, away),
    ]
    if sport is Sport.HOCKEY:
        probabilities += _totals(HOCKEY_TOTAL_LINE, over)
    else:
        btts = 0.40 + 0.22 * unit_interval(fixture_id, "p-btts")
        probabilities += [
            (Market.double_chance(), Selection.HOME_OR_DRAW, home + draw),
            (Market.double_chance(), Selection.HOME_OR_AWAY, home + away),
            (Market.double_chance(), Selection.DRAW_OR_AWAY, draw + away),
            *_totals(FOOTBALL_TOTAL_LINE, over),
            (Market.btts(), Selection.YES, btts),
            (Market.btts(), Selection.NO, 1.0 - btts),
            (Market.asian_handicap(HANDICAP_LINE), Selection.HOME, home),
            (Market.asian_handicap(HANDICAP_LINE), Selection.AWAY, 1.0 - home),
        ]
    return tuple(
        FakeQuote(
            market=market,
            selection=selection,
            probability=probability,
            odds=_price(probability, fixture_id, market, selection),
        )
        for market, selection, probability in probabilities
    )


def _totals(line: float, over: float) -> list[tuple[Market, Selection, float]]:
    return [
        (Market.totals(line), Selection.OVER, over),
        (Market.totals(line), Selection.UNDER, 1.0 - over),
    ]


def _price(probability: float, *key: object) -> float:
    """Bookmaker odds: fair odds times a factor in ``[0.93, 1.15)`` (margin or value)."""
    factor = 0.93 + 0.22 * unit_interval(*key, "edge")
    return max(MIN_FAKE_ODDS, round(factor / probability, ODDS_PRECISION))


def drifted_odds(odds: float, *key: object) -> float:
    """Odds after a recalculation: a stable move within about -6%..+4%."""
    factor = 0.94 + 0.10 * unit_interval(*key, "drift")
    return max(MIN_FAKE_ODDS, round(odds * factor, ODDS_PRECISION))


def settle(quote: FakeQuote, home_goals: int, away_goals: int) -> ResultMark:
    """Win or loss of a selection for a full-time score (fake lines never push)."""
    return ResultMark.WON if _selection_won(quote, home_goals, away_goals) else ResultMark.LOST


def _selection_won(quote: FakeQuote, home_goals: int, away_goals: int) -> bool:
    difference = home_goals - away_goals
    line = quote.market.line or 0.0
    match quote.market.type:
        case MarketType.MATCH_RESULT | MarketType.DOUBLE_CHANCE:
            return quote.selection in _winning_result_selections(difference)
        case MarketType.TOTALS:
            return (home_goals + away_goals > line) == (quote.selection is Selection.OVER)
        case MarketType.BTTS:
            both_scored = home_goals > 0 and away_goals > 0
            return both_scored == (quote.selection is Selection.YES)
        case MarketType.ASIAN_HANDICAP:
            return (difference + line > 0) == (quote.selection is Selection.HOME)


def _winning_result_selections(difference: int) -> frozenset[Selection]:
    if difference > 0:
        return frozenset({Selection.HOME, Selection.HOME_OR_DRAW, Selection.HOME_OR_AWAY})
    if difference == 0:
        return frozenset({Selection.DRAW, Selection.HOME_OR_DRAW, Selection.DRAW_OR_AWAY})
    return frozenset({Selection.AWAY, Selection.HOME_OR_AWAY, Selection.DRAW_OR_AWAY})


def llm_note(*key: object) -> str:
    """A sample Gemini note (Uzbek, as the real reviewer writes)."""
    return pick(_LLM_NOTES, *key)


def build_detail(fixture: FakeFixture, now: datetime) -> FixtureDetail:
    """Detail card of ``fixture`` as seen at ``now``."""
    fid = fixture.fixture_id
    finished = fixture.finished(now)
    return FixtureDetail(
        fixture_id=fid,
        sport=fixture.sport,
        league=fixture.league.name,
        home_team=fixture.home.name,
        away_team=fixture.away.name,
        kickoff=fixture.kickoff,
        home_form=_form(fid, TeamSide.HOME),
        away_form=_form(fid, TeamSide.AWAY),
        home_stats=_stats(fid, TeamSide.HOME),
        away_stats=_stats(fid, TeamSide.AWAY),
        head_to_head=_head_to_head(fixture),
        absences=_absences(fid),
        top_scores=_top_scores(fid),
        markets=tuple(
            MarketQuote(
                market=quote.market,
                selection=quote.selection,
                probability=quote.probability,
                odds=quote.odds,
                ev=quote.ev,
            )
            for quote in fixture.quotes
        ),
        odds_moves=tuple(
            OddsMove(
                market=quote.market,
                selection=quote.selection,
                opening=drifted_odds(quote.odds, fid, quote.market, quote.selection, "open"),
                current=quote.odds,
            )
            for quote in fixture.quotes
        ),
        lineups_confirmed=fixture.kickoff - now <= timedelta(hours=1),
        home_score=fixture.home_score if finished and fixture.result_reported else None,
        away_score=fixture.away_score if finished and fixture.result_reported else None,
    )


def _form(fixture_id: str, side: TeamSide) -> tuple[FormResult, ...]:
    return tuple(
        pick(tuple(FormResult), fixture_id, side, "form", index) for index in range(_FORM_LENGTH)
    )


def _stats(fixture_id: str, side: TeamSide) -> TeamStats:
    return TeamStats(
        matches=6 + int(unit_interval(fixture_id, side, "matches") * 6),
        goals_for_avg=round(0.8 + 1.4 * unit_interval(fixture_id, side, "gf"), 2),
        goals_against_avg=round(0.6 + 1.2 * unit_interval(fixture_id, side, "ga"), 2),
        xg_for_avg=round(0.9 + 1.2 * unit_interval(fixture_id, side, "xgf"), 2),
        xg_against_avg=round(0.7 + 1.0 * unit_interval(fixture_id, side, "xga"), 2),
    )


def _head_to_head(fixture: FakeFixture) -> tuple[HeadToHead, ...]:
    meetings = []
    for index in range(_H2H_LENGTH):
        reverse = index % 2 == 1
        meetings.append(
            HeadToHead(
                kickoff=fixture.kickoff - timedelta(days=180 * (index + 1)),
                home_team=fixture.away.name if reverse else fixture.home.name,
                away_team=fixture.home.name if reverse else fixture.away.name,
                home_score=int(unit_interval(fixture.fixture_id, "h2h-home", index) * 4),
                away_score=int(unit_interval(fixture.fixture_id, "h2h-away", index) * 3),
            )
        )
    return tuple(meetings)


def _absences(fixture_id: str) -> tuple[Absence, ...]:
    if unit_interval(fixture_id, "absence") < ABSENCE_SHARE:
        return ()
    side = pick(tuple(TeamSide), fixture_id, "absence-side")
    reason = pick(_ABSENCE_REASONS, fixture_id, "absence-reason")
    return (Absence(side=side, player="Asosiy himoyachi", reason=reason),)


def _top_scores(fixture_id: str) -> tuple[ScoreProbability, ...]:
    start = int(unit_interval(fixture_id, "scores") * (len(_TOP_SCORES) - 2))
    return tuple(
        ScoreProbability(home_goals=home, away_goals=away, probability=0.11 - 0.01 * rank)
        for rank, (home, away) in enumerate(_TOP_SCORES[start : start + 3])
    )
