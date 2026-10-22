"""Team search for the bot (plan §6.3): typo-tolerant, Latin/Cyrillic, prefix-friendly.

Users type in Uzbek Latin or Cyrillic (``Арсенал``, ``Manchester yunayted``) and often
only the start of a name (``bars``). Both the query and every team name are reduced to one
search key: Cyrillic → Uzbek Latin transliteration, then
:func:`~app.domain.normalize_name` (no case, accents, apostrophes or punctuation). Keys are
scored with ``rapidfuzz`` (WRatio) and boosted when the query is a prefix of the name or of
one of its words, so ``bar`` ranks Barcelona above names that merely contain ``bar``.

The pure functions (:func:`to_latin`, :func:`to_cyrillic`, :func:`search_key`,
:func:`rank`) have no I/O; :func:`search_teams` loads candidates from the database.
"""

# Transliteration tables are made of look-alike Cyrillic/Latin letters on purpose.
# ruff: noqa: RUF001, RUF003

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from rapidfuzz import fuzz, process
from sqlalchemy import func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import FixtureRow, LeagueRow, TeamRow
from app.db.search_aliases import SEARCH_ALIASES
from app.domain import ExternalRef, Sport, normalize_name

DEFAULT_LIMIT: Final = 10
MIN_SCORE: Final = 60.0
PREFIX_BOOST: Final = 15.0
WORD_PREFIX_BOOST: Final = 10.0
MAX_SCORE: Final = 100.0

# Uzbek Cyrillic → Uzbek Latin (official 1995 alphabet); Russian-only letters included.
_CYRILLIC_TO_LATIN: Final[dict[str, str]] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "'", "ы": "i", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "ў": "o'", "қ": "q", "ғ": "g'", "ҳ": "h",
}  # fmt: skip
_LATIN_DIGRAPHS: Final[dict[str, str]] = {
    "o'": "ў", "g'": "ғ", "sh": "ш", "ch": "ч", "yo": "ё", "yu": "ю", "ya": "я", "ts": "ц",
}  # fmt: skip
_LATIN_TO_CYRILLIC: Final[dict[str, str]] = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ", "i": "и",
    "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "қ",
    "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "x": "х", "y": "й", "z": "з",
    "c": "к", "w": "в",
}  # fmt: skip
# Uzbek apostrophe variants (oʻ, gʻ, tutuq belgisi) all become a plain apostrophe.
_APOSTROPHES: Final = str.maketrans(dict.fromkeys("ʻʼ‘’`´", "'"))


def to_latin(text: str) -> str:
    """Uzbek Cyrillic letters → Uzbek Latin; everything else is kept (lower-cased)."""
    lowered = text.casefold().translate(_APOSTROPHES)
    return "".join(_CYRILLIC_TO_LATIN.get(char, char) for char in lowered)


def to_cyrillic(text: str) -> str:
    """Uzbek Latin → Cyrillic (digraphs first); other characters are kept (lower-cased)."""
    lowered = text.casefold().translate(_APOSTROPHES)
    result: list[str] = []
    index = 0
    while index < len(lowered):
        pair = lowered[index : index + 2]
        if pair in _LATIN_DIGRAPHS:
            result.append(_LATIN_DIGRAPHS[pair])
            index += 2
            continue
        result.append(_LATIN_TO_CYRILLIC.get(lowered[index], lowered[index]))
        index += 1
    return "".join(result)


def search_key(text: str) -> str:
    """The comparable form of a query or a name (Latin, no case/accents/punctuation)."""
    return normalize_name(to_latin(text))


def _alias_keys() -> dict[str, tuple[str, ...]]:
    table: dict[str, list[str]] = {}
    for spelling, targets in SEARCH_ALIASES.items():
        known = table.setdefault(search_key(spelling), [])
        known.extend(k for k in map(search_key, targets) if k and k not in known)
    return {key: tuple(targets) for key, targets in table.items()}


# Typed spelling (as a search key) -> search keys of the names the sources use.
_ALIAS_KEYS: Final = _alias_keys()


@dataclass(frozen=True, slots=True)
class Candidate:
    """Something searchable: an id and its display name."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class Hit:
    """A ranked candidate (score 0..100, higher is better)."""

    id: int
    name: str
    score: float


def rank(query: str, candidates: Sequence[Candidate], *, limit: int = DEFAULT_LIMIT) -> list[Hit]:
    """Best ``limit`` candidates for ``query`` (score ≥ ``MIN_SCORE``), best first.

    Ties are broken by shorter name, then name, so results are stable.
    """
    key = search_key(query)
    if not key or limit <= 0:
        return []
    keys = [search_key(candidate.name) for candidate in candidates]
    best: dict[int, float] = {}
    for variant in (key, *_ALIAS_KEYS.get(key, ())):
        scored = process.extract(variant, keys, scorer=fuzz.WRatio, limit=None, score_cutoff=0)
        for name_key, base, index in scored:
            score = min(base + _prefix_boost(variant, name_key), MAX_SCORE)
            best[index] = max(best.get(index, 0.0), float(score))
    hits = [
        Hit(candidates[index].id, candidates[index].name, score)
        for index, score in best.items()
        if score >= MIN_SCORE
    ]
    hits.sort(key=lambda hit: (-hit.score, len(hit.name), hit.name))
    return hits[:limit]


def _prefix_boost(key: str, name_key: str) -> float:
    if name_key == key:
        return MAX_SCORE
    if name_key.startswith(key):
        return PREFIX_BOOST
    if any(word.startswith(key) for word in name_key.split()):
        return WORD_PREFIX_BOOST
    return 0.0


@dataclass(frozen=True, slots=True)
class TeamHit:
    """A team found by :func:`search_teams`; ``league`` is its most recent league."""

    team_id: int
    ref: ExternalRef
    sport: Sport
    name: str
    league: str | None
    score: float


async def search_teams(
    session: AsyncSession, text: str, *, sport: Sport | None = None, limit: int = DEFAULT_LIMIT
) -> list[TeamHit]:
    """Teams whose name matches ``text`` (see module docstring), best first."""
    query = select(TeamRow.id, TeamRow.name, TeamRow.source, TeamRow.external_id, TeamRow.sport)
    if sport is not None:
        query = query.where(TeamRow.sport == sport)
    rows = {row.id: row for row in await session.execute(query)}
    hits = rank(text, [Candidate(row.id, row.name) for row in rows.values()], limit=limit)
    leagues = await _latest_leagues(session, [hit.id for hit in hits])
    return [
        TeamHit(
            team_id=hit.id,
            ref=ExternalRef(rows[hit.id].source, rows[hit.id].external_id),
            sport=rows[hit.id].sport,
            name=hit.name,
            league=leagues.get(hit.id),
            score=hit.score,
        )
        for hit in hits
    ]


async def _latest_leagues(session: AsyncSession, team_ids: Iterable[int]) -> dict[int, str]:
    """Name of the league of each team's most recent fixture (one grouped query).

    Aggregates in SQL (latest kick-off per team and league) instead of loading the whole
    fixture history of the teams.
    """
    ids = list(team_ids)
    if not ids:
        return {}
    appearances = union_all(
        select(
            FixtureRow.home_team_id.label("team_id"),
            FixtureRow.league_id.label("league_id"),
            FixtureRow.kickoff_at.label("kickoff_at"),
        ).where(FixtureRow.home_team_id.in_(ids)),
        select(FixtureRow.away_team_id, FixtureRow.league_id, FixtureRow.kickoff_at).where(
            FixtureRow.away_team_id.in_(ids)
        ),
    ).subquery()
    latest = func.max(appearances.c.kickoff_at)
    rows = await session.execute(
        select(appearances.c.team_id, LeagueRow.name, latest)
        .join(LeagueRow, appearances.c.league_id == LeagueRow.id)
        .group_by(appearances.c.team_id, LeagueRow.id, LeagueRow.name)
    )
    newest: dict[int, tuple[datetime, str]] = {}
    for team_id, league, kickoff in rows:
        if team_id not in newest or kickoff > newest[team_id][0]:
            newest[team_id] = (kickoff, league)
    return {team_id: league for team_id, (_, league) in newest.items()}
