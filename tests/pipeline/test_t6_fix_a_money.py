"""T6 fix round A: open bets always settle, cash-out stays on its bet, Kelly follows the balance."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import update

from app.db.enums import Outcome, ResultSource
from app.db.repo import (
    create_bet,
    get_bet,
    settled_profit_total,
    signal_has_bets,
    unsettled_signals,
    upsert_live,
)
from app.db.schema import SignalRow
from app.pipeline import settle as settle_module
from app.pipeline.config import PipelineConfig, StakingMode
from app.pipeline.delivery import NullDelivery
from app.pipeline.errors import ManualResultError
from app.pipeline.profile import load_profile
from app.pipeline.settle import BetPayout, run_settlement, settle_manually
from app.services.interfaces import BetDraft, ReportRequest, ResultMark
from tests.integration.t4_support import USER, finished
from tests.integration.test_t4_hand_league import FIXTURE, KICKOFF, MODEL_WEIGHT, WORLD_A, batch
from tests.pipeline.test_fix_round_money import (
    HandModels,
    hand_world,
    home_signal,
    open_bare,
    push_prices,
    recalc_home,
)

AFTER_MATCH = KICKOFF + timedelta(hours=3)


async def _finish(bare, home: int, away: int) -> None:  # type: ignore[no-untyped-def]
    bare.clock.now = AFTER_MATCH
    async with bare.sessions.begin() as session:
        await upsert_live(session, results=[finished(FIXTURE, home, away)])


async def _forget_recommendation(bare, signal_id: int) -> None:  # type: ignore[no-untyped-def]
    """Turn the signal into a custom no-value pick: never recommended, still bettable."""
    async with bare.sessions.begin() as session:
        await session.execute(
            update(SignalRow).where(SignalRow.id == signal_id).values(recommended_at=None)
        )


# ---------------------------------------------------------------------------- decision #19
async def test_a_bet_on_a_never_recommended_signal_is_settled_and_reaches_the_bank(
    tmp_path: Path,
) -> None:
    async with hand_world(tmp_path / "custom.db") as bare:
        record = await home_signal(bare)
        services = bare.system.services
        settings = await services.settings.get_settings(USER)
        await services.settings.update_settings(
            USER, replace(settings, bank=Decimal(1000), unit=Decimal(10))
        )
        await services.bets.place_bet(
            USER, BetDraft(signal_id=record.id, odds_taken=2.0, stake=Decimal(50))
        )
        await _forget_recommendation(bare, record.id)
        await _finish(bare, 2, 0)

        report = await run_settlement(bare.ctx, NullDelivery())

        assert report.settled >= 1
        assert await services.bets.list_bets(USER, open_only=True) == ()
        [bet] = await services.bets.list_bets(USER)
        assert bet.outcome is ResultMark.WON
        assert bet.profit == Decimal(50)
        summary = await services.bets.bank_summary(USER)
        assert summary.balance == Decimal(1050)  # the bank counts the bet, recommended or not
        assert summary.open_bets == 0


async def test_a_never_recommended_signal_without_a_bet_is_still_left_alone(
    tmp_path: Path,
) -> None:
    async with hand_world(tmp_path / "quiet.db") as bare:
        record = await home_signal(bare)
        await _forget_recommendation(bare, record.id)
        await _finish(bare, 2, 0)

        async with bare.sessions() as session:
            pending = await unsettled_signals(session, kicked_off_before=bare.clock())

        # no open bet: it is not a recommendation, nothing to close
        assert record.id not in {r.id for r in pending}


async def test_a_never_recommended_signal_stays_out_of_the_recommended_report(
    tmp_path: Path,
) -> None:
    async with hand_world(tmp_path / "report.db") as bare:
        record = await home_signal(bare)
        services = bare.system.services
        await services.bets.place_bet(USER, BetDraft(signal_id=record.id, stake=Decimal(10)))
        recommended = (await services.reports.report(USER, ReportRequest())).signals
        await _forget_recommendation(bare, record.id)
        await _finish(bare, 2, 0)
        await run_settlement(bare.ctx, NullDelivery())

        report = await services.reports.report(USER, ReportRequest())

        assert report.signals == recommended - 1  # the custom pick is not a recommendation
        assert report.roi is None  # and its bet is not in the recommended-signal ROI


# ---------------------------------------------------------------------------- isolation
async def test_one_signal_that_cannot_be_settled_never_blocks_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with hand_world(tmp_path / "isolate.db") as bare:
        await home_signal(bare)
        await _finish(bare, 1, 0)
        real = settle_module._settle_one
        calls: list[int] = []

        async def flaky(ctx, record):  # type: ignore[no-untyped-def]
            calls.append(record.id)
            if len(calls) == 1:
                raise RuntimeError("bad row")
            return await real(ctx, record)

        monkeypatch.setattr(settle_module, "_settle_one", flaky)

        first = await run_settlement(bare.ctx, NullDelivery())
        assert len(calls) >= 2  # the failure did not stop the loop
        assert first.settled == len(calls) - 1
        errors = await bare.ctx.ledger.recent_errors()
        assert any("bad row" in message for _, _, message in errors)

        retried = await run_settlement(bare.ctx, NullDelivery())  # the next run settles it
        assert retried.settled == 1
        assert first.settled + retried.settled == len(calls) - 1


# ---------------------------------------------------------------------------- decision #22
async def _two_bets(bare, record):  # type: ignore[no-untyped-def]
    cashed = await bare.system.services.bets.place_bet(
        USER, BetDraft(signal_id=record.id, odds_taken=2.0, stake=Decimal(250))
    )
    other = await bare.system.services.bets.place_bet(
        USER, BetDraft(signal_id=record.id, odds_taken=2.0, stake=Decimal(100))
    )
    return cashed, other


async def test_a_payout_cash_out_closes_only_its_bet_and_the_signal_waits(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "cash.db") as bare:
        record = await home_signal(bare)
        cashed, other = await _two_bets(bare, record)
        bare.clock.now = AFTER_MATCH

        stored = await settle_manually(
            bare.ctx,
            record,
            Outcome.CASHOUT,
            payout=BetPayout(cashed.bet_id, Decimal(250), 2.0, Decimal(200)),
        )

        assert stored is None  # another bet is open: the signal has no result yet
        views = {b.bet_id: b for b in await bare.system.services.bets.list_bets(USER)}
        assert views[cashed.bet_id].profit == Decimal(-50)
        assert views[other.bet_id].outcome is None
        await _finish(bare, 2, 0)  # the API result later closes the rest, not the cashed bet
        await run_settlement(bare.ctx, NullDelivery())
        views = {b.bet_id: b for b in await bare.system.services.bets.list_bets(USER)}
        assert views[cashed.bet_id].profit == Decimal(-50)
        assert views[other.bet_id].profit == Decimal(100)


async def test_cashing_out_the_last_open_bet_gives_the_signal_its_result(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "last.db") as bare:
        record = await home_signal(bare)
        bet = await bare.system.services.bets.place_bet(
            USER, BetDraft(signal_id=record.id, odds_taken=2.0, stake=Decimal(100))
        )
        bare.clock.now = AFTER_MATCH

        stored = await settle_manually(
            bare.ctx,
            record,
            Outcome.CASHOUT,
            payout=BetPayout(bet.bet_id, Decimal(100), 2.0, Decimal(150)),
        )

        assert stored is not None
        assert stored.outcome is Outcome.CASHOUT
        assert stored.source is ResultSource.MANUAL
        assert stored.profit_units == record.stake_units / 2  # +50% of the advised stake


@pytest.mark.parametrize("amount", [Decimal("200.01"), Decimal(-1)])
async def test_a_payout_beyond_what_the_bet_pays_is_refused(
    tmp_path: Path, amount: Decimal
) -> None:
    async with hand_world(tmp_path / "bound.db") as bare:
        record = await home_signal(bare)
        bet = await bare.system.services.bets.place_bet(
            USER, BetDraft(signal_id=record.id, odds_taken=2.0, stake=Decimal(100))
        )

        with pytest.raises(ManualResultError):
            await settle_manually(
                bare.ctx,
                record,
                Outcome.CASHOUT,
                payout=BetPayout(bet.bet_id, Decimal(100), 2.0, amount),
            )

        async with bare.sessions() as session:
            assert (await get_bet(session, USER, bet.bet_id)).outcome is None  # type: ignore[union-attr]


async def test_the_payout_ceiling_itself_is_allowed(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "ceiling.db") as bare:
        record = await home_signal(bare)
        bet = await bare.system.services.bets.place_bet(
            USER, BetDraft(signal_id=record.id, odds_taken=2.0, stake=Decimal(100))
        )
        bare.clock.now = AFTER_MATCH
        await settle_manually(
            bare.ctx,
            record,
            Outcome.CASHOUT,
            payout=BetPayout(bet.bet_id, Decimal(100), 2.0, Decimal(200)),
        )
        [settled] = await bare.system.services.bets.list_bets(USER)
        assert settled.profit == Decimal(100)


@pytest.mark.parametrize(
    ("units", "ok"), [("-1", True), ("-1.01", False), ("1.10", True), ("1.11", False)]
)
async def test_a_signal_cash_out_in_units_stays_within_loss_and_full_win(
    tmp_path: Path, units: str, ok: bool
) -> None:
    async with hand_world(tmp_path / "units.db") as bare:
        record = await home_signal(bare)  # 1 unit at 2.10
        bare.clock.now = AFTER_MATCH
        call = settle_manually(bare.ctx, record, Outcome.CASHOUT, cashout_units=Decimal(units))
        if ok:
            assert (await call).profit_units == Decimal(units)  # type: ignore[union-attr]
        else:
            with pytest.raises(ManualResultError):
                await call


async def test_a_money_cash_out_is_not_rescaled_by_a_later_manual_mark(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "keep.db") as bare:
        record = await home_signal(bare)
        cashed, _ = await _two_bets(bare, record)
        bare.clock.now = AFTER_MATCH
        await settle_manually(
            bare.ctx,
            record,
            Outcome.CASHOUT,
            payout=BetPayout(cashed.bet_id, Decimal(250), 2.0, Decimal(200)),
        )

        await settle_manually(bare.ctx, record, Outcome.WIN)

        views = {b.bet_id: b for b in await bare.system.services.bets.list_bets(USER)}
        assert views[cashed.bet_id].profit == Decimal(-50)  # the exact money stays
        assert views[cashed.bet_id].outcome is ResultMark.CASHOUT


# ---------------------------------------------------------------------------- Kelly (#11)
async def test_the_profile_balance_is_the_bank_plus_settled_profit(tmp_path: Path) -> None:
    async with hand_world(tmp_path / "balance.db") as bare:
        record = await home_signal(bare)
        services = bare.system.services
        settings = await services.settings.get_settings(USER)
        await services.settings.update_settings(
            USER, replace(settings, bank=Decimal(1000), unit=Decimal(10))
        )
        await services.bets.place_bet(
            USER, BetDraft(signal_id=record.id, odds_taken=2.0, stake=Decimal(100))
        )
        async with bare.sessions() as session:
            before = await load_profile(session, USER)
        assert before.balance == Decimal(1000)  # an open stake does not move it

        await _finish(bare, 0, 1)
        await run_settlement(bare.ctx, NullDelivery())

        async with bare.sessions() as session:
            after = await load_profile(session, USER)
            assert await settled_profit_total(session, USER) == Decimal(-100)
        assert (after.bank, after.balance) == (Decimal(1000), Decimal(900))


async def test_kelly_sizes_a_new_signal_from_the_balance_not_the_starting_bank(
    tmp_path: Path,
) -> None:
    config = PipelineConfig(model_weight=MODEL_WEIGHT, staking=StakingMode.KELLY)

    async def stake_with(path: Path, loss: Decimal) -> Decimal:
        async with open_bare(path, batch(WORLD_A), config=config) as bare:
            object.__setattr__(bare.ctx, "models", HandModels())
            services = bare.system.services
            settings = await services.settings.get_settings(USER)
            await services.settings.update_settings(
                USER, replace(settings, bank=Decimal(1000), unit=Decimal(1))
            )
            if loss:  # a settled losing bet elsewhere shrinks the balance
                async with bare.sessions.begin() as session:
                    other = await create_bet_record(session, bare, loss)
                    assert other is not None
            record = await home_signal(bare)
            return record.stake_units

    full = await stake_with(tmp_path / "full.db", Decimal(0))
    shrunk = await stake_with(tmp_path / "shrunk.db", Decimal(500))

    assert shrunk == pytest.approx(full / 2, rel=0.05)  # a half-size bank, half-size stake


async def create_bet_record(session, bare, loss: Decimal):  # type: ignore[no-untyped-def]
    """A lost, settled bet that is not tied to any signal (history before the first signal)."""
    from app.db.repo import settle_bet  # noqa: PLC0415

    bet = await create_bet(
        session,
        user_id=USER,
        signal_id=None,
        odds_taken=2.0,
        stake=loss,
        placed_at=bare.clock() - timedelta(days=3),
    )
    return await settle_bet(
        session,
        bet.id,
        user_id=USER,
        outcome=Outcome.LOSS,
        profit=-loss,
        settled_at=bare.clock() - timedelta(days=2),
    )


async def test_a_recalculation_keeps_the_stake_that_backs_a_placed_bet(tmp_path: Path) -> None:
    config = PipelineConfig(model_weight=MODEL_WEIGHT, staking=StakingMode.KELLY)
    async with open_bare(tmp_path / "backed.db", batch(WORLD_A), config=config) as bare:
        object.__setattr__(bare.ctx, "models", HandModels())
        services = bare.system.services
        settings = await services.settings.get_settings(USER)
        await services.settings.update_settings(
            USER, replace(settings, bank=Decimal(1000), unit=Decimal(1))
        )
        record = await home_signal(bare)
        free = (await _after_a_smaller_edge(bare, record)).stake_units
        assert free < record.stake_units  # without a bet Kelly follows the smaller edge

    async with open_bare(tmp_path / "backed2.db", batch(WORLD_A), config=config) as bare:
        object.__setattr__(bare.ctx, "models", HandModels())
        services = bare.system.services
        settings = await services.settings.get_settings(USER)
        await services.settings.update_settings(
            USER, replace(settings, bank=Decimal(1000), unit=Decimal(1))
        )
        record = await home_signal(bare)
        await services.bets.place_bet(USER, BetDraft(signal_id=record.id, stake=Decimal(5)))
        async with bare.sessions() as session:
            assert await signal_has_bets(session, record.id)

        kept = await _after_a_smaller_edge(bare, record)

        assert kept.stake_units == record.stake_units  # profit basis of the placed bet


async def _after_a_smaller_edge(bare, record):  # type: ignore[no-untyped-def]
    bare.clock.now += timedelta(minutes=30)
    await push_prices(bare, 1.90, at=bare.clock())
    return (await recalc_home(bare, record)).after.record


async def test_the_status_screen_masks_secrets_stored_before_they_were_known(
    tmp_path: Path,
) -> None:
    from app.pipeline.redaction import register_secrets  # noqa: PLC0415

    async with hand_world(tmp_path / "status.db") as bare:
        await bare.ctx.ledger.record_error("odds", "failed with hunter2hunter2", now=bare.clock())
        register_secrets(["hunter2hunter2"])
        try:
            status = await bare.system.services.status.system_status()
        finally:
            register_secrets(())

        [entry] = status.recent_errors
        assert "hunter2hunter2" not in entry.message
