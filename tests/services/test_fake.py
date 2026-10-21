"""Behaviour specific to the fake (budget, cache, sample data), beyond the shared contract."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.domain import Market, MarketType, Selection, Sport
from app.services._fake_metrics import profit_units
from app.services._fake_world import FakeQuote, settle, unit_interval
from app.services.errors import BudgetExceededError
from app.services.fake import (
    CACHE_TTL,
    FAKE_AUTOMATIC_USAGE,
    FAKE_MANUAL_RESERVE,
    create_fake_services,
    system_clock,
)
from app.services.interfaces import (
    PRELIMINARY_AFTER,
    RECALC_COOLDOWN,
    AnalysisQuery,
    BetDraft,
    ConfidenceLevel,
    DateSpec,
    ReportGroup,
    ReportRequest,
    ResultMark,
    ResultMarkRequest,
    Services,
    SignalView,
)
from tests.services.contract_suite import MutableClock, analyze_week, retro_signals

USER = 7


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def services(clock: MutableClock) -> Services:
    return create_fake_services(clock=clock)


async def test_same_clock_gives_the_same_world(clock: MutableClock) -> None:
    first = await analyze_week(create_fake_services(clock=clock), USER)
    second = await analyze_week(create_fake_services(clock=clock), USER)
    assert first == second


def test_system_clock_is_utc() -> None:
    assert system_clock().tzinfo is UTC
    assert create_fake_services().status is not None


async def test_cached_data_is_not_charged_twice(services: Services) -> None:
    query = AnalysisQuery(dates=DateSpec.next_days(7))
    first = await services.analysis.analyze(USER, query)
    estimate = await services.analysis.estimate_cost(USER, query)

    assert first.api_requests_used > 0
    assert estimate.api_requests == 0
    assert estimate.budget_remaining == FAKE_MANUAL_RESERVE - first.api_requests_used


async def test_budget_exhaustion(services: Services, clock: MutableClock) -> None:
    signal = (await analyze_week(services, USER)).signals[0]
    while (
        await services.analysis.estimate_cost(USER, AnalysisQuery(dates=DateSpec.today()))
    ).budget_remaining:
        await services.analysis.recalc_signal(USER, signal.signal_id)
        clock.advance(RECALC_COOLDOWN)

    stale = await services.analysis.recalc_signal(USER, signal.signal_id)
    assert stale.signal.data_as_of is not None
    assert stale.signal.data_as_of < clock()
    assert not stale.changed

    clock.advance(CACHE_TTL)
    with pytest.raises(BudgetExceededError) as excinfo:
        await services.analysis.analyze(USER, AnalysisQuery(dates=DateSpec.next_days(14)))
    assert excinfo.value.available == 0
    assert excinfo.value.needed > 0
    status = await services.status.system_status()
    assert status.quotas[0].used == FAKE_AUTOMATIC_USAGE + FAKE_MANUAL_RESERVE


async def test_custom_analysis_includes_hockey_marked_experimental(services: Services) -> None:
    result = await services.analysis.analyze(
        USER,
        AnalysisQuery(
            dates=DateSpec.next_days(3),
            sports=frozenset({Sport.HOCKEY}),
            markets=frozenset(MarketType),
            include_no_value=True,
        ),
    )
    assert result.signals
    assert all(signal.experimental for signal in result.signals)
    assert {signal.market.type for signal in result.signals} == {
        MarketType.MATCH_RESULT,
        MarketType.TOTALS,
    }


async def test_far_signals_are_preliminary_with_lower_confidence(
    services: Services, clock: MutableClock
) -> None:
    result = await analyze_week(services, USER, dates=DateSpec.next_days(14))
    far = [s for s in result.signals if s.kickoff - clock() > PRELIMINARY_AFTER]
    near = [s for s in result.signals if s.kickoff - clock() <= PRELIMINARY_AFTER]
    assert far
    assert near
    assert all(signal.preliminary for signal in far)
    assert not any(signal.preliminary for signal in near)
    assert all(signal.confidence is not ConfidenceLevel.HIGH for signal in far)


async def test_value_signals_are_reviewed(services: Services) -> None:
    result = await analyze_week(services, USER)
    assert all(signal.llm_verdict is not None and signal.llm_note for signal in result.signals)
    assert all(signal.stake_units > 0 for signal in result.signals)


async def test_recalc_query_handles_several_signals_of_one_fixture(services: Services) -> None:
    custom = await services.analysis.analyze(
        USER,
        AnalysisQuery(
            dates=DateSpec.tomorrow(), markets=frozenset(MarketType), include_no_value=True
        ),
    )
    batch = await services.analysis.recalc_query(USER, custom.query_id)
    assert len(batch.diffs) == len(custom.signals)
    assert batch.skipped_cooldown == ()


async def test_recalc_query_skips_started_matches(services: Services) -> None:
    retro = await services.analysis.analyze(
        USER,
        AnalysisQuery(
            dates=DateSpec.between(date(2026, 10, 15), date(2026, 10, 20)),
            min_ev=0.0,
        ),
    )
    batch = await services.analysis.recalc_query(USER, retro.query_id)
    assert batch.diffs == ()
    assert set(batch.skipped_started) == {s.signal_id for s in retro.signals}


async def test_detail_of_finished_reported_fixture_has_a_score(services: Services) -> None:
    settled = next(s for s in await retro_signals(services, USER) if s.result)
    detail = await services.analysis.fixture_detail(settled.fixture_id)
    assert detail.home_score is not None
    assert detail.away_score is not None


async def test_loss_limit_and_bank_history(services: Services) -> None:
    settings = await services.settings.get_settings(USER)
    await services.settings.update_settings(
        USER,
        replace(settings, bank=Decimal(100), unit=Decimal(1), daily_loss_limit=Decimal(1)),
    )
    signals = await retro_signals(services, USER)
    lost = next(s for s in signals if s.result is ResultMark.LOST)
    won = next(s for s in signals if s.result is ResultMark.WON)
    for signal in (lost, won):
        await services.bets.place_bet(
            USER, BetDraft(signal_id=signal.signal_id, odds_taken=2.0, stake=Decimal(5))
        )

    bank = await services.bets.bank_summary(USER)

    assert bank.balance == Decimal(100)
    assert bank.history
    assert bank.open_bets == 0
    assert bank.loss_today == 0
    assert bank.month_profit == 0


async def test_cashout_bet_profit_scales_with_stake(services: Services) -> None:
    signal = next(s for s in await analyze_week_past(services) if s.stake_units > 0)
    await services.results.mark_result(
        USER,
        ResultMarkRequest(
            signal_id=signal.signal_id,
            mark=ResultMark.CASHOUT,
            cashout_profit_units=signal.stake_units,
        ),
    )
    bet = await services.bets.place_bet(
        USER, BetDraft(signal_id=signal.signal_id, stake=Decimal(20))
    )
    assert bet.outcome is ResultMark.CASHOUT
    assert bet.profit == Decimal(20)


async def analyze_week_past(services: Services) -> tuple[SignalView, ...]:
    result = await services.analysis.analyze(
        USER,
        AnalysisQuery(
            dates=DateSpec.between(date(2026, 10, 14), date(2026, 10, 20)),
            min_ev=0.0,
            markets=frozenset(MarketType),
        ),
    )
    return result.signals


async def test_report_by_every_group(services: Services, clock: MutableClock) -> None:
    await analyze_week_past(services)
    for group in ReportGroup:
        report = await services.reports.report(
            USER, ReportRequest.last_days(14, clock().date(), group_by=group)
        )
        assert sum(segment.signals for segment in report.segments) == report.settled


async def test_refresh_time(services: Services, clock: MutableClock) -> None:
    status = await services.status.system_status()
    assert status.last_refresh_at == datetime(2026, 10, 21, 3, 30, tzinfo=UTC)
    clock.now = datetime(2026, 10, 21, 2, 0, tzinfo=UTC)
    earlier = await services.status.system_status()
    assert earlier.last_refresh_at == datetime(2026, 10, 20, 3, 30, tzinfo=UTC)


def quote(market: Market, selection: Selection) -> FakeQuote:
    return FakeQuote(market=market, selection=selection, probability=0.5, odds=2.0)


@pytest.mark.parametrize(
    ("market", "selection", "score", "expected"),
    [
        (Market.match_result(), Selection.HOME, (2, 1), ResultMark.WON),
        (Market.match_result(), Selection.DRAW, (1, 1), ResultMark.WON),
        (Market.match_result(), Selection.AWAY, (1, 1), ResultMark.LOST),
        (Market.double_chance(), Selection.DRAW_OR_AWAY, (0, 2), ResultMark.WON),
        (Market.double_chance(), Selection.HOME_OR_AWAY, (0, 0), ResultMark.LOST),
        (Market.totals(2.5), Selection.OVER, (2, 1), ResultMark.WON),
        (Market.totals(2.5), Selection.UNDER, (2, 1), ResultMark.LOST),
        (Market.btts(), Selection.YES, (1, 0), ResultMark.LOST),
        (Market.btts(), Selection.NO, (1, 0), ResultMark.WON),
        (Market.asian_handicap(-0.5), Selection.HOME, (1, 1), ResultMark.LOST),
        (Market.asian_handicap(-0.5), Selection.AWAY, (1, 1), ResultMark.WON),
    ],
)
def test_settlement(
    market: Market, selection: Selection, score: tuple[int, int], expected: ResultMark
) -> None:
    assert settle(quote(market, selection), *score) is expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, None),
        (ResultMark.WON, Decimal("1.70")),
        (ResultMark.HALF_WON, Decimal("0.850")),
        (ResultMark.LOST, Decimal(-2)),
        (ResultMark.HALF_LOST, Decimal(-1)),
        (ResultMark.VOID, Decimal(0)),
        (ResultMark.CASHOUT, Decimal("0.3")),
    ],
)
def test_profit_units(result: ResultMark | None, expected: Decimal | None) -> None:
    view = SignalView(
        signal_id=1,
        fixture_id="f",
        sport=Sport.FOOTBALL,
        league="L",
        home_team="A",
        away_team="B",
        kickoff=datetime(2026, 10, 1, tzinfo=UTC),
        market=Market.btts(),
        selection=Selection.YES,
        odds=1.85,
        bookmaker="pinnacle",
        probability=0.6,
        ev=0.11,
        confidence=ConfidenceLevel.HIGH,
        stake_units=Decimal(2),
        result=result,
    )
    assert profit_units(view, Decimal("0.3")) == expected


def test_unit_interval_is_stable_and_bounded() -> None:
    values = [unit_interval("fixture", index) for index in range(200)]
    assert values == [unit_interval("fixture", index) for index in range(200)]
    assert all(0 <= value < 1 for value in values)
    assert len(set(values)) == len(values)
