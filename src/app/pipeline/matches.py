"""Matches straight from Football-Data.org: nothing is read from or written to the database.

The match list is always the provider's current answer (the only copy is the client's short
TTL cache), so a match that was moved or cancelled never lingers as a stale stored row. The
only trace in the database is the call log the client writes for every request.

The user asks for local days (``Asia/Tashkent`` by default), the provider filters by UTC day:
a local day spans two UTC days, so the feed asks for every UTC day that overlaps the window
and keeps the matches whose kick-off falls inside it.

The services layer may not import ``app.clients``; the match models are re-exported here and
provider failures are translated into :mod:`app.pipeline.errors`.
"""

from collections.abc import Awaitable
from datetime import UTC, date, datetime, time, timedelta
from http import HTTPStatus
from zoneinfo import ZoneInfo

from app.clients.football_data_org import PROVIDER, FootballDataClient
from app.clients.football_data_org_models import Match, MatchStatus, MatchTeam, Player
from app.clients.http import ProviderHttpError
from app.clients.quota import RequestPurpose
from app.errors import ExternalServiceError
from app.pipeline.errors import MatchNotFoundError, ProviderUnavailableError

__all__ = ["Match", "MatchFeed", "MatchStatus", "MatchTeam", "Player", "utc_bounds_of_days"]

# Ids of matches outside the plan answer 403, unknown ids 404: both mean "no such match here".
_MISSING_MATCH = frozenset({HTTPStatus.NOT_FOUND, HTTPStatus.FORBIDDEN})


def utc_bounds_of_days(day_from: date, day_to: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """``[start, end)`` in UTC of the local days ``day_from``..``day_to`` (both included)."""
    start = datetime.combine(day_from, time(), zone).astimezone(UTC)
    end = datetime.combine(day_to + timedelta(days=1), time(), zone).astimezone(UTC)
    return start, end


class MatchFeed:
    """Read-through access to the match provider (every call is a user action)."""

    def __init__(self, client: FootballDataClient) -> None:
        """Wrap the provider client; its rate limit and cache apply to every call."""
        self._client = client

    async def matches_on(self, day_from: date, day_to: date, zone: ZoneInfo) -> list[Match]:
        """Matches kicking off on the local days ``day_from``..``day_to``, earliest first.

        Raises:
            ProviderUnavailableError: the provider failed after its retries.
        """
        start, end = utc_bounds_of_days(day_from, day_to, zone)
        last_instant = end - timedelta(microseconds=1)
        matches = await _translated(
            self._client.matches_between(
                start.date(), last_instant.date(), purpose=RequestPurpose.MANUAL
            )
        )
        unique = {match.id: match for match in matches if start <= match.utc_date < end}
        return sorted(unique.values(), key=lambda match: (match.utc_date, match.id))

    async def match(self, match_id: int) -> Match:
        """One match with score, coaches and line-ups.

        Raises:
            MatchNotFoundError: the provider does not serve this id.
            ProviderUnavailableError: the provider failed after its retries.
        """
        try:
            return await _translated(self._client.match(match_id, purpose=RequestPurpose.MANUAL))
        except ProviderUnavailableError as error:
            cause = error.__cause__
            if isinstance(cause, ProviderHttpError) and cause.status_code in _MISSING_MATCH:
                raise MatchNotFoundError(match_id) from cause
            raise


async def _translated[T](call: Awaitable[T]) -> T:
    """Await a provider call, turning its failures into pipeline errors."""
    try:
        return await call
    except ExternalServiceError as error:
        raise ProviderUnavailableError(PROVIDER, str(error)) from error
