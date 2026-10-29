"""MatchesService: the real one over a stub feed, the fake one, and the shared range rule."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.pipeline.errors import MatchNotFoundError, ProviderUnavailableError
from app.pipeline.matches import Match
from app.services._fake_matches import FakeMatchesService
from app.services.errors import NotFoundError, ServiceUnavailableError, ValidationFailedError
from app.services.fake import create_fake_services
from app.services.interfaces import MAX_MATCH_DAYS, MatchState, check_match_days
from app.services.real.matches import RealMatchesService, match_item, match_sheet

USER = 1
DAY = date(2026, 10, 30)


def provider_match(status: str = "FINISHED", **extra: object) -> Match:
    body: dict[str, object] = {
        "id": 497410,
        "utcDate": "2026-10-30T16:00:00Z",
        "status": status,
        "competition": {"id": 2021, "name": "Premier League", "code": "PL"},
        "homeTeam": {
            "id": 61,
            "name": "Chelsea FC",
            "formation": "4-2-3-1",
            "coach": {"name": "Enzo Maresca"},
            "lineup": [
                {"name": "Robert Sanchez", "position": "Goalkeeper", "shirtNumber": 1},
                {"name": None, "position": "Defence"},
            ],
            "bench": [{"name": "Filip Jorgensen", "shirtNumber": 12}],
        },
        "awayTeam": {"id": 64, "name": "Liverpool FC", "lineup": [{"name": "Alisson"}]},
        "score": {"fullTime": {"home": 1, "away": 2}, "halfTime": {"home": 0, "away": 1}},
    }
    body.update(extra)
    return Match.model_validate(body)


class StubFeed:
    def __init__(self, matches: list[Match], error: Exception | None = None) -> None:
        self.matches = matches
        self.error = error
        self.zones: list[ZoneInfo] = []

    async def matches_on(self, day_from: date, day_to: date, zone: ZoneInfo) -> list[Match]:  # noqa: ARG002 - protocol signature
        self.zones.append(zone)
        if self.error is not None:
            raise self.error
        return self.matches

    async def match(self, match_id: int) -> Match:  # noqa: ARG002 - protocol signature
        if self.error is not None:
            raise self.error
        return self.matches[0]


def real(feed: StubFeed | None) -> RealMatchesService:
    settings = create_fake_services().settings
    return RealMatchesService(feed, settings)  # type: ignore[arg-type]  # structural stub


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("TIMED", MatchState.SCHEDULED),
        ("IN_PLAY", MatchState.LIVE),
        ("PAUSED", MatchState.LIVE),
        ("FINISHED", MatchState.FINISHED),
        ("POSTPONED", MatchState.POSTPONED),
        ("CANCELLED", MatchState.CANCELLED),
        ("SOMETHING_NEW", MatchState.OTHER),
    ],
)
def test_status_mapping(status: str, state: MatchState) -> None:
    assert match_item(provider_match(status)).state is state


def test_item_and_sheet_mapping() -> None:
    sheet = match_sheet(provider_match())
    item = sheet.match
    assert item.kickoff == datetime(2026, 10, 30, 16, 0, tzinfo=UTC)
    assert (item.competition, item.competition_code) == ("Premier League", "PL")
    assert (item.home, item.away, item.home_goals, item.away_goals) == (
        "Chelsea FC",
        "Liverpool FC",
        1,
        2,
    )
    assert (sheet.home_ht_goals, sheet.away_ht_goals) == (0, 1)
    assert sheet.home.coach == "Enzo Maresca"
    assert [p.name for p in sheet.home.starters] == ["Robert Sanchez"]  # nameless entry dropped
    assert sheet.home.bench[0].shirt_number == 12
    assert sheet.lineups_announced


def test_cup_draw_without_teams_or_competition() -> None:
    item = match_item(
        provider_match(homeTeam={"id": None, "name": None}, competition=None, status="SCHEDULED")
    )
    assert item.home is None
    assert item.competition == ""


async def test_real_list_uses_the_user_zone() -> None:
    feed = StubFeed([provider_match()])
    items = await real(feed).list_matches(USER, DAY, DAY)
    assert [item.match_id for item in items] == [497410]
    assert feed.zones == [ZoneInfo("Asia/Tashkent")]


async def test_real_without_key_says_unavailable() -> None:
    service = real(None)
    with pytest.raises(ServiceUnavailableError, match="FOOTBALL_DATA_ORG_KEY"):
        await service.list_matches(USER, DAY, DAY)
    with pytest.raises(ServiceUnavailableError):
        await service.get_match(USER, 1)


async def test_real_translates_provider_errors() -> None:
    down = real(StubFeed([], error=ProviderUnavailableError("football-data-org", "HTTP 503")))
    with pytest.raises(ServiceUnavailableError):
        await down.list_matches(USER, DAY, DAY)
    with pytest.raises(ServiceUnavailableError):
        await down.get_match(USER, 1)
    missing = real(StubFeed([], error=MatchNotFoundError(5)))
    with pytest.raises(NotFoundError):
        await missing.get_match(USER, 5)


async def test_real_validates_the_range_before_asking_the_provider() -> None:
    feed = StubFeed([])
    with pytest.raises(ValidationFailedError):
        await real(feed).list_matches(USER, DAY, DAY - timedelta(days=1))
    assert feed.zones == []


def test_range_rule() -> None:
    check_match_days(DAY, DAY)
    check_match_days(DAY, DAY + timedelta(days=MAX_MATCH_DAYS - 1))
    with pytest.raises(ValidationFailedError):
        check_match_days(DAY, DAY + timedelta(days=MAX_MATCH_DAYS))
    with pytest.raises(ValidationFailedError):
        check_match_days(DAY, DAY - timedelta(days=1))


async def test_fake_samples_are_stable_and_openable() -> None:
    service = create_fake_services().matches
    assert isinstance(service, FakeMatchesService)
    items = await service.list_matches(USER, DAY, DAY + timedelta(days=1))
    assert len(items) == 4
    assert items == await service.list_matches(USER, DAY, DAY + timedelta(days=1))
    sheet = await service.get_match(USER, items[0].match_id)
    assert sheet.match == items[0]
    with pytest.raises(NotFoundError):
        await service.get_match(USER, 3)
