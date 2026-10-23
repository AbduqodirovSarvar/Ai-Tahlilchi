"""T4 QA (8): user A's data is unreachable and unchangeable through user B's calls.

Every user-scoped method of the real services is called as user B with the identifiers of
user A. Each call must behave as if A's data did not exist, and A's complete state (signal,
bets, templates, watchlist, settings, report) must be identical before and after.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.pipeline.config import PipelineConfig
from app.services.errors import CooldownActiveError, NotFoundError
from app.services.interfaces import (
    AnalysisQuery,
    BetDraft,
    DateSpec,
    ReportRequest,
    ResultMark,
    ResultMarkRequest,
    Services,
    TemplateDraft,
    WatchKind,
    WatchNotification,
    WatchTarget,
)
from tests.integration.t4_support import USER, Bare, analyse_as, open_bare, permissive
from tests.integration.test_t4_hand_league import FIXTURE, MODEL_WEIGHT, WORLD_A, HandModels, batch

A, B = USER, USER + 100
TOKEN = "tap-A"
CONFIG = PipelineConfig(model_weight=MODEL_WEIGHT)


@dataclass
class World:
    bare: Bare
    a_signal: int
    b_signal: int
    a_template: int
    a_query: str
    a_target: WatchTarget

    @property
    def services(self) -> Services:
        return self.bare.system.services


async def snapshot_of(services: Services, user: int, signal_id: int) -> tuple[object, ...]:
    """Everything user ``user`` can see, as comparable values."""
    return (
        await services.signals.get_signal(user, signal_id),
        await services.signals.list_signals(user, DateSpec.today(), include_hidden=True),
        await services.bets.list_bets(user, limit=100),
        await services.bets.bank_summary(user),
        await services.templates.list_templates(user),
        await services.watchlist.list_watchlist(user),
        await services.settings.get_settings(user),
        await services.reports.report(user, ReportRequest.recent(14)),
        (await services.reports.export(user)).content,
        await services.results.pending_results(user),
    )


@pytest.fixture
async def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    async with open_bare(tmp_path / "iso.db", batch(WORLD_A), config=CONFIG) as bare:
        object.__setattr__(bare.ctx, "models", HandModels())
        services = bare.system.services
        for user in (A, B):
            await permissive(services, user, odds_min=1.01, odds_max=20.0)
        await services.settings.update_settings(
            A,
            replace(await services.settings.get_settings(A), bank=Decimal(5000), unit=Decimal(50)),
        )
        a_result = await analyse_as(services, A, DateSpec.today())
        b_result = await analyse_as(services, B, DateSpec.today())
        (a_signal,) = a_result.signals
        (b_signal,) = b_result.signals
        assert a_signal.signal_id != b_signal.signal_id
        await services.bets.place_bet(
            A,
            BetDraft(
                signal_id=a_signal.signal_id, odds_taken=2.1, stake=Decimal(50), client_token=TOKEN
            ),
        )
        template = await services.templates.save_template(
            A,
            TemplateDraft(
                name="A-only",
                query=AnalysisQuery.from_settings(
                    await services.settings.get_settings(A), DateSpec.today()
                ),
            ),
        )
        (team,) = await services.catalog.search_teams(FIXTURE.home.name, limit=1)
        target = WatchTarget(WatchKind.TEAM, team.team_id)
        await services.watchlist.add_watch(A, target)
        await services.results.add_note(A, a_signal.signal_id, "A's private note")
        yield World(
            bare,
            a_signal.signal_id,
            b_signal.signal_id,
            template.template_id,
            a_result.query_id,
            target,
        )


type Call = Callable[[World], Awaitable[object]]

# user B acts on identifiers that belong to user A: every call must fail as "not found"
FOREIGN_BY_ID: dict[str, Call] = {
    "get_signal": lambda w: w.services.signals.get_signal(B, w.a_signal),
    "hide_signal": lambda w: w.services.signals.hide_signal(B, w.a_signal),
    "recalc_signal": lambda w: w.services.analysis.recalc_signal(B, w.a_signal),
    "recalc_query": lambda w: w.services.analysis.recalc_query(B, w.a_query),
    "place_bet": lambda w: w.services.bets.place_bet(
        B, BetDraft(signal_id=w.a_signal, odds_taken=2.0, stake=Decimal(10))
    ),
    "place_bet_with_the_other_users_token": lambda w: w.services.bets.place_bet(
        B,
        BetDraft(signal_id=w.a_signal, odds_taken=2.0, stake=Decimal(10), client_token=TOKEN),
    ),
    "mark_result": lambda w: w.services.results.mark_result(
        B, ResultMarkRequest(signal_id=w.a_signal, mark=ResultMark.WON)
    ),
    "add_note": lambda w: w.services.results.add_note(B, w.a_signal, "mine now"),
    "delete_template": lambda w: w.services.templates.delete_template(B, w.a_template),
    "set_notifications": lambda w: w.services.watchlist.set_notifications(
        B, w.a_target, frozenset({WatchNotification.RESULT})
    ),
}


@pytest.mark.parametrize("name", list(FOREIGN_BY_ID))
async def test_foreign_ids_are_not_found_and_change_nothing(world: World, name: str) -> None:
    before = await snapshot_of(world.services, A, world.a_signal)

    with pytest.raises(NotFoundError):
        await FOREIGN_BY_ID[name](world)

    assert await snapshot_of(world.services, A, world.a_signal) == before


async def test_a_foreign_recalc_attempt_does_not_burn_the_owners_cooldown(world: World) -> None:
    with pytest.raises(NotFoundError):
        await world.services.analysis.recalc_signal(B, world.a_signal)

    diff = await world.services.analysis.recalc_signal(A, world.a_signal)  # right away: allowed

    assert diff.signal.signal_id == world.a_signal


async def test_ids_in_a_batch_that_belong_to_someone_else_are_skipped_silently(
    world: World,
) -> None:
    services = world.services
    before = await snapshot_of(services, A, world.a_signal)

    assert await services.signals.get_signals(B, [world.a_signal]) == ()
    batch_result = await services.analysis.recalc_signals(B, [world.a_signal])
    assert batch_result.diffs == ()
    assert (
        batch_result.skipped_cooldown == batch_result.skipped_started == batch_result.failed == ()
    )
    assert await services.watchlist.remove_watch(B, world.a_target) is False

    assert await snapshot_of(services, A, world.a_signal) == before


async def test_lists_and_aggregates_only_ever_contain_the_callers_own_rows(world: World) -> None:
    services = world.services
    own = await services.signals.list_signals(B, DateSpec.today(), include_hidden=True)
    exported = (await services.reports.export(B)).content.decode("utf-8-sig")

    assert [s.signal_id for s in own] == [world.b_signal]
    assert await services.bets.list_bets(B, limit=100) == ()
    assert await services.templates.list_templates(B) == ()
    assert await services.watchlist.list_watchlist(B) == ()
    assert (await services.bets.bank_summary(B)).balance is None  # A's 5000 bank is not B's
    assert (await services.settings.get_settings(B)).bank is None
    assert f"\n{world.a_signal}," not in exported
    summary = await services.results.day_summary(B, world.bare.clock().date())
    assert summary.signals == 1  # B's own signal only, not A's as well
    assert (await services.reports.report(B, ReportRequest.recent(14))).signals == 1


async def test_the_same_bet_token_from_two_users_creates_two_independent_bets(world: World) -> None:
    services = world.services
    mine = await services.bets.place_bet(
        B,
        BetDraft(signal_id=world.b_signal, odds_taken=2.0, stake=Decimal(10), client_token=TOKEN),
    )
    theirs = await services.bets.list_bets(A, limit=100)

    assert mine.stake == Decimal(10)
    assert [bet.bet_id for bet in theirs] != [mine.bet_id]
    assert len(theirs) == 1
    assert theirs[0].stake == Decimal(50)
    assert [b.bet_id for b in await services.bets.list_bets(B, limit=100)] == [mine.bet_id]


async def test_one_users_settings_change_never_reaches_the_other(world: World) -> None:
    services = world.services
    before = await services.settings.get_settings(A)

    await services.settings.update_settings(
        B,
        replace(
            await services.settings.get_settings(B), bank=Decimal(1), unit=Decimal(1), max_signals=3
        ),
    )

    assert await services.settings.get_settings(A) == before


async def test_recalc_batches_and_cooldowns_are_per_signal_owner(world: World) -> None:
    """Both users hold a signal on the same fixture; the cooldown is shared per fixture."""
    services = world.services
    await services.analysis.recalc_signal(A, world.a_signal)

    with pytest.raises(CooldownActiveError):
        await services.analysis.recalc_signal(B, world.b_signal)
    world.bare.clock.advance(timedelta(minutes=10))
    assert (
        await services.analysis.recalc_signal(B, world.b_signal)
    ).signal.signal_id == world.b_signal


async def test_validation_errors_do_not_reveal_whether_the_id_exists(world: World) -> None:
    """A foreign id and a non-existent id are indistinguishable to the caller."""
    services = world.services
    with pytest.raises(NotFoundError) as foreign:
        await services.signals.get_signal(B, world.a_signal)
    with pytest.raises(NotFoundError) as missing:
        await services.signals.get_signal(B, 987_654)

    assert type(foreign.value) is type(missing.value)
    assert re.sub(r"\d+", "N", str(foreign.value)) == re.sub(r"\d+", "N", str(missing.value))
