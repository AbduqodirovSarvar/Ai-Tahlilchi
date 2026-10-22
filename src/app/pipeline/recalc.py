"""Recalculation of stored signals with fresh data (F4.4).

The engine behind ``/qayta``, the 24-hour update and the pre-match update.

Per fixture (signals of one fixture share the fetch and the model): refresh the fixture's
status and prices from the provider when the budget allows, price the signals again with the
same chain as a new analysis, store a new ``signal_versions`` entry and judge the new numbers
against the thresholds the signal was *created* with (not today's settings). A postponed,
cancelled or abandoned match cancels its signals. When the budget is spent nothing is fetched:
the stored numbers are re-evaluated and ``data_as_of`` says how old they are.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo

from app.db.enums import Confidence, SignalStatus, SignalVersionReason
from app.db.repo import (
    FixtureRecord,
    SignalDraft,
    SignalRecord,
    Thresholds,
    cancel_recalcs,
    get_fixture_by_id,
    set_signal_status,
    upsert_signal,
)
from app.domain import FixtureStatus, OddsKind, Sport
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineError
from app.pipeline.ingest import ingest_odds, ingest_results, provider_of
from app.pipeline.odds import current_quotes, data_as_of, load_decision_snapshots
from app.pipeline.persist import refresh_quote_time, unchanged
from app.pipeline.profile import UserProfile, load_profile
from app.pipeline.provider import Purpose
from app.pipeline.selection import passes
from app.pipeline.types import AnalysisRequest, SignalCard
from app.pipeline.valuation import Candidate, ValuationInput, stake_units, value_fixture

logger = get_logger(__name__)

_NOT_PLAYABLE: Final = {
    FixtureStatus.POSTPONED: "postponed",
    FixtureStatus.CANCELLED: "cancelled",
    FixtureStatus.ABANDONED: "abandoned",
}
FALLBACK_THRESHOLDS: Final = Thresholds(0.0, 0.0, 1.0001, 1000.0)
"""For signals stored before thresholds were recorded: anything with a positive EV passes."""
FETCH_REQUESTS: Final = 2
"""Provider requests one fixture's refresh spends: its result/status and its odds."""


class RecalcVerdict(StrEnum):
    """What a recalculation means for a signal (values equal the contract's)."""

    STILL_VALID = "still_valid"
    BELOW_THRESHOLD = "below_threshold"
    CANCELLED = "cancelled"
    NO_PRICE = "no_price"
    """The user's bookmaker has no fresh price any more: the stored numbers are unconfirmed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RecalcResult:
    """Before/after of one signal. ``stale``: no fresh data could be fetched."""

    before: SignalCard
    after: SignalCard
    verdict: RecalcVerdict
    recalculated_at: datetime
    stale: bool = False
    cancel_reason: str | None = None
    lineups_confirmed: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class RecalcOptions:
    """How a recalculation behaves."""

    reason: SignalVersionReason
    purpose: Purpose
    odds_kind: OddsKind
    fetch: bool = True
    check_lineups: bool = False


async def recalc_signals(
    ctx: PipelineContext, signals: Sequence[SignalRecord], options: RecalcOptions
) -> tuple[list[RecalcResult], list[int]]:
    """Recalculate ``signals``; returns the results and the ids that failed.

    One failing fixture is logged and its signals reported as failed; the rest go on.
    """
    by_fixture: defaultdict[int, list[SignalRecord]] = defaultdict(list)
    for record in signals:
        by_fixture[record.fixture.id].append(record)
    results: list[RecalcResult] = []
    failed: list[int] = []
    for group in by_fixture.values():
        try:
            results.extend(await _recalc_fixture(ctx, group, options))
        except Exception as error:  # one broken fixture must not stop the batch
            logger.warning("recalc_failed", fixture=group[0].fixture.id, error=repr(error))
            failed.extend(record.id for record in group)
    return results, failed


async def _refresh(
    ctx: PipelineContext, fixture: FixtureRecord, options: RecalcOptions
) -> tuple[bool, bool]:
    """Fetch the fixture's status and prices; returns ``(stale, lineups_confirmed)``."""
    provider = ctx.providers.get(fixture.fixture.sport)
    ref = fixture.fixture.ref
    if not options.fetch or provider is None or ref.source != provider.source:
        return True, False
    quota = await provider.quota()
    if quota.available(options.purpose) < FETCH_REQUESTS:
        return True, False
    try:
        await ingest_results(ctx, fixture.fixture.sport, [ref], purpose=options.purpose)
        await ingest_odds(
            ctx, fixture.fixture.sport, [ref], kind=options.odds_kind, purpose=options.purpose
        )
        lineups = options.check_lineups and await provider_of(
            ctx, fixture.fixture.sport
        ).lineups_confirmed(ref, purpose=options.purpose)
    except PipelineError as error:
        logger.warning("recalc_fetch_failed", fixture=str(ref), error=str(error))
        return True, False
    return False, lineups


async def _recalc_fixture(
    ctx: PipelineContext, group: Sequence[SignalRecord], options: RecalcOptions
) -> list[RecalcResult]:
    now = ctx.clock()
    first = group[0].fixture
    stale, lineups = await _refresh(ctx, first, options)
    async with ctx.sessions() as session:
        fixture = await get_fixture_by_id(session, first.id)
        if fixture is None:  # pragma: no cover - signals reference an existing fixture
            msg = f"fixture {first.id} disappeared"
            raise PipelineError(msg)
        snapshots = await load_decision_snapshots(session, [fixture.fixture.ref])
    reason = _NOT_PLAYABLE.get(fixture.fixture.status)
    if reason is not None:
        return await _cancel(ctx, group, reason, now)
    quotes = current_quotes(
        snapshots.get(fixture.fixture.ref, []), fixture.fixture.ref, fixture.fixture.kickoff
    )
    league = ctx.leagues.canonical(fixture.fixture.league.ref)
    fitted = await ctx.models.get(league, now.astimezone(UTC).date() + timedelta(days=1))
    overlay = await ctx.learning.load()  # calibration only: an existing signal is never filtered
    valuation = value_fixture(
        ValuationInput(
            fixture=fixture.fixture,
            quotes=quotes,
            fitted=fitted,
            now=now,
            markets=frozenset(record.market.type for record in group),
            lineups_known=lineups,
            allow_model_only=True,
            calibration=overlay.calibration,
        ),
        ctx.config,
    )
    as_of = data_as_of(quotes, ctx.config.bet_bookmaker) if stale else None
    by_key = {(c.market, c.selection): c for c in valuation.candidates}
    results = []
    async with ctx.sessions.begin() as session:
        profiles = {user: await load_profile(session, user) for user in {r.user_id for r in group}}
        for record in group:
            candidate = by_key.get((record.market, record.selection))
            draft = _draft(ctx, record, candidate, profiles[record.user_id])
            # A rerun that finds the same numbers (a retry after a failed message) must not
            # pile up identical versions.
            after = (
                await refresh_quote_time(session, record, draft)
                if unchanged(record, draft)
                else await upsert_signal(session, draft, reason=options.reason)
            )
            results.append(
                _result(
                    ctx,
                    record,
                    after,
                    candidate=candidate,
                    now=now,
                    freshness=(stale, as_of, lineups),
                )
            )
    return results


def _draft(
    ctx: PipelineContext, record: SignalRecord, candidate: Candidate | None, profile: UserProfile
) -> SignalDraft:
    """The signal's new numbers; unchanged ones when it can no longer be priced.

    The stake is advised afresh from the new numbers with the same staking rule as a new
    signal; a non-positive EV keeps the stake already advised (a signal that was recommended
    never ends up with a zero stake, and the next positive EV is not stuck at zero).
    """
    thresholds = record.thresholds or FALLBACK_THRESHOLDS
    if candidate is None:
        odds, probability, ev, confidence = (
            record.odds,
            record.p_model,
            record.ev,
            record.confidence,
        )
        stake, fair_odds, quoted_at = record.stake_units, record.fair_odds, record.quoted_at
        raw_probability = record.p_raw
    else:
        odds, probability, ev = candidate.odds, candidate.probability, candidate.ev
        confidence = Confidence(candidate.confidence.value)
        advised = stake_units(candidate, ctx.config, bank=profile.bank, unit=profile.unit)
        stake = advised if advised > 0 and record.recommended_at is not None else record.stake_units
        fair_odds, quoted_at = candidate.fair_odds, candidate.quoted_at
        raw_probability = candidate.raw_probability
    return SignalDraft(
        user_id=record.user_id,
        fixture_id=record.fixture.id,
        market=record.market,
        selection=record.selection,
        odds=odds,
        p_model=probability,
        p_raw=raw_probability,
        ev=ev,
        confidence=confidence,
        stake_units=stake,
        source=record.source,
        thresholds=thresholds,
        recommended_at=record.recommended_at,
        fair_odds=fair_odds,
        quoted_at=quoted_at,
    )


def _result(  # noqa: PLR0913 - one value per fact of the result
    ctx: PipelineContext,
    before: SignalRecord,
    after: SignalRecord,
    *,
    candidate: Candidate | None,
    now: datetime,
    freshness: tuple[bool, datetime | None, bool],
) -> RecalcResult:
    stale, as_of, lineups = freshness
    thresholds = before.thresholds or FALLBACK_THRESHOLDS
    verdict = _verdict(candidate, thresholds)
    flags = () if candidate is None else candidate.risk_flags
    bookmaker = ctx.config.bet_bookmaker
    return RecalcResult(
        before=SignalCard(record=before, bookmaker=bookmaker, no_value=False),
        after=SignalCard(
            record=after,
            bookmaker=bookmaker,
            no_value=after.recommended_at is None,
            risk_flags=flags,
            data_as_of=as_of,
        ),
        verdict=verdict,
        recalculated_at=now,
        stale=stale,
        lineups_confirmed=lineups,
    )


def _verdict(candidate: Candidate | None, thresholds: Thresholds) -> RecalcVerdict:
    """No price -> ``NO_PRICE``; valid only with a market anchor (never a model-only EV)."""
    if candidate is None:
        return RecalcVerdict.NO_PRICE
    if candidate.anchored and passes(candidate, _probe(thresholds)):
        return RecalcVerdict.STILL_VALID
    return RecalcVerdict.BELOW_THRESHOLD


def _probe(thresholds: Thresholds) -> AnalysisRequest:
    """A request carrying only the creation thresholds, so :func:`passes` can judge a signal."""
    return AnalysisRequest(
        user_id=0,
        start=datetime.min.replace(tzinfo=UTC),
        end=datetime.max.replace(tzinfo=UTC),
        zone=ZoneInfo("UTC"),
        sports=frozenset({Sport.FOOTBALL}),
        markets=frozenset(),
        min_probability=thresholds.min_prob,
        min_ev=thresholds.min_ev,
        odds_min=thresholds.odds_min,
        odds_max=thresholds.odds_max,
    )


async def _cancel(
    ctx: PipelineContext, group: Sequence[SignalRecord], reason: str, now: datetime
) -> list[RecalcResult]:
    """Cancel every signal of a fixture that will not be played as scheduled."""
    results = []
    bookmaker = ctx.config.bet_bookmaker
    async with ctx.sessions.begin() as session:
        for record in group:
            await set_signal_status(session, record.id, SignalStatus.CANCELLED)
            await cancel_recalcs(session, record.id, now=now)
            results.append(
                RecalcResult(
                    before=SignalCard(record=record, bookmaker=bookmaker, no_value=False),
                    after=SignalCard(
                        record=record,
                        bookmaker=bookmaker,
                        no_value=False,
                    ),
                    verdict=RecalcVerdict.CANCELLED,
                    recalculated_at=now,
                    cancel_reason=reason,
                )
            )
    return results
