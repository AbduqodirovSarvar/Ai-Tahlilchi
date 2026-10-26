"""Frozen row DTOs returned by the repository (never ORM objects).

Domain objects (``Fixture``, ``FixtureResult``, ``OddsSnapshot``, ``Market``) are used
where they exist; the records below add the database id and storage-only fields.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Final

from app.db.enums import (
    Confidence,
    DateMode,
    LlmVerdict,
    Outcome,
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
from app.domain import CallOutcome, Fixture, FixtureResult, Market, Provider, Selection, Sport

MIN_WEEKDAY: Final = 1
MAX_WEEKDAY: Final = 7


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results and the total number of matches."""

    items: tuple[T, ...]
    total: int
    offset: int
    limit: int | None


@dataclass(frozen=True, slots=True)
class FixtureRecord:
    """A stored fixture: its id, the domain fixture and its result once known.

    ``league_id``/``home_team_id``/``away_team_id`` are the catalog ids (``leagues.id``,
    ``teams.id``) the bot's entity ids are made of; ``None`` only for hand-built records.
    """

    id: int
    fixture: Fixture
    result: FixtureResult | None
    home_xg: float | None = None
    away_xg: float | None = None
    league_id: int | None = None
    home_team_id: int | None = None
    away_team_id: int | None = None


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Filters in force when a signal was created/recommended (service contract v1.1)."""

    min_prob: float
    min_ev: float
    odds_min: float
    odds_max: float


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalDraft:
    """What the pipeline computed for one selection of one fixture for one user.

    ``recommended_at`` is the moment the selection passed the query's thresholds (``None``
    = shown without being a recommendation, e.g. ``no_value``); it is only ever set once.
    """

    user_id: int
    fixture_id: int
    market: Market
    selection: Selection
    odds: float
    p_model: float
    ev: float
    confidence: Confidence
    stake_units: Decimal
    source: SignalSource
    thresholds: Thresholds
    recommended_at: datetime | None = None
    llm_verdict: LlmVerdict | None = None
    llm_note: str | None = None
    fair_odds: float | None = None
    quoted_at: datetime | None = None
    p_raw: float | None = None
    """The probability before calibration (``None`` = ``p_model`` is already raw)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalResultRecord:
    """Settlement of a signal (table ``results``)."""

    signal_id: int
    outcome: Outcome
    source: ResultSource
    profit_units: Decimal
    closing_odds: float | None
    clv: float | None
    user_note: str | None
    settled_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalRecord:
    """A stored signal with its fixture, latest version number and settlement."""

    id: int
    user_id: int
    fixture: FixtureRecord
    market: Market
    selection: Selection
    odds: float
    p_model: float
    ev: float
    confidence: Confidence
    stake_units: Decimal
    llm_verdict: LlmVerdict | None
    llm_note: str | None
    source: SignalSource
    status: SignalStatus
    recommended_at: datetime | None
    thresholds: Thresholds | None
    version: int
    created_at: datetime
    updated_at: datetime
    result: SignalResultRecord | None
    recommended_odds: float | None = None
    fair_odds: float | None = None
    quoted_at: datetime | None = None
    p_raw: float | None = None

    @property
    def first_odds(self) -> float:
        """The odds profit and CLV are judged on: the first recommended ones (decision #17).

        ``odds`` follows recalculations; a signal that was never recommended has no
        recommended odds, so its current ``odds`` stand in.
        """
        return self.odds if self.recommended_odds is None else self.recommended_odds

    @property
    def retro(self) -> bool:
        """Analysed after kick-off: never a recommendation, never in reports."""
        return self.source is SignalSource.RETRO


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalVersionRecord:
    """The numbers of one (re)calculation of a signal."""

    signal_id: int
    version: int
    odds: float
    p_model: float
    ev: float
    reason: SignalVersionReason
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class UserRecord:
    """A whitelisted user."""

    id: int
    is_admin: bool
    timezone: str
    bank: Decimal | None
    unit: Decimal | None
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class UserSettingsRecord:
    """Stored analysis preferences of one user (plan §6.5, loss limits §6.9)."""

    user_id: int
    sports: tuple[str, ...]
    leagues: tuple[int, ...]
    markets: tuple[str, ...]
    max_signals: int
    min_prob: float
    min_ev: float
    odds_min: float
    odds_max: float
    risk_profile: RiskProfile
    send_time: time
    prematch_minutes: int | None
    quiet_hours_start: time | None
    quiet_hours_end: time | None
    daily_loss_limit: Decimal | None = None
    weekly_loss_limit: Decimal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WatchRecord:
    """A followed team or league; ``entity_id`` is ``teams.id`` / ``leagues.id``."""

    id: int
    user_id: int
    kind: WatchKind
    sport: Sport
    entity_id: int
    notify_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SavedQueryDraft:
    """A named analysis request to save (unique per user and name)."""

    user_id: int
    name: str
    date_mode: DateMode
    days: int | None
    date_from: date | None
    date_to: date | None
    sports: tuple[str, ...]
    min_prob: float
    min_ev: float
    scope: QueryScope
    scope_ids: tuple[int, ...]
    markets: tuple[str, ...]
    max_signals: int | None
    sort: SortOrder
    per_day: bool = False
    include_no_value: bool = False
    schedule_weekdays: tuple[int, ...] = ()
    schedule_at: time | None = None

    def __post_init__(self) -> None:
        """A schedule is weekdays (ISO 1..7) together with a local time, or neither."""
        if bool(self.schedule_weekdays) != (self.schedule_at is not None):
            raise ValueError("schedule_weekdays and schedule_at are set together")
        if not all(MIN_WEEKDAY <= day <= MAX_WEEKDAY for day in self.schedule_weekdays):
            raise ValueError("schedule_weekdays must be ISO weekdays 1..7")


@dataclass(frozen=True, slots=True, kw_only=True)
class SavedQueryRecord:
    """A stored saved query."""

    id: int
    query: SavedQueryDraft
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class RecalcTask:
    """A scheduled automatic recalculation; ``claimed_at`` is set while a worker owns it."""

    id: int
    signal_id: int
    kind: RecalcKind
    run_at: datetime
    done: bool
    claimed_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class UserBetRecord:
    """A bet the user placed."""

    id: int
    user_id: int
    signal_id: int | None
    odds_taken: float
    stake: Decimal
    outcome: Outcome | None
    profit: Decimal | None
    placed_at: datetime
    settled_at: datetime | None
    client_token: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalNoteRecord:
    """A user's note on one of their signals."""

    id: int
    user_id: int
    signal_id: int
    text: str
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class CallSummary:
    """One logged provider call without its response (cheap to list).

    ``request_preview`` is only the first ``REQUEST_PREVIEW_CHARS`` characters of the request
    (see :mod:`app.db.repo.calls`); :class:`CallDetail` has the whole text.
    """

    id: int
    at: datetime
    provider: Provider
    operation: str
    outcome: CallOutcome
    duration_ms: int
    http_status: int | None
    quota_cost: int
    size_bytes: int | None
    error: str | None
    request_preview: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CallDetail:
    """One logged provider call with the full request and response text."""

    id: int
    at: datetime
    provider: Provider
    operation: str
    outcome: CallOutcome
    duration_ms: int
    http_status: int | None
    quota_cost: int
    size_bytes: int | None
    error: str | None
    request: str
    response: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CallStat:
    """Calls of one provider operation since a moment (the admin's usage summary).

    ``avg_duration_ms`` covers only calls that reached the provider (ok or error), so cache
    hits do not drag the average to zero; ``None`` when there were none.
    """

    provider: Provider
    operation: str
    calls: int
    errors: int
    cached: int
    blocked: int
    avg_duration_ms: float | None
    quota_cost: int
