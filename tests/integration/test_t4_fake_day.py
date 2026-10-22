"""T4 QA: the "fake day" with its prices and settlement checked against the world's own data.

``tests/pipeline/test_day_e2e.py`` recomputes profit from the stake and odds the service
reports. Here the odds are checked against the price list the scripted provider serves (an
independent source), the EV identity is re-derived, and every outcome is derived from the
final scores of the world's hidden feed.
"""

from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain import BOOKMAKER_1XBET, MarketType, Selection
from app.pipeline.daily import run_due_digests
from app.pipeline.refresh import refresh
from app.pipeline.settle import run_settlement
from app.services.interfaces import DateSpec, ReportRequest, ResultMark
from tests.integration.t4_support import permissive
from tests.integration.test_t4_settlement import own_result
from tests.pipeline.harness import Env, open_env
from tests.pipeline.providers import scripted_provider
from tests.pipeline.world import World

USER = 7401


@pytest.fixture
async def env(history_template: tuple[Path, World], tmp_path: Path) -> AsyncIterator[Env]:
    template, world = history_template
    async with open_env(
        template,
        world,
        tmp_path / "day.db",
        provider_factory=lambda clock: scripted_provider(world, clock),
        reveal_results=False,
    ) as opened:
        yield opened


def one_x_bet_prices(world: World) -> dict[tuple[str, str, object, Selection], float]:
    """The 1xBet price list of the world, by team names (the provider uses other ids)."""
    names = {f.ref: (f.home.name, f.away.name) for f in world.fixtures}
    return {
        (*names[s.fixture], s.market, s.selection): s.odds
        for s in world.odds
        if s.bookmaker == BOOKMAKER_1XBET
    }


async def test_every_signal_of_the_day_is_priced_by_the_users_bookmaker_and_settles_correctly(
    env: Env,
) -> None:
    services = env.system.services
    await permissive(services, USER, min_ev=0.02)
    assert (await refresh(env.ctx)).ok
    await run_due_digests(env.ctx, env.system.delivery)
    signals = await services.signals.list_signals(USER, DateSpec.next_days(3))
    assert len(signals) >= 3

    prices = one_x_bet_prices(env.world)
    for signal in signals:
        key = (signal.home_team, signal.away_team, signal.market, signal.selection)
        assert signal.bookmaker == BOOKMAKER_1XBET
        assert signal.odds == prices[key]
        assert signal.ev == pytest.approx(signal.probability * signal.odds - 1, abs=1e-9)
        assert signal.ev >= 0.02
        assert signal.stake_units == Decimal(1)  # flat staking
        assert signal.market.type in {
            MarketType.MATCH_RESULT,
            MarketType.DOUBLE_CHANCE,
            MarketType.TOTALS,
            MarketType.BTTS,
            MarketType.ASIAN_HANDICAP,
        }

    env.clock.advance(timedelta(hours=1))  # not over yet: nothing settles
    assert (await run_settlement(env.ctx, env.system.delivery)).settled == 0
    env.clock.advance(timedelta(hours=20))
    report = await run_settlement(env.ctx, env.system.delivery)
    assert report.settled == len(signals)

    scores = {
        (f.home.name, f.away.name): env.world.feed[f.ref]
        for f in env.world.fixtures
        if f.ref in env.world.feed
    }
    settled = await services.signals.get_signals(USER, [s.signal_id for s in signals])
    profit = Decimal(0)
    for view in settled:
        final = scores[view.home_team, view.away_team]
        assert final.home_score is not None
        assert final.away_score is not None
        won = own_result(view.market, view.selection, final.home_score, final.away_score) == "win"
        assert view.result is (ResultMark.WON if won else ResultMark.LOST)
        profit += Decimal(str(view.odds)) - 1 if won else Decimal(-1)

    summary = await services.reports.report(USER, ReportRequest.recent(14))
    assert summary.profit_units == profit
