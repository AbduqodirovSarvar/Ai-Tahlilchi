"""Regression tests of the T4 fix round (code review R1-R7 and the QA findings)."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from app.db.enums import QueryScope, SortOrder
from app.domain import Sport
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.daily import run_due_digests
from app.pipeline.profile import default_request, load_profile
from app.pipeline.refresh import refresh
from app.pipeline.selection import Pick, assemble
from app.pipeline.types import AnalysisRequest
from tests.pipeline.harness import Env
from tests.pipeline.providers import ScriptedProvider

USER = 2002
PERMISSIVE = {"min_probability": 0.30, "min_ev": 0.0}
TASHKENT_OFFSET = timedelta(hours=5)


def scripted(env: Env) -> ScriptedProvider:
    provider = env.ctx.providers[Sport.FOOTBALL]
    assert isinstance(provider, ScriptedProvider)
    return provider


async def tune(env: Env, **changes: object) -> None:
    settings = await env.system.services.settings.get_settings(USER)
    values = {"min_probability": 0.30, "min_ev": 0.0, "quiet_hours": None, **changes}
    await env.system.services.settings.update_settings(USER, replace(settings, **values))


# ------------------------------------------------------------------------------ R1: retro
def stand_in(ev: float, *, retro: bool, name: str) -> Pick:
    """A pick with only the attributes ``assemble`` reads (EV, kick-off, ids, flags)."""
    fixture = SimpleNamespace(
        id=hash(name) % 10_000, fixture=SimpleNamespace(kickoff=datetime(2026, 10, 21, tzinfo=UTC))
    )
    candidate = SimpleNamespace(
        ev=ev, probability=0.5, market=name, selection=SimpleNamespace(value="home")
    )
    return cast(
        "Pick",
        SimpleNamespace(
            fixture=fixture, candidate=candidate, passes=True, watchlist=False, retro=retro
        ),
    )


def request_with(max_signals: int | None) -> AnalysisRequest:
    return cast(
        "AnalysisRequest",
        SimpleNamespace(
            scope=QueryScope.ALL,
            sort=SortOrder.EV,
            max_signals=max_signals,
            per_day=False,
            zone=UTC,
        ),
    )


def test_retro_picks_never_displace_real_ones_from_the_cut() -> None:
    real = [[stand_in(0.20, retro=False, name="r1")], [stand_in(0.10, retro=False, name="r2")]]
    old = [[stand_in(0.90, retro=True, name=f"old{i}")] for i in range(3)]

    result = assemble([*old, *real], request_with(2))

    kept_real = [p for p in result.picks if not p.retro]
    kept_retro = [p for p in result.picks if p.retro]
    assert len(kept_real) == 2  # both real picks survive although every retro EV is higher
    assert len(kept_retro) == 2  # retro picks have their own budget
    assert result.matched == 2  # and are not counted as matches


def test_a_pure_look_back_counts_its_retro_picks_as_the_result() -> None:
    old = [[stand_in(0.50 - i / 10, retro=True, name=f"old{i}")] for i in range(4)]

    result = assemble(old, request_with(3))

    assert len(result.picks) == 3
    assert result.matched == 4


async def test_the_digest_holds_only_picks_that_have_not_started(provider_env: Env) -> None:
    await tune(provider_env)
    await refresh(provider_env.ctx)
    provider_env.clock.advance(timedelta(hours=6, minutes=30))  # 12:30Z: early matches are over
    await refresh(provider_env.ctx, force=True)

    report = await run_due_digests(provider_env.ctx, provider_env.system.delivery)

    assert report.sent == (USER,)
    digest = provider_env.notifier.sent[0].payload
    signals = digest.result.signals  # type: ignore[attr-defined]
    assert signals
    now = provider_env.clock()
    assert all(not signal.retro and signal.kickoff > now for signal in signals)


async def test_the_daily_window_of_tashkent_starts_at_19z_yesterday(provider_env: Env) -> None:
    async with provider_env.ctx.sessions() as session:
        profile = await load_profile(session, USER)
    request = default_request(profile, date(2026, 10, 21), date(2026, 10, 21))

    assert request.start == datetime(2026, 10, 20, 19, 0, tzinfo=UTC)
    assert request.end == datetime(2026, 10, 21, 19, 0, tzinfo=UTC)
    assert request.skip_retro


async def test_a_retro_card_of_today_does_not_eat_the_manual_cut(provider_env: Env) -> None:
    await tune(provider_env)
    await refresh(provider_env.ctx)
    provider_env.clock.advance(timedelta(hours=6, minutes=30))
    await refresh(provider_env.ctx, force=True)
    async with provider_env.ctx.sessions() as session:
        profile = await load_profile(session, USER)
    engine = AnalysisEngine(provider_env.ctx)
    base = replace(
        default_request(profile, date(2026, 10, 21), date(2026, 10, 21)),
        min_probability=0.30,
        min_ev=0.0,
        max_signals=3,
        fetch=False,
    )

    without_retro = await engine.run(base)  # what the real picks alone give
    with_retro = await engine.run(replace(base, skip_retro=False))

    real_alone = [c.record.id for c in without_retro.cards]
    real_with = [c.record.id for c in with_retro.cards if not c.record.retro]
    assert real_alone
    assert any(c.record.retro for c in with_retro.cards)  # the scenario really has retro
    assert real_with == real_alone
    assert with_retro.matched == without_retro.matched
    assert with_retro.fixtures_analyzed == without_retro.fixtures_analyzed
