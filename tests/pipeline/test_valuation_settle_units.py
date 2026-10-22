"""Valuation skips and staking, settlement arithmetic, CLV, the model cache, the detail card."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.enums import Outcome
from app.db.repo import list_fixtures
from app.domain import (
    BOOKMAKER_PINNACLE,
    ExternalRef,
    Market,
    MarketType,
    OddsKind,
    OddsSnapshot,
    Selection,
    Sport,
)
from app.models import Settlement, team_key
from app.pipeline.config import PipelineConfig, StakingMode
from app.pipeline.detail import build_fixture_card
from app.pipeline.modeling import FittedLeague
from app.pipeline.odds import current_quotes, data_as_of, load_decision_snapshots
from app.pipeline.settle import clv_of, profit_of
from app.pipeline.valuation import (
    FLAG_NO_MARKET_REFERENCE,
    SKIP_NO_MODEL,
    SKIP_NO_PRICE,
    SKIP_THIN_HISTORY,
    SKIP_UNKNOWN_TEAM,
    ValuationInput,
    stake_units,
    value_fixture,
)
from tests.pipeline.harness import Env
from tests.pipeline.world import START, team_of


async def one_fixture(env: Env):  # type: ignore[no-untyped-def]
    async with env.ctx.sessions() as session:
        page = await list_fixtures(session, start=START, end=START + timedelta(days=1))
        record = page.items[0]
        snapshots = (await load_decision_snapshots(session, [record.fixture.ref]))[
            record.fixture.ref
        ]
    quotes = current_quotes(snapshots, record.fixture.ref, record.fixture.kickoff)
    league = env.ctx.leagues.canonical(record.fixture.league.ref)
    fitted = await env.ctx.models.get(league, START.date() + timedelta(days=1))
    return record, quotes, fitted, snapshots


def item(record, quotes, fitted, **changes) -> ValuationInput:  # type: ignore[no-untyped-def]
    values = {
        "fixture": record.fixture,
        "quotes": quotes,
        "fitted": fitted,
        "now": START,
        "markets": frozenset(MarketType),
    }
    return ValuationInput(**{**values, **changes})


async def test_a_fixture_is_valued_with_every_priced_selection(seeded_env: Env) -> None:
    record, quotes, fitted, snapshots = await one_fixture(seeded_env)

    valuation = value_fixture(item(record, quotes, fitted), seeded_env.ctx.config)

    assert valuation.skip is None
    assert {c.market.type for c in valuation.candidates} == set(MarketType)
    for candidate in valuation.candidates:
        assert 0 < candidate.probability <= 1
        assert candidate.anchored
        assert candidate.confidence.value in {"medium", "low"}  # never high without xG/line-ups
    assert data_as_of(quotes, seeded_env.ctx.config.bet_bookmaker) is not None
    assert data_as_of(quotes, "nobody") is None
    assert snapshots


async def test_the_reasons_a_fixture_is_skipped(seeded_env: Env) -> None:
    record, quotes, fitted, _ = await one_fixture(seeded_env)
    config = seeded_env.ctx.config

    no_price = value_fixture(item(record, quotes, fitted), replace(config, bet_bookmaker="nobody"))
    no_model = value_fixture(item(record, quotes, FittedLeague(None, {})), config)
    thin = value_fixture(item(record, quotes, fitted), replace(config, min_team_matches=10_000))
    newcomer = team_of("Newly Promoted FC")
    promoted = replace(record.fixture, home=newcomer)
    counts = {team_key(newcomer): 20, team_key(record.fixture.away): 20}
    unknown = value_fixture(
        item(replace(record, fixture=promoted), quotes, FittedLeague(fitted.model, counts)), config
    )

    assert no_price.skip == SKIP_NO_PRICE
    assert no_model.skip == SKIP_NO_MODEL
    assert thin.skip == SKIP_THIN_HISTORY
    assert unknown.skip == SKIP_UNKNOWN_TEAM


async def test_markets_without_a_market_anchor_need_the_custom_analysis(seeded_env: Env) -> None:
    record, quotes, fitted, _ = await one_fixture(seeded_env)
    unanchored = [q for q in quotes if q.bookmaker != BOOKMAKER_PINNACLE]
    config = seeded_env.ctx.config

    strict = value_fixture(item(record, unanchored, fitted), config)
    custom = value_fixture(item(record, unanchored, fitted, allow_model_only=True), config)

    assert strict.candidates == ()
    assert custom.candidates
    assert all(FLAG_NO_MARKET_REFERENCE in c.risk_flags for c in custom.candidates)


async def test_staking_flat_and_kelly(seeded_env: Env) -> None:
    record, quotes, fitted, _ = await one_fixture(seeded_env)
    config = seeded_env.ctx.config
    candidates = value_fixture(item(record, quotes, fitted), config).candidates
    positive = max(candidates, key=lambda c: c.ev)
    negative = min(candidates, key=lambda c: c.ev)
    assert positive.ev > 0 > negative.ev

    assert stake_units(negative, config, bank=None, unit=None) == Decimal(0)
    assert stake_units(positive, config, bank=None, unit=None) == config.flat_unit
    kelly = replace(config, staking=StakingMode.KELLY)
    units = stake_units(positive, kelly, bank=Decimal(1000), unit=Decimal(10))
    assert Decimal("0.1") <= units <= Decimal(2)  # at most 2% of the bank = 2 units
    assert stake_units(positive, kelly, bank=None, unit=None) == config.flat_unit


def test_profit_arithmetic() -> None:
    assert profit_of(Decimal(2), 2.5, Settlement.WIN) == Decimal(3)
    assert profit_of(Decimal(2), 2.5, Settlement.LOSS) == Decimal(-2)
    assert profit_of(Decimal(2), 2.5, Settlement.PUSH) == Decimal(0)
    assert profit_of(Decimal(2), 2.5, Settlement.HALF_WIN) == Decimal("1.5")
    assert profit_of(Decimal(2), 2.5, Settlement.HALF_LOSS) == Decimal(-1)
    assert Outcome.WIN.value == "win"


def test_clv_against_the_devigged_close() -> None:
    fixture = ExternalRef("football-data", "f")
    close = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)

    def row(bookmaker: str, selection: Selection, odds: float) -> OddsSnapshot:
        return OddsSnapshot(
            fixture=fixture,
            bookmaker=bookmaker,
            market=Market.btts(),
            selection=selection,
            odds=odds,
            taken_at=close,
            kind=OddsKind.PREMATCH,
        )

    snapshots = [
        row(BOOKMAKER_PINNACLE, Selection.YES, 1.90),
        row(BOOKMAKER_PINNACLE, Selection.NO, 2.00),
        row("1xbet", Selection.YES, 1.80),
        row("1xbet", Selection.NO, 2.05),
    ]

    closing, clv = clv_of(snapshots, fixture, Market.btts(), Selection.YES, odds=2.10)

    assert closing is not None
    assert clv is not None
    assert clv > 0  # 2.10 beat a fair close of about 1.95
    assert clv_of([], fixture, Market.btts(), Selection.YES, odds=2.1) == (None, None)
    only_soft = [s for s in snapshots if s.bookmaker == "1xbet"]
    assert clv_of(only_soft, fixture, Market.btts(), Selection.YES, odds=2.1)[1] is None


async def test_the_model_is_fitted_once_per_league_and_day(seeded_env: Env) -> None:
    _, _, fitted, _ = await one_fixture(seeded_env)
    league = ExternalRef("football-data", "E0")
    day = START.date() + timedelta(days=1)

    again = await seeded_env.ctx.models.get(league, day)
    assert again is fitted or again == fitted
    assert fitted.model is not None
    assert len(fitted.team_matches) == 12
    broken = await seeded_env.ctx.models.get(ExternalRef("football-data", "NOPE"), day)
    assert broken.model is None
    assert broken.reason


async def test_the_detail_card_collects_form_scores_and_prices(seeded_env: Env) -> None:
    async with seeded_env.ctx.sessions() as session:
        page = await list_fixtures(session, start=START, end=START + timedelta(days=1))
    record = page.items[0]

    card = await build_fixture_card(seeded_env.ctx, record.id)

    assert card is not None
    assert len(card.home_form) == 5
    assert card.home_stats.matches > 0
    assert card.head_to_head
    assert len(card.top_scores) == 3
    assert card.top_scores[0][2] >= card.top_scores[1][2]
    assert card.quotes
    assert card.data_as_of is not None
    assert await build_fixture_card(seeded_env.ctx, 10**9) is None


def test_configuration_is_validated() -> None:
    from app.errors import ConfigurationError  # noqa: PLC0415

    with pytest.raises(ConfigurationError):
        PipelineConfig(model_weight=1.5)
    with pytest.raises(ConfigurationError):
        PipelineConfig(refresh_days_ahead=0)
    assert Sport.FOOTBALL
