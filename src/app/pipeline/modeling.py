"""Dixon-Coles fits per league, cached per ``(league, as_of, history size)``.

A fit costs seconds, a prediction microseconds, so one fit serves every fixture of a league
on a day. The cache key includes the number of matches in the fit window: when a refresh
stores new results the key changes and the next request refits, so a stale model never
outlives its data. Fits run in a worker thread (``asyncio.to_thread``) and are serialised,
which also makes concurrent requests share one fit instead of racing to compute it.
"""

import asyncio
from collections import Counter, OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from types import MappingProxyType
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repo import FixtureRecord, finished_fixtures
from app.domain import ExternalRef
from app.logging import get_logger
from app.models import (
    DixonColesModel,
    MatchRecord,
    ModelFitError,
    ModelInputError,
    fit_dixon_coles,
    match_records,
)
from app.pipeline.config import PipelineConfig
from app.pipeline.leagues import LeagueDirectory

logger = get_logger(__name__)

CACHE_CAPACITY: Final = 64


@dataclass(frozen=True, slots=True)
class FittedLeague:
    """A league's model (``None`` when it could not be fitted) and its data volume.

    ``team_matches`` counts the matches of each team (model key ``source:external_id``) in
    the fit window; the confidence level and the minimum-history filter use it.
    """

    model: DixonColesModel | None
    team_matches: Mapping[str, int]
    reason: str | None = None


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC)


class ModelCache:
    """Fits and remembers one model per league and ``as_of`` day."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        leagues: LeagueDirectory,
        config: PipelineConfig,
    ) -> None:
        """Read history from ``sessions``; ``leagues`` says which references are one league."""
        self._sessions = sessions
        self._leagues = leagues
        self._config = config
        self._cache: OrderedDict[tuple[ExternalRef, date, int], FittedLeague] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, league: ExternalRef, as_of: date) -> FittedLeague:
        """The model of ``league`` fitted on matches strictly before ``as_of`` (UTC date)."""
        group = self._leagues.group(league)
        async with self._sessions() as session:
            records = await self._history(session, group, as_of)
        fixtures = [record.fixture for record in records]
        results = [record.result for record in records if record.result is not None]
        matches = match_records(fixtures, results)
        key = (self._leagues.canonical(league), as_of, len(matches))
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            fitted = await self._fit(matches, as_of)
            self._cache[key] = fitted
            while len(self._cache) > CACHE_CAPACITY:
                self._cache.popitem(last=False)
            return fitted

    async def _history(
        self, session: AsyncSession, group: tuple[ExternalRef, ...], as_of: date
    ) -> list[FixtureRecord]:
        end = _day_start(as_of)
        return await finished_fixtures(
            session,
            leagues=group,
            start=end - timedelta(days=self._config.history_days),
            end=end,
            preferred_sources=self._config.history_sources,
        )

    async def _fit(self, matches: list[MatchRecord], as_of: date) -> FittedLeague:
        counts: Counter[str] = Counter()
        for match in matches:
            counts[match.home_team] += 1
            counts[match.away_team] += 1
        team_matches = MappingProxyType(dict(counts))
        try:
            model = await asyncio.to_thread(fit_dixon_coles, matches, as_of, xi=self._config.xi)
        except (ModelInputError, ModelFitError) as error:
            logger.warning(
                "model_fit_failed", matches=len(matches), as_of=str(as_of), error=str(error)
            )
            return FittedLeague(None, team_matches, reason=str(error))
        return FittedLeague(model, team_matches)
