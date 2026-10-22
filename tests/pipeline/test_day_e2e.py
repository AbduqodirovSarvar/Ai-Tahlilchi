"""The "fake day": refresh -> daily digest -> pre-match update -> results -> settlement -> report.

A scripted provider plays API-Football over a database that only holds history. Everything is
driven by an injected clock; nothing sleeps. The expected profit is recomputed here with plain
arithmetic from the final scores, independently of the code under test.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain import MarketType, Selection
from app.pipeline.daily import run_due_digests
from app.pipeline.prematch import run_due_recalcs
from app.pipeline.refresh import refresh
from app.pipeline.settle import run_settlement
from app.services.interfaces import (
    BetDraft,
    DateSpec,
    ReportRequest,
    ResultMark,
    SignalView,
)
from tests.pipeline.harness import Env, open_env
from tests.pipeline.providers import scripted_provider
from tests.pipeline.world import World

USER = 1001


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


async def configure(env: Env) -> None:
    settings = await env.system.services.settings.get_settings(USER)
    await env.system.services.settings.update_settings(
        USER,
        replace(
            settings,
            min_probability=0.30,
            min_ev=0.02,
            quiet_hours=None,
            bank=Decimal(1000),
            unit=Decimal(10),
        ),
    )


def won(view: SignalView, home: int, away: int) -> bool:  # noqa: PLR0911 - one branch per market
    """Plain-arithmetic settlement of the markets the world prices (half lines only)."""
    market, selection = view.market, view.selection
    if market.type is MarketType.MATCH_RESULT:
        if home == away:
            return selection is Selection.DRAW
        return selection is (Selection.HOME if home > away else Selection.AWAY)
    if market.type is MarketType.DOUBLE_CHANCE:
        if selection is Selection.HOME_OR_DRAW:
            return home >= away
        if selection is Selection.DRAW_OR_AWAY:
            return away >= home
        return home != away
    if market.type is MarketType.TOTALS:
        return (home + away > 2.5) == (selection is Selection.OVER)
    if market.type is MarketType.BTTS:
        return (home > 0 and away > 0) == (selection is Selection.YES)
    return (home - away - 0.5 > 0) == (selection is Selection.HOME)  # asian handicap -0.5


async def test_a_whole_day(env: Env) -> None:  # noqa: PLR0915 - one scripted day
    services, notifier, clock = env.system.services, env.notifier, env.clock
    await configure(env)

    # 08:30 refresh: fixtures of three days and the morning odds; a second run is a no-op.
    first = await refresh(env.ctx)
    assert first.ok
    assert first.fixtures > 0
    assert first.odds > 0
    assert (await refresh(env.ctx)).already_done

    # The user's digest: one message, signals stored, recalculations queued.
    report = await run_due_digests(env.ctx, env.system.delivery)
    assert report.sent == (USER,)
    assert len([s for s in notifier.sent if s.method == "daily_digest"]) == 1
    signals = await services.signals.list_signals(USER, DateSpec.next_days(3))
    assert signals
    assert all(s.recommended and not s.retro for s in signals)

    # Idempotency: the same tick again sends nothing and creates nothing new.
    again = await run_due_digests(env.ctx, env.system.delivery)
    assert again.sent == ()
    assert len([s for s in notifier.sent if s.method == "daily_digest"]) == 1
    assert await services.signals.list_signals(USER, DateSpec.next_days(3)) == signals

    # A bet with a client token, tapped twice, is recorded once.
    draft = BetDraft(signal_id=signals[0].signal_id, client_token="tap-1")
    bet = await services.bets.place_bet(USER, draft)
    assert await services.bets.place_bet(USER, draft) == bet
    assert bet.stake == Decimal(10) * signals[0].stake_units

    # The minute ticks of the scheduler: every signal gets one pre-match update, once.
    notified = 0
    for _ in range(24):
        clock.advance(timedelta(minutes=30))
        prematch = await run_due_recalcs(env.ctx, env.system.delivery)
        assert prematch.failed == 0
        notified += prematch.notified
    updates = [s for s in notifier.sent if s.method == "signal_update"]
    assert notified == len(updates) == len(signals)
    assert {u.key.subject for u in updates if u.key} == {str(s.signal_id) for s in signals}
    assert (await run_due_recalcs(env.ctx, env.system.delivery)).notified == 0

    # The matches end; settlement stores outcomes and tells the user once per signal.
    clock.advance(timedelta(hours=2))
    settle = await run_settlement(env.ctx, env.system.delivery)
    assert settle.settled == len(signals)
    assert settle.waiting == 0
    assert (await run_settlement(env.ctx, env.system.delivery)).settled == 0
    assert len([s for s in notifier.sent if s.method == "result_settled"]) == len(signals)

    scores = {
        (f.home.name, f.away.name): env.world.feed[f.ref]
        for f in env.world.fixtures
        if f.ref in env.world.feed
    }
    settled = await services.signals.get_signals(USER, [s.signal_id for s in signals])
    expected_profit = Decimal(0)
    expected_wins = 0
    for view in settled:
        result = scores[(view.home_team, view.away_team)]
        assert result.home_score is not None
        assert result.away_score is not None
        is_win = won(view, result.home_score, result.away_score)
        assert view.result is (ResultMark.WON if is_win else ResultMark.LOST)
        expected_wins += is_win
        stake = view.stake_units
        expected_profit += stake * (Decimal(repr(view.odds)) - 1) if is_win else -stake

    summary = await services.reports.report(USER, ReportRequest.recent(14))
    assert (summary.signals, summary.settled) == (len(signals), len(signals))
    assert (summary.won, summary.lost) == (expected_wins, len(signals) - expected_wins)
    assert summary.profit_units == expected_profit.quantize(Decimal("0.0001"))
    staked = sum((s.stake_units for s in signals), Decimal(0))
    assert summary.yield_rate == pytest.approx(float(expected_profit / staked))
    assert summary.clv is not None  # a pre-match price was stored for the signals

    # The user's bet was settled with its own stake and odds.
    (settled_bet,) = await services.bets.list_bets(USER)
    first_view = next(v for v in settled if v.signal_id == bet.signal_id)
    expected_bet = (
        bet.stake * (Decimal(repr(bet.odds_taken)) - 1)
        if first_view.result is ResultMark.WON
        else -bet.stake
    )
    assert settled_bet.outcome is first_view.result
    assert settled_bet.profit == expected_bet
    bank = await services.bets.bank_summary(USER)
    assert bank.balance == Decimal(1000) + expected_bet
    assert bank.open_bets == 0
