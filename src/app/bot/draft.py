"""The analysis wizard's working state, stored in FSM data as plain JSON-safe values.

Keeping the draft JSON-serialisable means the in-memory FSM storage can later be swapped
for a persistent one (Redis/SQLite) without touching handlers (ADR 0006). A draft that
cannot be decoded (old format, storage wiped) is reported as :class:`StaleDraftError` and
the user is asked to start again.
"""

from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any, Self

from app.domain import MarketType, Sport
from app.services.interfaces import (
    AnalysisQuery,
    DateMode,
    DateSpec,
    ScopeKind,
    SortKey,
    UserSettingsView,
)

type Option = tuple[str, str]
"""``(entity_id, label)`` of a picker button; buttons carry the option's index."""


class StaleDraftError(LookupError):
    """There is no usable wizard draft (flow finished, cancelled or bot restarted)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WizardDraft:
    """Choices made so far in 📊 Tahlil, pre-filled from the user's settings."""

    date_mode: str = DateMode.TODAY.value
    days: int | None = None
    date_from: str | None = None
    date_to: str | None = None
    calendar_start: str | None = None
    calendar_month: int = 0
    sports: tuple[str, ...] = (Sport.FOOTBALL.value,)
    min_probability: float = 0.45
    min_ev: float = 0.05
    scope: str = ScopeKind.ALL.value
    scope_ids: tuple[str, ...] = ()
    scope_labels: tuple[str, ...] = ()
    options: tuple[Option, ...] = ()
    page: int = 0
    page_count: int = 0
    max_signals: int | None = None
    per_day: bool = False
    markets: tuple[str, ...] = (MarketType.MATCH_RESULT.value,)
    sort: str = SortKey.EV.value
    message_id: int | None = None
    editing: bool = False
    defaults: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: UserSettingsView) -> Self:
        """Start a draft with the user's defaults (plan §6.2: defaults from settings)."""
        defaults: dict[str, Any] = {
            "min_probability": settings.min_probability,
            "min_ev": settings.min_ev,
            "max_signals": settings.max_signals,
            "markets": sorted(market.value for market in settings.markets),
            "sports": sorted(sport.value for sport in settings.sports),
        }
        return cls(
            sports=tuple(defaults["sports"]),
            min_probability=settings.min_probability,
            min_ev=settings.min_ev,
            max_signals=settings.max_signals,
            markets=tuple(defaults["markets"]),
            defaults=defaults,
        )

    # ------------------------------------------------------------------ serialisation
    def to_data(self) -> dict[str, Any]:
        """JSON-safe representation for FSM storage."""
        data = asdict(self)
        data["options"] = [list(option) for option in self.options]
        return data

    @classmethod
    def from_data(cls, data: object) -> Self:
        """Decode a stored draft.

        Raises:
            StaleDraftError: missing or malformed (e.g. written by an older bot version).
        """
        if not isinstance(data, dict):
            raise StaleDraftError
        try:
            values = dict(data)
            for key in ("sports", "scope_ids", "scope_labels", "markets"):
                values[key] = tuple(values[key])
            values["options"] = tuple((str(a), str(b)) for a, b in values["options"])
            draft = cls(**values)
            # Touch every typed view once so a corrupt value fails here, not mid-flow.
            _ = (draft.sport_set, draft.market_set, draft.scope_kind, draft.sort_key)
            draft.date_spec()
        except (KeyError, TypeError, ValueError):
            raise StaleDraftError from None
        return draft

    # ------------------------------------------------------------------ typed views
    @property
    def sport_set(self) -> frozenset[Sport]:
        """Chosen sports as enum members."""
        return frozenset(Sport(value) for value in self.sports)

    @property
    def market_set(self) -> frozenset[MarketType]:
        """Chosen markets as enum members."""
        return frozenset(MarketType(value) for value in self.markets)

    @property
    def scope_kind(self) -> ScopeKind:
        """Chosen scope as enum member."""
        return ScopeKind(self.scope)

    @property
    def sort_key(self) -> SortKey:
        """Chosen sort as enum member."""
        return SortKey(self.sort)

    def date_spec(self) -> DateSpec:
        """The chosen dates as a (possibly relative) :class:`DateSpec`."""
        mode = DateMode(self.date_mode)
        if mode is DateMode.RANGE and self.date_from and self.date_to:
            return DateSpec.between(
                date.fromisoformat(self.date_from), date.fromisoformat(self.date_to)
            )
        if mode is DateMode.NEXT_DAYS and self.days:
            return DateSpec.next_days(self.days)
        return {
            DateMode.TOMORROW: DateSpec.tomorrow,
            DateMode.WEEKEND: DateSpec.weekend,
        }.get(mode, DateSpec.today)()

    def with_dates(self, spec: DateSpec) -> Self:
        """Copy with ``spec`` as the chosen dates."""
        return replace(
            self,
            date_mode=spec.mode.value,
            days=spec.days,
            date_from=None if spec.date_from is None else spec.date_from.isoformat(),
            date_to=None if spec.date_to is None else spec.date_to.isoformat(),
            calendar_start=None,
        )

    def with_scope(self, kind: ScopeKind) -> Self:
        """Copy with a new scope; switching kind drops the previous selection."""
        if kind.value == self.scope:
            return self
        return replace(self, scope=kind.value, scope_ids=(), scope_labels=(), options=(), page=0)

    def toggle_scope_item(self, entity_id: str, label: str) -> Self:
        """Add or remove one league/team/fixture of the scope."""
        if entity_id in self.scope_ids:
            keep = [i for i, item in enumerate(self.scope_ids) if item != entity_id]
            return replace(
                self,
                scope_ids=tuple(self.scope_ids[i] for i in keep),
                scope_labels=tuple(self.scope_labels[i] for i in keep),
            )
        return replace(
            self,
            scope_ids=(*self.scope_ids, entity_id),
            scope_labels=(*self.scope_labels, label),
        )

    def to_query(self) -> AnalysisQuery:
        """Build the service request.

        Raises:
            ValidationFailedError: the combination is invalid (e.g. empty scope).
        """
        scope = self.scope_kind
        return AnalysisQuery(
            dates=self.date_spec(),
            sports=self.sport_set,
            min_probability=self.min_probability,
            min_ev=self.min_ev,
            scope=scope,
            scope_ids=frozenset() if scope is ScopeKind.ALL else frozenset(self.scope_ids),
            markets=self.market_set,
            max_signals=self.max_signals,
            per_day=self.per_day and self.max_signals is not None,
            sort=self.sort_key,
        )
