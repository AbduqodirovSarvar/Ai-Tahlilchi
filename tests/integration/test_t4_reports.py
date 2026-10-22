"""T4 QA (11): what reports count, and the watchlist/league scope rules of the contract.

Run on the full seeded world with counts derived independently (from what was asked and what
came back), not from the report code.
"""

from datetime import timedelta
from pathlib import Path

from app.domain import Sport
from app.services.interfaces import (
    DateSpec,
    FixtureListRequest,
    ReportRequest,
    ScopeKind,
    SignalSource,
    WatchKind,
    WatchTarget,
)
from tests.integration.t4_support import analyse_as, permissive
from tests.pipeline.harness import open_env
from tests.pipeline.world import START

USER_ONE, USER_TWO = 7301, 7302


def exported_ids(content: bytes) -> set[int]:
    """Signal ids in the CSV export (first column, after the header)."""
    lines = content.decode().strip().splitlines()[1:]
    return {int(line.split(",", 1)[0]) for line in lines}


async def test_reports_count_recommended_signals_only_hidden_ones_included_retro_never(
    seeded_template,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    template, world = seeded_template
    async with open_env(template, world, tmp_path / "reports.db") as env:
        services = env.system.services
        await permissive(services, USER_ONE)
        recommended = await analyse_as(services, USER_ONE, DateSpec.next_days(3))
        yesterday = (START - timedelta(days=3)).date()  # the previous round was played then
        retro = await analyse_as(services, USER_ONE, DateSpec.between(yesterday, yesterday))
        shown = await analyse_as(
            services, USER_ONE, DateSpec.next_days(3), include_no_value=True, max_signals=None
        )

        assert recommended.signals
        assert retro.retro
        assert retro.signals
        assert all(s.source is SignalSource.RETRO and not s.recommended for s in retro.signals)
        # a selection counts once any run recommended it; shown-but-not-recommended never does
        expected = {s.signal_id for s in (*recommended.signals, *shown.signals) if s.recommended}
        no_value = {s.signal_id for s in shown.signals if not s.recommended}
        assert no_value  # the custom run created non-recommendations too
        assert expected.isdisjoint(no_value)

        report = await services.reports.report(USER_ONE, ReportRequest.recent(14))
        assert report.signals == len(expected)
        assert exported_ids(await _export(services)) == expected

        for hidden in list(expected)[:2]:
            await services.signals.hide_signal(USER_ONE, hidden)
        after = await services.reports.report(USER_ONE, ReportRequest.recent(14))
        assert after == report  # hiding changes the view, not the books
        assert exported_ids(await _export(services)) == expected
        visible = {
            s.signal_id
            for s in await services.signals.list_signals(USER_ONE, DateSpec.next_days(3))
        }
        assert visible.isdisjoint(list(expected)[:2])
        everything = {
            s.signal_id
            for s in await services.signals.list_signals(
                USER_ONE, DateSpec.next_days(3), include_hidden=True
            )
        }
        assert expected <= everything

        yesterday_summary = await services.results.day_summary(USER_ONE, yesterday)
        assert yesterday_summary.signals == 0  # a look-back is not a result of the user's
        assert not any(
            s.signal_id in {r.signal_id for r in retro.signals}
            for s in await services.results.pending_results(USER_ONE)
        )


async def _export(services) -> bytes:  # type: ignore[no-untyped-def]
    return (await services.reports.export(USER_ONE)).content


async def pick_team(services, name: str) -> str:  # type: ignore[no-untyped-def]
    (team,) = await services.catalog.search_teams(name, limit=1)
    return str(team.team_id)


async def test_watched_fixtures_are_added_outside_the_cut_and_the_matched_count(
    seeded_template,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    template, world = seeded_template
    async with open_env(template, world, tmp_path / "watch.db") as env:
        services = env.system.services
        await permissive(services, USER_ONE)
        await permissive(services, USER_TWO)
        baseline = await analyse_as(services, USER_ONE, DateSpec.today(), max_signals=1)
        everything = await analyse_as(services, USER_ONE, DateSpec.today(), max_signals=None)
        assert len(baseline.signals) == 1
        top_teams = {baseline.signals[0].home_team, baseline.signals[0].away_team}
        # a team that plays today but not in the single best fixture
        outsider = next(
            t
            for t in ("Chelsea", "Barcelona", "Sevilla", "Everton", "Getafe")
            if t not in top_teams
        )
        await services.watchlist.add_watch(
            USER_TWO, WatchTarget(WatchKind.TEAM, await pick_team(services, outsider))
        )
        watched = await analyse_as(services, USER_TWO, DateSpec.today(), max_signals=1)

    flagged = [s for s in watched.signals if s.watchlist]
    regular = [s for s in watched.signals if not s.watchlist]
    assert len(regular) == 1  # the cut still applies to the regular ones
    assert len(flagged) == 1  # the watched fixture is added beyond it
    assert outsider in {flagged[0].home_team, flagged[0].away_team}
    # it is not counted as a match: had it matched before, the count drops by exactly one
    was_a_match = any(outsider in {s.home_team, s.away_team} for s in everything.signals)
    assert watched.matched == baseline.matched - int(was_a_match)
    assert watched.requested == 1
    keys = [(-s.ev, s.kickoff) for s in watched.signals]
    assert keys == sorted(keys)  # the merged list is still in the sort order


async def test_scope_all_honours_the_settings_leagues_and_adds_watched_teams(
    seeded_template,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    template, world = seeded_template
    async with open_env(template, world, tmp_path / "scope.db") as env:
        services = env.system.services
        leagues = {o.name: o.league_id for o in await services.catalog.list_leagues(Sport.FOOTBALL)}
        premier = leagues["Premier League"]
        await permissive(services, USER_ONE, league_ids=frozenset({premier}))
        only_premier = await analyse_as(
            services, USER_ONE, DateSpec.next_days(2), max_signals=None, include_no_value=True
        )
        fixtures = await services.catalog.list_fixtures(
            USER_ONE, FixtureListRequest(dates=DateSpec.today(), page_size=20)
        )

        await services.watchlist.add_watch(
            USER_ONE, WatchTarget(WatchKind.TEAM, await pick_team(services, "Barcelona"))
        )
        with_watch = await analyse_as(
            services, USER_ONE, DateSpec.next_days(2), max_signals=None, include_no_value=True
        )
        explicit = await analyse_as(
            services,
            USER_ONE,
            DateSpec.next_days(2),
            max_signals=None,
            scope=ScopeKind.LEAGUES,
            scope_ids=frozenset({leagues["La Liga"]}),
        )

    assert only_premier.signals
    assert {s.league for s in only_premier.signals} == {"Premier League"}
    assert {item.league for item in fixtures.items} == {"Premier League"}
    spanish = [s for s in with_watch.signals if s.league == "La Liga"]
    assert spanish
    assert all(s.watchlist and "Barcelona" in {s.home_team, s.away_team} for s in spanish)
    assert {s.league for s in explicit.signals} <= {"La Liga"}  # an explicit scope overrides
    assert explicit.signals
