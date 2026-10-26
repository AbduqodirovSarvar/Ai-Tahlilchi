"""The fake browser keeps the real one's rules where the bot can see them (refresh, filters)."""

from datetime import timedelta

import pytest

from app.domain import FixtureStatus, Sport
from app.services.errors import CooldownActiveError, NotAuthorizedError
from app.services.fake import create_fake_services
from app.services.interfaces import (
    MANUAL_REFRESH_MIN_INTERVAL,
    StoredFixtureRequest,
    StoredTeamRequest,
)
from tests.services.contract_suite import MutableClock

ADMIN = 7


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


async def test_the_refresh_reports_and_then_waits_for_the_gap(clock: MutableClock) -> None:
    browser = create_fake_services(clock=clock).browser

    report = await browser.refresh_database(ADMIN)
    clock.advance(timedelta(minutes=10))
    with pytest.raises(CooldownActiveError) as caught:
        await browser.refresh_database(ADMIN)
    clock.advance(MANUAL_REFRESH_MIN_INTERVAL - timedelta(minutes=10))
    again = await browser.refresh_database(ADMIN)

    assert report.complete
    assert report.api_requests > 0
    assert caught.value.retry_after == MANUAL_REFRESH_MIN_INTERVAL - timedelta(minutes=10)
    assert again.started_at - report.started_at == MANUAL_REFRESH_MIN_INTERVAL
    overview = await browser.database_overview(ADMIN)
    assert overview.last_refresh_at == again.started_at


async def test_only_listed_administrators_may_refresh(clock: MutableClock) -> None:
    browser = create_fake_services(clock=clock, admin_ids=frozenset({ADMIN})).browser

    with pytest.raises(NotAuthorizedError):
        await browser.refresh_database(ADMIN + 1)

    assert (await browser.refresh_database(ADMIN)).complete


async def test_filters_combine_with_and(clock: MutableClock) -> None:
    browser = create_fake_services(clock=clock).browser
    everything = await browser.list_stored_fixtures(ADMIN, StoredFixtureRequest(page_size=20))
    team = everything.items[0].home_team_id

    own = await browser.list_stored_fixtures(
        ADMIN, StoredFixtureRequest(team_ids=frozenset({team}), page_size=20)
    )
    other_league = next(
        i.league_id for i in everything.items if i.league_id != own.items[0].league_id
    )
    none = await browser.list_stored_fixtures(
        ADMIN,
        StoredFixtureRequest(
            team_ids=frozenset({team}), league_ids=frozenset({other_league}), page_size=20
        ),
    )
    finished = await browser.list_stored_fixtures(
        ADMIN, StoredFixtureRequest(statuses=frozenset({FixtureStatus.FINISHED}), page_size=20)
    )

    assert own.total > 0
    assert all(team in (i.home_team_id, i.away_team_id) for i in own.items)
    assert none.total == 0
    assert {i.status for i in finished.items} == {FixtureStatus.FINISHED}


async def test_sport_and_league_narrow_leagues_and_teams(clock: MutableClock) -> None:
    browser = create_fake_services(clock=clock).browser

    hockey = await browser.list_stored_leagues(ADMIN, sport=Sport.HOCKEY)
    teams = await browser.list_stored_teams(
        ADMIN,
        StoredTeamRequest(sport=Sport.HOCKEY, league_ids=frozenset({hockey[0].league_id})),
    )
    nobody = await browser.list_stored_teams(
        ADMIN, StoredTeamRequest(sport=Sport.FOOTBALL, league_ids=frozenset({hockey[0].league_id}))
    )

    assert [league.sport for league in hockey] == [Sport.HOCKEY]
    assert teams.total > 0
    assert {team.sport for team in teams.items} == {Sport.HOCKEY}
    assert nobody.total == 0
