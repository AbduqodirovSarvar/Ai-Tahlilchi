"""T4 QA: look-ahead, a missing 1xBet price, crash injection, concurrency and the LLM hook.

Everything runs on the real services over a temporary database; time is injected.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.db.enums import LlmVerdict, RecalcKind
from app.db.repo import claim_due_recalcs, upsert_live
from app.db.schema import OddsSnapshotRow
from app.domain import (
    BOOKMAKER_1XBET,
    BOOKMAKER_PINNACLE,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    Market,
    MarketType,
    OddsKind,
    ResultStatus,
    Selection,
    Sport,
)
from app.pipeline.config import PipelineConfig
from app.pipeline.daily import run_due_digests
from app.pipeline.delivery import NullDelivery
from app.pipeline.errors import ProviderUnavailableError
from app.pipeline.ledger import backoff
from app.pipeline.prematch import run_due_recalcs
from app.pipeline.provider import QuotaSnapshot
from app.pipeline.review import LlmCandidateReviewer, Review, ReviewCandidate, ReviewKey
from app.pipeline.settle import run_settlement
from app.services.errors import BusyError, CooldownActiveError, NotFoundError
from app.services.interfaces import DateSpec, SignalView
from tests.integration.t4_support import (
    SOURCE,
    USER,
    Bare,
    Crash,
    FlakyNotifier,
    add_signal,
    analyse_as,
    finished,
    make_fixture,
    open_bare,
    permissive,
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
    batch,
)
from tests.pipeline.harness import open_env
from tests.pipeline.world import START, TEAM_NAMES, league_of, team_of

DIGEST_TIME = utc(2026, 10, 21, 4, 0)  # 09:00 in Asia/Tashkent
CONFIG = PipelineConfig(model_weight=MODEL_WEIGHT)
LEASE = timedelta(minutes=15)


async def prime(bare: Bare) -> None:
    """Hand model, and the user known with loose thresholds."""
    object.__setattr__(bare.ctx, "models", HandModels())
    await permissive(bare.system.services, USER, odds_min=1.01, odds_max=20.0)


# ============================================================================ look-ahead
def numbers(views: Sequence[SignalView]) -> dict[tuple[str, str, Market, Selection], tuple]:  # type: ignore[type-arg]
    return {
        (v.home_team, v.away_team, v.market, v.selection): (
            v.odds,
            v.probability,
            v.ev,
            v.confidence,
        )
        for v in views
    }


def plant_results(day_offset_hours: float, *, count: int = 6) -> list[tuple]:  # type: ignore[type-arg]
    """Extra finished matches (9-0 home wins) between the teams of the first league."""
    from app.domain import ExternalRef, Fixture, Sport  # noqa: PLC0415

    teams = TEAM_NAMES["E0"]
    rows = []
    for index in range(count):
        home, away = teams[index], teams[index + count]
        fixture = Fixture(
            ref=ExternalRef(
                "football-data", f"E0/2026/PLANT-{day_offset_hours}-{index}/{home}/{away}"
            ),
            sport=Sport.FOOTBALL,
            league=league_of("E0"),
            season=2026,
            kickoff=START + timedelta(hours=day_offset_hours, minutes=index),
            home=team_of(home),
            away=team_of(away),
            status=FixtureStatus.FINISHED,
        )
        result = FixtureResult(
            fixture=fixture.ref, status=ResultStatus.FINISHED, home_score=9, away_score=0
        )
        rows.append((fixture, result))
    return rows


async def world_numbers(env, user: int) -> dict:  # type: ignore[no-untyped-def,type-arg]
    services = env.system.services
    await permissive(services, user)
    result = await analyse_as(services, user, DateSpec.next_days(3))
    assert result.signals
    return numbers(result.signals)


async def plant(env, rows: list[tuple]) -> None:  # type: ignore[no-untyped-def,type-arg]
    async with env.sessions.begin() as session:
        await upsert_live(session, fixtures=[f for f, _ in rows], results=[r for _, r in rows])


async def test_results_on_or_after_the_cutoff_day_cannot_change_an_earlier_signal(
    seeded_template,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    template, world = seeded_template
    async with open_env(template, world, tmp_path / "ahead.db") as env:
        baseline = await world_numbers(env, 7101)
        # the model's cut-off is the start of tomorrow (UTC): these are all on or after it
        await plant(env, plant_results(24.0))
        await plant(env, plant_results(30.0))
        after = await world_numbers(env, 7102)

    assert after == baseline


async def test_the_probe_has_teeth_results_before_the_cutoff_do_change_signals(
    seeded_template,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    template, world = seeded_template
    async with open_env(template, world, tmp_path / "control.db") as env:
        baseline = await world_numbers(env, 7101)
        await plant(env, plant_results(-3.0))  # this morning: legitimately visible
        after = await world_numbers(env, 7102)

    assert after != baseline


async def test_prices_taken_at_or_after_kickoff_cannot_change_an_earlier_signal(
    seeded_template,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    template, world = seeded_template
    upcoming = [f for f in world.fixtures if f.status is FixtureStatus.SCHEDULED][:40]

    def absurd(taken_offset: timedelta) -> list:  # type: ignore[type-arg]
        rows = []
        for fixture in upcoming:
            for bookmaker in (BOOKMAKER_1XBET, BOOKMAKER_PINNACLE):
                rows += snapshot(
                    fixture,
                    bookmaker,
                    ONE_X_TWO,
                    x12(50.0, 50.0, 1.02),
                    kind=OddsKind.RECALC,
                    taken_at=fixture.kickoff + taken_offset,
                )
        return rows

    async with open_env(template, world, tmp_path / "prices.db") as env:
        baseline = await world_numbers(env, 7101)
        async with env.sessions.begin() as session:
            await upsert_live(session, odds=absurd(timedelta(0)) + absurd(timedelta(hours=1)))
        after = await world_numbers(env, 7102)

    assert after == baseline


async def test_prices_taken_before_kickoff_do_change_signals(
    seeded_template,
    tmp_path: Path,  # type: ignore[no-untyped-def]
) -> None:
    template, world = seeded_template
    upcoming = [f for f in world.fixtures if f.status is FixtureStatus.SCHEDULED][:40]
    rows = []
    for fixture in upcoming:
        rows += snapshot(
            fixture,
            BOOKMAKER_1XBET,
            ONE_X_TWO,
            x12(50.0, 50.0, 1.02),
            kind=OddsKind.RECALC,
            taken_at=START,
        )
    async with open_env(template, world, tmp_path / "prices-control.db") as env:
        baseline = await world_numbers(env, 7101)
        async with env.sessions.begin() as session:
            await upsert_live(session, odds=rows)
        after = await world_numbers(env, 7102)

    assert after != baseline


# ============================================================================ missing 1xBet
def only_pinnacle(fixture, extra=()):  # type: ignore[no-untyped-def]
    return HistoryBatch(
        fixtures=(fixture,),
        odds=tuple(
            snapshot(
                fixture,
                BOOKMAKER_PINNACLE,
                ONE_X_TWO,
                PINNACLE,
                kind=OddsKind.MORNING,
                taken_at=utc(2026, 10, 21, 5),
            )
        )
        + tuple(extra),
    )


async def custom(bare: Bare) -> list[SignalView]:
    await prime(bare)
    result = await analyse_as(
        bare.system.services,
        USER,
        DateSpec.today(),
        include_no_value=True,
        markets=frozenset({MarketType.MATCH_RESULT}),
    )
    return list(result.signals)


async def test_without_a_1xbet_price_there_is_no_signal_and_pinnacles_is_never_used(
    tmp_path: Path,
) -> None:
    async with open_bare(tmp_path / "nopinn.db", only_pinnacle(FIXTURE), config=CONFIG) as bare:
        views = await custom(bare)
        stored = await bare.system.services.signals.list_signals(
            USER, DateSpec.today(), include_hidden=True
        )

    assert views == []
    assert stored == ()


async def test_an_incomplete_1xbet_quote_is_not_completed_from_pinnacle(tmp_path: Path) -> None:
    partial = snapshot(
        FIXTURE,
        BOOKMAKER_1XBET,
        ONE_X_TWO,
        {Selection.HOME: 2.2},
        kind=OddsKind.MORNING,
        taken_at=utc(2026, 10, 21, 5),
    )
    async with open_bare(
        tmp_path / "partial.db", only_pinnacle(FIXTURE, partial), config=CONFIG
    ) as bare:
        views = await custom(bare)

    assert views == []


async def test_every_signal_carries_a_1xbet_price_when_both_bookmakers_quote(
    tmp_path: Path,
) -> None:
    prices = replace(WORLD_A, away=3.7)  # every 1xBet price differs from Pinnacle's
    async with open_bare(tmp_path / "both.db", batch(prices), config=CONFIG) as bare:
        views = await custom(bare)

    assert views
    one_x_bet = {
        Selection.HOME: prices.home,
        Selection.DRAW: prices.draw,
        Selection.AWAY: prices.away,
    }
    for view in views:
        assert view.bookmaker == BOOKMAKER_1XBET
        assert view.odds == one_x_bet[view.selection]
        assert view.odds != PINNACLE[view.selection]


async def test_when_the_1xbet_price_vanishes_a_recalculation_keeps_the_old_price(
    tmp_path: Path,
) -> None:
    async with open_bare(tmp_path / "vanish.db", batch(WORLD_A), config=CONFIG) as bare:
        await prime(bare)
        services = bare.system.services
        first = await analyse_as(services, USER, DateSpec.today())
        (signal,) = first.signals
        async with bare.sessions.begin() as session:
            await session.execute(
                delete(OddsSnapshotRow).where(OddsSnapshotRow.bookmaker == BOOKMAKER_1XBET)
            )
        diff = await services.analysis.recalc_signal(USER, signal.signal_id)

    assert diff.signal.odds == signal.odds == WORLD_A.home  # never Pinnacle's 2.0
    assert diff.after.odds == diff.before.odds
    assert diff.verdict.value == "no_price"  # it can no longer be priced at 1xBet


class DownProvider:
    """A provider of the hand league's source whose every request fails after the retries."""

    sport = Sport.FOOTBALL
    source = SOURCE

    def markets(self) -> frozenset[MarketType]:
        return frozenset(MarketType)

    def adapter(self, purpose: object) -> "DownProvider":  # noqa: ARG002 - protocol signature
        return self

    async def fetch_results(self, fixtures: object) -> list[object]:  # noqa: ARG002 - protocol signature
        raise ProviderUnavailableError("api-football", "503 after 3 attempts")

    fetch_odds = fetch_fixtures = fetch_results

    async def quota(self) -> QuotaSnapshot:
        return QuotaSnapshot(
            provider="api-football",
            sport=Sport.FOOTBALL,
            used=0,
            limit=100,
            manual_used=0,
            remaining=100,
            automatic_available=70,
        )


async def test_a_recalculation_during_an_outage_keeps_the_numbers_and_says_how_old_they_are(
    tmp_path: Path,
) -> None:
    async with open_bare(tmp_path / "down.db", batch(WORLD_A), config=CONFIG) as bare:
        await prime(bare)
        services = bare.system.services
        (signal,) = (await analyse_as(services, USER, DateSpec.today())).signals
        object.__setattr__(bare.ctx, "providers", {Sport.FOOTBALL: DownProvider()})
        diff = await services.analysis.recalc_signal(USER, signal.signal_id)
        batch_result = await services.analysis.recalc_signals(USER, [signal.signal_id])

    assert (diff.after.odds, diff.after.probability, diff.after.ev) == (
        diff.before.odds,
        diff.before.probability,
        diff.before.ev,
    )
    assert diff.signal.data_as_of == MORNING  # the age of the price the numbers are based on
    assert batch_result.skipped_cooldown == (signal.signal_id,)  # the attempt burned the cooldown


# ============================================================================ crash injection
async def stored_signals(bare: Bare) -> list[SignalView]:
    return list(
        await bare.system.services.signals.list_signals(USER, DateSpec.today(), include_hidden=True)
    )


async def test_a_failed_digest_keeps_the_signals_and_a_rerun_delivers_it_once(
    tmp_path: Path,
) -> None:
    notifier = FlakyNotifier()
    notifier.fail["daily_digest"] = 1
    async with open_bare(
        tmp_path / "digest.db", batch(WORLD_A), config=CONFIG, notifier=notifier, now=DIGEST_TIME
    ) as bare:
        await prime(bare)
        await permissive(bare.system.services, USER, min_probability=0.45, min_ev=0.05)

        first = await run_due_digests(bare.ctx, bare.system.delivery)
        after_failure = await stored_signals(bare)
        too_soon = await run_due_digests(bare.ctx, bare.system.delivery)
        bare.clock.advance(backoff(1))
        second = await run_due_digests(bare.ctx, bare.system.delivery)
        third = await run_due_digests(bare.ctx, bare.system.delivery)
        final = await stored_signals(bare)

    assert first.failed == (USER,)
    assert after_failure  # persisted although the message did not go out
    assert too_soon.sent == too_soon.failed == ()  # the retry waits for its backoff
    assert second.sent == (USER,)
    assert third.sent == third.failed == ()
    assert notifier.count("daily_digest") == 1
    assert [s.signal_id for s in final] == [s.signal_id for s in after_failure]
    assert {s.version for s in final} == {1}  # the rerun created no new version


async def test_a_kill_after_sending_repeats_the_digest_only_after_the_lease_with_the_same_key(
    tmp_path: Path,
) -> None:
    """At-least-once by design: same ledger key, so a notifier can drop the repeat."""
    notifier = FlakyNotifier()
    notifier.crash_after.add("daily_digest")
    async with open_bare(
        tmp_path / "kill.db", batch(WORLD_A), config=CONFIG, notifier=notifier, now=DIGEST_TIME
    ) as bare:
        await prime(bare)
        await permissive(bare.system.services, USER, min_probability=0.45, min_ev=0.05)
        with pytest.raises(Crash):
            await run_due_digests(bare.ctx, bare.system.delivery)
        signals = await stored_signals(bare)

        bare.clock.advance(LEASE - timedelta(minutes=1))
        inside = await run_due_digests(bare.ctx, bare.system.delivery)
        sent_inside = notifier.count("daily_digest")

        bare.clock.advance(timedelta(minutes=2))
        after = await run_due_digests(bare.ctx, bare.system.delivery)
        final = await stored_signals(bare)

    assert inside.sent == ()
    assert sent_inside == 1
    assert after.sent == (USER,)
    first, repeat = (n.key for n in notifier.sent if n.method == "daily_digest")
    assert first == repeat
    assert [s.signal_id for s in final] == [s.signal_id for s in signals]


async def schedule_tomorrow(bare: Bare, *, odds_move: bool) -> int:
    """A recommended signal for tomorrow's match (24 h and pre-match tasks queued)."""
    await prime(bare)
    await permissive(bare.system.services, USER, min_probability=0.45, min_ev=0.05)
    result = await analyse_as(bare.system.services, USER, DateSpec.tomorrow())
    assert result.signals
    if odds_move:
        moved = snapshot(
            FIXTURE,
            BOOKMAKER_1XBET,
            ONE_X_TWO,
            x12(2.60, 4.40, 4.00),
            kind=OddsKind.RECALC,
            taken_at=utc(2026, 10, 20, 12),
        )
        async with bare.sessions.begin() as session:
            await upsert_live(session, odds=moved)
    return result.signals[0].signal_id


async def fresh_prices(bare: Bare, at: datetime) -> None:
    """The morning prices taken again at ``at`` (a stale price is no decision basis)."""
    rows = [
        *snapshot(
            FIXTURE, BOOKMAKER_PINNACLE, ONE_X_TWO, PINNACLE, kind=OddsKind.RECALC, taken_at=at
        ),
        *snapshot(
            FIXTURE, BOOKMAKER_1XBET, ONE_X_TWO,
            x12(WORLD_A.home, WORLD_A.draw, WORLD_A.away), kind=OddsKind.RECALC, taken_at=at,
        ),
    ]  # fmt: skip
    async with bare.sessions.begin() as session:
        await upsert_live(session, odds=rows)


YESTERDAY_NOON = utc(2026, 10, 20, 6, 0)
EARLIER = utc(2026, 10, 20, 5, 0)  # the morning prices of the day before


async def test_a_failed_prematch_message_is_retried_and_delivered_once(tmp_path: Path) -> None:
    notifier = FlakyNotifier()
    notifier.fail["signal_update"] = 1
    async with open_bare(
        tmp_path / "pre.db",
        batch(WORLD_A, EARLIER),
        config=CONFIG,
        notifier=notifier,
        now=YESTERDAY_NOON,
    ) as bare:
        await schedule_tomorrow(bare, odds_move=False)
        bare.clock.now = KICKOFF - timedelta(minutes=55)
        await fresh_prices(bare, bare.clock() - timedelta(minutes=5))
        first = await run_due_recalcs(bare.ctx, bare.system.delivery)
        too_soon = await run_due_recalcs(bare.ctx, bare.system.delivery)
        bare.clock.advance(backoff(1))
        second = await run_due_recalcs(bare.ctx, bare.system.delivery)
        third = await run_due_recalcs(bare.ctx, bare.system.delivery)

    prematch_updates = [n for n in notifier.sent if n.method == "signal_update"]
    assert first.failed >= 1
    assert (too_soon.notified, too_soon.failed) == (0, 0)  # not re-claimed every minute
    assert len(prematch_updates) == 1
    assert (third.notified, third.failed) == (0, 0)
    assert second.notified == 1


async def test_a_failed_24h_message_is_not_lost(tmp_path: Path) -> None:
    notifier = FlakyNotifier()
    notifier.fail["signal_update"] = 1
    async with open_bare(
        tmp_path / "h24.db",
        batch(WORLD_A, EARLIER),
        config=CONFIG,
        notifier=notifier,
        now=YESTERDAY_NOON,
    ) as bare:
        await schedule_tomorrow(bare, odds_move=True)
        bare.clock.now = utc(2026, 10, 20, 15, 5)
        first = await run_due_recalcs(bare.ctx, bare.system.delivery)
        bare.clock.advance(backoff(1))
        await run_due_recalcs(bare.ctx, bare.system.delivery)

    assert first.failed == 1
    assert notifier.count("signal_update") >= 1


async def test_the_24h_message_goes_out_once_when_nothing_fails(tmp_path: Path) -> None:
    notifier = FlakyNotifier()
    async with open_bare(
        tmp_path / "h24ok.db",
        batch(WORLD_A, EARLIER),
        config=CONFIG,
        notifier=notifier,
        now=YESTERDAY_NOON,
    ) as bare:
        await schedule_tomorrow(bare, odds_move=True)
        bare.clock.now = utc(2026, 10, 20, 15, 5)
        await run_due_recalcs(bare.ctx, bare.system.delivery)
        await run_due_recalcs(bare.ctx, bare.system.delivery)

    assert notifier.count("signal_update") == 1


KICKOFF_PAST = utc(2026, 10, 21, 10)


async def two_finished_signals(bare: Bare) -> list[int]:
    ids = []
    batch_fixtures = [
        make_fixture("settle-a", KICKOFF_PAST),
        make_fixture("settle-b", KICKOFF_PAST),
    ]
    async with bare.sessions.begin() as session:
        from app.db.history import upsert_batch  # noqa: PLC0415

        await upsert_batch(session, HistoryBatch(fixtures=tuple(batch_fixtures)))
    for fixture in batch_fixtures:
        record = await add_signal(bare, fixture, ONE_X_TWO, Selection.HOME, 2.0, Decimal(1))
        ids.append(record.id)
    bare.clock.now = utc(2026, 10, 21, 14)
    async with bare.sessions.begin() as session:
        await upsert_live(session, results=[finished(f, 2, 0) for f in batch_fixtures])
    return ids


async def test_a_failed_result_message_is_not_lost(tmp_path: Path) -> None:
    notifier = FlakyNotifier()
    notifier.fail["result_settled"] = 1
    async with open_bare(
        tmp_path / "result.db", None, config=CONFIG, notifier=notifier, now=utc(2026, 10, 21, 8)
    ) as bare:
        await permissive(bare.system.services, USER)
        await two_finished_signals(bare)
        first = await run_settlement(bare.ctx, bare.system.delivery)
        bare.clock.advance(backoff(1))
        second = await run_settlement(bare.ctx, bare.system.delivery)

    assert (first.settled, first.announce_failed) == (2, 1)  # the failure stops nothing else
    assert second.notified == 1
    assert notifier.count("result_settled") == 2


async def test_a_failed_result_message_does_not_double_settle_or_lose_a_result(
    tmp_path: Path,
) -> None:
    notifier = FlakyNotifier()
    notifier.fail["result_settled"] = 1
    async with open_bare(
        tmp_path / "result2.db", None, config=CONFIG, notifier=notifier, now=utc(2026, 10, 21, 8)
    ) as bare:
        await permissive(bare.system.services, USER)
        ids = await two_finished_signals(bare)
        await run_settlement(bare.ctx, bare.system.delivery)
        bare.clock.advance(backoff(1))
        await run_settlement(bare.ctx, bare.system.delivery)
        views = await bare.system.services.signals.get_signals(USER, ids)
        third = await run_settlement(bare.ctx, NullDelivery())

    assert [v.result.value for v in views if v.result] == ["win", "win"]
    assert third.settled == 0


# ============================================================================ concurrency
async def test_two_simultaneous_analyses_of_one_user_give_one_busy_error(tmp_path: Path) -> None:
    async with open_bare(tmp_path / "busy.db", batch(WORLD_A), config=CONFIG) as bare:
        await prime(bare)
        services = bare.system.services
        outcomes = await asyncio.gather(
            analyse_as(services, USER, DateSpec.today()),
            analyse_as(services, USER, DateSpec.today()),
            return_exceptions=True,
        )
        later = await analyse_as(services, USER, DateSpec.today())

    busy = [o for o in outcomes if isinstance(o, BusyError)]
    done = [o for o in outcomes if not isinstance(o, BaseException)]
    assert len(busy) == len(done) == 1
    assert busy[0].user_id == USER
    assert later.signals  # the lock was released


async def test_other_users_do_not_block_each_other(tmp_path: Path) -> None:
    async with open_bare(tmp_path / "parallel.db", batch(WORLD_A), config=CONFIG) as bare:
        await prime(bare)
        await permissive(bare.system.services, USER + 1, odds_min=1.01, odds_max=20.0)
        outcomes = await asyncio.gather(
            analyse_as(bare.system.services, USER, DateSpec.today()),
            analyse_as(bare.system.services, USER + 1, DateSpec.today()),
        )

    assert all(o.signals for o in outcomes)


async def test_a_failing_analysis_releases_the_busy_lock(tmp_path: Path) -> None:
    async with open_bare(tmp_path / "lock.db", batch(WORLD_A), config=CONFIG) as bare:
        await prime(bare)
        services = bare.system.services
        with pytest.raises(NotFoundError):  # a stand-in failure inside the locked section
            await services.analysis.recalc_signal(USER, 999)
        first = await analyse_as(services, USER, DateSpec.today())
        again = await analyse_as(services, USER, DateSpec.today())

    assert first.signals
    assert [s.signal_id for s in again.signals] == [s.signal_id for s in first.signals]


async def test_recalc_cooldown_is_ten_minutes_to_the_second(tmp_path: Path) -> None:
    async with open_bare(tmp_path / "cool.db", batch(WORLD_A), config=CONFIG) as bare:
        await prime(bare)
        services = bare.system.services
        (signal,) = (await analyse_as(services, USER, DateSpec.today())).signals
        await services.analysis.recalc_signal(USER, signal.signal_id)

        bare.clock.advance(timedelta(minutes=9, seconds=59))
        with pytest.raises(CooldownActiveError) as raised:
            await services.analysis.recalc_signal(USER, signal.signal_id)
        bare.clock.advance(timedelta(seconds=1))  # exactly ten minutes after the first
        again = await services.analysis.recalc_signal(USER, signal.signal_id)

    assert raised.value.retry_after == timedelta(seconds=1)
    assert again.signal.signal_id == signal.signal_id


async def test_two_simultaneous_recalcs_of_one_fixture_respect_the_cooldown(tmp_path: Path) -> None:
    async with open_bare(tmp_path / "race.db", batch(WORLD_A), config=CONFIG) as bare:
        await prime(bare)
        services = bare.system.services
        (signal,) = (await analyse_as(services, USER, DateSpec.today())).signals
        outcomes = await asyncio.gather(
            services.analysis.recalc_signal(USER, signal.signal_id),
            services.analysis.recalc_signal(USER, signal.signal_id),
            return_exceptions=True,
        )

    assert sum(isinstance(o, CooldownActiveError) for o in outcomes) == 1


async def test_two_scheduler_ticks_at_once_never_handle_a_task_twice(tmp_path: Path) -> None:
    notifier = FlakyNotifier()
    async with open_bare(
        tmp_path / "ticks.db",
        batch(WORLD_A, EARLIER),
        config=CONFIG,
        notifier=notifier,
        now=YESTERDAY_NOON,
    ) as bare:
        await schedule_tomorrow(bare, odds_move=False)
        bare.clock.now = KICKOFF - timedelta(minutes=30)  # both tasks are due
        await fresh_prices(bare, bare.clock() - timedelta(minutes=5))
        reports = await asyncio.gather(
            run_due_recalcs(bare.ctx, bare.system.delivery),
            run_due_recalcs(bare.ctx, bare.system.delivery),
        )

    handled = sum(r.notified + r.silent + r.skipped + r.failed for r in reports)
    assert handled == 2  # one 24 h task and one pre-match task, each exactly once
    assert notifier.count("signal_update") == 1


async def test_a_dead_workers_claim_is_retaken_only_after_the_lease(tmp_path: Path) -> None:
    notifier = FlakyNotifier()
    async with open_bare(
        tmp_path / "lease.db",
        batch(WORLD_A, EARLIER),
        config=CONFIG,
        notifier=notifier,
        now=YESTERDAY_NOON,
    ) as bare:
        await schedule_tomorrow(bare, odds_move=False)
        bare.clock.now = KICKOFF - timedelta(minutes=30)
        await fresh_prices(bare, bare.clock() - timedelta(minutes=5))
        async with bare.sessions.begin() as session:
            dead = await claim_due_recalcs(session, now=bare.clock())  # the worker dies here
        assert {t.kind for t in dead} == {RecalcKind.BEFORE_24H, RecalcKind.PREMATCH}

        bare.clock.advance(LEASE - timedelta(minutes=1))
        inside = await run_due_recalcs(bare.ctx, bare.system.delivery)
        bare.clock.advance(timedelta(minutes=2))
        after = await run_due_recalcs(bare.ctx, bare.system.delivery)
        later = await run_due_recalcs(bare.ctx, bare.system.delivery)

    assert inside == replace(inside, notified=0, silent=0, skipped=0, failed=0)
    assert after.notified + after.silent + after.skipped == 2
    assert notifier.count("signal_update") == 1
    assert (later.notified, later.silent, later.skipped, later.failed) == (0, 0, 0, 0)


async def test_two_digest_ticks_at_once_send_one_digest(tmp_path: Path) -> None:
    notifier = FlakyNotifier()
    async with open_bare(
        tmp_path / "twice.db", batch(WORLD_A), config=CONFIG, notifier=notifier, now=DIGEST_TIME
    ) as bare:
        await prime(bare)
        await permissive(bare.system.services, USER, min_probability=0.45, min_ev=0.05)
        reports = await asyncio.gather(
            run_due_digests(bare.ctx, bare.system.delivery),
            run_due_digests(bare.ctx, bare.system.delivery),
        )
        signals = await stored_signals(bare)

    assert sum(len(r.sent) for r in reports) == 1
    assert notifier.count("daily_digest") == 1
    assert len(signals) == 1


# ============================================================================ the LLM hook
class Scripted:
    """A reviewer answering from a script (or failing)."""

    def __init__(self, answer: Mapping[ReviewKey, Review] | BaseException | float = 0.0) -> None:
        self.answer = answer
        self.calls = 0

    async def review(self, candidates: Sequence[ReviewCandidate]) -> Mapping[ReviewKey, Review]:  # noqa: ARG002 - protocol signature
        self.calls += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        if isinstance(self.answer, float):
            await asyncio.sleep(self.answer)
            return {}
        return self.answer


class EveryCandidate:
    def __init__(self, verdict: LlmVerdict) -> None:
        self.verdict = verdict

    async def review(self, candidates: Sequence[ReviewCandidate]) -> Mapping[ReviewKey, Review]:
        return {
            c.key: Review(verdict=self.verdict, note="Izoh <b>", risk_flags=("rotatsiya",))
            for c in candidates
        }


async def run_with(tmp_path: Path, name: str, reviewer, **config):  # type: ignore[no-untyped-def]
    async with open_bare(
        tmp_path / f"{name}.db",
        batch(WORLD_A),
        config=replace(CONFIG, **config),
        reviewer=reviewer,
    ) as bare:
        await prime(bare)
        recommended = await analyse_as(bare.system.services, USER, DateSpec.today())
        custom_run = await analyse_as(
            bare.system.services, USER, DateSpec.today(), include_no_value=True
        )
    return recommended, custom_run


def figures(views: Sequence[SignalView]) -> list[tuple]:  # type: ignore[type-arg]
    return sorted(
        (str(v.market), v.selection.value, v.odds, v.probability, v.ev, v.stake_units, v.no_value)
        for v in views
    )


async def test_without_an_llm_the_signal_is_complete_but_has_no_verdict(tmp_path: Path) -> None:
    recommended, _ = await run_with(tmp_path, "none", None)

    assert recommended.signals
    assert all(v.llm_verdict is None and v.llm_note is None for v in recommended.signals)


async def test_the_real_llm_service_switched_off_changes_nothing(tmp_path: Path) -> None:
    from app.llm.service import LlmService  # noqa: PLC0415

    baseline, _ = await run_with(tmp_path, "base", None)
    disabled, _ = await run_with(tmp_path, "off", LlmCandidateReviewer(LlmService(None)))

    assert figures(disabled.signals) == figures(baseline.signals)


async def test_a_backend_that_raises_is_swallowed_by_the_service(tmp_path: Path) -> None:
    from app.llm.service import LlmService  # noqa: PLC0415

    class Broken:
        async def generate_json(self, *args: object, **kwargs: object) -> object:  # noqa: ARG002 - protocol signature
            raise RuntimeError("boom")

    baseline, _ = await run_with(tmp_path, "base2", None)
    broken, _ = await run_with(
        tmp_path,
        "broken",
        LlmCandidateReviewer(LlmService(Broken())),  # type: ignore[arg-type]
    )

    assert figures(broken.signals) == figures(baseline.signals)


@pytest.mark.parametrize(
    "reviewer",
    [Scripted(RuntimeError("llm exploded")), Scripted(ValueError("bad json")), Scripted(0.3)],
    ids=["runtime-error", "value-error", "too-slow"],
)
async def test_a_failing_or_slow_reviewer_never_changes_the_signals(
    tmp_path: Path, reviewer: Scripted
) -> None:
    baseline, _ = await run_with(tmp_path, "b", None)
    result, _ = await run_with(tmp_path, "f", reviewer, review_timeout=0.05)

    assert reviewer.calls >= 1
    assert figures(result.signals) == figures(baseline.signals)
    assert all(v.llm_verdict is None for v in result.signals)


async def test_rad_removes_the_recommendation_but_never_changes_a_number(tmp_path: Path) -> None:
    baseline, baseline_custom = await run_with(tmp_path, "b2", None)
    rejected, rejected_custom = await run_with(tmp_path, "rad", EveryCandidate(LlmVerdict.REJECT))

    assert rejected.signals == ()
    by_key = {(str(v.market), v.selection): v for v in baseline_custom.signals}
    for view in rejected_custom.signals:
        twin = by_key[str(view.market), view.selection]
        assert (view.odds, view.probability, view.ev) == (twin.odds, twin.probability, twin.ev)
        if twin.recommended:
            assert view.no_value
            assert view.stake_units in {twin.stake_units, Decimal(0)}
    assert baseline.signals


async def test_ehtiyot_stays_recommended_with_the_same_numbers(tmp_path: Path) -> None:
    baseline, _ = await run_with(tmp_path, "b3", None)
    cautious, _ = await run_with(tmp_path, "ehtiyot", EveryCandidate(LlmVerdict.CAUTION))

    assert figures(cautious.signals) == figures(baseline.signals)
    assert all(v.llm_verdict is not None and v.recommended for v in cautious.signals)
