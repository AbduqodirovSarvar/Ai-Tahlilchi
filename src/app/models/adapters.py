"""Conversions from domain objects (:mod:`app.domain`) to model inputs.

Conventions every caller (pipeline, backtest) relies on:

- **Team key.** A team is identified in a fitted model by ``str(team.ref)``
  (``source:external_id``), never by its display name: names collide across sources and
  change spelling. Cross-source matching (API-Football vs football-data) is the data layer's
  job; the model only sees the resolved reference.
- **Match date.** A match's model date is the **UTC calendar date of its kickoff**
  (``fixture.kickoff`` is UTC by contract). ``fit_dixon_coles(as_of=d)`` therefore uses
  matches whose UTC kickoff date is strictly before ``d``. A late kickoff in the Americas
  can fall on the next UTC day; that is harmless because both the fit cut-off and the
  prediction date use the same convention.
- **Unknown teams.** A team missing from a fitted model raises ``UnknownTeamError``
  (promoted sides, stale data). Callers skip such fixtures and log them; there is no
  league-average fallback (ADR 0003).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Final

from app.domain import (
    ExternalRef,
    Fixture,
    FixtureResult,
    Market,
    OddsKind,
    OddsSnapshot,
    ResultStatus,
    Selection,
    Team,
)
from app.models.exceptions import ModelInputError
from app.models.poisson import MatchRecord


def team_key(team: Team) -> str:
    """Identifier of ``team`` inside a fitted model (``source:external_id``)."""
    return str(team.ref)


def match_date(fixture: Fixture) -> date:
    """Model date of a fixture: the UTC calendar date of its kickoff."""
    return fixture.kickoff.date()


def match_record(fixture: Fixture, result: FixtureResult) -> MatchRecord | None:
    """The fit input for a played fixture, or ``None`` when it has no final score.

    Only ``finished`` results count: an abandoned match's partial score is not a result.

    Raises:
        ModelInputError: ``result`` belongs to another fixture.
    """
    if result.fixture != fixture.ref:
        msg = f"result for {result.fixture} does not belong to fixture {fixture.ref}"
        raise ModelInputError(msg)
    if result.status is not ResultStatus.FINISHED:
        return None
    # A finished FixtureResult always carries both scores (domain invariant).
    if result.home_score is None or result.away_score is None:  # pragma: no cover
        return None
    return MatchRecord(
        home_team=team_key(fixture.home),
        away_team=team_key(fixture.away),
        home_goals=result.home_score,
        away_goals=result.away_score,
        match_date=match_date(fixture),
    )


def match_records(
    fixtures: Iterable[Fixture], results: Iterable[FixtureResult]
) -> list[MatchRecord]:
    """Fit inputs for every fixture that has a finished result, in fixture order."""
    by_fixture = {result.fixture: result for result in results}
    records = []
    for fixture in fixtures:
        result = by_fixture.get(fixture.ref)
        record = None if result is None else match_record(fixture, result)
        if record is not None:
            records.append(record)
    return records


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketQuote:
    """A complete set of prices of one market at one bookmaker and moment (``kind``).

    Complete means every selection of the market is priced, which is what de-vig needs.
    ``taken_at`` is the newest snapshot time in the set.
    """

    fixture: ExternalRef
    bookmaker: str
    kind: OddsKind
    market: Market
    prices: Mapping[Selection, float]
    taken_at: datetime

    def __post_init__(self) -> None:
        """Require exactly the market's selections, each with odds above 1."""
        if set(self.prices) != set(self.market.selections):
            msg = f"quote for {self.market} must price {self.market.selections}, got {self.prices}"
            raise ModelInputError(msg)
        if any(not price > 1.0 for price in self.prices.values()):
            msg = f"quote for {self.market} has odds <= 1: {self.prices}"
            raise ModelInputError(msg)
        object.__setattr__(self, "prices", MappingProxyType(dict(self.prices)))

    def ordered_prices(self) -> tuple[float, ...]:
        """Prices in the market's selection order (the order de-vig results come back in)."""
        return tuple(self.prices[selection] for selection in self.market.selections)


type _QuoteKey = tuple[ExternalRef, str, OddsKind, Market]

# Kinds a bet decision may be based on; only the closing price is by definition at kickoff.
DECISION_KINDS: Final = frozenset(kind for kind in OddsKind if kind is not OddsKind.CLOSING)


def is_pre_kickoff(snapshot: OddsSnapshot, kickoff: datetime) -> bool:
    """False for a decision-time price (not ``closing``) captured at or after kickoff."""
    return snapshot.kind not in DECISION_KINDS or snapshot.taken_at < kickoff


def complete_quotes(
    snapshots: Iterable[OddsSnapshot], kickoffs: Mapping[ExternalRef, datetime] | None = None
) -> list[MarketQuote]:
    """Group snapshots into complete per-(fixture, bookmaker, kind, market) quotes.

    The newest snapshot of each selection wins (a source may re-send a price). Groups with a
    selection missing are dropped: a partial set cannot be de-vigged. Output order is the
    order in which each group was first seen. With ``kickoffs``, decision-time snapshots
    (every kind but ``closing``) taken at or after their fixture's kickoff are ignored:
    defence in depth against a late re-sent price deciding a bet.
    """
    latest: dict[_QuoteKey, dict[Selection, OddsSnapshot]] = {}
    for snapshot in snapshots:
        kickoff = None if kickoffs is None else kickoffs.get(snapshot.fixture)
        if kickoff is not None and not is_pre_kickoff(snapshot, kickoff):
            continue
        key = (snapshot.fixture, snapshot.bookmaker, snapshot.kind, snapshot.market)
        group = latest.setdefault(key, {})
        current = group.get(snapshot.selection)
        if current is None or snapshot.taken_at >= current.taken_at:
            group[snapshot.selection] = snapshot
    quotes = []
    for (fixture, bookmaker, kind, market), group in latest.items():
        if set(group) != set(market.selections):
            continue
        quotes.append(
            MarketQuote(
                fixture=fixture,
                bookmaker=bookmaker,
                kind=kind,
                market=market,
                prices={selection: item.odds for selection, item in group.items()},
                taken_at=max(item.taken_at for item in group.values()),
            )
        )
    return quotes


def quotes_by_fixture(quotes: Sequence[MarketQuote]) -> dict[ExternalRef, list[MarketQuote]]:
    """Index quotes by fixture, keeping their order."""
    grouped: dict[ExternalRef, list[MarketQuote]] = {}
    for quote in quotes:
        grouped.setdefault(quote.fixture, []).append(quote)
    return grouped
