"""Matching API-Football teams to football-data.co.uk teams (same club, two sources).

History (football-data, team id = its spelling such as ``Man United``) and live data
(API-Football, numeric ids, ``Manchester United``) must refer to the same club for the
model to use history when pricing a live fixture. Matching order:

1. curated alias table (:data:`TEAM_ALIASES`, API-Football name → football-data name);
2. exact match of :func:`~app.domain.normalize_name` keys;
3. fuzzy match (``rapidfuzz`` WRatio) above ``threshold`` with a clear ``margin`` over the
   runner-up.

A history team is never claimed by two provider teams; such conflicts and near-ties are
reported as ambiguous, low scores as unmatched. **Nothing is merged silently**: the caller
gets a :class:`TeamMatchReport` to log/review and an :class:`IdentityMap` built from the
confident matches only. Match one league at a time to keep candidates relevant.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

from rapidfuzz import fuzz, process

from app.domain import ExternalRef, Fixture, League, Team, normalize_name
from app.sports.football_leagues import LeagueCatalog

DEFAULT_THRESHOLD: Final = 90.0
DEFAULT_MARGIN: Final = 5.0
_FUZZY_CANDIDATES: Final = 2

# API-Football spelling → football-data.co.uk spelling, only where they differ beyond
# accents/punctuation. Built from knowledge of both sources for the ten default leagues;
# extend it from the "unmatched" report after the first real sync (F0).
_ALIASES: Final[Mapping[str, str]] = {
    # England
    "Manchester United": "Man United",
    "Manchester City": "Man City",
    "Nottingham Forest": "Nott'm Forest",
    "Sheffield Utd": "Sheffield United",
    "Sheffield Wednesday": "Sheffield Weds",
    "Hull City": "Hull",
    "Stoke City": "Stoke",
    "Oxford United": "Oxford",
    "Queens Park Rangers": "QPR",
    # Spain
    "Athletic Club": "Ath Bilbao",
    "Atletico Madrid": "Ath Madrid",
    "Real Betis": "Betis",
    "Celta Vigo": "Celta",
    "Espanyol": "Espanol",
    "Granada CF": "Granada",
    "Real Sociedad": "Sociedad",
    "Rayo Vallecano": "Vallecano",
    # Germany
    "Bayern München": "Bayern Munich",
    "Borussia Dortmund": "Dortmund",
    "Eintracht Frankfurt": "Ein Frankfurt",
    "1. FC Köln": "FC Koln",
    "1. FC Heidenheim": "Heidenheim",
    "1899 Hoffenheim": "Hoffenheim",
    "Bayer Leverkusen": "Leverkusen",
    "Borussia Mönchengladbach": "M'gladbach",
    "FSV Mainz 05": "Mainz",
    "FC St. Pauli": "St Pauli",
    "SV Darmstadt 98": "Darmstadt",
    "Hamburger SV": "Hamburg",
    # Italy
    "AC Milan": "Milan",
    "AS Roma": "Roma",
    "Hellas Verona": "Verona",
    # France
    "Stade Brestois 29": "Brest",
    "Paris Saint Germain": "Paris SG",
    "Saint Etienne": "St Etienne",
    "Clermont Foot": "Clermont",
    # Netherlands
    "RKC Waalwijk": "Waalwijk",
    "PEC Zwolle": "Zwolle",
    "FC Twente": "Twente",
    "FC Utrecht": "Utrecht",
    "Almere City FC": "Almere City",
    # Portugal
    "FC Porto": "Porto",
    "Sporting CP": "Sp Lisbon",
    "SC Braga": "Sp Braga",
    # Belgium
    "Club Brugge KV": "Club Brugge",
    "KV Mechelen": "Mechelen",
    "OH Leuven": "Oud-Heverlee Leuven",
    "Standard Liege": "Standard",
    "Union St. Gilloise": "St. Gilloise",
    # Turkey
    "Istanbul Basaksehir": "Basaksehir",
    "Gaziantep FK": "Gaziantep",
    "Göztepe": "Goztep",
    "Adana Demirspor": "Ad. Demirspor",
}
TEAM_ALIASES: Final[Mapping[str, str]] = {
    normalize_name(provider): normalize_name(history) for provider, history in _ALIASES.items()
}


class MatchMethod(StrEnum):
    """How a pair was matched."""

    ALIAS = "alias"
    EXACT = "exact"
    FUZZY = "fuzzy"


@dataclass(frozen=True, slots=True)
class TeamMatch:
    """A provider team and the history team it is the same club as."""

    provider: Team
    history: Team
    method: MatchMethod
    score: float


@dataclass(frozen=True, slots=True)
class AmbiguousMatch:
    """A provider team with several plausible history teams (needs an alias)."""

    provider: Team
    candidates: tuple[Team, ...]


@dataclass(frozen=True, slots=True)
class TeamMatchReport:
    """Outcome of :func:`match_teams`; only ``matched`` is ever used for identity."""

    matched: tuple[TeamMatch, ...] = ()
    ambiguous: tuple[AmbiguousMatch, ...] = ()
    unmatched: tuple[Team, ...] = ()


def match_teams(
    provider_teams: Iterable[Team],
    history_teams: Sequence[Team],
    *,
    aliases: Mapping[str, str] = TEAM_ALIASES,
    threshold: float = DEFAULT_THRESHOLD,
    margin: float = DEFAULT_MARGIN,
) -> TeamMatchReport:
    """Pair each provider team with at most one history team (see module docstring)."""
    by_key: dict[str, list[Team]] = {}
    for team in history_teams:
        by_key.setdefault(team.normalized_name, []).append(team)
    choices = list(by_key)
    candidates: list[TeamMatch] = []
    ambiguous: list[AmbiguousMatch] = []
    unmatched: list[Team] = []
    for provider in dict.fromkeys(provider_teams):
        outcome = _match_one(provider, by_key, choices, aliases, (threshold, margin))
        if isinstance(outcome, TeamMatch):
            candidates.append(outcome)
        elif isinstance(outcome, AmbiguousMatch):
            ambiguous.append(outcome)
        else:
            unmatched.append(provider)
    claims = Counter(match.history.ref for match in candidates)
    matched = [match for match in candidates if claims[match.history.ref] == 1]
    ambiguous.extend(
        AmbiguousMatch(match.provider, (match.history,))
        for match in candidates
        if claims[match.history.ref] > 1
    )
    return TeamMatchReport(tuple(matched), tuple(ambiguous), tuple(unmatched))


def _match_one(
    provider: Team,
    by_key: Mapping[str, list[Team]],
    choices: list[str],
    aliases: Mapping[str, str],
    limits: tuple[float, float],
) -> TeamMatch | AmbiguousMatch | None:
    key = provider.normalized_name
    for method, candidate_key in ((MatchMethod.ALIAS, aliases.get(key)), (MatchMethod.EXACT, key)):
        if candidate_key is None or candidate_key not in by_key:
            continue
        teams = by_key[candidate_key]
        if len(teams) > 1:
            return AmbiguousMatch(provider, tuple(teams))
        return TeamMatch(provider, teams[0], method, 100.0)
    return _fuzzy(provider, by_key, choices, limits)


def _fuzzy(
    provider: Team,
    by_key: Mapping[str, list[Team]],
    choices: list[str],
    limits: tuple[float, float],
) -> TeamMatch | AmbiguousMatch | None:
    threshold, margin = limits
    ranked = process.extract(
        provider.normalized_name, choices, scorer=fuzz.WRatio, limit=_FUZZY_CANDIDATES
    )
    if not ranked or ranked[0][1] < threshold:
        return None
    best_key, best_score, _ = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    teams = by_key[best_key]
    if best_score - runner_up < margin or len(teams) > 1:
        tied = [key for key, score, _ in ranked if best_score - score < margin]
        return AmbiguousMatch(provider, tuple(team for key in tied for team in by_key[key]))
    return TeamMatch(provider, teams[0], MatchMethod.FUZZY, float(best_score))


@dataclass(frozen=True, slots=True)
class IdentityMap:
    """Rewrites provider teams/leagues to their history identity (one row per club).

    Built from confident matches only; anything unknown passes through unchanged, so an
    unmatched team simply keeps its API-Football identity.
    """

    teams: Mapping[ExternalRef, Team] = field(default_factory=dict)
    leagues: Mapping[ExternalRef, League] = field(default_factory=dict)

    @classmethod
    def from_report(
        cls, report: TeamMatchReport, catalog: LeagueCatalog | None = None
    ) -> "IdentityMap":
        """Map matched teams, and tracked leagues to their football-data league."""
        leagues: dict[ExternalRef, League] = {}
        if catalog is not None:
            leagues = {lg.api_ref: lg.football_data_league() for lg in catalog.leagues}
        teams = {match.provider.ref: match.history for match in report.matched}
        return cls(teams=teams, leagues=leagues)

    def provider_ref(self, canonical: ExternalRef) -> ExternalRef | None:
        """The provider reference of a canonical team, or ``None`` if not mapped."""
        for provider, team in self.teams.items():
            if team.ref == canonical:
                return provider
        return None

    def apply(self, fixture: Fixture) -> Fixture:
        """``fixture`` with its league and teams replaced by their canonical identity."""
        return replace(
            fixture,
            league=self.leagues.get(fixture.league.ref, fixture.league),
            home=self.teams.get(fixture.home.ref, fixture.home),
            away=self.teams.get(fixture.away.ref, fixture.away),
        )
