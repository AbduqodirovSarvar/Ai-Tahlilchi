"""MatchFeed: local days to UTC days, filtering, ordering and error translation (no network)."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.clients.football_data_org import PROVIDER
from app.clients.football_data_org_models import MatchLineups
from app.clients.football_data_org_requests import CompetitionMatchFilters
from app.clients.http import ProviderHttpError
from app.clients.http import ProviderUnavailableError as ClientUnavailable
from app.clients.quota import RequestPurpose
from app.pipeline.errors import MatchNotFoundError, ProviderUnavailableError
from app.pipeline.matches import Match, MatchFeed, utc_bounds_of_days

TASHKENT = ZoneInfo("Asia/Tashkent")  # UTC+5, no daylight saving


def match(match_id: int, kickoff: datetime, status: str = "TIMED") -> Match:
    return Match.model_validate(
        {
            "id": match_id,
            "utcDate": kickoff.isoformat(),
            "status": status,
            "homeTeam": {"id": 1, "name": "Home"},
            "awayTeam": {"id": 2, "name": "Away"},
        }
    )


class StubClient:
    """A FootballDataClient that answers from memory and remembers what it was asked."""

    def __init__(self, matches: list[Match], error: Exception | None = None) -> None:
        self.matches = matches
        self.error = error
        self.ranges: list[tuple[date, date, RequestPurpose]] = []

    async def matches_between(
        self, date_from: date, date_to: date, *, purpose: RequestPurpose
    ) -> list[Match]:
        self.ranges.append((date_from, date_to, purpose))
        if self.error is not None:
            raise self.error
        return list(self.matches)

    async def competition_matches(
        self,
        competition: str | int,
        filters: CompetitionMatchFilters,
        *,
        purpose: RequestPurpose,
    ) -> list[Match]:
        raise NotImplementedError

    async def match(self, match_id: int, *, purpose: RequestPurpose) -> Match:  # noqa: ARG002 - protocol signature
        if self.error is not None:
            raise self.error
        return next(item for item in self.matches if item.id == match_id)

    async def lineups(self, match_id: int, *, purpose: RequestPurpose) -> MatchLineups:
        raise NotImplementedError


def test_local_day_spans_two_utc_days() -> None:
    start, end = utc_bounds_of_days(date(2026, 10, 30), date(2026, 10, 30), TASHKENT)
    assert start == datetime(2026, 10, 29, 19, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 30, 19, 0, tzinfo=UTC)


async def test_matches_on_keeps_only_the_local_day_sorted_and_unique() -> None:
    late_previous_utc = match(1, datetime(2026, 10, 29, 20, 30, tzinfo=UTC))  # 01:30 local 30.10
    evening = match(2, datetime(2026, 10, 30, 16, 0, tzinfo=UTC))  # 21:00 local
    next_local_day = match(3, datetime(2026, 10, 30, 19, 0, tzinfo=UTC))  # 00:00 local 31.10
    before = match(4, datetime(2026, 10, 29, 18, 59, tzinfo=UTC))  # 23:59 local 29.10
    client = StubClient([evening, next_local_day, late_previous_utc, before, evening])

    found = await MatchFeed(client).matches_on(date(2026, 10, 30), date(2026, 10, 30), TASHKENT)

    assert [item.id for item in found] == [1, 2]
    assert client.ranges == [(date(2026, 10, 29), date(2026, 10, 30), RequestPurpose.MANUAL)]


async def test_provider_failure_is_translated() -> None:
    client = StubClient([], error=ClientUnavailable("football-data-org: gave up after 4 tries"))
    with pytest.raises(ProviderUnavailableError) as info:
        await MatchFeed(client).matches_on(date(2026, 10, 30), date(2026, 10, 30), TASHKENT)
    assert info.value.provider == PROVIDER


@pytest.mark.parametrize("status", [403, 404])
async def test_unknown_or_restricted_match_is_not_found(status: int) -> None:
    client = StubClient([], error=ProviderHttpError(PROVIDER, status, "nope"))
    with pytest.raises(MatchNotFoundError):
        await MatchFeed(client).match(99)


async def test_other_http_errors_stay_unavailable() -> None:
    client = StubClient([], error=ProviderHttpError(PROVIDER, 400, "bad request"))
    with pytest.raises(ProviderUnavailableError):
        await MatchFeed(client).match(99)


async def test_match_is_returned_as_is() -> None:
    wanted = match(7, datetime(2026, 10, 30, 16, 0, tzinfo=UTC), status="FINISHED")
    assert await MatchFeed(StubClient([wanted])).match(7) == wanted
