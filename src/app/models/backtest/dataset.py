"""Backtest input: finished fixtures with their result and complete price quotes."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Final

from app.domain import Fixture, HistoryBatch, Market, OddsKind
from app.models.adapters import (
    DECISION_KINDS,
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

    @property
    def usable_quotes(self) -> tuple[MarketQuote, ...]:
        """Quotes without decision-time prices captured at or after kickoff.

        :func:`build_dataset` already drops such snapshots; this second guard keeps a
        hand-built item from letting a post-kickoff price decide a bet.
        """
        kickoff = self.fixture.kickoff
        return tuple(
            quote
            for quote in self.quotes
            if quote.kind not in DECISION_KINDS or quote.taken_at < kickoff
        )

    def quote(self, kind: OddsKind, bookmaker: str, market: Market) -> MarketQuote | None:
        """The usable complete quote of ``market`` by ``bookmaker`` at ``kind``, if any."""
        for quote in self.usable_quotes:
            if quote.kind is kind and quote.bookmaker == bookmaker and quote.market == market:
                return quote
        return None


def build_dataset(batch: HistoryBatch) -> tuple[BacktestFixture, ...]:
    """Finished fixtures of ``batch`` with their quotes, in kickoff order.

    Fixtures without a finished result (scheduled, postponed, abandoned) are left out: they
    can neither be learnt from nor settled. Decision-time prices taken at or after kickoff
    are dropped (:func:`app.models.adapters.complete_quotes`).
    """
    results = {result.fixture: result for result in batch.results}
    kickoffs = {fixture.ref: fixture.kickoff for fixture in batch.fixtures}
    quotes = quotes_by_fixture(complete_quotes(batch.odds, kickoffs))
    items = []
    for fixture in batch.fixtures:
        result = results.get(fixture.ref)
        record = None if result is None else match_record(fixture, result)
        if record is not None:
            items.append(BacktestFixture(fixture, record, tuple(quotes.get(fixture.ref, ()))))
    return tuple(sorted(items, key=lambda item: (item.fixture.kickoff, item.key)))


# A European season (start year Y) is over by 30 June of Y + 1.
SEASON_END_MONTH: Final = 6
SEASON_END_DAY: Final = 30
# A finished season must hold at least this share of a full double round robin per league;
# a few unparseable source rows are tolerated, a half-imported file is not.
MIN_COMPLETE_SHARE: Final = 0.9


def season_end(season: int) -> date:
    """Last day of a season that starts in ``season`` (30 June of the next year)."""
    return date(season + 1, SEASON_END_MONTH, SEASON_END_DAY)


def complete_seasons(fixtures: Iterable[Fixture], *, today: date) -> tuple[int, ...]:
    """Seasons that may serve as a hold-out: over by the calendar and fully played.

    A season is complete when ``season_end(season)`` is before ``today`` *and* every league
    of that season has at least ``MIN_COMPLETE_SHARE`` of its double round robin
    (``n_teams * (n_teams - 1)``) among ``fixtures``. The calendar rule matters because
    football-data files list only played matches, so a season in progress looks "finished"
    match by match. Leagues with a calendar-year season are not covered by this rule.
    """
    counts: dict[tuple[str, int], int] = {}
    teams: dict[tuple[str, int], set[str]] = {}
    for fixture in fixtures:
        key = (str(fixture.league.ref), fixture.season)
        counts[key] = counts.get(key, 0) + 1
        teams.setdefault(key, set()).update((str(fixture.home.ref), str(fixture.away.ref)))
    incomplete = {
        season
        for (league, season), n_fixtures in counts.items()
        if season_end(season) >= today
        or n_fixtures < MIN_COMPLETE_SHARE * _round_robin_size(len(teams[league, season]))
    }
    return tuple(sorted({season for _, season in counts} - incomplete))


def _round_robin_size(n_teams: int) -> int:
    return n_teams * (n_teams - 1)


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
