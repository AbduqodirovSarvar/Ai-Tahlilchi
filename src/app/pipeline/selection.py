"""Which candidates become signals: thresholds, best-per-fixture, ordering, caps (pure).

The rules of the service contract (``docs/contracts.md`` §4, "Semantika"):

- a selection passes with ``probability >= min_probability``, ``ev >= min_ev``, the odds
  inside the user's range and no model/market gap above 15 points;
- from each fixture the single best passing selection is taken (by the sort key); a custom
  analysis (``include_no_value``) keeps every selection and flags the failing ones;
- the result is sorted, then cut to ``max_signals`` in total or per local day;
- watched fixtures (scope ``all``) are always included - a failing one with its highest-EV
  selection - outside the cut and outside the ``matched`` count.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from app.db.enums import QueryScope, SortOrder
from app.db.repo import FixtureRecord
from app.pipeline.types import AnalysisRequest
from app.pipeline.valuation import Candidate


@dataclass(frozen=True, slots=True, kw_only=True)
class Pick:
    """A candidate chosen for a fixture, with the verdict of the thresholds."""

    fixture: FixtureRecord
    candidate: Candidate
    passes: bool
    watchlist: bool = False
    retro: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Assembly:
    """The final ordered picks and how many regular picks matched before the cut."""

    picks: tuple[Pick, ...]
    matched: int


def passes(candidate: Candidate, request: AnalysisRequest) -> bool:
    """True when ``candidate`` is a recommendation under ``request``'s thresholds."""
    return (
        candidate.probability >= request.min_probability
        and candidate.ev >= request.min_ev
        and request.odds_min <= candidate.odds <= request.odds_max
        and not candidate.assessment.sanity_flag
    )


def sort_key(order: SortOrder) -> Callable[[Pick], tuple[object, ...]]:
    """Ordering of picks: EV and probability descending, kick-off ascending; stable ties."""

    def key(pick: Pick) -> tuple[object, ...]:
        candidate = pick.candidate
        tie_break = (pick.fixture.id, str(candidate.market), candidate.selection.value)
        kickoff = pick.fixture.fixture.kickoff
        match order:
            case SortOrder.EV:
                return (-candidate.ev, kickoff, *tie_break)
            case SortOrder.PROBABILITY:
                return (-candidate.probability, kickoff, *tie_break)
            case SortOrder.KICKOFF:
                return (kickoff, -candidate.ev, *tie_break)

    return key


def picks_for_fixture(
    fixture: FixtureRecord,
    candidates: Sequence[Candidate],
    request: AnalysisRequest,
    *,
    watched: bool,
    retro: bool,
) -> list[Pick]:
    """The picks one fixture contributes under ``request``."""
    picks = [
        Pick(
            fixture=fixture,
            candidate=candidate,
            passes=passes(candidate, request),
            watchlist=watched,
            retro=retro,
        )
        for candidate in candidates
    ]
    if request.include_no_value:
        return picks
    passing = [pick for pick in picks if pick.passes]
    if passing:
        return [min(passing, key=sort_key(request.sort))]
    if watched and picks:
        return [max(picks, key=lambda pick: pick.candidate.ev)]
    return []


def assemble(by_fixture: Sequence[Sequence[Pick]], request: AnalysisRequest) -> Assembly:
    """Sort, cut and merge the per-fixture picks (see the module docstring)."""
    regular: list[Pick] = []
    watched: list[Pick] = []
    for picks in by_fixture:
        is_watched = request.scope is QueryScope.ALL and any(pick.watchlist for pick in picks)
        (watched if is_watched else regular).extend(picks)
    order = sort_key(request.sort)
    regular.sort(key=order)
    kept = _cut(regular, request)
    return Assembly(picks=tuple(sorted([*kept, *watched], key=order)), matched=len(regular))


def _cut(picks: list[Pick], request: AnalysisRequest) -> list[Pick]:
    """Apply ``max_signals`` in total or per local day, keeping the order."""
    if request.max_signals is None:
        return picks
    if not request.per_day:
        return picks[: request.max_signals]
    per_day: dict[date, int] = {}
    kept = []
    for pick in picks:
        day = local_day(pick, request.zone)
        if per_day.get(day, 0) < request.max_signals:
            per_day[day] = per_day.get(day, 0) + 1
            kept.append(pick)
    return kept


def local_day(pick: Pick, zone: ZoneInfo) -> date:
    """The user's local kick-off date of ``pick``."""
    return pick.fixture.fixture.kickoff.astimezone(zone).date()
