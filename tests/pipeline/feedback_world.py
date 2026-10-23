"""A tiny finished world for the feedback-loop tests: fixtures with scores, signals, results."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.db.enums import (
    Confidence,
    LlmVerdict,
    Outcome,
    ResultSource,
    SignalSource,
    SignalVersionReason,
)
from app.db.repo import (
    SignalDraft,
    SignalRecord,
    SignalResultRecord,
    Thresholds,
    ensure_user,
    get_signal,
    settle_signal,
    upsert_signal,
)
from app.domain import (
    ExternalRef,
    Fixture,
    FixtureStatus,
    HistoryBatch,
    Market,
    ResultStatus,
    Selection,
    Sport,
)
from app.pipeline.context import PipelineContext
from app.pipeline.review import PostmortemAnalyst
from tests.integration.t4_support import SOURCE, Bare, fixture_id, make_fixture, open_bare, utc
from tests.pipeline.world import START, league_of, team_of

USER = 8001
OTHER = 8002
KICKOFF = utc(2026, 10, 20, 15)
NOW = utc(2026, 10, 21, 6)
SETTLED = utc(2026, 10, 20, 18)


def finished_fixture(name: str, kickoff: datetime = KICKOFF, league: str = "E0") -> Fixture:
    """A finished football-data fixture (the score goes into the batch)."""
    return make_fixture(name, kickoff, status=FixtureStatus.FINISHED, league=league)


def batch_of(*scored: tuple[Fixture, int, int], upcoming: tuple[Fixture, ...] = ()) -> HistoryBatch:
    """A history batch of finished fixtures with their scores, plus scheduled ones."""
    from tests.integration.t4_support import finished  # noqa: PLC0415

    return HistoryBatch(
        fixtures=(*(f for f, _, _ in scored), *upcoming),
        results=tuple(finished(f, h, a, ResultStatus.FINISHED) for f, h, a in scored),
    )


def versus(
    home: str, away: str, kickoff: datetime = KICKOFF, *, league: str = "E0", finished: bool = True
) -> Fixture:
    """A fixture between two named teams (the same team can appear in many fixtures)."""
    return Fixture(
        ref=ExternalRef(SOURCE, f"{league}/2026/{home}/{away}"),
        sport=Sport.FOOTBALL,
        league=league_of(league),
        season=2026,
        kickoff=kickoff,
        home=team_of(home),
        away=team_of(away),
        status=FixtureStatus.FINISHED if finished else FixtureStatus.SCHEDULED,
    )


@asynccontextmanager
async def open_world(
    path: Path,
    *scored: tuple[Fixture, int, int],
    analyst: PostmortemAnalyst | None = None,
    upcoming: tuple[Fixture, ...] = (),
) -> AsyncIterator[Bare]:
    """A bare database holding ``scored`` (and ``upcoming``); ``analyst`` is the post-mortem LLM."""
    async with open_bare(path, batch_of(*scored, upcoming=upcoming), now=NOW) as bare:
        if analyst is not None:
            bare.ctx = replace(bare.ctx, postmortem=analyst)
        yield bare


async def place(  # noqa: PLR0913 - the columns of one signal
    bare: Bare,
    fixture: Fixture,
    *,
    user_id: int = USER,
    market: Market | None = None,
    selection: Selection = Selection.HOME,
    odds: float = 2.0,
    verdict: LlmVerdict | None = None,
    note: str | None = None,
    recommended: bool = True,
    source: SignalSource = SignalSource.ANALYSIS,
) -> SignalRecord:
    """Store a signal with exact numbers (p 0.55, EV 0.1)."""
    db_id = await fixture_id(bare.sessions, fixture)
    async with bare.sessions.begin() as session:
        await ensure_user(session, user_id, timezone="Asia/Tashkent")
        return await upsert_signal(
            session,
            SignalDraft(
                user_id=user_id,
                fixture_id=db_id,
                market=market or Market.match_result(),
                selection=selection,
                odds=odds,
                p_model=0.55,
                ev=0.1,
                confidence=Confidence.MEDIUM,
                stake_units=Decimal(1),
                source=source,
                thresholds=Thresholds(0.3, 0.0, 1.01, 100.0),
                recommended_at=NOW if recommended else None,
                llm_verdict=verdict,
                llm_note=note,
            ),
            reason=SignalVersionReason.INITIAL,
        )


async def settle(  # noqa: PLR0913 - the columns of one result
    bare: Bare,
    signal: SignalRecord,
    outcome: Outcome = Outcome.LOSS,
    *,
    at: datetime = SETTLED,
    source: ResultSource = ResultSource.API,
    clv: float | None = None,
) -> SignalRecord:
    """Give ``signal`` its result and return it as stored."""
    profit = {Outcome.LOSS: Decimal(-1), Outcome.WIN: Decimal(1)}.get(outcome, Decimal(0))
    async with bare.sessions.begin() as session:
        await settle_signal(
            session,
            SignalResultRecord(
                signal_id=signal.id,
                outcome=outcome,
                source=source,
                profit_units=profit,
                closing_odds=None,
                clv=clv,
                user_note=None,
                settled_at=at,
            ),
        )
    async with bare.sessions() as session:
        stored = await get_signal(session, signal.id)
    assert stored is not None
    return stored


def context_of(bare: Bare) -> PipelineContext:
    """The bare environment's context (kept as a function so tests read naturally)."""
    return bare.ctx


__all__ = [
    "KICKOFF",
    "NOW",
    "OTHER",
    "SETTLED",
    "START",
    "USER",
    "batch_of",
    "context_of",
    "finished_fixture",
    "open_world",
    "place",
    "settle",
    "versus",
]
