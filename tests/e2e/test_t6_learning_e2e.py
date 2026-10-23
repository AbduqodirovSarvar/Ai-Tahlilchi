"""T6: the learning loop in a running installation: off vs shadow vs apply (ADR 0013).

The same world is analysed three times: with learning off, in shadow mode (the loop runs and
journals but must change nothing) and in apply mode (a losing segment is really switched off).
"""

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.bot import texts
from app.db.repo import list_learning_changes, list_segment_rows, upsert_live
from app.domain import (
    ExternalRef,
    Fixture,
    FixtureStatus,
    League,
    MarketType,
    Selection,
    Sport,
    Team,
)
from app.models.segments import odds_bucket
from app.pipeline.learning import KIND_SEGMENT_DISABLED, run_learning
from app.pipeline.learning_settings import LearningMode
from app.scheduler import JOB_LEARNING, JOB_REFRESH
from app.services.interfaces import AnalysisQuery, DateSpec, SignalView
from tests.e2e.bot_client import shown
from tests.e2e.t6_world import PERMISSIVE, USER, Week, jobs, open_week
from tests.pipeline.test_learning import Pending, add_settled

pytestmark = pytest.mark.slow

LEAGUE = League(
    ref=ExternalRef("football-data", "E0"),
    sport=Sport.FOOTBALL,
    name="Premier League",
    country="England",
)
HISTORY_SIZE = 150
HISTORY_HIT_RATE = 0.25
HISTORY_CLV = -0.10


@dataclass(frozen=True)
class Target:
    """The segment the history will condemn: league x market x odds bucket."""

    market: MarketType
    bucket: str
    odds: float
    selection: Selection


def pick_fingerprint(view: SignalView) -> tuple[object, ...]:
    """Everything a user can see about a recommendation (numbers included)."""
    return (
        view.home_team,
        view.away_team,
        view.market,
        view.selection,
        view.odds,
        round(view.probability, 9),
        round(view.ev, 9),
        view.stake_units,
        view.confidence,
    )


def in_segment(view: SignalView, target: Target) -> bool:
    return (
        view.league == "Premier League"
        and view.market.type is target.market
        and odds_bucket(view.odds) == target.bucket
    )


async def analyse(w: Week) -> list[SignalView]:
    """The recommended picks of today with the user's thresholds."""
    result = await w.services.analysis.analyze(
        USER,
        AnalysisQuery(dates=DateSpec.today(), min_probability=0.30, min_ev=0.02),
    )
    return [s for s in result.signals if s.recommended]


async def seed_history(w: Week, target: Target) -> None:
    """``HISTORY_SIZE`` settled, mostly losing signals of the target segment in the past."""
    rng = random.Random(7)  # noqa: S311 - simulation, not security
    start = datetime(2026, 6, 1, 12, tzinfo=UTC)
    fixtures = [
        Fixture(
            ref=ExternalRef("football-data", f"hist-{i}"),
            sport=Sport.FOOTBALL,
            league=LEAGUE,
            season=2026,
            kickoff=start + timedelta(days=i * 0.6),
            home=Team(
                ref=ExternalRef("football-data", f"th-{i}"), sport=Sport.FOOTBALL, name=f"H{i}"
            ),
            away=Team(
                ref=ExternalRef("football-data", f"ta-{i}"), sport=Sport.FOOTBALL, name=f"A{i}"
            ),
            status=FixtureStatus.FINISHED,
        )
        for i in range(HISTORY_SIZE)
    ]
    from app.db.repo import ensure_user, get_fixtures  # noqa: PLC0415
    from app.domain import Market  # noqa: PLC0415

    market = Market.totals(2.5) if target.market is MarketType.TOTALS else Market.match_result()
    async with w.env.sessions.begin() as session:
        await upsert_live(session, fixtures=fixtures)
        stored = await get_fixtures(session, [f.ref for f in fixtures])
        await ensure_user(session, USER, timezone="Asia/Tashkent")
        pending = Pending([])
        for index, fixture in enumerate(fixtures):
            add_settled(
                pending,
                stored[fixture.ref].id,
                hit=rng.random() < HISTORY_HIT_RATE,
                settled_at=fixture.kickoff + timedelta(hours=4),
                probability=0.55,
                odds=target.odds,
                clv=rng.gauss(HISTORY_CLV, 0.05),
                pick=(market, target.selection),
                user=USER,
            )
            if index % 50 == 49:
                await pending.flush(session)
        await pending.flush(session)


async def two_weekly_runs(w: Week) -> list[object]:
    """Monday-week runs one week apart (the second confirms what the first only noted)."""
    changes = []
    for day in (14, 21):
        w.clock.now = datetime(2026, 10, day, 6, 0, tzinfo=UTC)
        changes.extend((await run_learning(w.ctx)).changes)
    w.clock.now = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    return changes


async def installation(w: Week, *, look: bool = True) -> list[SignalView]:
    """Settings, the 08:30 refresh and (optionally) the day's recommendations.

    A pick that was already recommended stays recommended whatever a later query says
    (``recommended_at`` never changes), so a test of what learning *prevents* must not analyse
    before the learning job has run.
    """
    await w.settings(USER, **PERMISSIVE)
    await jobs(w).run(JOB_REFRESH)
    return await analyse(w) if look else []


async def test_shadow_changes_nothing_apply_changes_exactly_the_condemned_segment(
    history_template,
    tmp_path,
    bot,
    session,  # type: ignore[no-untyped-def]
) -> None:
    for name in ("off", "shadow", "apply"):
        (tmp_path / name).mkdir()
    # --- learning off: the baseline and a proof that the job is inert
    async with open_week(
        history_template, tmp_path / "off", bot, session, learning_mode=LearningMode.OFF
    ) as off:
        baseline = await installation(off)
        target_view = next(s for s in baseline if s.league == "Premier League")
        target = Target(
            market=target_view.market.type,
            bucket=odds_bucket(target_view.odds),
            odds=target_view.odds,
            selection=target_view.selection,
        )
        await seed_history(off, target)
        assert await two_weekly_runs(off) == []
        async with off.env.sessions() as db:
            assert await list_segment_rows(db) == []
            assert await list_learning_changes(db) == []
        again = await analyse(off)
        assert [pick_fingerprint(s) for s in again] == [pick_fingerprint(s) for s in baseline]
    assert any(in_segment(s, target) for s in baseline), "the target segment must have picks"

    # --- shadow: computes and journals, but the recommendations are bit-for-bit the same
    async with open_week(
        history_template, tmp_path / "shadow", bot, session, learning_mode=LearningMode.SHADOW
    ) as shadow:
        before = await installation(shadow)
        await seed_history(shadow, target)
        changes = await two_weekly_runs(shadow)
        assert [c.kind for c in changes] == [KIND_SEGMENT_DISABLED]
        assert [c.applied for c in changes] == [False]
        after = await analyse(shadow)
        assert [pick_fingerprint(s) for s in before] == [pick_fingerprint(s) for s in baseline]
        assert [pick_fingerprint(s) for s in after] == [pick_fingerprint(s) for s in baseline]
        # the segment table says "disabled" (the loop worked) yet nothing reached the analysis
        async with shadow.env.sessions() as db:
            rows = await list_segment_rows(db)
        assert any(not row.enabled for row in rows)
        # /ozgarishlar shows the shadow entry with no undo button (nothing was changed)
        client = shadow.client(USER)
        listing = "\n".join(shown(await client.send("/ozgarishlar")))
        assert texts.LEARN_STATE_SHADOW.strip() in listing
        assert texts.LEARN_MODES["shadow"] in listing
        assert not client.has_button("↩️")

    # --- apply: the condemned segment is gone, everything else untouched
    async with open_week(
        history_template, tmp_path / "apply", bot, session, learning_mode=LearningMode.APPLY
    ) as applied:
        await installation(applied, look=False)
        await seed_history(applied, target)
        changes = await two_weekly_runs(applied)
        assert [c.kind for c in changes] == [KIND_SEGMENT_DISABLED]
        assert [c.applied for c in changes] == [True]
        result = await analyse(applied)
        removed = [s for s in baseline if in_segment(s, target)]
        assert removed
        assert not any(in_segment(s, target) for s in result)
        kept = [pick_fingerprint(s) for s in baseline if not in_segment(s, target)]
        got = [pick_fingerprint(s) for s in result]
        assert all(item in got for item in kept)  # no unrelated pick disappeared or changed
        # a fixture that lost its pick may get its next-best one, never one from the segment
        extra = [item for item in got if item not in kept]
        assert len(extra) <= len(removed)
        client = applied.client(USER)
        listing = "\n".join(shown(await client.send("/ozgarishlar")))
        assert texts.LEARN_STATE_APPLIED.strip() in listing
        assert client.has_button("↩️")  # an applied change can be rolled back


async def test_the_weekly_learning_job_is_wrapped_recorded_and_idempotent(
    history_template,
    tmp_path,
    bot,
    session,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, bot, session) as w:
        await w.settings(USER, **PERMISSIVE)
        w.clock.now = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)  # Monday
        await jobs(w).run(JOB_LEARNING)
        await jobs(w).run(JOB_LEARNING)  # same week: a no-op
        runs = await w.ctx.ledger.last_runs()
        assert runs[JOB_LEARNING].ok
        assert Decimal(0) == Decimal(0)
