"""Settlement (F4.6): results -> outcomes, profit, closing-line value, user bets.

For every recommended, non-retro signal whose match should be over and which has no result:

1. fetch the results of the fixtures not yet stored as finished (automatic budget; a failure
   skips the fetch and settles what is already known);
2. when the fixture is finished with a score, compute the outcome with ``settle_selection``
   (Asian half outcomes included), the profit in units and the CLV against the de-vigged
   closing price, and store them (``settle_signal``: a manual mark is never overwritten);
3. settle the user's bets on it with the bet's own odds and stake;
4. tell the user once (ledger key ``(settled, local day, user, signal)``).

A fixture reported finished without a score cannot exist (the storage merge rules), so
"finished without result" shows up as a fixture that is still not finished: the signal waits
for the next run. A cancelled or abandoned match voids its signals.

CLV: ``ln(odds taken / fair closing odds)``. The closing price is the stored ``closing``
snapshot when the provider supplied one, otherwise the last pre-match capture (the nearest
thing to the close we fetch); Asian lines without a reference distribution and fixtures
without a Pinnacle quote have no CLV.
"""

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from app.db.enums import Outcome, ResultSource, SignalStatus
from app.db.repo import (
    SignalRecord,
    SignalResultRecord,
    bets_for_signal,
    latest_odds,
    settle_bet,
    settle_signal,
    signals_for_fixtures,
    unsettled_signals,
)
from app.domain import (
    BOOKMAKER_PINNACLE,
    ExternalRef,
    FixtureStatus,
    Market,
    OddsKind,
    OddsSnapshot,
    Selection,
)
from app.logging import get_logger
from app.models import (
    ModelInputError,
    Settlement,
    asian_fair_odds,
    complete_quotes,
    devig_quote,
    settle_selection,
)
from app.models.asian import settlement_returns
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import Delivery, SettledDelivery, WatchDelivery
from app.pipeline.errors import PipelineError
from app.pipeline.ingest import ingest_results, provider_of
from app.pipeline.ledger import LedgerKey
from app.pipeline.prematch import watch_flags_for
from app.pipeline.profile import load_profile
from app.pipeline.provider import AUTOMATIC
from app.pipeline.types import SignalCard

logger = get_logger(__name__)

JOB: Final = "settled"
WATCH_RESULT: Final = "result"
_OUTCOMES: Final = {
    Settlement.WIN: Outcome.WIN,
    Settlement.HALF_WIN: Outcome.HALF_WIN,
    Settlement.PUSH: Outcome.VOID,
    Settlement.HALF_LOSS: Outcome.HALF_LOSS,
    Settlement.LOSS: Outcome.LOSS,
}
_CLOSING_KINDS: Final = (OddsKind.CLOSING, OddsKind.PREMATCH)
_VOIDING: Final = frozenset({FixtureStatus.CANCELLED, FixtureStatus.ABANDONED})


@dataclass(frozen=True, slots=True)
class SettleReport:
    """Counts of one settlement run."""

    settled: int = 0
    waiting: int = 0
    notified: int = 0
    fetch_failed: bool = False


def settlement_of(record: SignalRecord) -> Settlement | None:
    """The settlement of ``record`` from its fixture's final score, ``None`` while unknown."""
    result = record.fixture.result
    if result is None or result.home_score is None or result.away_score is None:
        return None
    return settle_selection(record.market, record.selection, result.home_score, result.away_score)


def profit_of(stake: Decimal, odds: float, settlement: Settlement) -> Decimal:
    """Profit of ``stake`` at ``odds`` for ``settlement`` (negative on a loss)."""
    return stake * Decimal(repr(settlement_returns(odds)[settlement]))


def clv_of(
    snapshots: list[OddsSnapshot],
    fixture: ExternalRef,
    market: Market,
    selection: Selection,
    odds: float,
) -> tuple[float | None, float | None]:
    """``(closing odds, CLV)`` of a signal from the closing snapshots of its fixture."""
    quotes = [q for q in complete_quotes(snapshots) if q.market == market]
    own = [q for q in quotes if q.fixture == fixture]
    sharp = next((q for q in own if q.bookmaker == BOOKMAKER_PINNACLE), None)
    closing = None if not own else own[0].prices[selection]
    if sharp is None:
        return closing, None
    try:
        fair = asian_fair_odds(devig_quote(sharp).outcomes[selection])
    except ModelInputError:  # e.g. an Asian line without a reference: no CLV
        return closing, None
    return closing, math.log(odds / fair)


async def run_settlement(ctx: PipelineContext, delivery: Delivery) -> SettleReport:
    """Settle everything that can be settled now (see the module docstring)."""
    now = ctx.clock()
    async with ctx.sessions() as session:
        pending = await unsettled_signals(
            session, kicked_off_before=now - ctx.config.match_duration
        )
    fetch_failed = await _fetch(ctx, pending)
    async with ctx.sessions() as session:
        fresh = await signals_for_fixtures(
            session, {r.fixture.id for r in pending}, statuses=tuple(SignalStatus)
        )
    ids = {record.id for record in pending}
    settled = waiting = notified = 0
    for record in (r for r in fresh if r.id in ids and r.result is None):
        stored = await _settle_one(ctx, record)
        if stored is None:
            waiting += 1
            continue
        settled += 1
        notified += await _announce(ctx, delivery, record, stored)
    await ctx.ledger.record_run(
        JOB, ok=not fetch_failed, detail=f"settled={settled} waiting={waiting}", now=ctx.clock()
    )
    return SettleReport(settled, waiting, notified, fetch_failed)


async def _fetch(ctx: PipelineContext, pending: list[SignalRecord]) -> bool:
    """Fetch results of the pending fixtures not stored as finished; True on failure."""
    sport = next((r.fixture.fixture.sport for r in pending), None)
    if sport is None or sport not in ctx.providers:
        return False
    source = provider_of(ctx, sport).source
    refs = sorted(
        {
            r.fixture.fixture.ref
            for r in pending
            if r.fixture.fixture.status is not FixtureStatus.FINISHED
            and r.fixture.fixture.ref.source == source
        },
        key=str,
    )
    try:
        await ingest_results(ctx, sport, refs, purpose=AUTOMATIC)
    except PipelineError as error:
        logger.warning("settle_fetch_failed", error=str(error))
        return True
    return False


async def _settle_one(ctx: PipelineContext, record: SignalRecord) -> SignalResultRecord | None:
    fixture = record.fixture.fixture
    now = ctx.clock()
    settlement = settlement_of(record)
    if settlement is None:
        if fixture.status not in _VOIDING:
            return None
        return await _store(
            ctx,
            record,
            SignalResultRecord(
                signal_id=record.id,
                outcome=Outcome.VOID,
                source=ResultSource.API,
                profit_units=Decimal(0),
                closing_odds=None,
                clv=None,
                user_note=None,
                settled_at=now,
            ),
        )
    async with ctx.sessions() as session:
        prices = await latest_odds(session, [fixture.ref], kinds=_CLOSING_KINDS)
    closing, clv = clv_of(
        prices.get(fixture.ref, []), fixture.ref, record.market, record.selection, record.odds
    )
    result = SignalResultRecord(
        signal_id=record.id,
        outcome=_OUTCOMES[settlement],
        source=ResultSource.API,
        profit_units=profit_of(record.stake_units, record.odds, settlement),
        closing_odds=closing,
        clv=clv,
        user_note=None,
        settled_at=now,
    )
    return await _store(ctx, record, result, settlement)


async def _store(
    ctx: PipelineContext,
    record: SignalRecord,
    result: SignalResultRecord,
    settlement: Settlement | None = None,
) -> SignalResultRecord:
    """Persist the result and settle the user's bets on the signal in one transaction."""
    async with ctx.sessions.begin() as session:
        stored = await settle_signal(session, result)
        for bet in await bets_for_signal(session, record.id):
            profit = _bet_profit(bet.stake, bet.odds_taken, stored, record, settlement)
            await settle_bet(
                session, bet.id, outcome=stored.outcome, profit=profit, settled_at=result.settled_at
            )
    return stored


def _bet_profit(
    stake: Decimal,
    odds: float,
    stored: SignalResultRecord,
    record: SignalRecord,
    settlement: Settlement | None,
) -> Decimal:
    if stored.outcome is Outcome.CASHOUT:
        units = record.stake_units or Decimal(1)
        return stake * stored.profit_units / units
    if stored.outcome is Outcome.VOID:
        return Decimal(0)
    if settlement is not None and stored.source is ResultSource.API:
        return profit_of(stake, odds, settlement)
    return profit_of(stake, odds, _from_outcome(stored.outcome))


def _from_outcome(outcome: Outcome) -> Settlement:
    return {value: key for key, value in _OUTCOMES.items()}.get(outcome, Settlement.PUSH)


async def _announce(
    ctx: PipelineContext, delivery: Delivery, record: SignalRecord, stored: SignalResultRecord
) -> int:
    """Send the settlement message once; returns 1 when it went out."""
    now: datetime = ctx.clock()
    async with ctx.sessions() as session:
        profile = await load_profile(session, record.user_id)
    day = profile.schedule.local_day(now)
    key = LedgerKey(JOB, day, record.user_id, str(record.id))
    if profile.schedule.is_quiet(now) or not await ctx.ledger.claim(key, now=now):
        return 0
    card = SignalCard(record=record, bookmaker=ctx.config.bet_bookmaker, no_value=False)
    try:
        await delivery.settled(
            record.user_id, SettledDelivery(card=card, profit_units=stored.profit_units), key
        )
        flags = await watch_flags_for(ctx, profile, record.fixture)
        if WATCH_RESULT in flags:
            watch_key = LedgerKey("watch.result", day, record.user_id, str(record.fixture.id))
            if await ctx.ledger.claim(watch_key, now=now):
                await delivery.watch_event(
                    record.user_id,
                    WatchDelivery(event=WATCH_RESULT, fixture=record.fixture, card=card),
                    watch_key,
                )
                await ctx.ledger.complete(watch_key, now=now)
    except Exception:
        await ctx.ledger.fail(key, now=ctx.clock())
        raise
    await ctx.ledger.complete(key, now=ctx.clock())
    return 1
