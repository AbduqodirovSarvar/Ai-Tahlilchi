"""Review the chosen picks and store them as signals (idempotently).

Order: the LLM hook sees the recommended picks first (no database session is open while it
runs), then everything is written in one short transaction. Storing follows the repository's
rules: one signal per user + fixture + market + selection, ``recommended_at`` written once,
retro picks never overwrite real signals. A rerun that finds identical numbers writes
nothing, so re-running a daily analysis neither duplicates signals nor piles up versions.

Decision #14: a ``rad`` verdict makes the pick *not a recommendation* (it is kept, flagged
no-value, only in custom analyses and for watched fixtures); ``ehtiyot`` stays recommended
and carries its verdict and note; with no LLM there is simply no verdict.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import Confidence, LlmVerdict, RecalcKind, SignalSource, SignalVersionReason
from app.db.repo import (
    SignalDraft,
    SignalRecord,
    enqueue_recalc,
    list_user_signals,
    signal_has_bets,
    touch_signal_quote_time,
    upsert_signal,
)
from app.domain import Market, Selection
from app.llm.inputs import TeamForm
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.form import forms_for
from app.pipeline.review import NoReview, Review, ReviewCandidate, ReviewKey, review_safely
from app.pipeline.selection import Pick
from app.pipeline.types import AnalysisRequest, SignalCard
from app.pipeline.valuation import stake_units

logger = get_logger(__name__)

EPSILON: Final = 1e-9
_ZERO: Final = Decimal(0)

type SignalKey = tuple[int, Market, Selection]


@dataclass(frozen=True, slots=True)
class Stored:
    """Cards of the stored signals, in pick order, and the adjusted ``matched`` count."""

    cards: tuple[SignalCard, ...]
    matched: int


def _key(pick: Pick) -> ReviewKey:
    return (pick.fixture.id, pick.candidate.market, pick.candidate.selection)


def _to_review_candidate(
    pick: Pick, user_id: int, forms: tuple[TeamForm, ...] = ()
) -> ReviewCandidate:
    fixture = pick.fixture.fixture
    candidate = pick.candidate
    return ReviewCandidate(
        fixture_id=pick.fixture.id,
        league=fixture.league.name,
        home_team=fixture.home.name,
        away_team=fixture.away.name,
        kickoff=fixture.kickoff,
        market=candidate.market,
        selection=candidate.selection,
        odds=candidate.odds,
        probability=candidate.probability,
        ev=candidate.ev,
        confidence=Confidence(candidate.confidence.value),
        market_probability=candidate.market_probability,
        confidence_reasons=tuple(reason.value for reason in candidate.assessment.reasons),
        user_id=user_id,
        league_id=pick.fixture.league_id,
        home_team_id=pick.fixture.home_team_id,
        away_team_id=pick.fixture.away_team_id,
        forms=forms,
    )


async def _forms(ctx: PipelineContext, picks: Sequence[Pick]) -> dict[int, tuple[TeamForm, ...]]:
    """Recent form of the teams of ``picks`` for the reviewer; unreadable history = no forms."""
    if not picks or isinstance(ctx.reviewer, NoReview):  # nobody to show the form to
        return {}
    try:
        return await forms_for(ctx, [pick.fixture for pick in picks])
    except SQLAlchemyError as error:
        logger.warning("form_unavailable", error=repr(error))
        return {}


def apply_reviews(
    picks: Sequence[Pick], reviews: Mapping[ReviewKey, Review], request: AnalysisRequest
) -> tuple[list[Pick], int]:
    """Picks after the verdicts, and how many regular matched picks a ``rad`` removed.

    A rejected pick stops being a recommendation. It stays in the list only when the user
    asked for everything (custom analysis) or follows the fixture; otherwise it is dropped.
    """
    kept: list[Pick] = []
    rejected = 0
    for pick in picks:
        review = reviews.get(_key(pick))
        if review is None or review.verdict is not LlmVerdict.REJECT or not pick.passes:
            kept.append(pick)
            continue
        if request.include_no_value or pick.watchlist:
            kept.append(replace(pick, passes=False))
        else:
            rejected += 1
    return kept, rejected


def _source(pick: Pick, request: AnalysisRequest) -> SignalSource:
    if pick.retro:
        return SignalSource.RETRO
    if request.include_no_value:
        return SignalSource.CUSTOM
    if pick.watchlist:
        return SignalSource.WATCHLIST
    return request.source


async def refresh_quote_time(
    session: AsyncSession, prior: SignalRecord, draft: SignalDraft
) -> SignalRecord:
    """``prior`` with the price time of ``draft``; same numbers, so no new version is written."""
    if draft.quoted_at is None or prior.quoted_at == draft.quoted_at:
        return prior
    await touch_signal_quote_time(session, prior.id, draft.quoted_at)
    return replace(prior, quoted_at=draft.quoted_at)


def _same_fair_odds(prior: float | None, new: float | None) -> bool:
    if new is None:
        return True
    return prior is not None and abs(prior - new) < EPSILON


def unchanged(prior: SignalRecord, draft: SignalDraft) -> bool:
    """True when storing ``draft`` would change nothing the user could see."""
    return (
        abs(prior.odds - draft.odds) < EPSILON
        and abs(prior.p_model - draft.p_model) < EPSILON
        and abs(prior.ev - draft.ev) < EPSILON
        and prior.confidence is draft.confidence
        and prior.stake_units == draft.stake_units
        and _same_fair_odds(prior.fair_odds, draft.fair_odds)
        and (draft.llm_verdict is None or prior.llm_verdict is draft.llm_verdict)
        and (draft.llm_note is None or prior.llm_note == draft.llm_note)
        and (prior.recommended_at is not None or draft.recommended_at is None)
    )


async def keep_backed_stake(
    session: AsyncSession, prior: SignalRecord | None, draft: SignalDraft
) -> SignalDraft:
    """``draft`` with the stake of ``prior`` when bets already back it.

    The signal's profit is measured on its advised stake and the user's bets were sized from
    it, so a later Kelly recalculation (the bank moved) must not rewrite it.
    """
    if prior is None or prior.stake_units == draft.stake_units:
        return draft
    if not await signal_has_bets(session, prior.id):
        return draft
    return replace(draft, stake_units=prior.stake_units)


def _draft(  # noqa: PLR0913, PLR0917 - everything one draft is made of
    ctx: PipelineContext,
    request: AnalysisRequest,
    pick: Pick,
    prior: SignalRecord | None,
    review: Review | None,
    now: datetime,
) -> SignalDraft:
    candidate = pick.candidate
    was_recommended = prior is not None and prior.recommended_at is not None
    keeps_stake = pick.passes or was_recommended
    stake = stake_units(candidate, ctx.config, bank=request.bank, unit=request.unit)
    if prior is not None and was_recommended and stake == 0:
        stake = prior.stake_units  # a dip below zero EV must not erase the advised stake
    return SignalDraft(
        user_id=request.user_id,
        fixture_id=pick.fixture.id,
        market=candidate.market,
        selection=candidate.selection,
        odds=candidate.odds,
        p_model=candidate.probability,
        p_raw=candidate.raw_probability,
        ev=candidate.ev,
        confidence=Confidence(candidate.confidence.value),
        stake_units=stake if keeps_stake else _ZERO,
        source=_source(pick, request),
        thresholds=request.thresholds,
        recommended_at=now if pick.passes and not pick.retro else None,
        llm_verdict=None if review is None else review.verdict,
        llm_note=None if review is None else review.note,
        fair_odds=candidate.fair_odds,
        quoted_at=candidate.quoted_at,
    )


async def _schedule_recalcs(
    ctx: PipelineContext,
    session: AsyncSession,
    record: SignalRecord,
    request: AnalysisRequest,
    now: datetime,
) -> None:
    """Queue the 24 h and the pre-match recalculation of a recommended future signal."""
    kickoff = record.fixture.fixture.kickoff
    plan = [(RecalcKind.BEFORE_24H, kickoff - ctx.config.before_kickoff_lead)]
    if request.prematch_minutes is not None:
        plan.append((RecalcKind.PREMATCH, kickoff - timedelta(minutes=request.prematch_minutes)))
    for kind, run_at in plan:
        if run_at > now:
            await enqueue_recalc(session, record.id, kind=kind, run_at=run_at)


async def store_picks(
    ctx: PipelineContext, request: AnalysisRequest, picks: Sequence[Pick], *, matched: int
) -> Stored:
    """Review and store ``picks``; returns their cards (see the module docstring)."""
    now = ctx.clock()
    reviewable = [pick for pick in picks if pick.passes and not pick.retro]
    forms = await _forms(ctx, reviewable)
    to_review = [
        _to_review_candidate(pick, request.user_id, forms.get(pick.fixture.id, ()))
        for pick in reviewable
    ]
    reviews = await review_safely(ctx.reviewer, to_review, limit_seconds=ctx.config.review_timeout)
    adjusted, rejected = apply_reviews(picks, reviews, request)
    cards: list[SignalCard] = []
    async with ctx.sessions.begin() as session:
        stored = await list_user_signals(
            session,
            request.user_id,
            start=request.start,
            end=request.end,
            include_hidden=True,
            include_retro=True,
        )
        existing: dict[SignalKey, SignalRecord] = {
            (record.fixture.id, record.market, record.selection): record for record in stored
        }
        for pick in adjusted:
            review = reviews.get(_key(pick))
            prior = existing.get((pick.fixture.id, pick.candidate.market, pick.candidate.selection))
            draft = await keep_backed_stake(
                session, prior, _draft(ctx, request, pick, prior, review, now)
            )
            if prior is not None and unchanged(prior, draft):
                record = await refresh_quote_time(session, prior, draft)
            else:
                record = await upsert_signal(session, draft, reason=SignalVersionReason.INITIAL)
            if record.recommended_at is not None and not record.retro:
                await _schedule_recalcs(ctx, session, record, request, now)
            flags = (*pick.candidate.risk_flags, *(() if review is None else review.risk_flags))
            cards.append(
                SignalCard(
                    record=record,
                    bookmaker=ctx.config.bet_bookmaker,
                    no_value=record.recommended_at is None and not pick.passes,
                    watchlist=pick.watchlist,
                    risk_flags=tuple(dict.fromkeys(flags)),
                    data_as_of=pick.candidate.quoted_at,
                )
            )
    return Stored(cards=tuple(cards), matched=matched - rejected)
