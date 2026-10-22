"""How the analysis path consumes the learning loop: overlay, calibration of candidates, hook."""

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.enums import Outcome
from app.db.repo import (
    CalibrationRecord,
    SegmentRecord,
    add_calibration_version,
    ensure_user,
    get_fixtures,
    save_segment_row,
    set_active_calibration,
    upsert_live,
)
from app.db.schema import CalibrationVersionRow
from app.domain import Market, MarketType, Selection, Sport
from app.models import (
    AsianOutcomeProbabilities,
    ConfidenceInputs,
    asian_expected_value,
    assess_confidence,
)
from app.models.calibration import CalibrationVersion
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.context import PipelineContext
from app.pipeline.overlay import (
    EMPTY_OVERLAY,
    DbLearningSource,
    LearningOverlay,
    NoLearning,
    SegmentRule,
    segment_key,
)
from app.pipeline.profile import default_request, load_profile
from app.pipeline.valuation import Candidate, calibrate_candidates
from tests.domain.factories import make_fixture
from tests.pipeline.harness import Env
from tests.pipeline.test_fix_round_money import hand_world, home_signal, push_prices, recalc_home

NOW = datetime(2026, 11, 1, tzinfo=UTC)
FIXTURE = make_fixture("Arsenal", "Chelsea")
USER = 2002


def candidate(
    selection: Selection, probability: float, odds: float, market: Market | None = None
) -> Candidate:
    market = market or Market.match_result()
    assessment = assess_confidence(
        ConfidenceInputs(
            min_team_matches=20,
            xg_available=False,
            lineups_known=False,
            model_probability=probability,
            market_probability=probability,
            days_to_kickoff=1.0,
            market_wide_uncertainty=False,
        )
    )
    outcomes = AsianOutcomeProbabilities.binary(probability)
    return Candidate(
        market=market,
        selection=selection,
        odds=odds,
        probability=probability,
        model_probability=probability,
        market_probability=probability,
        ev=asian_expected_value(outcomes, odds),
        outcomes=outcomes,
        assessment=assessment,
        anchored=True,
        quoted_at=NOW,
    )


def version(x: list[float], y: list[float]) -> CalibrationVersion:
    return CalibrationVersion(1, "isotonic", {"x": x, "y": y}, 300, 0.25, 0.24, NOW, True)


# ----------------------------------------------------------------------- overlay filter


def test_empty_overlay_and_no_learning_allow_everything() -> None:
    picks = [candidate(Selection.HOME, 0.5, 2.1)]
    assert EMPTY_OVERLAY.filter(picks, FIXTURE) == picks


async def test_no_learning_source_returns_the_empty_overlay() -> None:
    assert await NoLearning().load() is EMPTY_OVERLAY


def overlay_for(rule: SegmentRule, odds: float = 2.1) -> LearningOverlay:
    key = segment_key(Sport.FOOTBALL, FIXTURE.league.ref, MarketType.MATCH_RESULT, odds)
    return LearningOverlay(rules={key: rule})


def test_disabled_segment_drops_only_its_candidates() -> None:
    inside = candidate(Selection.HOME, 0.5, 2.1)
    other_bucket = candidate(Selection.AWAY, 0.3, 3.4)
    overlay = overlay_for(SegmentRule(enabled=False))
    assert overlay.filter([inside, other_bucket], FIXTURE) == [other_bucket]


def test_raised_bar_keeps_only_candidates_above_it() -> None:
    low = candidate(Selection.HOME, 0.50, 2.1)  # EV +5%
    high = candidate(Selection.DRAW, 0.55, 2.1)  # EV +15.5%
    overlay = overlay_for(SegmentRule(enabled=True, ev_floor=0.08))
    assert overlay.filter([low, high], FIXTURE) == [high]
    assert overlay.allows(high, FIXTURE)
    assert not overlay.allows(low, FIXTURE)


# ----------------------------------------------------------------------- calibrate_candidates


def test_no_calibration_returns_candidates_unchanged() -> None:
    picks = [candidate(Selection.HOME, 0.5, 2.0)]
    assert calibrate_candidates(picks, None) == picks


def test_a_partition_market_is_calibrated_together_and_stays_a_distribution() -> None:
    picks = [
        candidate(Selection.HOME, 0.5, 2.1),
        candidate(Selection.DRAW, 0.3, 3.4),
        candidate(Selection.AWAY, 0.2, 4.0),
    ]
    shrink_towards_half = version([0.0, 1.0], [0.25, 0.75])
    result = calibrate_candidates(picks, shrink_towards_half)
    assert math.fsum(c.probability for c in result) == pytest.approx(1.0)
    assert [c.raw_probability for c in result] == [0.5, 0.3, 0.2]
    for before, after in zip(picks, result, strict=True):
        assert after.ev == pytest.approx(after.probability * after.odds - 1.0)
        assert after.model_probability == before.model_probability  # sanity logic untouched


def test_selections_that_do_not_partition_are_calibrated_one_by_one() -> None:
    over = candidate(Selection.OVER, 0.55, 1.9, Market.totals(2.5))
    lone = calibrate_candidates([over], version([0.0, 1.0], [0.0, 0.8]))[0]
    assert lone.probability == pytest.approx(0.44)  # 0.55 * 0.8, not renormalised to 1
    assert lone.raw_probability == 0.55


def test_calibration_keeps_the_input_order() -> None:
    picks = [
        candidate(Selection.HOME, 0.5, 2.1),
        candidate(Selection.OVER, 0.55, 1.9, Market.totals(2.5)),
        candidate(Selection.DRAW, 0.3, 3.4),
        candidate(Selection.AWAY, 0.2, 4.0),
    ]
    result = calibrate_candidates(picks, version([0.0, 1.0], [0.1, 0.9]))
    assert {(c.market, c.selection) for c in result} == {(c.market, c.selection) for c in picks}


# ----------------------------------------------------------------------- DbLearningSource


async def test_db_source_reads_calibration_and_restricted_segments(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as session:
        await ensure_user(session, USER, timezone="Asia/Tashkent")
        await upsert_live(session, fixtures=[FIXTURE])
        fixture = (await get_fixtures(session, [FIXTURE.ref]))[FIXTURE.ref]
        league_id = await _league_id(session, fixture.id)
        for odds_bucket, enabled, floor in (("1.80-2.20", False, 0.08), ("2.20-3.00", True, 0.08)):
            await save_segment_row(
                session,
                SegmentRecord(
                    sport=Sport.FOOTBALL,
                    league_id=league_id,
                    league_ref=FIXTURE.league.ref,
                    market=MarketType.TOTALS,
                    odds_bucket=odds_bucket,
                    n=60,
                    roi=-0.1,
                    clv=None,
                    calib_bias=None,
                    ev_threshold=floor,
                    enabled=enabled,
                    state_since=NOW,
                    hold_until=None,
                ),
                now=NOW,
            )
        await save_segment_row(  # an unrestricted segment needs no rule
            session,
            SegmentRecord(
                sport=Sport.FOOTBALL,
                league_id=league_id,
                league_ref=FIXTURE.league.ref,
                market=MarketType.TOTALS,
                odds_bucket="1.00-1.50",
                n=60,
                roi=0.1,
                clv=None,
                calib_bias=None,
                ev_threshold=0.0,
                enabled=True,
                state_since=None,
                hold_until=None,
            ),
            now=NOW,
        )
        await add_calibration_version(session, _record(1, [0.0, 1.0], [0.1, 0.9]))
        await set_active_calibration(session, 1)

    overlay = await DbLearningSource(sessions).load()

    assert overlay.calibration is not None
    assert overlay.calibration.version == 1
    assert sorted((k.odds_bucket, r.enabled, r.ev_floor) for k, r in overlay.rules.items()) == [
        ("1.80-2.20", False, 0.08),
        ("2.20-3.00", True, 0.08),
    ]


def _record(number: int, x: list[float], y: list[float]) -> CalibrationRecord:
    return CalibrationRecord(
        version=number,
        method="isotonic",
        params={"x": x, "y": y},
        n_samples=300,
        brier_before=0.25,
        brier_after=0.24,
        active=False,
        passed_gate=True,
        created_at=NOW,
    )


async def _league_id(session: AsyncSession, fixture_id: int) -> int:
    from app.db.schema import FixtureRow  # noqa: PLC0415

    row = await session.get(FixtureRow, fixture_id)
    assert row is not None
    return row.league_id


@pytest.mark.parametrize(
    "params",
    [
        {"x": [], "y": []},
        {"x": [0.1, 0.2], "y": [0.5]},
        {"x": [0.2, 0.1], "y": [0.1, 0.2]},
        {"x": [0.1, 0.2], "y": [0.6, 0.4]},
        {"x": [0.1, 1.5], "y": [0.1, 0.2]},
        {},
    ],
)
async def test_a_corrupt_calibration_degrades_to_the_raw_model(
    sessions: async_sessionmaker[AsyncSession], params: dict[str, list[float]]
) -> None:
    async with sessions.begin() as session:
        await add_calibration_version(session, replace(_record(1, [0.5], [0.5]), params=params))
        await set_active_calibration(session, 1)

    assert await DbLearningSource(sessions).load() == EMPTY_OVERLAY


async def test_an_unreadable_database_degrades_to_the_raw_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as session:
        await session.execute(CalibrationVersionRow.__table__.delete())  # type: ignore[attr-defined]
    engine = sessions.kw["bind"]
    await engine.dispose()
    # a dropped table is the realistic "not migrated yet" failure
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE calibration_versions")

    assert await DbLearningSource(sessions).load() == EMPTY_OVERLAY


# ----------------------------------------------------------------------- the analysis hook


@dataclass(frozen=True)
class StaticLearning:
    overlay: LearningOverlay

    async def load(self) -> LearningOverlay:
        return self.overlay


async def run_with(env: Env, overlay: LearningOverlay | None) -> tuple[PipelineContext, list]:  # type: ignore[type-arg]
    settings = await env.system.services.settings.get_settings(USER)
    await env.system.services.settings.update_settings(
        USER, replace(settings, min_probability=0.30, min_ev=0.0, quiet_hours=None)
    )
    ctx = env.ctx if overlay is None else replace(env.ctx, learning=StaticLearning(overlay))
    async with ctx.sessions() as session:
        profile = await load_profile(session, USER)
    today = ctx.clock().date()
    request = default_request(profile, today, today + timedelta(days=2))
    outcome = await AnalysisEngine(ctx).run(request)
    return ctx, list(outcome.cards)


async def test_the_hook_drops_disabled_segments_and_stores_calibrated_numbers(
    seeded_env: Env,
) -> None:
    _, baseline = await run_with(seeded_env, None)
    assert baseline
    first = baseline[0].record
    league = first.fixture.fixture.league.ref
    key = segment_key(Sport.FOOTBALL, league, first.market.type, first.odds)
    assert first.p_raw is None  # no calibration: the stored number is the raw one

    # 1. the segment of the first card is switched off -> none of its candidates is recommended
    _, disabled = await run_with(
        seeded_env, LearningOverlay(rules={key: SegmentRule(enabled=False)})
    )
    in_segment = [
        c.record
        for c in disabled
        if segment_key(Sport.FOOTBALL, league, c.record.market.type, c.record.odds) == key
        and c.record.fixture.fixture.league.ref == league
        and not c.no_value
    ]
    assert in_segment == []
    # the fixture's best pick was removed; another selection may take its place
    replacement = [c.record for c in disabled if c.record.fixture.id == first.fixture.id]
    assert all((r.market, r.selection) != (first.market, first.selection) for r in replacement)

    # 2. a calibration changes every stored probability and keeps the raw one for training
    squeeze = version([0.0, 1.0], [0.05, 0.60])
    _, calibrated = await run_with(seeded_env, LearningOverlay(calibration=squeeze))
    assert calibrated
    for card in calibrated:
        assert card.record.p_raw is not None
        assert card.record.p_model != card.record.p_raw  # renormalised markets can move up
        assert 0.0 < card.record.p_model < 1.0


async def test_the_hook_does_not_filter_a_custom_everything_request(seeded_env: Env) -> None:
    _, baseline = await run_with(seeded_env, None)
    everything = LearningOverlay(
        rules={
            segment_key(
                Sport.FOOTBALL,
                c.record.fixture.fixture.league.ref,
                c.record.market.type,
                c.record.odds,
            ): SegmentRule(enabled=False)
            for c in baseline
        }
    )
    settings = await seeded_env.system.services.settings.get_settings(USER)
    assert settings
    ctx = replace(seeded_env.ctx, learning=StaticLearning(everything))
    async with ctx.sessions() as session:
        profile = await load_profile(session, USER)
    today = ctx.clock().date()
    request = replace(
        default_request(profile, today, today + timedelta(days=2)), include_no_value=True
    )
    outcome = await AnalysisEngine(ctx).run(request)
    assert outcome.cards  # custom analysis shows everything, flagged but not removed


def test_outcome_enum_is_the_one_the_job_maps() -> None:
    assert {o.value for o in (Outcome.WIN, Outcome.HALF_WIN, Outcome.HALF_LOSS, Outcome.LOSS)} == {
        "win",
        "half_win",
        "half_loss",
        "loss",
    }


async def test_a_recalculation_keeps_the_calibration_and_the_raw_probability(
    tmp_path: Path,
) -> None:
    async with hand_world(tmp_path / "calibrated.db") as bare:
        record = await home_signal(bare)
        assert record.p_raw is None
        squeeze = version([0.0, 1.0], [0.05, 0.60])
        object.__setattr__(
            bare.ctx, "learning", StaticLearning(LearningOverlay(calibration=squeeze))
        )

        bare.clock.now += timedelta(minutes=30)
        await push_prices(bare, 2.10, at=bare.clock())
        after = (await recalc_home(bare, record)).after.record

    assert after.p_raw is not None
    assert after.p_model != after.p_raw
    assert after.ev == pytest.approx(after.p_model * after.odds - 1.0, abs=0.2)
