"""Settlement (F4.6): results -> outcomes, profit, closing-line value, user bets.

For every recommended, non-retro signal whose match should be over and which has no result:

1. fetch the results of the fixtures not yet stored as finished (automatic budget; a failure
   skips the fetch and settles what is already known);
2. when the fixture is finished with a score, compute the outcome with ``settle_selection``
   (Asian half outcomes included), the profit in units and the CLV against the de-vigged
   closing price, and store them (``settle_signal``: a manual mark is never overwritten);
   a cancelled or abandoned match voids the signal whatever score it had;
3. settle the user's bets on it with the bet's own odds and stake (bets placed after the
   signal was settled are closed by the same run);
4. tell the user once (ledger key ``(settled, local day, user, signal)``). A message that
   could not be delivered never blocks the other settlements: it is retried with a backoff by
   later runs, and given up on (with an administrator alert) after a few attempts.

A fixture reported finished without a score cannot exist (the storage merge rules), so
"finished without result" shows up as a fixture that is still not finished: the signal waits
for the next run. A postponed match also waits.

Signal profit and CLV use the *first recommended* odds (``recommended_odds``, decision #17):
recalculations move the current odds towards the close, which would otherwise pull every CLV
towards zero. A user's own bet settles on the odds the user actually took.

CLV: ``ln(odds / fair closing odds)``. The closing price is the stored ``closing`` snapshot
when the provider supplied one, otherwise the last pre-kick-off capture (the nearest thing to
the close we fetch); quotes taken at or after kick-off never count. Asian lines without a
reference distribution and fixtures without a Pinnacle quote have no CLV. The stored closing
odds are those of the bookmaker the pick was priced at.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Final

from app.db.enums import Outcome, ResultSource, SignalStatus
from app.db.repo import (
    SignalRecord,
    SignalResultRecord,
    UserBetRecord,
    bets_for_signal,
    get_signal,
    latest_odds,
    open_bets_of_settled_signals,
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
    MarketQuote,
    ModelInputError,
    Settlement,
    asian_fair_odds,
    complete_quotes,
    devig_quote,
    settle_selection,
)
from app.models.asian import settlement_returns
from app.pipeline.alerts import alert_throttled
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import Delivery, SettledDelivery, WatchDelivery
from app.pipeline.errors import DeliveryNotConfirmedError, PipelineError
from app.pipeline.ingest import ingest_results, provider_of
from app.pipeline.ledger import LedgerKey
from app.pipeline.outbox import deliver_watch_event
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
_REFERENCE_ODDS: Final = 2.0
"""At these odds a win pays exactly one unit, so the returns read as payout shares."""


@dataclass(frozen=True, slots=True)
class BetPayout:
    """What the bookmaker paid out for one of the user's bets (a money cash-out)."""

    bet_id: int
    stake: Decimal
    amount: Decimal

    @property
    def profit(self) -> Decimal:
        """Money won (or lost) by cashing out: payout minus stake."""
        return self.amount - self.stake


@dataclass(frozen=True, slots=True)
class SettleReport:
    """Counts of one settlement run."""

    settled: int = 0
    waiting: int = 0
    notified: int = 0
    fetch_failed: bool = False
    bets_closed: int = 0
    announce_failed: int = 0


def settlement_of(record: SignalRecord) -> Settlement | None:
    """The settlement of ``record``: ``None`` while it cannot be known.

    A cancelled or abandoned match is a push whatever score it carries (bookmakers return
    such stakes); a postponed or unfinished one is unknown; otherwise the final score decides.
    """
    fixture = record.fixture
    if fixture.fixture.status in _VOIDING:
        return Settlement.PUSH
    result = fixture.result
    if result is None or result.home_score is None or result.away_score is None:
        return None
    return settle_selection(record.market, record.selection, result.home_score, result.away_score)


def outcome_of(record: SignalRecord) -> Outcome | None:
    """The outcome the final score implies for ``record`` (what a retro look-back shows)."""
    settlement = settlement_of(record)
    return None if settlement is None else _OUTCOMES[settlement]


def profit_of(stake: Decimal, odds: float, settlement: Settlement) -> Decimal:
    """Profit of ``stake`` at ``odds`` for ``settlement`` (negative on a loss).

    Money is built from ``Decimal(str(odds))`` (the odds as written, e.g. ``1.01``), never from
    the repr of a float computation, so no binary noise enters the arithmetic.
    """
    share = Decimal(str(settlement_returns(_REFERENCE_ODDS)[settlement]))
    if share > 0:
        return stake * (Decimal(str(odds)) - 1) * share
    return stake * share


def clv_of(  # noqa: PLR0913 - the kick-off and the bookmaker only refine the same lookup
    snapshots: list[OddsSnapshot],
    fixture: ExternalRef,
    market: Market,
    selection: Selection,
    odds: float,
    *,
    kickoff: datetime | None = None,
    bookmaker: str = BOOKMAKER_PINNACLE,
) -> tuple[float | None, float | None]:
    """``(closing odds, CLV)`` of a signal from the closing snapshots of its fixture.

    Snapshots taken at or after ``kickoff`` are ignored. The closing odds are ``bookmaker``'s
    own closing price (Pinnacle's by default; ``None`` when it has none); CLV compares
    ``odds`` with Pinnacle's de-vigged closing price. Per bookmaker the ``closing`` quote
    beats the newest pre-match one.
    """
    kickoffs = None if kickoff is None else {fixture: kickoff}
    quotes = [
        q
        for q in complete_quotes(snapshots, kickoffs)
        if q.market == market and q.fixture == fixture
    ]
    closing_quote = _closing_quote(quotes, bookmaker)
    closing = None if closing_quote is None else closing_quote.prices[selection]
    sharp = _closing_quote(quotes, BOOKMAKER_PINNACLE)
    if sharp is None:
        return closing, None
    try:
        fair = asian_fair_odds(devig_quote(sharp).outcomes[selection])
    except ModelInputError:  # e.g. an Asian line without a reference: no CLV
        return closing, None
    return closing, math.log(odds / fair)


def _closing_quote(quotes: list[MarketQuote], bookmaker: str) -> MarketQuote | None:
    """``bookmaker``'s quote nearest the close: a ``closing`` one, else the newest."""
    own = [q for q in quotes if q.bookmaker == bookmaker]
    if not own:
        return None
    return max(own, key=lambda q: (q.kind is OddsKind.CLOSING, q.taken_at))


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
    settled = waiting = notified = failed = 0
    for record in (r for r in fresh if r.id in ids and r.result is None):
        stored = await _settle_one(ctx, record)
        if stored is None:
            waiting += 1
            continue
        settled += 1
        sent = await _announce(ctx, delivery, record, stored)
        notified += sent == 1
        failed += sent == -1
    bets_closed = await _close_orphan_bets(ctx)
    retried = await _retry_announcements(ctx, delivery)
    await ctx.ledger.purge_expired(now=ctx.clock())
    await ctx.ledger.record_run(
        JOB, ok=not fetch_failed, detail=f"settled={settled} waiting={waiting}", now=ctx.clock()
    )
    return SettleReport(
        settled, waiting, notified + retried, fetch_failed, bets_closed, announce_failed=failed
    )


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
        return None
    if fixture.status in _VOIDING:
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
            settlement,
        )
    async with ctx.sessions() as session:
        prices = await latest_odds(
            session, [fixture.ref], kinds=_CLOSING_KINDS, taken_before=fixture.kickoff
        )
    closing, clv = clv_of(
        prices.get(fixture.ref, []),
        fixture.ref,
        record.market,
        record.selection,
        record.first_odds,
        kickoff=fixture.kickoff,
        bookmaker=ctx.config.bet_bookmaker,
    )
    result = SignalResultRecord(
        signal_id=record.id,
        outcome=_OUTCOMES[settlement],
        source=ResultSource.API,
        profit_units=profit_of(record.stake_units, record.first_odds, settlement),
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
    payout: BetPayout | None = None,
) -> SignalResultRecord:
    """Persist the result and settle the user's bets on the signal in one transaction."""
    async with ctx.sessions.begin() as session:
        stored = await settle_signal(session, result)
        for bet in await bets_for_signal(session, record.id):
            if payout is not None and bet.id == payout.bet_id:
                profit = payout.profit  # exact money, not a rescaled ratio
            else:
                profit = _bet_profit(bet.stake, bet.odds_taken, stored, record, settlement)
            await settle_bet(
                session,
                bet.id,
                user_id=record.user_id,
                outcome=stored.outcome,
                profit=profit,
                settled_at=result.settled_at,
            )
    return stored


async def _close_orphan_bets(ctx: PipelineContext) -> int:
    """Close open bets whose signal is already settled (placed after the settlement)."""
    async with ctx.sessions() as session:
        open_bets = await open_bets_of_settled_signals(session)
    by_signal: defaultdict[int, list[UserBetRecord]] = defaultdict(list)
    for bet in open_bets:
        if bet.signal_id is not None:
            by_signal[bet.signal_id].append(bet)
    closed = 0
    for signal_id, bets in by_signal.items():
        async with ctx.sessions() as session:
            record = await get_signal(session, signal_id)
        if record is None or record.result is None:  # pragma: no cover - the query guarantees both
            continue
        stored = record.result
        async with ctx.sessions.begin() as session:
            for bet in bets:
                profit = _bet_profit(bet.stake, bet.odds_taken, stored, record, None)
                await settle_bet(
                    session,
                    bet.id,
                    user_id=bet.user_id,
                    outcome=stored.outcome,
                    profit=profit,
                    settled_at=ctx.clock(),
                )
                closed += 1
    if closed:
        logger.info("orphan_bets_closed", count=closed)
    return closed


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
    """Send the settlement message once: 1 sent, 0 skipped, -1 failed (it will be retried)."""
    now: datetime = ctx.clock()
    async with ctx.sessions() as session:
        profile = await load_profile(session, record.user_id)
    day = profile.schedule.local_day(now)
    key = LedgerKey(JOB, day, record.user_id, str(record.id))
    if profile.schedule.is_quiet(now) or not await ctx.ledger.claim(key, now=now):
        return 0
    return await _deliver(ctx, delivery, key, replace(record, result=stored))


async def _deliver(
    ctx: PipelineContext, delivery: Delivery, key: LedgerKey, record: SignalRecord
) -> int:
    """Deliver an already claimed settlement message; a failure is recorded, never raised."""
    stored = record.result
    if stored is None:  # pragma: no cover - callers pass settled signals only
        await ctx.ledger.complete(key, now=ctx.clock())
        return 0
    card = SignalCard(record=record, bookmaker=ctx.config.bet_bookmaker, no_value=False)
    try:
        reference = await delivery.settled(
            record.user_id, SettledDelivery(card=card, profit_units=stored.profit_units), key
        )
        if reference is None:
            raise DeliveryNotConfirmedError(f"settlement of signal {record.id}")
    except Exception as error:  # one undeliverable message must not stop the other settlements
        failure = await ctx.ledger.fail(key, now=ctx.clock(), detail=str(error)[:200])
        logger.warning(
            "settle_message_failed",
            signal_id=record.id,
            attempts=failure.attempts,
            error=repr(error),
        )
        await ctx.ledger.record_error(JOB, f"signal {record.id}: {error}", now=ctx.clock())
        if failure.exhausted:
            await alert_throttled(
                ctx,
                delivery,
                JOB,
                f"result message of signal {record.id} gave up: {error}",
                throttle_key=f"{JOB}.gave_up",
            )
        return -1
    await ctx.ledger.complete(key, now=ctx.clock())
    await _announce_watch(ctx, delivery, record, card, key)
    return 1


async def _announce_watch(
    ctx: PipelineContext, delivery: Delivery, record: SignalRecord, card: SignalCard, key: LedgerKey
) -> None:
    """The watchlist ``result`` event of the fixture, once per user and day."""
    async with ctx.sessions() as session:
        profile = await load_profile(session, record.user_id)
    flags = await watch_flags_for(ctx, profile, record.fixture)
    if WATCH_RESULT not in flags:
        return
    watch_key = LedgerKey("watch.result", key.day, record.user_id, str(record.fixture.id))
    message = WatchDelivery(event=WATCH_RESULT, fixture=record.fixture, card=card)
    await deliver_watch_event(ctx, delivery, record.user_id, message, watch_key)


async def _retry_announcements(ctx: PipelineContext, delivery: Delivery) -> int:
    """Re-send settlement messages whose earlier delivery failed (backoff over)."""
    now = ctx.clock()
    sent = 0
    for key in await ctx.ledger.due_retries(JOB, now=now):
        async with ctx.sessions() as session:
            record = await get_signal(session, int(key.subject), user_id=key.user_id)
            profile = await load_profile(session, key.user_id)
        if record is None or record.result is None:
            await ctx.ledger.complete(key, now=now, detail="signal gone")
            continue
        if profile.schedule.is_quiet(now) or not await ctx.ledger.claim(key, now=now):
            continue
        sent += await _deliver(ctx, delivery, key, record) == 1
    return sent


async def settle_manually(
    ctx: PipelineContext,
    record: SignalRecord,
    outcome: Outcome,
    *,
    cashout_units: Decimal | None = None,
    payout: BetPayout | None = None,
) -> SignalResultRecord:
    """The user's own result (win, loss, void or cash-out) for ``record`` and its bets.

    A manual result replaces an automatic one and is never overwritten by a later automatic
    settlement (``settle_signal``). Bets follow: a cash-out in units scales to each bet's
    stake; a money ``payout`` closes that bet at exactly ``payout - stake`` and gives the
    signal the same return on its recommended stake (other bets scale by that ratio); void
    returns the stake.
    """
    stake = record.stake_units
    odds = record.first_odds
    cashout = cashout_units or Decimal(0)
    if payout is not None:
        cashout = stake * payout.profit / payout.stake
    profit = {
        Outcome.WIN: profit_of(stake, odds, Settlement.WIN),
        Outcome.LOSS: profit_of(stake, odds, Settlement.LOSS),
        Outcome.VOID: Decimal(0),
        Outcome.CASHOUT: cashout,
    }[outcome]
    result = SignalResultRecord(
        signal_id=record.id,
        outcome=outcome,
        source=ResultSource.MANUAL,
        profit_units=profit,
        closing_odds=None,
        clv=None,
        user_note=None,
        settled_at=ctx.clock(),
    )
    return await _store(ctx, record, result, payout=payout)
