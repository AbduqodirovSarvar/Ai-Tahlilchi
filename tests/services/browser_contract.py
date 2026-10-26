"""Reusable behavioural tests of the database browser (contract v1.11, ``DataBrowserService``).

Like :mod:`tests.services.contract_suite` it is data-agnostic and runs against both the fake
and the real services; the concrete modules provide the ``services``, ``clock`` and
``user_id`` fixtures.
"""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.errors import NotFoundError
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    Services,
    StoredFixtureRequest,
    StoredOddsRequest,
    StoredTeamRequest,
)
from tests.services.contract_suite import OTHER_USER

TASHKENT = ZoneInfo("Asia/Tashkent")
PAGE = 5


class BrowserContract:
    """The read-only database browser: what is stored, paged, filtered and isolated."""

    async def test_overview_describes_the_stored_data(
        self, services: Services, user_id: int
    ) -> None:
        overview = await services.browser.database_overview(user_id)
        tables = {table.name: table for table in overview.tables}
        assert tables["fixtures"].rows > 0
        assert tables["leagues"].rows > 0
        assert tables["signals"].own
        assert not tables["fixtures"].own
        assert overview.size_bytes > 0
        assert overview.earliest_kickoff is not None
        assert overview.latest_kickoff is not None
        assert overview.earliest_kickoff <= overview.latest_kickoff

    async def test_leagues_are_named_ordered_and_counted(
        self, services: Services, user_id: int
    ) -> None:
        leagues = await services.browser.list_stored_leagues(user_id)
        assert [league.name for league in leagues] == sorted(league.name for league in leagues)
        counted = [league for league in leagues if league.fixtures]
        assert counted
        for league in counted:
            assert league.first_kickoff is not None
            assert league.last_kickoff is not None
            assert league.first_kickoff <= league.last_kickoff
            assert 0 <= league.upcoming <= league.fixtures

    async def test_fixtures_are_paged_in_kickoff_order(
        self, services: Services, user_id: int
    ) -> None:
        first = await services.browser.list_stored_fixtures(
            user_id, StoredFixtureRequest(page_size=PAGE)
        )
        assert first.total > PAGE
        assert first.page_count == -(-first.total // PAGE)
        assert len(first.items) == PAGE
        kickoffs = [item.kickoff for item in first.items]
        assert kickoffs == sorted(kickoffs)
        assert all(kickoff.utcoffset() == timedelta(0) for kickoff in kickoffs)
        second = await services.browser.list_stored_fixtures(
            user_id, StoredFixtureRequest(page=1, page_size=PAGE)
        )
        assert {item.fixture_id for item in first.items}.isdisjoint(
            item.fixture_id for item in second.items
        )
        assert second.items[0].kickoff >= first.items[-1].kickoff

    async def test_a_page_past_the_end_is_empty_not_an_error(
        self, services: Services, user_id: int
    ) -> None:
        page = await services.browser.list_stored_fixtures(
            user_id, StoredFixtureRequest(page=10_000, page_size=PAGE)
        )
        assert page.items == ()
        assert page.total > 0

    async def test_the_league_filter_keeps_only_that_league(
        self, services: Services, user_id: int
    ) -> None:
        leagues = await services.browser.list_stored_leagues(user_id)
        chosen = next(league for league in leagues if league.fixtures)
        page = await services.browser.list_stored_fixtures(
            user_id, StoredFixtureRequest(league_ids=frozenset({chosen.league_id}), page_size=20)
        )
        assert page.total == chosen.fixtures
        assert {item.league_id for item in page.items} == {chosen.league_id}
        assert {item.league for item in page.items} == {chosen.name}

    async def test_the_local_date_filter_means_the_users_calendar_day(
        self, services: Services, user_id: int
    ) -> None:
        sample = (
            await services.browser.list_stored_fixtures(
                user_id, StoredFixtureRequest(with_odds=True, page_size=1)
            )
        ).items[0]
        day = sample.kickoff.astimezone(TASHKENT).date()
        page = await services.browser.list_stored_fixtures(
            user_id, StoredFixtureRequest(date_from=day, date_to=day, page_size=20)
        )
        assert sample.fixture_id in {item.fixture_id for item in page.items}
        assert {item.kickoff.astimezone(TASHKENT).date() for item in page.items} == {day}
        before = await services.browser.list_stored_fixtures(
            user_id, StoredFixtureRequest(date_to=day - timedelta(days=1), page_size=20)
        )
        assert sample.fixture_id not in {item.fixture_id for item in before.items}

    async def test_a_slice_without_fixtures_gives_an_empty_page(
        self, services: Services, user_id: int
    ) -> None:
        far = date(2099, 1, 1)
        page = await services.browser.list_stored_fixtures(
            user_id, StoredFixtureRequest(date_from=far, date_to=far)
        )
        assert (page.items, page.total, page.page_count) == ((), 0, 0)

    async def test_the_detail_matches_the_listing(self, services: Services, user_id: int) -> None:
        listing = await services.browser.list_stored_fixtures(
            user_id, StoredFixtureRequest(with_odds=True, page_size=3)
        )
        listed = listing.items[0]
        detail = await services.browser.get_stored_fixture(user_id, listed.fixture_id)
        shown = sum(len(group.quotes) for group in detail.odds)
        assert detail.fixture == listed
        assert detail.odds_total >= shown > 0
        assert detail.truncated == (detail.odds_total > shown)

    @pytest.mark.parametrize("bad_id", ["424242424", "x", "-1"])
    async def test_an_unknown_fixture_is_not_found(
        self, services: Services, user_id: int, bad_id: str
    ) -> None:
        with pytest.raises(NotFoundError):
            await services.browser.get_stored_fixture(user_id, bad_id)
        with pytest.raises(NotFoundError):
            await services.browser.list_stored_odds(user_id, StoredOddsRequest(fixture_id=bad_id))

    async def test_signals_in_the_detail_are_the_askers_own(
        self, services: Services, user_id: int
    ) -> None:
        query = AnalysisQuery(
            dates=DateSpec.next_days(3), min_probability=0.30, min_ev=0.0, max_signals=3
        )
        result = await services.analysis.analyze(user_id, query)
        assert result.signals
        view = result.signals[0]
        mine = await services.browser.get_stored_fixture(user_id, view.fixture_id)
        theirs = await services.browser.get_stored_fixture(OTHER_USER, view.fixture_id)
        assert view.signal_id in {signal.signal_id for signal in mine.signals}
        assert theirs.signals == ()
        own = {t.name: t.rows for t in (await services.browser.database_overview(user_id)).tables}
        other = {
            t.name: t.rows for t in (await services.browser.database_overview(OTHER_USER)).tables
        }
        assert own["signals"] >= len(result.signals)
        assert other["signals"] == 0

    async def test_teams_are_searchable_and_paged(self, services: Services, user_id: int) -> None:
        teams = await services.browser.list_stored_teams(user_id, StoredTeamRequest(page_size=4))
        assert teams.total >= len(teams.items) > 0
        names = [team.name for team in teams.items]
        assert names == sorted(names)
        needle = teams.items[0].name[:4].upper()
        found = await services.browser.list_stored_teams(
            user_id, StoredTeamRequest(text=needle, page_size=20)
        )
        assert teams.items[0].team_id in {team.team_id for team in found.items}
        nothing = await services.browser.list_stored_teams(
            user_id, StoredTeamRequest(text="zzzzqqq-no-such-team")
        )
        assert (nothing.items, nothing.total, nothing.page_count) == ((), 0, 0)

    async def test_odds_are_listed_newest_first_and_filterable(
        self, services: Services, user_id: int
    ) -> None:
        listed = await services.browser.list_stored_fixtures(
            user_id, StoredFixtureRequest(with_odds=True, page_size=3)
        )
        fixture_id = listed.items[0].fixture_id
        page = await services.browser.list_stored_odds(
            user_id, StoredOddsRequest(fixture_id=fixture_id, page_size=PAGE)
        )
        assert page.total > 0
        taken = [quote.taken_at for quote in page.items]
        assert taken == sorted(taken, reverse=True)
        bookmaker = page.items[0].bookmaker
        only = await services.browser.list_stored_odds(
            user_id, StoredOddsRequest(fixture_id=fixture_id, bookmaker=bookmaker, page_size=20)
        )
        assert {quote.bookmaker for quote in only.items} == {bookmaker}
        past_end = await services.browser.list_stored_odds(
            user_id, StoredOddsRequest(fixture_id=fixture_id, page=10_000)
        )
        assert past_end.items == ()
