"""SQLAlchemy 2 ORM schema for every table of the main plan (§7), plus leagues and teams.

Conventions (see ``docs/adr/0002-domain-and-schema.md``):

- Every timestamp is :class:`~app.db.types.UTCDateTime`; every money amount (bank, stake,
  profit, units) is :class:`~app.db.types.Money` (``Decimal``); odds and probabilities are
  ``float``.
- Provider entities are unique by ``(source, external_id)``; that pair is the upsert key.
- Market lines are stored as ``0`` for markets without a line, so a line is never NULL
  and the odds/signal unique keys work on every backend (NULLs never collide in UNIQUE).
- Enums are stored as their string values without database CHECKs (adding a member is not
  a migration); the ORM validates them on write.
- Relationships use ``lazy="raise"``: accidental N+1 lazy loads fail loudly; load related
  rows explicitly with ``selectinload``/``joinedload``.
"""

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, ClassVar, Final

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.enums import (
    Confidence,
    DateMode,
    LessonStatus,
    LlmVerdict,
    Outcome,
    PostmortemCategory,
    QueryScope,
    RecalcKind,
    ResultSource,
    RiskProfile,
    SignalSource,
    SignalStatus,
    SignalVersionReason,
    SortOrder,
    WatchKind,
)
from app.db.types import Money, UTCDateTime, enum_column
from app.domain import FixtureStatus, MarketType, OddsKind, Selection, Sport

NAMING_CONVENTION: Final = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
SOURCE_LENGTH: Final = 64
EXTERNAL_ID_LENGTH: Final = 255
NAME_LENGTH: Final = 255
SHORT_TEXT_LENGTH: Final = 64
NO_LINE: Final = 0.0
_LINE_MARKETS_SQL: Final = f"('{MarketType.TOTALS.value}', '{MarketType.ASIAN_HANDICAP.value}')"
LINE_ONLY_FOR_LINE_MARKETS_SQL: Final = f"market IN {_LINE_MARKETS_SQL} OR line = 0"
# Opening and closing are single moments: at most one price per selection and kind.
SINGLE_MOMENT_KINDS: Final = frozenset({OddsKind.OPENING, OddsKind.CLOSING})
SINGLE_MOMENT_ODDS_SQL: Final = f"kind IN ('{OddsKind.OPENING.value}', '{OddsKind.CLOSING.value}')"


def utcnow() -> datetime:
    """Current time in UTC (column default)."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base with deterministic constraint names (stable Alembic diffs)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: UTCDateTime(),
        Decimal: Money(),
        date: Date(),
        time: Time(),
    }


class CreatedAtMixin:
    """``created_at`` set on insert."""

    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TimestampMixin(CreatedAtMixin):
    """``created_at`` and ``updated_at`` (bulk upserts must set ``updated_at`` themselves)."""

    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- reference data


class LeagueRow(TimestampMixin, Base):
    """A competition at a data source."""

    __tablename__ = "leagues"
    __table_args__ = (UniqueConstraint("source", "external_id"), Index(None, "sport"))

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[Sport] = mapped_column(enum_column(Sport))
    source: Mapped[str] = mapped_column(String(SOURCE_LENGTH))
    external_id: Mapped[str] = mapped_column(String(EXTERNAL_ID_LENGTH))
    name: Mapped[str] = mapped_column(String(NAME_LENGTH))
    country: Mapped[str | None] = mapped_column(String(NAME_LENGTH))


class TeamRow(TimestampMixin, Base):
    """A team as named by its source, with a normalized name for cross-source lookup."""

    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index(None, "sport", "normalized_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[Sport] = mapped_column(enum_column(Sport))
    source: Mapped[str] = mapped_column(String(SOURCE_LENGTH))
    external_id: Mapped[str] = mapped_column(String(EXTERNAL_ID_LENGTH))
    name: Mapped[str] = mapped_column(String(NAME_LENGTH))
    normalized_name: Mapped[str] = mapped_column(String(NAME_LENGTH))
    country: Mapped[str | None] = mapped_column(String(NAME_LENGTH))


class FixtureRow(TimestampMixin, Base):
    """A fixture with its latest known status and full-time/half-time result."""

    __tablename__ = "fixtures"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        CheckConstraint("home_team_id <> away_team_id", name="distinct_teams"),
        CheckConstraint(
            "(home_score IS NULL OR home_score >= 0) AND (away_score IS NULL OR away_score >= 0)",
            name="non_negative_scores",
        ),
        Index(None, "kickoff_at"),
        Index(None, "league_id", "season", "kickoff_at"),
        Index(None, "home_team_id", "kickoff_at"),
        Index(None, "away_team_id", "kickoff_at"),
        Index(None, "status", "kickoff_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[Sport] = mapped_column(enum_column(Sport))
    source: Mapped[str] = mapped_column(String(SOURCE_LENGTH))
    external_id: Mapped[str] = mapped_column(String(EXTERNAL_ID_LENGTH))
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id", ondelete="RESTRICT"))
    season: Mapped[int]
    kickoff_at: Mapped[datetime]
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="RESTRICT"))
    status: Mapped[FixtureStatus] = mapped_column(enum_column(FixtureStatus))
    home_score: Mapped[int | None]
    away_score: Mapped[int | None]
    home_ht_score: Mapped[int | None]
    away_ht_score: Mapped[int | None]
    home_xg: Mapped[float | None]
    away_xg: Mapped[float | None]

    league: Mapped[LeagueRow] = relationship(lazy="raise")
    home_team: Mapped[TeamRow] = relationship(foreign_keys=[home_team_id], lazy="raise")
    away_team: Mapped[TeamRow] = relationship(foreign_keys=[away_team_id], lazy="raise")


class OddsSnapshotRow(Base):
    """One price of one selection at one moment (``kind`` says which moment)."""

    __tablename__ = "odds_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "fixture_id", "bookmaker", "market", "selection", "line", "kind", "taken_at"
        ),
        CheckConstraint("odds > 1", name="odds_above_one"),
        CheckConstraint(LINE_ONLY_FOR_LINE_MARKETS_SQL, name="line_only_for_line_markets"),
        Index(None, "taken_at"),
        Index(
            "uq_odds_snapshots_single_moment",
            "fixture_id",
            "bookmaker",
            "market",
            "selection",
            "line",
            "kind",
            unique=True,
            sqlite_where=text(SINGLE_MOMENT_ODDS_SQL),
            postgresql_where=text(SINGLE_MOMENT_ODDS_SQL),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id", ondelete="CASCADE"))
    bookmaker: Mapped[str] = mapped_column(String(SOURCE_LENGTH))
    market: Mapped[MarketType] = mapped_column(enum_column(MarketType))
    selection: Mapped[Selection] = mapped_column(enum_column(Selection))
    line: Mapped[float] = mapped_column(default=NO_LINE)
    odds: Mapped[float]
    kind: Mapped[OddsKind] = mapped_column(enum_column(OddsKind))
    taken_at: Mapped[datetime]

    fixture: Mapped[FixtureRow] = relationship(lazy="raise")


# --------------------------------------------------------------------------- users


class UserRow(Base):
    """A whitelisted Telegram user; ``id`` is the Telegram user id."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    is_admin: Mapped[bool] = mapped_column(default=False)
    timezone: Mapped[str] = mapped_column(String(SHORT_TEXT_LENGTH))
    bank: Mapped[Decimal | None]
    unit: Mapped[Decimal | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UserSettingsRow(Base):
    """Analysis preferences of one user (plan §6.5). Lists are JSON arrays of ids/values."""

    __tablename__ = "user_settings"
    __table_args__ = (
        CheckConstraint("min_prob >= 0 AND min_prob <= 1", name="min_prob_range"),
        CheckConstraint("odds_min > 1 AND odds_max > odds_min", name="odds_range"),
        CheckConstraint("max_signals >= 1", name="max_signals_positive"),
        CheckConstraint("prematch_minutes IS NULL OR prematch_minutes > 0", name="prematch"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    sports: Mapped[list[str]] = mapped_column(JSON, default=list)
    leagues: Mapped[list[int]] = mapped_column(JSON, default=list)
    markets: Mapped[list[str]] = mapped_column(JSON, default=list)
    max_signals: Mapped[int]
    min_prob: Mapped[float]
    min_ev: Mapped[float]
    odds_min: Mapped[float]
    odds_max: Mapped[float]
    risk_profile: Mapped[RiskProfile] = mapped_column(enum_column(RiskProfile))
    send_time: Mapped[time]
    prematch_minutes: Mapped[int | None]
    quiet_hours_start: Mapped[time | None]
    quiet_hours_end: Mapped[time | None]
    # Responsible-gambling stop (plan §6.9); NULL = no limit.
    daily_loss_limit: Mapped[Decimal | None]
    weekly_loss_limit: Mapped[Decimal | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class WatchlistRow(CreatedAtMixin, Base):
    """A team or league a user follows; ``entity_id`` is ``teams.id`` or ``leagues.id``."""

    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("user_id", "kind", "entity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[WatchKind] = mapped_column(enum_column(WatchKind))
    sport: Mapped[Sport] = mapped_column(enum_column(Sport))
    entity_id: Mapped[int]
    notify_flags: Mapped[list[str]] = mapped_column(JSON, default=list)


class SavedQueryRow(TimestampMixin, Base):
    """A named analysis request, optionally run on a schedule (plan §6.2)."""

    __tablename__ = "saved_queries"
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        CheckConstraint(
            "date_from IS NULL OR date_to IS NULL OR date_to >= date_from", name="date_range"
        ),
        CheckConstraint("days IS NULL OR days >= 1", name="days_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(NAME_LENGTH))
    date_mode: Mapped[DateMode] = mapped_column(enum_column(DateMode))
    days: Mapped[int | None]
    date_from: Mapped[date | None]
    date_to: Mapped[date | None]
    sports: Mapped[list[str]] = mapped_column(JSON, default=list)
    min_prob: Mapped[float]
    min_ev: Mapped[float]
    scope: Mapped[QueryScope] = mapped_column(enum_column(QueryScope))
    scope_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    markets: Mapped[list[str]] = mapped_column(JSON, default=list)
    max_signals: Mapped[int | None]
    sort: Mapped[SortOrder] = mapped_column(enum_column(SortOrder))
    schedule_cron: Mapped[str | None] = mapped_column(String(SHORT_TEXT_LENGTH))


# --------------------------------------------------------------------------- signals


class SignalRow(TimestampMixin, Base):
    """A recommendation for one selection; its latest numbers (history: signal_versions)."""

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("user_id", "fixture_id", "market", "selection", "line"),
        CheckConstraint("odds > 1", name="odds_above_one"),
        CheckConstraint("p_model >= 0 AND p_model <= 1", name="p_model_range"),
        CheckConstraint(LINE_ONLY_FOR_LINE_MARKETS_SQL, name="line_only_for_line_markets"),
        Index(None, "status", "created_at"),
        Index(None, "user_id", "created_at"),
        Index(None, "fixture_id"),
        Index(None, "user_id", "recommended_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    fixture_id: Mapped[int] = mapped_column(ForeignKey("fixtures.id", ondelete="RESTRICT"))
    market: Mapped[MarketType] = mapped_column(enum_column(MarketType))
    selection: Mapped[Selection] = mapped_column(enum_column(Selection))
    line: Mapped[float] = mapped_column(default=NO_LINE)
    odds: Mapped[float]
    p_model: Mapped[float]
    ev: Mapped[float]
    confidence: Mapped[Confidence] = mapped_column(enum_column(Confidence))
    stake_units: Mapped[Decimal]
    llm_verdict: Mapped[LlmVerdict | None] = mapped_column(enum_column(LlmVerdict))
    llm_note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[SignalSource] = mapped_column(enum_column(SignalSource))
    status: Mapped[SignalStatus] = mapped_column(enum_column(SignalStatus))
    # Set once, when the selection first passed a query's thresholds; never changed
    # afterwards (service contract v1.1). NULL = not (yet) a recommendation.
    recommended_at: Mapped[datetime | None]
    # Thresholds in force when the signal was created/recommended; recalc verdicts use
    # these, not the user's current settings. NULL only for rows written before 0003.
    min_prob: Mapped[float | None]
    min_ev: Mapped[float | None]
    odds_min: Mapped[float | None]
    odds_max: Mapped[float | None]

    fixture: Mapped[FixtureRow] = relationship(lazy="raise")


class SignalVersionRow(CreatedAtMixin, Base):
    """Snapshot of a signal's numbers at each (re)calculation."""

    __tablename__ = "signal_versions"
    __table_args__ = (
        UniqueConstraint("signal_id", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("odds > 1", name="odds_above_one"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"))
    version: Mapped[int]
    odds: Mapped[float]
    p_model: Mapped[float]
    ev: Mapped[float]
    reason: Mapped[SignalVersionReason] = mapped_column(enum_column(SignalVersionReason))


class SignalResultRow(Base):
    """Settlement of a signal (table ``results``; at most one per signal)."""

    __tablename__ = "results"
    __table_args__ = (
        UniqueConstraint("signal_id"),
        CheckConstraint("closing_odds IS NULL OR closing_odds > 1", name="closing_odds"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"))
    outcome: Mapped[Outcome] = mapped_column(enum_column(Outcome))
    source: Mapped[ResultSource] = mapped_column(enum_column(ResultSource))
    profit_units: Mapped[Decimal]
    closing_odds: Mapped[float | None]
    clv: Mapped[float | None]
    user_note: Mapped[str | None] = mapped_column(Text)
    settled_at: Mapped[datetime] = mapped_column(default=utcnow)


class ScheduledRecalcRow(Base):
    """A pending automatic recalculation (24 h before / pre-match) of a signal."""

    __tablename__ = "scheduled_recalcs"
    __table_args__ = (UniqueConstraint("signal_id", "kind"), Index(None, "done", "run_at"))

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"))
    run_at: Mapped[datetime]
    kind: Mapped[RecalcKind] = mapped_column(enum_column(RecalcKind))
    done: Mapped[bool] = mapped_column(default=False)
    done_at: Mapped[datetime | None]


class UserBetRow(Base):
    """A bet the user says they placed (real odds and stake may differ from the signal)."""

    __tablename__ = "user_bets"
    __table_args__ = (
        CheckConstraint("odds_taken > 1", name="odds_above_one"),
        Index(None, "user_id", "placed_at"),
        Index(None, "signal_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"))
    odds_taken: Mapped[float]
    stake: Mapped[Decimal]
    outcome: Mapped[Outcome | None] = mapped_column(enum_column(Outcome))
    profit: Mapped[Decimal | None]
    placed_at: Mapped[datetime] = mapped_column(default=utcnow)
    settled_at: Mapped[datetime | None]


# --------------------------------------------------------------------------- feedback loop


class PostmortemRow(CreatedAtMixin, Base):
    """Root-cause classification of a lost signal; ``tags`` hold team/league/market keys."""

    __tablename__ = "postmortems"
    __table_args__ = (Index(None, "signal_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"))
    category: Mapped[PostmortemCategory] = mapped_column(enum_column(PostmortemCategory))
    reason: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)


class LessonRow(Base):
    """A repeated, tagged observation fed back to the LLM prompt (plan §6.13)."""

    __tablename__ = "lessons"
    __table_args__ = (
        CheckConstraint("confirmations >= 0", name="confirmations_non_negative"),
        Index(None, "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    confirmations: Mapped[int] = mapped_column(default=0)
    status: Mapped[LessonStatus] = mapped_column(enum_column(LessonStatus))
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)


class SegmentStatRow(Base):
    """Performance of one league x market x odds bucket; may raise its EV bar or disable it."""

    __tablename__ = "segment_stats"
    __table_args__ = (
        UniqueConstraint("sport", "league_id", "market", "odds_bucket"),
        CheckConstraint("n >= 0", name="n_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[Sport] = mapped_column(enum_column(Sport))
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id", ondelete="CASCADE"))
    market: Mapped[MarketType] = mapped_column(enum_column(MarketType))
    odds_bucket: Mapped[str] = mapped_column(String(SHORT_TEXT_LENGTH))
    n: Mapped[int]
    roi: Mapped[float]
    clv: Mapped[float | None]
    calib_bias: Mapped[float | None]
    ev_threshold: Mapped[float]
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class CalibrationVersionRow(CreatedAtMixin, Base):
    """A fitted probability calibration; exactly one is expected to be ``active``."""

    __tablename__ = "calibration_versions"
    __table_args__ = (
        UniqueConstraint("version"),
        CheckConstraint("n_samples >= 0", name="n_samples_non_negative"),
        Index(None, "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int]
    method: Mapped[str] = mapped_column(String(SHORT_TEXT_LENGTH))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    n_samples: Mapped[int]
    brier_before: Mapped[float]
    brier_after: Mapped[float]
    active: Mapped[bool] = mapped_column(default=False)


# --------------------------------------------------------------------------- operations


class ApiUsageRow(Base):
    """Requests spent per day, provider and sport (``sport=''`` when not sport-specific)."""

    __tablename__ = "api_usage"
    __table_args__ = (
        UniqueConstraint("usage_date", "provider", "sport"),
        CheckConstraint(
            "requests_used >= 0 AND reserved_manual >= 0", name="counters_non_negative"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    usage_date: Mapped[date]
    provider: Mapped[str] = mapped_column(String(SOURCE_LENGTH))
    sport: Mapped[str] = mapped_column(String(SHORT_TEXT_LENGTH), default="")
    requests_used: Mapped[int] = mapped_column(default=0)
    reserved_manual: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
