"""MatchesService: the real one over a stub feed, the fake one, and the shared range rule."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain import Market, Selection
from app.models import ConfidenceLevel as ModelConfidence
from app.pipeline.errors import MatchNotFoundError, ProviderUnavailableError
from app.pipeline.forecast import Chance, Forecast, ForecastSkip, LikelyScore
from app.pipeline.matches import Match
from app.services._fake_matches import FakeMatchesService
from app.services.errors import NotFoundError, ServiceUnavailableError, ValidationFailedError
from app.services.fake import create_fake_services
from app.services.interfaces import (
    MAX_MATCH_DAYS,
    ConfidenceLevel,
    ForecastStatus,
    MatchForecast,
    MatchState,
    OutcomeChance,
    check_match_days,
)
from app.services.real.matches import (
    RealMatchesService,
    match_forecast,
    match_item,
    match_sheet,
)

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


class StubForecaster:
    def __init__(self) -> None:
        self.asked: list[list[int]] = []

    async def forecast(self, matches: list[Match]) -> dict[int, Forecast]:
        self.asked.append([m.id for m in matches])
        result = Market.match_result()
        return {
            m.id: Forecast(
                match_id=m.id,
                chances=(
                    Chance(result, Selection.HOME, 0.5),
                    Chance(result, Selection.DRAW, 0.3),
                    Chance(result, Selection.AWAY, 1.0000000001),  # float overshoot
                ),
                scores=(LikelyScore(1, 0, 0.11),),
                team_matches=25,
                confidence=ModelConfidence.MEDIUM,
            )
            for m in matches
        }


def real(feed: StubFeed | None, forecaster: StubForecaster | None = None) -> RealMatchesService:
    settings = create_fake_services().settings
    return RealMatchesService(feed, settings, forecaster)  # type: ignore[arg-type]  # stubs


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


async def test_forecasts_of_a_day_and_of_one_match() -> None:
    forecaster = StubForecaster()
    service = real(StubFeed([provider_match("TIMED")]), forecaster)
    [line] = await service.forecast_day(USER, DAY)
    assert line.match.match_id == 497410
    forecast = line.forecast
    assert forecast.status is ForecastStatus.READY
    assert forecast.confidence is ConfidenceLevel.MEDIUM
    assert forecast.chance(Market.match_result(), Selection.AWAY).probability == 1.0  # type: ignore[union-attr]
    assert forecast.scores[0].home_goals == 1
    single = await service.forecast_match(USER, 497410)
    assert single.forecast.team_matches == 25
    assert forecaster.asked == [[497410], [497410]]


async def test_without_a_forecaster_every_forecast_is_unavailable() -> None:
    [line] = await real(StubFeed([provider_match("TIMED")])).forecast_day(USER, DAY)
    assert line.forecast.status is ForecastStatus.UNAVAILABLE
    assert not line.forecast.chances


async def test_forecast_errors_follow_the_match_errors() -> None:
    down = real(
        StubFeed([], error=ProviderUnavailableError("football-data-org", "503")), StubForecaster()
    )
    with pytest.raises(ServiceUnavailableError):
        await down.forecast_day(USER, DAY)
    missing = real(StubFeed([], error=MatchNotFoundError(5)), StubForecaster())
    with pytest.raises(NotFoundError):
        await missing.forecast_match(USER, 5)
    with pytest.raises(ServiceUnavailableError):
        await real(None, StubForecaster()).forecast_day(USER, DAY)


@pytest.mark.parametrize("skip", list(ForecastSkip))
def test_every_skip_reason_has_a_contract_status(skip: ForecastSkip) -> None:
    mapped = match_forecast(Forecast(match_id=1, skip=skip, team_matches=3))
    assert mapped.status.value == skip.value
    assert mapped.team_matches == 3
    assert not mapped.chances


def test_forecast_dto_helpers() -> None:
    result = Market.match_result()
    forecast = MatchForecast(
        match_id=1,
        status=ForecastStatus.READY,
        chances=(
            OutcomeChance(market=result, selection=Selection.HOME, probability=0.25),
            OutcomeChance(market=result, selection=Selection.DRAW, probability=0.0),
            OutcomeChance(market=result, selection=Selection.AWAY, probability=0.75),
        ),
    )
    assert forecast.favourite is not None
    assert forecast.favourite.selection is Selection.AWAY
    home = forecast.chance(result, Selection.HOME)
    assert home is not None
    assert home.fair_odds == pytest.approx(4.0)
    assert forecast.chance(result, Selection.DRAW).fair_odds is None  # type: ignore[union-attr]
    assert forecast.chance(Market.btts(), Selection.YES) is None
    assert MatchForecast(match_id=2, status=ForecastStatus.NO_MODEL).favourite is None
    with pytest.raises(ValidationFailedError):
        OutcomeChance(market=result, selection=Selection.HOME, probability=1.5)


async def test_fake_forecasts() -> None:
    service = create_fake_services().matches
    lines = await service.forecast_day(USER, DAY)
    assert len(lines) == 2
    assert all(line.forecast.status is ForecastStatus.READY for line in lines)
    single = await service.forecast_match(USER, lines[0].match.match_id)
    assert single.forecast.favourite is not None
