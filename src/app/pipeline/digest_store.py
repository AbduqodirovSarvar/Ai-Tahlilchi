"""What a digest-style message (morning digest, scheduled template) remembers between attempts.

Two things are kept in the ledger so a retry is cheap and a later update can reply:

- the *analysis* behind the message (signal ids and counters, job ``daily.outcome``): a retry
  after a failed delivery re-sends the same message instead of re-running the analysis and the
  LLM review;
- the *message reference* of each recommended signal in it (job ``msg``), so a pre-match update
  is a reply to the message that announced the signal.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Final

from app.db.repo import get_signal
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.ledger import STAMP_DAY, LedgerKey
from app.pipeline.types import AnalysisOutcome, SignalCard

logger = get_logger(__name__)

MESSAGE_JOB: Final = "msg"
OUTCOME_JOB: Final = "daily.outcome"


def message_key(user_id: int, signal_id: int) -> LedgerKey:
    """Where the reference of the message that announced a signal is remembered."""
    return LedgerKey(MESSAGE_JOB, STAMP_DAY, user_id, f"signal:{signal_id}")


async def remember_announcement(
    ctx: PipelineContext, user_id: int, outcome: AnalysisOutcome, reference: str
) -> None:
    """Remember ``reference`` as the message that announced every recommended signal."""
    now = ctx.clock()
    for card in outcome.cards:
        if card.record.recommended_at is not None:
            await ctx.ledger.complete(
                message_key(user_id, card.record.id), now=now, detail=reference
            )


def _outcome_key(key: LedgerKey) -> LedgerKey:
    return LedgerKey(OUTCOME_JOB, key.day, key.user_id, key.subject)


async def outcome_for(
    ctx: PipelineContext, key: LedgerKey, run: Callable[[], Awaitable[AnalysisOutcome]]
) -> AnalysisOutcome:
    """The analysis of ``key``: the stored one on a retry, otherwise ``run()`` (then stored)."""
    stored = await _load_outcome(ctx, key)
    if stored is not None:
        return stored
    outcome = await run()
    await ctx.ledger.complete(_outcome_key(key), now=ctx.clock(), detail=_encode(outcome))
    return outcome


def _encode(outcome: AnalysisOutcome) -> str:
    return json.dumps(
        {
            "cards": [
                {
                    "id": card.record.id,
                    "no_value": card.no_value,
                    "watchlist": card.watchlist,
                    "risk_flags": list(card.risk_flags),
                    "data_as_of": None if card.data_as_of is None else card.data_as_of.isoformat(),
                }
                for card in outcome.cards
            ],
            "fixtures_analyzed": outcome.fixtures_analyzed,
            "matched": outcome.matched,
            "requested": outcome.requested,
            "api_requests_used": outcome.api_requests_used,
            "failed_fixtures": outcome.failed_fixtures,
            "skipped_no_price": outcome.skipped_no_price,
            "skipped_stale_price": outcome.skipped_stale_price,
            "data_stale": outcome.data_stale,
        }
    )


async def _load_outcome(ctx: PipelineContext, key: LedgerKey) -> AnalysisOutcome | None:
    """The analysis stored for ``key``, or ``None`` (none yet, or it cannot be read back)."""
    entry = await ctx.ledger.get(_outcome_key(key))
    if entry is None or not entry.detail:
        return None
    try:
        data = json.loads(entry.detail)
        cards = await _load_cards(ctx, data["cards"])
        if cards is None:  # a signal vanished: analyse again rather than send a gap
            return None
        return AnalysisOutcome(
            cards=cards,
            fixtures_analyzed=data["fixtures_analyzed"],
            matched=data["matched"],
            requested=data["requested"],
            api_requests_used=data["api_requests_used"],
            failed_fixtures=data["failed_fixtures"],
            skipped_no_price=data["skipped_no_price"],
            skipped_stale_price=data["skipped_stale_price"],
            data_stale=data["data_stale"],
        )
    except (KeyError, TypeError, ValueError) as error:  # damaged entry: just analyse again
        logger.warning("digest_outcome_unreadable", user_id=key.user_id, error=repr(error))
        return None


async def _load_cards(
    ctx: PipelineContext, items: list[dict[str, Any]]
) -> tuple[SignalCard, ...] | None:
    cards = []
    async with ctx.sessions() as session:
        for item in items:
            record = await get_signal(session, item["id"])
            if record is None:
                return None
            as_of = item["data_as_of"]
            cards.append(
                SignalCard(
                    record=record,
                    bookmaker=ctx.config.bet_bookmaker,
                    no_value=item["no_value"],
                    watchlist=item["watchlist"],
                    risk_flags=tuple(item["risk_flags"]),
                    data_as_of=None if as_of is None else datetime.fromisoformat(as_of),
                )
            )
    return tuple(cards)
