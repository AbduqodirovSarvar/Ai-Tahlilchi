"""T4 QA (2): settlement of every outcome type, profit units, the Decimal bank and CLV.

The expected outcome and profit of each case are derived here from first principles (a
handicap or total is split into half stakes on its two neighbouring lines), never by calling
``settle_selection``. Profit is compared as an exact ``Decimal`` against
``stake * (Decimal(str(odds)) - 1) * share``.
"""

import math
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.db.enums import Outcome
from app.db.repo import get_signal, upsert_live
from app.db.types import MONEY_QUANTUM
from app.domain import (
    BOOKMAKER_1XBET,
    BOOKMAKER_PINNACLE,
    Fixture,
    FixtureResult,
    HistoryBatch,
    Market,
    MarketType,
    OddsKind,
    ResultStatus,
    Selection,
)
from app.models import Settlement, settle_selection
from app.pipeline.delivery import NullDelivery
from app.pipeline.settle import profit_of, run_settlement
from app.services.interfaces import BetDraft
from tests.integration.t4_support import (
    USER,
    Bare,
    add_signal,
    finished,
    make_fixture,
    open_bare,
    snapshot,
    utc,
    x12,
)

KICKOFF = utc(2026, 10, 21, 10)
BEFORE_KICKOFF = utc(2026, 10, 21, 8)
AFTER_MATCH = utc(2026, 10, 21, 14)
WIN, HALF_WIN, PUSH, HALF_LOSS, LOSS = "win", "half_win", "push", "half_loss", "loss"
OUTCOME = {
    WIN: Outcome.WIN,
    HALF_WIN: Outcome.HALF_WIN,
    PUSH: Outcome.VOID,
    HALF_LOSS: Outcome.HALF_LOSS,
    LOSS: Outcome.LOSS,
}
SHARE = {
    WIN: Decimal(1),
    HALF_WIN: Decimal("0.5"),
    PUSH: Decimal(0),
    HALF_LOSS: Decimal("-0.5"),
    LOSS: Decimal(-1),
}
BET_STAKE = Decimal("12.50")
START_BANK = Decimal("1000.00")


# ------------------------------------------------------------------------------ the oracle
def sign(value: float) -> int:
    """+1 win, 0 push, -1 loss for one half stake whose margin after the line is ``value``."""
    return int(math.copysign(1, value)) if value else 0


def combined(parts: list[int]) -> str:
    return {1.0: WIN, 0.5: HALF_WIN, 0.0: PUSH, -0.5: HALF_LOSS, -1.0: LOSS}[
        sum(parts) / len(parts)
    ]


def half_stakes(line: float) -> list[float]:
    """A quarter line is two half stakes on the neighbouring lines; others are one stake."""
    return [line - 0.25, line + 0.25] if round(line * 4) % 2 else [line]


def own_result(market: Market, selection: Selection, home: int, away: int) -> str:
    """Independent oracle: how one selection settles at the regular-time score."""
    kind = market.type
    if kind is MarketType.MATCH_RESULT:
        wins = {
            Selection.HOME: home > away,
            Selection.DRAW: home == away,
            Selection.AWAY: home < away,
        }
        return WIN if wins[selection] else LOSS
    if kind is MarketType.DOUBLE_CHANCE:
        wins = {
            Selection.HOME_OR_DRAW: home >= away,
            Selection.DRAW_OR_AWAY: home <= away,
            Selection.HOME_OR_AWAY: home != away,
        }
        return WIN if wins[selection] else LOSS
    if kind is MarketType.BTTS:
        return WIN if (home > 0 and away > 0) == (selection is Selection.YES) else LOSS
    assert market.line is not None
    if kind is MarketType.TOTALS:
        direction = 1 if selection is Selection.OVER else -1
        return combined([sign(direction * (home + away - p)) for p in half_stakes(market.line)])
    if selection is Selection.HOME:
        return combined([sign((home - away) + p) for p in half_stakes(market.line)])
    return combined([sign((away - home) - p) for p in half_stakes(market.line)])


@dataclass(frozen=True)
class Case:
    name: str
    market: Market
    selection: Selection
    odds: float
    stake: str
    score: tuple[int, int]
    expected: str  # written by hand, checked against the oracle below


def case(  # noqa: PLR0913, PLR0917 - one column per field
    name: str,
    market: Market,
    selection: Selection,
    odds: float,
    score: tuple[int, int],
    expected: str,
) -> Case:
    return Case(name, market, selection, odds, "2.0", score, expected)


X12, DC, BTTS = Market.match_result(), Market.double_chance(), Market.btts()
CASES = [
    case("1x2-win", X12, Selection.HOME, 2.10, (2, 1), WIN),
    case("1x2-draw-win", X12, Selection.DRAW, 3.40, (0, 0), WIN),
    case("1x2-loss", X12, Selection.HOME, 1.91, (0, 1), LOSS),
    case("dc-win", DC, Selection.HOME_OR_DRAW, 1.35, (1, 1), WIN),
    case("btts-loss", BTTS, Selection.YES, 1.87, (0, 2), LOSS),
    case("over-2.5-win", Market.totals(2.5), Selection.OVER, 1.91, (2, 1), WIN),
    case("under-2.5-loss", Market.totals(2.5), Selection.UNDER, 1.95, (2, 1), LOSS),
    case("over-2.0-push", Market.totals(2.0), Selection.OVER, 1.83, (1, 1), PUSH),
    case("over-2.25-half-loss", Market.totals(2.25), Selection.OVER, 1.88, (2, 0), HALF_LOSS),
    case("over-2.75-half-win", Market.totals(2.75), Selection.OVER, 1.97, (2, 1), HALF_WIN),
    case("under-2.75-half-loss", Market.totals(2.75), Selection.UNDER, 1.97, (2, 1), HALF_LOSS),
    case(
        "ah-0.25-half-loss", Market.asian_handicap(-0.25), Selection.HOME, 1.95, (0, 0), HALF_LOSS
    ),
    case("ah-0.75-half-win", Market.asian_handicap(-0.75), Selection.HOME, 2.05, (1, 0), HALF_WIN),
    case("ah-0.0-push", Market.asian_handicap(0.0), Selection.HOME, 1.80, (1, 1), PUSH),
    case(
        "ah-0.25-away-half-win",
        Market.asian_handicap(-0.25),
        Selection.AWAY,
        1.93,
        (1, 1),
        HALF_WIN,
    ),
    case("ah-1.0-home-push", Market.asian_handicap(-1.0), Selection.HOME, 2.20, (2, 1), PUSH),
    case("ah-1.5-home-loss", Market.asian_handicap(-1.5), Selection.HOME, 2.60, (2, 1), LOSS),
    case("ah+0.5-away-win", Market.asian_handicap(0.5), Selection.AWAY, 1.99, (0, 1), WIN),
]


def test_the_oracle_agrees_with_the_hand_checked_table() -> None:
    for item in CASES:
        assert own_result(item.market, item.selection, *item.score) == item.expected, item.name


def expected_profit(stake: Decimal, odds: float, result: str) -> Decimal:
    """Exact: ``stake * (odds - 1) * share`` for wins, ``-stake`` shares for losses."""
    if result in {WIN, HALF_WIN}:
        return stake * (Decimal(str(odds)) - 1) * SHARE[result]
    return stake * SHARE[result]


def bet_odds(odds: float) -> float:
    """The user took a slightly different price than the signal's."""
    return round(odds + 0.05, 2)


# ------------------------------------------------------------------------------ the system
@pytest.fixture
async def settled(tmp_path: Path):  # type: ignore[no-untyped-def]
    fixtures = {item.name: make_fixture(item.name, KICKOFF) for item in CASES}
    batch = HistoryBatch(fixtures=tuple(fixtures.values()))
    async with open_bare(tmp_path / "settle.db", batch, now=BEFORE_KICKOFF) as bare:
        services = bare.system.services
        view = await services.settings.get_settings(USER)
        await services.settings.update_settings(
            USER, replace(view, bank=START_BANK, unit=Decimal(10), quiet_hours=None)
        )
        signals, bets = {}, {}
        for item in CASES:
            record = await add_signal(
                bare,
                fixtures[item.name],
                item.market,
                item.selection,
                item.odds,
                Decimal(item.stake),
            )
            signals[item.name] = record.id
            bets[item.name] = await services.bets.place_bet(
                USER,
                BetDraft(signal_id=record.id, odds_taken=bet_odds(item.odds), stake=BET_STAKE),
            )
        bare.clock.now = AFTER_MATCH
        results = [finished(fixtures[item.name], *item.score) for item in CASES]
        async with bare.sessions.begin() as session:
            await upsert_live(session, results=results)
        report = await run_settlement(bare.ctx, NullDelivery())
        yield bare, signals, bets, report


async def test_every_outcome_type_and_the_exact_profit(settled) -> None:  # type: ignore[no-untyped-def]
    bare, signals, _, _ = settled
    wrong = {}
    async with bare.sessions() as session:
        for item in CASES:
            record = await get_signal(session, signals[item.name])
            assert record is not None
            assert record.result is not None, item.name
            got = (record.result.outcome, record.result.profit_units)
            want = (
                OUTCOME[item.expected],
                expected_profit(Decimal(item.stake), item.odds, item.expected),
            )
            if got != want:
                wrong[item.name] = (got, want)

    assert wrong == {}


async def test_all_cases_are_settled_in_one_run(settled) -> None:  # type: ignore[no-untyped-def]
    _, _, _, report = settled
    assert (report.settled, report.waiting) == (len(CASES), 0)


async def test_the_users_own_bet_settles_at_its_own_price_and_stake(settled) -> None:  # type: ignore[no-untyped-def]
    bare, _, bets, _ = settled
    listed = {b.bet_id: b for b in await bare.system.services.bets.list_bets(USER, limit=100)}
    wrong = {}
    for item in CASES:
        bet = listed[bets[item.name].bet_id]
        want = expected_profit(BET_STAKE, bet_odds(item.odds), item.expected)
        if (bet.profit, bet.stake, bet.odds_taken) != (want, BET_STAKE, bet_odds(item.odds)):
            wrong[item.name] = (bet.profit, want)

    assert wrong == {}


async def test_the_decimal_bank_is_exactly_the_start_plus_every_settled_bet(settled) -> None:  # type: ignore[no-untyped-def]
    bare, _, _, _ = settled
    total = sum(
        (expected_profit(BET_STAKE, bet_odds(c.odds), c.expected) for c in CASES), Decimal(0)
    )
    bank = await bare.system.services.bets.bank_summary(USER)
    assert bank.balance == START_BANK + total
    assert bank.open_bets == 0
    assert bank.history[-1].balance == START_BANK + total


async def test_a_second_settlement_changes_nothing(settled) -> None:  # type: ignore[no-untyped-def]
    bare, signals, _, _ = settled
    before = {}
    async with bare.sessions() as session:
        for name, signal_id in signals.items():
            record = await get_signal(session, signal_id)
            assert record is not None
            before[name] = record.result
    again = await run_settlement(bare.ctx, NullDelivery())
    assert (again.settled, again.waiting) == (0, 0)
    async with bare.sessions() as session:
        for name, signal_id in signals.items():
            record = await get_signal(session, signal_id)
            assert record is not None
            assert record.result == before[name]


# ------------------------------------------------------------------------------ unusual ends
@dataclass(frozen=True)
class Odd:
    name: str
    result: ResultStatus
    score: tuple[int | None, int | None]


async def settle_one(tmp_path: Path, odd: Odd) -> tuple[Bare, int, object]:
    """One HOME 1X2 signal on a fixture that ends in an unusual way."""
    fixture = make_fixture(odd.name, KICKOFF)
    async with open_bare(
        tmp_path / f"{odd.name}.db", HistoryBatch(fixtures=(fixture,)), now=BEFORE_KICKOFF
    ) as bare:
        record = await add_signal(bare, fixture, X12, Selection.HOME, 2.0, Decimal(1))
        bare.clock.now = AFTER_MATCH
        outcome = FixtureResult(
            fixture=fixture.ref, status=odd.result, home_score=odd.score[0], away_score=odd.score[1]
        )
        async with bare.sessions.begin() as session:
            await upsert_live(session, results=[outcome])
        report = await run_settlement(bare.ctx, NullDelivery())
        async with bare.sessions() as session:
            stored = await get_signal(session, record.id)
        assert stored is not None
        return bare, record.id, (report, stored)


async def test_a_postponed_match_waits_and_stays_open(tmp_path: Path) -> None:
    _, _, (report, stored) = await settle_one(
        tmp_path, Odd("postponed", ResultStatus.POSTPONED, (None, None))
    )
    assert (report.settled, report.waiting) == (0, 1)
    assert stored.result is None


async def test_a_cancelled_match_is_void_with_zero_profit(tmp_path: Path) -> None:
    _, _, (report, stored) = await settle_one(
        tmp_path, Odd("cancelled", ResultStatus.CANCELLED, (None, None))
    )
    assert report.settled == 1
    assert stored.result is not None
    assert stored.result.outcome is Outcome.VOID
    assert stored.result.profit_units == 0


async def test_an_abandoned_match_without_a_score_is_void(tmp_path: Path) -> None:
    _, _, (_, stored) = await settle_one(
        tmp_path, Odd("abandoned", ResultStatus.ABANDONED, (None, None))
    )
    assert stored.result is not None
    assert stored.result.outcome is Outcome.VOID


async def test_an_abandoned_match_with_a_partial_score_is_void(tmp_path: Path) -> None:
    _, _, (_, stored) = await settle_one(
        tmp_path, Odd("abandoned-score", ResultStatus.ABANDONED, (2, 0))
    )
    assert stored.result is not None
    assert stored.result.outcome is Outcome.VOID
    assert stored.result.profit_units == 0


# ------------------------------------------------------------------------------ CLV
CLOSING_AT = utc(2026, 10, 21, 9, 30)


def clv_fixture(name: str) -> Fixture:
    return make_fixture(name, KICKOFF)


async def clv_of_signal(
    tmp_path: Path, name: str, prices: list, taken: float, selection: Selection = Selection.HOME
) -> tuple[float | None, float | None]:
    fixture = clv_fixture(name)
    batch = HistoryBatch(fixtures=(fixture,), odds=tuple(prices(fixture)))
    async with open_bare(tmp_path / f"{name}.db", batch, now=BEFORE_KICKOFF) as bare:
        record = await add_signal(bare, fixture, X12, selection, taken, Decimal(1))
        bare.clock.now = AFTER_MATCH
        async with bare.sessions.begin() as session:
            await upsert_live(session, results=[finished(fixture, 1, 0)])
        await run_settlement(bare.ctx, NullDelivery())
        async with bare.sessions() as session:
            stored = await get_signal(session, record.id)
    assert stored is not None
    assert stored.result is not None
    return stored.result.closing_odds, stored.result.clv


def pinnacle(
    fixture: Fixture, prices: dict[Selection, float], kind: OddsKind, hour: int, minute: int = 0
):  # type: ignore[no-untyped-def]
    return snapshot(
        fixture,
        BOOKMAKER_PINNACLE,
        X12,
        prices,
        kind=kind,
        taken_at=utc(2026, 10, 21, hour, minute),
    )


# Margin-free prices: 1/1.6 + 1/4 + 1/8 = 1, so the de-vigged fair probability of the home
# win is exactly 0.625 (and exactly 0.5 for 2.0 / 4.0 / 4.0) whatever the de-vig method is.
FAIR_PRICES = x12(1.6, 4.0, 8.0)
EARLIER_PRICES = x12(2.0, 4.0, 4.0)


async def test_clv_uses_the_last_prematch_snapshot_not_an_earlier_one(tmp_path: Path) -> None:
    def prices(fixture: Fixture):  # type: ignore[no-untyped-def]
        return [
            *pinnacle(fixture, EARLIER_PRICES, OddsKind.PREMATCH, 8, 0),
            *pinnacle(fixture, FAIR_PRICES, OddsKind.PREMATCH, 9, 30),
        ]

    _, clv = await clv_of_signal(tmp_path, "clv-last", prices, 2.0)  # type: ignore[arg-type]
    assert clv == pytest.approx(math.log(2.0 * 0.625), abs=1e-9)  # ln(1.25), not ln(1.0)


async def test_a_closing_snapshot_beats_a_prematch_one(tmp_path: Path) -> None:
    def prices(fixture: Fixture):  # type: ignore[no-untyped-def]
        return [
            *pinnacle(fixture, FAIR_PRICES, OddsKind.PREMATCH, 9, 30),
            *pinnacle(fixture, EARLIER_PRICES, OddsKind.CLOSING, 9, 59),
        ]

    _, clv = await clv_of_signal(tmp_path, "clv-closing", prices, 2.0)  # type: ignore[arg-type]
    assert clv == pytest.approx(math.log(2.0 * 0.5), abs=1e-9)  # = 0


async def test_clv_is_negative_when_the_price_taken_was_worse_than_the_close(
    tmp_path: Path,
) -> None:
    def prices(fixture: Fixture):  # type: ignore[no-untyped-def]
        return pinnacle(fixture, FAIR_PRICES, OddsKind.PREMATCH, 9, 30)

    _, clv = await clv_of_signal(tmp_path, "clv-worse", prices, 1.5)  # type: ignore[arg-type]
    assert clv == pytest.approx(math.log(1.5 * 0.625), abs=1e-9)
    assert clv is not None
    assert clv < 0


async def test_without_a_sharp_closing_price_there_is_no_clv(tmp_path: Path) -> None:
    def prices(fixture: Fixture):  # type: ignore[no-untyped-def]
        return snapshot(
            fixture, BOOKMAKER_1XBET, X12, FAIR_PRICES, kind=OddsKind.PREMATCH, taken_at=CLOSING_AT
        )

    _, clv = await clv_of_signal(tmp_path, "clv-none", prices, 2.0)  # type: ignore[arg-type]
    assert clv is None


async def test_a_price_captured_after_kickoff_is_never_the_close(tmp_path: Path) -> None:
    """A late prematch capture (after the whistle) must not replace the pre-kickoff one."""

    def prices(fixture: Fixture):  # type: ignore[no-untyped-def]
        return [
            *pinnacle(fixture, FAIR_PRICES, OddsKind.PREMATCH, 9, 30),
            *pinnacle(fixture, x12(1.05, 15.0, 30.0), OddsKind.PREMATCH, 11, 0),
        ]

    _, clv = await clv_of_signal(tmp_path, "clv-late", prices, 2.0)  # type: ignore[arg-type]
    assert clv == pytest.approx(math.log(2.0 * 0.625), abs=1e-9)


# ------------------------------------------------------------------------------ property
HANDICAP_LINES = [line / 4 for line in range(-12, 13)]
TOTAL_LINES = [line / 4 for line in range(2, 17)]
SETTLEMENT_NAME = {
    Settlement.WIN: WIN,
    Settlement.HALF_WIN: HALF_WIN,
    Settlement.PUSH: PUSH,
    Settlement.HALF_LOSS: HALF_LOSS,
    Settlement.LOSS: LOSS,
}


@settings(max_examples=400, deadline=None)
@given(
    kind=st.sampled_from(["ah", "totals"]),
    side=st.booleans(),
    line=st.sampled_from(HANDICAP_LINES),
    total_line=st.sampled_from(TOTAL_LINES),
    home=st.integers(0, 9),
    away=st.integers(0, 9),
)
def test_the_models_settlement_agrees_with_the_independent_oracle(  # noqa: PLR0913, PLR0917 - one draw each
    kind: str, side: bool, line: float, total_line: float, home: int, away: int
) -> None:
    if kind == "ah":
        market = Market.asian_handicap(line)
        selection = Selection.HOME if side else Selection.AWAY
    else:
        market = Market.totals(total_line)
        selection = Selection.OVER if side else Selection.UNDER

    assert SETTLEMENT_NAME[settle_selection(market, selection, home, away)] == own_result(
        market, selection, home, away
    )


@settings(max_examples=200, deadline=None)
@given(
    cents=st.integers(101, 2000),
    stake_cents=st.integers(1, 100_000),
    result=st.sampled_from([WIN, HALF_WIN, PUSH, HALF_LOSS, LOSS]),
)
def test_stored_profit_is_the_exact_decimal_within_one_quantum(
    cents: int, stake_cents: int, result: str
) -> None:
    """Float noise in ``profit_of`` can flip a rounding tie by one 0.0001, never more."""
    odds, stake = cents / 100, Decimal(stake_cents) / 100
    exact = expected_profit(stake, odds, result)
    settlement = {v: k for k, v in SETTLEMENT_NAME.items()}[result]

    stored = profit_of(stake, odds, settlement).quantize(MONEY_QUANTUM)

    assert abs(stored - exact) <= MONEY_QUANTUM


def test_profit_of_is_exact_for_two_decimal_odds() -> None:
    assert profit_of(Decimal("1.0"), 1.01, Settlement.WIN) == Decimal("0.01")
