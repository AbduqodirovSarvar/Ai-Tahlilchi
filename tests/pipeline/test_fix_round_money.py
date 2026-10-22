"""Fix round: stakes, recommended odds, stale prices, bets on settled signals (hand league)."""

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.db.enums import SignalVersionReason
from app.db.repo import create_bet, get_signal, upsert_live
from app.domain import BOOKMAKER_1XBET, BOOKMAKER_PINNACLE, OddsKind, Selection
from app.pipeline.config import PipelineConfig
from app.pipeline.delivery import NullDelivery
from app.pipeline.provider import AUTOMATIC
from app.pipeline.recalc import RecalcOptions, RecalcVerdict, recalc_signals
from app.pipeline.settle import run_settlement
from app.services.errors import ValidationFailedError
from app.services.interfaces import BetDraft, ResultMark
from tests.integration.t4_support import (
    USER,
    Bare,
    finished,
    open_bare,
    snapshot,
    utc,
    x12,
)
from tests.integration.test_t4_hand_league import (
    FIXTURE,
    KICKOFF,
    MODEL_WEIGHT,
    MORNING,
    ONE_X_TWO,
    PINNACLE,
    WORLD_A,
    HandModels,
    Prices,
    analyse,
    batch,
)

RECALC = RecalcOptions(
    reason=SignalVersionReason.RECALC, purpose=AUTOMATIC, odds_kind=OddsKind.RECALC, fetch=False
)
CONFIG = PipelineConfig(model_weight=MODEL_WEIGHT)
HOME = Selection.HOME


@asynccontextmanager
async def hand_world(
    path: Path, prices: Prices = WORLD_A, taken: datetime = MORNING
) -> AsyncIterator[Bare]:
    async with open_bare(path, batch(prices, taken), config=CONFIG) as bare:
        object.__setattr__(bare.ctx, "models", HandModels())
        yield bare


async def push_prices(  # noqa: PLR0913 - one switch per thing a test may vary
    bare: Bare,
    home: float,
    *,
    at: datetime,
    kind: OddsKind = OddsKind.RECALC,
    pinnacle: dict[Selection, float] | None = None,
    one_x_bet: bool = True,
) -> None:
    """Store a fresh 1X2 quote of Pinnacle and (unless told otherwise) 1xBet taken at ``at``."""
    rows = snapshot(
        FIXTURE, BOOKMAKER_PINNACLE, ONE_X_TWO, pinnacle or PINNACLE, kind=kind, taken_at=at
    )
    if one_x_bet:
        rows += snapshot(
            FIXTURE, BOOKMAKER_1XBET, ONE_X_TWO, x12(home, 4.40, 4.00), kind=kind, taken_at=at
        )
    async with bare.sessions.begin() as session:
        await upsert_live(session, odds=rows)


async def home_signal(bare: Bare):  # type: ignore[no-untyped-def]
    views = await analyse(bare)
    view = views[ONE_X_TWO, HOME]
    assert view.recommended
    async with bare.sessions() as session:
        record = await get_signal(session, view.signal_id)
    assert record is not None
    return record


async def recalc_home(bare: Bare, record):  # type: ignore[no-untyped-def]
    results, failed = await recalc_signals(bare.ctx, [record], RECALC)
    assert not failed
    return results[0]


# ------------------------------------------------------------------------------ R2: stake
async def test_a_recalculation_never_zeroes_the_stake_for_good(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "stake.db") as bare:
        record = await home_signal(bare)
        assert record.stake_units == Decimal(1)

        bare.clock.now += timedelta(minutes=30)
        await push_prices(bare, 1.50, at=bare.clock())  # EV < 0
        down = await recalc_home(bare, record)
        assert down.after.record.ev < 0
        assert down.after.record.stake_units == Decimal(1)  # kept, not zeroed
        assert down.verdict is RecalcVerdict.BELOW_THRESHOLD

        bare.clock.now += timedelta(minutes=30)
        await push_prices(bare, 2.30, at=bare.clock())  # EV > 0 again
        up = await recalc_home(bare, down.after.record)
        assert up.after.record.ev > 0
        assert up.after.record.stake_units == Decimal(1)
        assert up.verdict is RecalcVerdict.STILL_VALID


async def test_a_recalculation_advises_the_stake_afresh_with_the_staking_rule(
    tmp_path: Path,
) -> None:
    from app.pipeline.config import StakingMode  # noqa: PLC0415

    config = PipelineConfig(model_weight=MODEL_WEIGHT, staking=StakingMode.KELLY)
    async with open_bare(tmp_path / "kelly.db", batch(WORLD_A), config=config) as bare:
        object.__setattr__(bare.ctx, "models", HandModels())
        record = await home_signal(bare)
        before = record.stake_units
        assert before > 0

        bare.clock.now += timedelta(minutes=30)
        await push_prices(bare, 2.60, at=bare.clock())  # a much bigger edge
        after = (await recalc_home(bare, record)).after.record

        assert after.stake_units >= before  # Kelly follows the new edge (capped at 2%)


# ------------------------------------------------------------------------------ R7: odds
async def test_profit_and_clv_use_the_first_recommended_odds(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "odds.db") as bare:
        record = await home_signal(bare)
        assert record.recommended_odds == WORLD_A.home == 2.10

        bare.clock.now += timedelta(minutes=30)
        await push_prices(bare, 1.80, at=bare.clock())
        moved = (await recalc_home(bare, record)).after.record
        assert moved.odds == 1.80  # current odds follow the market
        assert moved.recommended_odds == 2.10  # the recommendation does not
        assert moved.first_odds == 2.10

        # the close: Pinnacle 2.0 (no margin -> fair 2.0) and 1xBet 1.75
        close = KICKOFF - timedelta(minutes=10)
        await push_prices(bare, 1.75, at=close, kind=OddsKind.PREMATCH, pinnacle=PINNACLE)
        bare.clock.now = KICKOFF + timedelta(hours=3)
        async with bare.sessions.begin() as session:
            await upsert_live(session, results=[finished(FIXTURE, 1, 0)])
        await run_settlement(bare.ctx, NullDelivery())

        async with bare.sessions() as session:
            settled = await get_signal(session, record.id)
        assert settled is not None
        assert settled.result is not None
        assert settled.result.profit_units == Decimal("1.10")  # 1 unit at 2.10, not at 1.80
        assert settled.result.clv == pytest.approx(math.log(2.10 / 2.0), abs=1e-9)
        assert settled.result.closing_odds == 1.75  # the pick's own bookmaker, not Pinnacle's


async def test_a_bet_settles_on_the_odds_the_user_took(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "bet.db") as bare:
        record = await home_signal(bare)
        bet = await bare.system.services.bets.place_bet(
            USER, BetDraft(signal_id=record.id, odds_taken=1.95, stake=Decimal(20))
        )
        bare.clock.now = KICKOFF + timedelta(hours=3)
        async with bare.sessions.begin() as session:
            await upsert_live(session, results=[finished(FIXTURE, 2, 0)])
        await run_settlement(bare.ctx, NullDelivery())

        [settled] = await bare.system.services.bets.list_bets(USER)
        assert settled.bet_id == bet.bet_id
        assert settled.outcome is ResultMark.WON
        assert settled.profit == Decimal("19.0")  # 20 * (1.95 - 1), exactly


# ------------------------------------------------------------------------------ R3: bets
async def test_a_bet_on_a_settled_signal_is_refused(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "late.db") as bare:
        record = await home_signal(bare)
        bare.clock.now = KICKOFF + timedelta(hours=3)
        async with bare.sessions.begin() as session:
            await upsert_live(session, results=[finished(FIXTURE, 1, 0)])
        await run_settlement(bare.ctx, NullDelivery())

        with pytest.raises(ValidationFailedError):
            await bare.system.services.bets.place_bet(
                USER, BetDraft(signal_id=record.id, stake=Decimal(10))
            )


async def test_a_bet_after_kickoff_is_refused_but_a_repeated_tap_is_answered(
    tmp_path: Path,
) -> None:
    async with hand_world(tmp_path / "kick.db") as bare:
        record = await home_signal(bare)
        draft = BetDraft(signal_id=record.id, stake=Decimal(10), client_token="bet:1:1")
        first = await bare.system.services.bets.place_bet(USER, draft)

        bare.clock.now = KICKOFF + timedelta(minutes=1)
        again = await bare.system.services.bets.place_bet(USER, draft)  # same tap: same bet
        assert again.bet_id == first.bet_id
        with pytest.raises(ValidationFailedError):
            await bare.system.services.bets.place_bet(
                USER, BetDraft(signal_id=record.id, stake=Decimal(5), client_token="bet:1:2")
            )


async def test_settlement_closes_a_bet_that_slipped_in_after_the_result(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "orphan.db") as bare:
        record = await home_signal(bare)
        bare.clock.now = KICKOFF + timedelta(hours=3)
        async with bare.sessions.begin() as session:
            await upsert_live(session, results=[finished(FIXTURE, 0, 1)])
        await run_settlement(bare.ctx, NullDelivery())
        async with bare.sessions.begin() as session:  # a racing write that bypassed the service
            await create_bet(
                session,
                user_id=USER,
                signal_id=record.id,
                odds_taken=2.0,
                stake=Decimal(10),
                placed_at=bare.clock(),
            )
        assert (await bare.system.services.bets.list_bets(USER, open_only=True)) != ()

        report = await run_settlement(bare.ctx, NullDelivery())

        assert report.bets_closed == 1
        assert (await bare.system.services.bets.list_bets(USER, open_only=True)) == ()
        [bet] = await bare.system.services.bets.list_bets(USER)
        assert bet.outcome is ResultMark.LOST
        assert bet.profit == Decimal(-10)


# ------------------------------------------------------------------------------ R6: prices
async def test_a_stale_bookmaker_price_is_skipped_and_counted(tmp_path: Path) -> None:
    stale = utc(2026, 10, 20, 20)  # ten hours before "now"
    async with hand_world(tmp_path / "stale.db", taken=stale) as bare:
        views = await analyse(bare)
        assert views == {}
        result = await bare.system.services.analysis.analyze(USER, await _query(bare))
        assert result.skipped_stale_price == 1
        assert result.skipped_no_price == 0
        assert result.signals == ()


async def test_a_missing_bookmaker_price_is_counted_as_no_price(tmp_path: Path) -> None:
    async with open_bare(tmp_path / "noprice.db", batch(WORLD_A), config=CONFIG) as bare:
        object.__setattr__(bare.ctx, "models", HandModels())
        # only Pinnacle prices the match: there is nothing to bet on at the user's bookmaker
        async with bare.sessions.begin() as session:
            await _drop_bookmaker(session, BOOKMAKER_1XBET)
        result = await bare.system.services.analysis.analyze(USER, await _query(bare))

        assert result.skipped_no_price == 1
        assert result.signals == ()


async def test_an_anchor_far_from_the_bet_price_in_time_is_not_used(tmp_path: Path) -> None:
    old_anchor = MORNING - timedelta(hours=5)  # more than max_quote_skew before 1xBet's quote
    async with open_bare(tmp_path / "skew.db", None, config=CONFIG) as bare:
        object.__setattr__(bare.ctx, "models", HandModels())
        rows = [
            *snapshot(
                FIXTURE, BOOKMAKER_PINNACLE, ONE_X_TWO, PINNACLE,
                kind=OddsKind.MORNING, taken_at=old_anchor,
            ),
            *snapshot(
                FIXTURE, BOOKMAKER_1XBET, ONE_X_TWO, x12(2.10, 4.40, 4.00),
                kind=OddsKind.MORNING, taken_at=MORNING,
            ),
        ]  # fmt: skip
        from app.db.history import upsert_batch  # noqa: PLC0415
        from app.domain import HistoryBatch  # noqa: PLC0415

        async with bare.sessions.begin() as session:
            await upsert_batch(session, HistoryBatch(fixtures=(FIXTURE,), odds=tuple(rows)))
        views = await analyse(bare)

        assert "no_market_reference" in views[ONE_X_TWO, HOME].risk_flags  # model-only


async def test_recalc_without_a_fresh_price_is_no_price_not_valid(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "noprice-recalc.db") as bare:
        record = await home_signal(bare)
        bare.clock.now += timedelta(hours=7)  # the stored quote is now older than 6 hours

        result = await recalc_home(bare, record)

        assert result.verdict is RecalcVerdict.NO_PRICE
        assert result.after.record.odds == record.odds  # the stored numbers are left as they were
        assert result.stale


async def test_a_model_only_ev_is_never_still_valid(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "modelonly.db") as bare:
        record = await home_signal(bare)
        bare.clock.now += timedelta(hours=4)
        # a fresh 1xBet price, but Pinnacle's quote is more than the allowed skew older
        await push_prices(bare, 3.0, at=bare.clock(), one_x_bet=True, pinnacle=PINNACLE)
        async with bare.sessions.begin() as session:
            await _drop_newer(session, BOOKMAKER_PINNACLE, bare.clock())

        result = await recalc_home(bare, record)

        assert result.after.record.ev > 0.05  # the model alone still likes it
        assert result.verdict is not RecalcVerdict.STILL_VALID


# ------------------------------------------------------------------------------ helpers
async def _query(bare: Bare):  # type: ignore[no-untyped-def]
    from dataclasses import replace  # noqa: PLC0415

    from app.domain import MarketType  # noqa: PLC0415
    from app.services.interfaces import AnalysisQuery, DateSpec  # noqa: PLC0415

    services = bare.system.services
    settings = await services.settings.get_settings(USER)
    await services.settings.update_settings(
        USER,
        replace(
            settings,
            min_probability=0.30,
            min_ev=0.0,
            odds_min=1.01,
            odds_max=20.0,
            markets=frozenset({MarketType.MATCH_RESULT}),
            quiet_hours=None,
        ),
    )
    return AnalysisQuery.from_settings(await services.settings.get_settings(USER), DateSpec.today())


async def _drop_bookmaker(session, bookmaker: str) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import delete  # noqa: PLC0415

    from app.db.schema import OddsSnapshotRow  # noqa: PLC0415

    await session.execute(delete(OddsSnapshotRow).where(OddsSnapshotRow.bookmaker == bookmaker))


async def _drop_newer(session, bookmaker: str, after: datetime) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import delete  # noqa: PLC0415

    from app.db.schema import OddsSnapshotRow  # noqa: PLC0415

    await session.execute(
        delete(OddsSnapshotRow).where(
            OddsSnapshotRow.bookmaker == bookmaker, OddsSnapshotRow.taken_at >= after
        )
    )


async def test_a_new_analysis_with_a_worse_price_keeps_the_advised_stake(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "again.db") as bare:
        record = await home_signal(bare)
        assert record.stake_units == Decimal(1)

        bare.clock.now += timedelta(minutes=30)
        await push_prices(bare, 1.50, at=bare.clock())  # EV < 0 now
        views = await analyse(bare)

        view = views[ONE_X_TWO, HOME]
        assert view.ev < 0
        assert view.recommended  # once recommended, always recommended
        assert view.stake_units == Decimal(1)
        assert view.recommended_odds == 2.10
