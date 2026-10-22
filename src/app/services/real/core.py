"""State shared by the real services and the conversions between storage and contract types.

Entity ids the bot sees are the storage ids rendered as text (``str(league.id)`` ...), which
keeps them far below the 40-byte ``callback_data`` budget. ``parse_id`` is the only place
that turns them back into integers, and a malformed id is a validation error of the field,
never a crash.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.pipeline import data
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.context import PipelineContext
from app.pipeline.profile import UserProfile, load_profile
from app.pipeline.types import SignalCard
from app.services.errors import NotFoundError, ValidationFailedError
from app.services.interfaces import (
    PRELIMINARY_AFTER,
    ConfidenceLevel,
    EntityId,
    QuietHours,
    ResultMark,
    RiskProfile,
    SignalSource,
    SignalState,
    SignalView,
    UserSettingsView,
    Verdict,
    infer_risk_profile,
)
from app.domain import Sport, MarketType

EXPERIMENTAL_SPORTS = frozenset({Sport.HOCKEY, Sport.BASKETBALL, Sport.VOLLEYBALL, Sport.TENNIS})


@dataclass(frozen=True, slots=True)
class Core:
    """The pipeline context and the per-process state of the real services."""

    ctx: PipelineContext
    busy: set[int] = field(default_factory=set)

    @property
    def engine(self) -> AnalysisEngine:
        """The one analysis engine."""
        return AnalysisEngine(self.ctx)

    def now(self) -> datetime:
        """The injected current UTC instant."""
        return self.ctx.clock()

    def local_today(self, zone: ZoneInfo) -> date:
        """Today's date in ``zone`` according to the injected clock."""
        return self.now().astimezone(zone).date()

    async def profile(self, user_id: int) -> UserProfile:
        """The stored profile of ``user_id`` (defaults for unknown users)."""
        async with self.ctx.sessions() as session:
            return await load_profile(session, user_id)


def parse_id(value: EntityId, field_name: str) -> int:
    """The storage id behind an entity id; a malformed id is a validation error."""
    try:
        return int(value)
    except ValueError:
        raise ValidationFailedError(field_name, f"unknown id {value!r}") from None


def parse_ids(values: frozenset[EntityId], field_name: str) -> frozenset[int]:
    """Storage ids of several entity ids."""
    return frozenset(parse_id(value, field_name) for value in values)


async def require_signal(core: Core, user_id: int, signal_id: int) -> data.SignalRecord:
    """The user's signal, or ``NotFoundError`` (other users' signals do not exist)."""
    async with core.ctx.sessions() as session:
        record = await data.get_signal(session, signal_id, user_id=user_id)
    if record is None:
        raise NotFoundError("signal", signal_id)
    return record


def to_view(
    core: Core,
    record: data.SignalRecord,
    *,
    card: SignalCard | None = None,
) -> SignalView:
    """The contract view of a stored signal (``card`` adds what only the pipeline knows)."""
    fixture = record.fixture.fixture
    recommended = record.recommended_at is not None and not record.retro
    verdict = None if record.llm_verdict is None else Verdict(record.llm_verdict.value)
    result = None if record.result is None else ResultMark(record.result.outcome.value)
    return SignalView(
        signal_id=record.id,
        fixture_id=str(record.fixture.id),
        sport=fixture.sport,
        league=fixture.league.name,
        home_team=fixture.home.name,
        away_team=fixture.away.name,
        kickoff=fixture.kickoff,
        market=record.market,
        selection=record.selection,
        odds=record.odds,
        bookmaker=core.ctx.config.bet_bookmaker,
        probability=record.p_model,
        ev=record.ev,
        confidence=ConfidenceLevel(record.confidence.value),
        stake_units=record.stake_units,
        llm_verdict=verdict,
        llm_note=record.llm_note,
        risk_flags=() if card is None else card.risk_flags,
        preliminary=fixture.kickoff - core.now() > PRELIMINARY_AFTER,
        no_value=not recommended and not record.retro if card is None else card.no_value,
        watchlist=record.source is data.SignalSource.WATCHLIST if card is None else card.watchlist,
        experimental=fixture.sport in EXPERIMENTAL_SPORTS,
        state=SignalState(record.status.value),
        result=result,
        version=max(record.version, 1),
        data_as_of=None if card is None else card.data_as_of,
        source=SignalSource(record.source.value),
        recommended_at=record.recommended_at if recommended else None,
    )


def settings_view(profile: UserProfile) -> UserSettingsView:
    """The contract view of a stored profile."""
    s = profile.settings
    quiet = (
        None
        if s.quiet_hours_start is None or s.quiet_hours_end is None
        else QuietHours(s.quiet_hours_start, s.quiet_hours_end)
    )
    return UserSettingsView(
        sports=frozenset(Sport(value) for value in s.sports),
        league_ids=frozenset(str(league_id) for league_id in s.leagues),
        markets=frozenset(MarketType(value) for value in s.markets),
        max_signals=s.max_signals,
        min_probability=s.min_prob,
        min_ev=s.min_ev,
        odds_min=s.odds_min,
        odds_max=s.odds_max,
        risk_profile=RiskProfile(s.risk_profile.value),
        send_time=s.send_time,
        prematch_minutes=s.prematch_minutes,
        quiet_hours=quiet,
        timezone=profile.zone.key,
        bank=profile.bank,
        unit=profile.unit,
        daily_loss_limit=s.daily_loss_limit,
        weekly_loss_limit=s.weekly_loss_limit,
    )


def settings_record(user_id: int, view: UserSettingsView) -> data.UserSettingsRecord:
    """The storage record of a settings view (risk profile re-derived from the thresholds)."""
    profile = infer_risk_profile(
        view.min_probability, view.min_ev, (view.odds_min, view.odds_max)
    )
    return data.UserSettingsRecord(
        user_id=user_id,
        sports=tuple(sorted(sport.value for sport in view.sports)),
        leagues=tuple(sorted(parse_ids(view.league_ids, "league_ids"))),
        markets=tuple(sorted(market.value for market in view.markets)),
        max_signals=view.max_signals,
        min_prob=view.min_probability,
        min_ev=view.min_ev,
        odds_min=view.odds_min,
        odds_max=view.odds_max,
        risk_profile=data.RiskProfile(profile.value),
        send_time=view.send_time,
        prematch_minutes=view.prematch_minutes,
        quiet_hours_start=None if view.quiet_hours is None else view.quiet_hours.start,
        quiet_hours_end=None if view.quiet_hours is None else view.quiet_hours.end,
        daily_loss_limit=view.daily_loss_limit,
        weekly_loss_limit=view.weekly_loss_limit,
    )


def money(value: Decimal | None) -> Decimal:
    """``value`` or zero."""
    return Decimal(0) if value is None else value

