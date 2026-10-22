"""Which stored prices a decision is based on.

A decision (a signal, a recalculation, a detail card) uses the freshest *complete* quote of
each bookmaker and market taken before kick-off. Snapshots of different moments are never
mixed inside one quote: ``complete_quotes`` keeps moments apart (by ``kind``), and
:func:`current_quotes` then picks, per bookmaker and market, the newest complete one. All of
them are relabelled with one kind so the market model (which works on a single moment) can
use them together.
"""

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import latest_odds
from app.domain import ExternalRef, Market, OddsKind, OddsSnapshot
from app.models import MarketQuote, complete_quotes

DECISION_KINDS: Final = (
    OddsKind.PREMATCH,
    OddsKind.RECALC,
    OddsKind.MORNING,
    OddsKind.OPENING,
)
"""Moments a decision may be based on, freshest first (``closing`` exists only after kick-off)."""
CURRENT_KIND: Final = OddsKind.RECALC
"""The single kind :func:`current_quotes` relabels its quotes with."""


async def load_decision_snapshots(
    session: AsyncSession, fixtures: Iterable[ExternalRef]
) -> dict[ExternalRef, list[OddsSnapshot]]:
    """The latest stored price of every selection, for each decision kind, per fixture."""
    refs = list(dict.fromkeys(fixtures))
    merged: dict[ExternalRef, list[OddsSnapshot]] = {ref: [] for ref in refs}
    for kind in DECISION_KINDS:
        for ref, snapshots in (await latest_odds(session, refs, kinds=(kind,))).items():
            merged[ref].extend(snapshots)
    return merged


def current_quotes(
    snapshots: Sequence[OddsSnapshot], fixture: ExternalRef, kickoff: datetime
) -> list[MarketQuote]:
    """Newest complete pre-kick-off quote per bookmaker and market, as one moment."""
    quotes = complete_quotes(snapshots, {fixture: kickoff})
    newest: dict[tuple[str, Market], MarketQuote] = {}
    for quote in quotes:
        key = (quote.bookmaker, quote.market)
        current = newest.get(key)
        if current is None or quote.taken_at > current.taken_at:
            newest[key] = quote
    return [replace(quote, kind=CURRENT_KIND) for quote in newest.values()]


def data_as_of(quotes: Sequence[MarketQuote], bookmaker: str) -> datetime | None:
    """When ``bookmaker``'s newest price in ``quotes`` was taken."""
    moments = [quote.taken_at for quote in quotes if quote.bookmaker == bookmaker]
    return max(moments, default=None)
