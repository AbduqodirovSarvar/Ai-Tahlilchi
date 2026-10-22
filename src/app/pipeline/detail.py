"""Data of the detailed fixture card (plan §6.8), computed from stored data only.

No provider request is made: form, venue averages, head-to-head, the likeliest scores and the
all-markets table come from the database and the cached model; absences and line-ups are not
stored, so the card says "unknown". Missing pieces (no model for a promoted side, no prices)
are simply left empty.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import FixtureRecord, finished_fixtures, get_fixture_by_id, odds_history
from app.domain import Market, OddsKind, Selection, Team
from app.models import DixonColesModel, UnknownTeamError, team_key, top_scores
from app.pipeline.context import PipelineContext
from app.pipeline.odds import current_quotes, load_decision_snapshots
from app.pipeline.overlay import load_overlay
from app.pipeline.valuation import ValuationInput, value_fixture

FORM_LENGTH: Final = 5
TOP_SCORE_COUNT: Final = 3
HEAD_TO_HEAD_LENGTH: Final = 5
_OPENING_KINDS: Final = (OddsKind.OPENING, OddsKind.MORNING)


@dataclass(frozen=True, slots=True)
class VenueStats:
    """Averages of a team in its venue role."""

    matches: int
    goals_for: float
    goals_against: float
    xg_for: float | None
    xg_against: float | None


@dataclass(frozen=True, slots=True)
class Meeting:
    """One earlier meeting of the two teams."""

    kickoff: datetime
    home: str
    away: str
    home_score: int
    away_score: int


@dataclass(frozen=True, slots=True)
class QuoteRow:
    """One selection of the all-markets table."""

    market: Market
    selection: Selection
    probability: float
    odds: float | None
    ev: float | None


@dataclass(frozen=True, slots=True)
class OddsShift:
    """A price at the first and the latest capture."""

    market: Market
    selection: Selection
    opening: float
    current: float


@dataclass(frozen=True, slots=True)
class FixtureCard:
    """Everything the detail card shows."""

    record: FixtureRecord
    home_form: tuple[str, ...]
    away_form: tuple[str, ...]
    home_stats: VenueStats
    away_stats: VenueStats
    head_to_head: tuple[Meeting, ...]
    top_scores: tuple[tuple[int, int, float], ...]
    quotes: tuple[QuoteRow, ...]
    odds_moves: tuple[OddsShift, ...]
    data_as_of: datetime | None


def _score(record: FixtureRecord) -> tuple[int, int] | None:
    result = record.result
    if result is None or result.home_score is None or result.away_score is None:
        return None
    return result.home_score, result.away_score


def _form(team: Team, history: list[FixtureRecord]) -> tuple[str, ...]:
    """The team's last results, oldest first, as ``win``/``draw``/``loss``."""
    marks = []
    for record in history:
        score = _score(record)
        if score is None or team.ref not in (record.fixture.home.ref, record.fixture.away.ref):
            continue
        own, other = score if record.fixture.home.ref == team.ref else score[::-1]
        marks.append("win" if own > other else "draw" if own == other else "loss")
    return tuple(marks[-FORM_LENGTH:])


def _venue(team: Team, history: list[FixtureRecord], *, home: bool) -> VenueStats:
    rows = [
        record
        for record in history
        if _score(record) is not None
        and (record.fixture.home.ref if home else record.fixture.away.ref) == team.ref
    ]
    if not rows:
        return VenueStats(0, 0.0, 0.0, None, None)
    scores = [s for record in rows if (s := _score(record)) is not None]
    own = [s[0] if home else s[1] for s in scores]
    conceded = [s[1] if home else s[0] for s in scores]
    xg = [(r.home_xg, r.away_xg) for r in rows if r.home_xg is not None and r.away_xg is not None]
    return VenueStats(
        matches=len(rows),
        goals_for=sum(own) / len(rows),
        goals_against=sum(conceded) / len(rows),
        xg_for=sum(x[0 if home else 1] for x in xg) / len(xg) if xg else None,
        xg_against=sum(x[1 if home else 0] for x in xg) / len(xg) if xg else None,
    )


def _meetings(card: FixtureRecord, history: list[FixtureRecord]) -> tuple[Meeting, ...]:
    pair = {card.fixture.home.ref, card.fixture.away.ref}
    found = [
        Meeting(
            record.fixture.kickoff,
            record.fixture.home.name,
            record.fixture.away.name,
            score[0],
            score[1],
        )
        for record in history
        if {record.fixture.home.ref, record.fixture.away.ref} == pair
        and (score := _score(record)) is not None
    ]
    return tuple(found[-HEAD_TO_HEAD_LENGTH:])


async def build_fixture_card(ctx: PipelineContext, fixture_id: int) -> FixtureCard | None:
    """The detail card of a stored fixture, or ``None`` when it does not exist."""
    now = ctx.clock()
    async with ctx.sessions() as session:
        record = await get_fixture_by_id(session, fixture_id)
        if record is None:
            return None
        fixture = record.fixture
        league = ctx.leagues.canonical(fixture.league.ref)
        end = datetime.combine(fixture.kickoff.date(), time(0), tzinfo=UTC)
        history = await finished_fixtures(
            session,
            leagues=ctx.leagues.group(league),
            start=end - timedelta(days=ctx.config.history_days),
            end=end,
            preferred_sources=ctx.config.history_sources,
        )
        snapshots = (await load_decision_snapshots(session, [fixture.ref])).get(fixture.ref, [])
        moves = await _moves(session, ctx, record)
    fitted = await ctx.models.get(league, fixture.kickoff.date())
    quotes = current_quotes(snapshots, fixture.ref, fixture.kickoff)
    overlay = await load_overlay(ctx.learning)
    valuation = value_fixture(
        ValuationInput(
            fixture=fixture,
            quotes=quotes,
            fitted=fitted,
            now=now,
            markets=frozenset(q.market.type for q in quotes),
            allow_model_only=True,
            calibration=overlay.calibration,
        ),
        ctx.config,
    )
    rows = tuple(
        QuoteRow(c.market, c.selection, c.probability, c.odds, c.ev) for c in valuation.candidates
    )
    return FixtureCard(
        record=record,
        home_form=_form(fixture.home, history),
        away_form=_form(fixture.away, history),
        home_stats=_venue(fixture.home, history, home=True),
        away_stats=_venue(fixture.away, history, home=False),
        head_to_head=_meetings(record, history),
        top_scores=_scores(fitted.model, fixture.home, fixture.away),
        quotes=rows,
        odds_moves=moves,
        data_as_of=max((q.taken_at for q in quotes), default=None),
    )


def _scores(
    model: DixonColesModel | None, home: Team, away: Team
) -> tuple[tuple[int, int, float], ...]:
    if model is None:
        return ()
    try:
        matrix = model.score_matrix(team_key(home), team_key(away))
    except UnknownTeamError:
        return ()
    return tuple(
        (s.home_goals, s.away_goals, s.probability) for s in top_scores(matrix, TOP_SCORE_COUNT)
    )


async def _moves(
    session: AsyncSession, ctx: PipelineContext, record: FixtureRecord
) -> tuple[OddsShift, ...]:
    history = await odds_history(session, record.fixture.ref, bookmakers=[ctx.config.bet_bookmaker])
    first: dict[tuple[Market, Selection], float] = {}
    last: dict[tuple[Market, Selection], float] = {}
    for snapshot in history:
        key = (snapshot.market, snapshot.selection)
        if snapshot.kind in _OPENING_KINDS:
            first.setdefault(key, snapshot.odds)
        last[key] = snapshot.odds
    return tuple(
        OddsShift(market, selection, first[(market, selection)], current)
        for (market, selection), current in last.items()
        if (market, selection) in first and first[(market, selection)] != current
    )
