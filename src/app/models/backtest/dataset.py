"""Backtest input: finished fixtures with their result and complete price quotes."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from app.domain import Fixture, HistoryBatch, Market, OddsKind
from app.models.adapters import (
    MarketQuote,
    complete_quotes,
    match_record,
    quotes_by_fixture,
    team_key,
)
from app.models.poisson import MatchRecord


@dataclass(frozen=True, slots=True)
class BacktestFixture:
    """One finished fixture: what the model may learn from it and what could be bet on."""

    fixture: Fixture
    record: MatchRecord
    quotes: tuple[MarketQuote, ...]

    @property
    def key(self) -> str:
        """Stable fixture identifier (``source:external_id``)."""
        return str(self.fixture.ref)

    @property
    def league(self) -> str:
        """League identifier (``source:external_id``)."""
        return str(self.fixture.league.ref)

    @property
    def league_name(self) -> str:
        """Human-readable league name for reports."""
        return self.fixture.league.name

    @property
    def season(self) -> int:
        """Season start year."""
        return self.fixture.season

    @property
    def match_date(self) -> date:
        """Model date (UTC kickoff date, see :mod:`app.models.adapters`)."""
        return self.record.match_date

    @property
    def home(self) -> str:
        """Home team key."""
        return team_key(self.fixture.home)

    @property
    def away(self) -> str:
        """Away team key."""
        return team_key(self.fixture.away)

    def quote(self, kind: OddsKind, bookmaker: str, market: Market) -> MarketQuote | None:
        """The complete quote of ``market`` by ``bookmaker`` at moment ``kind``, if any."""
        for quote in self.quotes:
            if quote.kind is kind and quote.bookmaker == bookmaker and quote.market == market:
                return quote
        return None


def build_dataset(batch: HistoryBatch) -> tuple[BacktestFixture, ...]:
    """Finished fixtures of ``batch`` with their quotes, in kickoff order.

    Fixtures without a finished result (scheduled, postponed, abandoned) are left out: they
    can neither be learnt from nor settled.
    """
    results = {result.fixture: result for result in batch.results}
    quotes = quotes_by_fixture(complete_quotes(batch.odds))
    items = []
    for fixture in batch.fixtures:
        result = results.get(fixture.ref)
        record = None if result is None else match_record(fixture, result)
        if record is not None:
            items.append(BacktestFixture(fixture, record, tuple(quotes.get(fixture.ref, ()))))
    return tuple(sorted(items, key=lambda item: (item.fixture.kickoff, item.key)))


def dataset_fingerprint(batch: HistoryBatch) -> str:
    """SHA-256 of a canonical text form of the batch (order-independent).

    Recorded in every report so a result can be tied to the exact data it was computed on,
    independently of the database file's physical layout.
    """
    lines = sorted(
        [
            *(_fixture_line(fixture) for fixture in batch.fixtures),
            *(
                f"R|{r.fixture}|{r.status}|{r.home_score}|{r.away_score}|{r.home_xg}|{r.away_xg}"
                for r in batch.results
            ),
            *(
                f"O|{o.fixture}|{o.bookmaker}|{o.market}|{o.selection}|{o.kind}|"
                f"{o.taken_at.isoformat()}|{o.odds!r}"
                for o in batch.odds
            ),
        ]
    )
    return _sha256(lines)


def _fixture_line(fixture: Fixture) -> str:
    return (
        f"F|{fixture.ref}|{fixture.league.ref}|{fixture.season}|{fixture.kickoff.isoformat()}|"
        f"{fixture.home.ref}|{fixture.away.ref}|{fixture.status}"
    )


def _sha256(lines: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
