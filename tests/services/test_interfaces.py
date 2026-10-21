"""DTO validation of the service contract (no service involved)."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import DomainValidationError, Market, MarketType, Selection, Sport
from app.services.errors import ValidationFailedError
from app.services.interfaces import (
    BALANCED_PRESET,
    CONSERVATIVE_PRESET,
    MAX_ENTITY_ID_BYTES,
    MAX_FUTURE_DAYS,
    MAX_WINDOW_DAYS,
    MIN_PROBABILITY_LOWER,
    MIN_PROBABILITY_UPPER,
    RISK_PRESETS,
    AnalysisProgress,
    AnalysisQuery,
    AnalysisResult,
    AnalysisStage,
    ApiQuota,
    BetDraft,
    ConfidenceLevel,
    CostEstimate,
    DateMode,
    DateSpec,
    DateWindow,
    FixtureListRequest,
    FixtureOption,
    LeagueOption,
    MarketQuote,
    OddsMove,
    QuietHours,
    RecalcDiff,
    RecalcVerdict,
    ReportRequest,
    ResultMark,
    ResultMarkRequest,
    RiskProfile,
    ScopeKind,
    SignalNumbers,
    SignalSource,
    SignalView,
    SportOption,
    TeamOption,
    TemplateDraft,
    TemplateSchedule,
    UserSettingsView,
    WatchKind,
    WatchTarget,
    horizon_end,
    infer_risk_profile,
    is_valid_entity_id,
)

TODAY = date(2026, 10, 21)  # a Wednesday
KICKOFF = datetime(2026, 10, 22, 19, 0, tzinfo=UTC)


def make_signal(**overrides: object) -> SignalView:
    values: dict[str, object] = {
        "signal_id": 1,
        "fixture_id": "fx:1",
        "sport": Sport.FOOTBALL,
        "league": "Premier League",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "kickoff": KICKOFF,
        "market": Market.totals(2.5),
        "selection": Selection.OVER,
        "odds": 1.85,
        "bookmaker": "pinnacle",
        "probability": 0.61,
        "ev": 0.1285,
        "confidence": ConfidenceLevel.HIGH,
        "stake_units": Decimal("1.5"),
    }
    values.update(overrides)
    return SignalView(**values)  # type: ignore[arg-type]


def numbers(odds: float) -> SignalNumbers:
    return SignalNumbers(
        odds=odds,
        probability=0.6,
        ev=0.6 * odds - 1,
        stake_units=Decimal(1),
        confidence=ConfidenceLevel.MEDIUM,
    )


def field_of(excinfo: pytest.ExceptionInfo[ValidationFailedError]) -> str:
    return excinfo.value.field


# --------------------------------------------------------------------------- dates
class TestDateWindow:
    def test_counts_both_ends(self) -> None:
        assert DateWindow(TODAY, TODAY).days == 1
        assert DateWindow(TODAY, TODAY + timedelta(days=6)).days == 7

    def test_rejects_reversed_window(self) -> None:
        with pytest.raises(ValidationFailedError) as excinfo:
            DateWindow(TODAY, TODAY - timedelta(days=1))
        assert field_of(excinfo) == "date_to"

    def test_rejects_windows_longer_than_the_maximum(self) -> None:
        DateWindow(TODAY, TODAY + timedelta(days=MAX_WINDOW_DAYS - 1))
        with pytest.raises(ValidationFailedError):
            DateWindow(TODAY, TODAY + timedelta(days=MAX_WINDOW_DAYS))

    def test_horizon_is_fourteen_days_counting_today(self) -> None:
        assert horizon_end(TODAY) == TODAY + timedelta(days=13)
        DateWindow(TODAY, horizon_end(TODAY)).ensure_within_horizon(TODAY)
        too_far = DateWindow(TODAY, TODAY + timedelta(days=MAX_FUTURE_DAYS))
        with pytest.raises(ValidationFailedError):
            too_far.ensure_within_horizon(TODAY)

    def test_next_days_and_between_share_one_horizon(self) -> None:
        widest = DateSpec.next_days(MAX_FUTURE_DAYS).resolve(TODAY)
        assert widest.date_to == horizon_end(TODAY)
        widest.ensure_within_horizon(TODAY)
        DateSpec.between(TODAY, horizon_end(TODAY)).resolve(TODAY).ensure_within_horizon(TODAY)
        beyond = DateSpec.between(TODAY, horizon_end(TODAY) + timedelta(days=1)).resolve(TODAY)
        with pytest.raises(ValidationFailedError):
            beyond.ensure_within_horizon(TODAY)

    def test_utc_bounds_follow_the_zone(self) -> None:
        start, end = DateWindow(TODAY, TODAY).utc_bounds(ZoneInfo("Asia/Tashkent"))
        assert start == datetime(2026, 10, 20, 19, 0, tzinfo=UTC)
        assert end - start == timedelta(hours=24)
        assert DateWindow(TODAY, TODAY).contains(start, ZoneInfo("Asia/Tashkent"))
        assert not DateWindow(TODAY, TODAY).contains(end, ZoneInfo("Asia/Tashkent"))

    def test_utc_bounds_are_dst_safe(self) -> None:
        london = ZoneInfo("Europe/London")
        autumn = DateWindow(date(2026, 10, 25), date(2026, 10, 25)).utc_bounds(london)
        spring = DateWindow(date(2026, 3, 29), date(2026, 3, 29)).utc_bounds(london)
        assert autumn[1] - autumn[0] == timedelta(hours=25)
        assert spring[1] - spring[0] == timedelta(hours=23)
        assert all(bound.tzinfo is UTC for bound in (*autumn, *spring))

    def test_past_windows_are_retro_and_allowed(self) -> None:
        past = DateWindow(TODAY - timedelta(days=30), TODAY - timedelta(days=1))
        past.ensure_within_horizon(TODAY)
        assert past.is_retro(TODAY)
        assert not DateWindow(TODAY - timedelta(days=1), TODAY).is_retro(TODAY)

    @given(
        start=st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1)),
        length=st.integers(min_value=1, max_value=MAX_WINDOW_DAYS),
    )
    def test_any_ordered_window_up_to_the_maximum_is_valid(self, start: date, length: int) -> None:
        window = DateWindow(start, start + timedelta(days=length - 1))
        assert window.days == length


class TestDateSpec:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            (DateSpec.today(), (TODAY, TODAY)),
            (DateSpec.tomorrow(), (date(2026, 10, 22), date(2026, 10, 22))),
            (DateSpec.weekend(), (date(2026, 10, 24), date(2026, 10, 25))),
            (DateSpec.next_days(3), (TODAY, date(2026, 10, 23))),
            (
                DateSpec.between(date(2026, 10, 1), date(2026, 10, 5)),
                (date(2026, 10, 1), date(2026, 10, 5)),
            ),
        ],
    )
    def test_resolves_relative_to_today(self, spec: DateSpec, expected: tuple[date, date]) -> None:
        window = spec.resolve(TODAY)
        assert (window.date_from, window.date_to) == expected

    @pytest.mark.parametrize(
        ("today", "expected"),
        [
            (date(2026, 10, 24), (date(2026, 10, 24), date(2026, 10, 25))),  # Saturday
            (date(2026, 10, 25), (date(2026, 10, 25), date(2026, 10, 25))),  # Sunday
            (date(2026, 10, 26), (date(2026, 10, 31), date(2026, 11, 1))),  # Monday
        ],
    )
    def test_weekend_inside_and_outside_the_weekend(
        self, today: date, expected: tuple[date, date]
    ) -> None:
        window = DateSpec.weekend().resolve(today)
        assert (window.date_from, window.date_to) == expected

    @pytest.mark.parametrize("days", [0, -1, MAX_FUTURE_DAYS + 1])
    def test_next_days_bounds(self, days: int) -> None:
        with pytest.raises(ValidationFailedError) as excinfo:
            DateSpec.next_days(days)
        assert field_of(excinfo) == "days"

    def test_next_days_requires_a_count(self) -> None:
        with pytest.raises(ValidationFailedError):
            DateSpec(mode=DateMode.NEXT_DAYS)

    def test_fields_must_match_the_mode(self) -> None:
        with pytest.raises(ValidationFailedError):
            DateSpec(mode=DateMode.TODAY, days=3)
        with pytest.raises(ValidationFailedError):
            DateSpec(mode=DateMode.TOMORROW, date_from=TODAY, date_to=TODAY)
        with pytest.raises(ValidationFailedError):
            DateSpec(mode=DateMode.RANGE, date_from=TODAY)

    def test_range_validates_its_window(self) -> None:
        with pytest.raises(ValidationFailedError):
            DateSpec.between(TODAY, TODAY - timedelta(days=1))

    @given(today=st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1)))
    def test_weekend_is_always_saturday_or_sunday_and_not_in_the_past(self, today: date) -> None:
        window = DateSpec.weekend().resolve(today)
        assert window.date_from >= today
        assert {window.date_from.weekday(), window.date_to.weekday()} <= {5, 6}
        assert window.days <= 2


# --------------------------------------------------------------------------- query
class TestAnalysisQuery:
    def test_defaults_are_valid(self) -> None:
        query = AnalysisQuery(dates=DateSpec.today())
        assert query.scope is ScopeKind.ALL
        assert query.max_signals is None

    @given(st.floats(min_value=MIN_PROBABILITY_LOWER, max_value=MIN_PROBABILITY_UPPER))
    def test_accepts_every_probability_in_range(self, probability: float) -> None:
        AnalysisQuery(dates=DateSpec.today(), min_probability=probability)

    @pytest.mark.parametrize("probability", [0.29, 0.951, 1.0, 0.0, float("nan")])
    def test_rejects_probability_out_of_range(self, probability: float) -> None:
        with pytest.raises(ValidationFailedError) as excinfo:
            AnalysisQuery(dates=DateSpec.today(), min_probability=probability)
        assert field_of(excinfo) == "min_probability"

    @pytest.mark.parametrize("min_ev", [-0.01, 0.21, float("inf")])
    def test_rejects_ev_out_of_range(self, min_ev: float) -> None:
        with pytest.raises(ValidationFailedError) as excinfo:
            AnalysisQuery(dates=DateSpec.today(), min_ev=min_ev)
        assert field_of(excinfo) == "min_ev"

    def test_scope_ids_follow_the_scope(self) -> None:
        AnalysisQuery(dates=DateSpec.today(), scope=ScopeKind.TEAMS, scope_ids=frozenset({"t"}))
        with pytest.raises(ValidationFailedError):
            AnalysisQuery(dates=DateSpec.today(), scope_ids=frozenset({"t"}))
        with pytest.raises(ValidationFailedError):
            AnalysisQuery(dates=DateSpec.today(), scope=ScopeKind.LEAGUES)
        with pytest.raises(ValidationFailedError):
            AnalysisQuery(
                dates=DateSpec.today(),
                scope=ScopeKind.FIXTURES,
                scope_ids=frozenset(str(n) for n in range(51)),
            )

    @pytest.mark.parametrize("count", [0, 101])
    def test_rejects_signal_count_out_of_range(self, count: int) -> None:
        with pytest.raises(ValidationFailedError):
            AnalysisQuery(dates=DateSpec.today(), max_signals=count)

    def test_per_day_needs_a_count(self) -> None:
        with pytest.raises(ValidationFailedError) as excinfo:
            AnalysisQuery(dates=DateSpec.today(), per_day=True)
        assert field_of(excinfo) == "per_day"

    def test_requires_sports_and_markets(self) -> None:
        with pytest.raises(ValidationFailedError):
            AnalysisQuery(dates=DateSpec.today(), sports=frozenset())
        with pytest.raises(ValidationFailedError):
            AnalysisQuery(dates=DateSpec.today(), markets=frozenset())

    def test_from_settings_copies_the_user_defaults(self) -> None:
        settings = UserSettingsView(
            min_probability=0.6, min_ev=0.07, max_signals=5, markets=frozenset({MarketType.BTTS})
        )
        query = AnalysisQuery.from_settings(settings, DateSpec.tomorrow())
        assert (query.min_probability, query.min_ev, query.max_signals) == (0.6, 0.07, 5)
        assert query.markets == {MarketType.BTTS}
        assert query.dates == DateSpec.tomorrow()


# --------------------------------------------------------------------------- settings
class TestUserSettings:
    def test_defaults_match_the_balanced_preset(self) -> None:
        settings = UserSettingsView()
        assert settings.risk_profile is RiskProfile.BALANCED
        assert settings.min_probability == BALANCED_PRESET.min_probability
        assert settings.zone == ZoneInfo("Asia/Tashkent")

    def test_presets_match_the_plan_table(self) -> None:
        assert {
            p: (r.min_probability, r.min_ev, r.odds_min, r.odds_max)
            for p, r in RISK_PRESETS.items()
        } == {
            RiskProfile.CONSERVATIVE: (0.60, 0.03, 1.30, 2.00),
            RiskProfile.BALANCED: (0.45, 0.05, 1.50, 3.00),
            RiskProfile.AGGRESSIVE: (0.30, 0.08, 2.00, 5.00),
        }

    def test_with_risk_profile_applies_thresholds(self) -> None:
        settings = UserSettingsView().with_risk_profile(RiskProfile.CONSERVATIVE)
        assert settings.min_probability == CONSERVATIVE_PRESET.min_probability
        assert (settings.odds_min, settings.odds_max) == (1.30, 2.00)
        custom = settings.with_risk_profile(RiskProfile.CUSTOM)
        assert custom.risk_profile is RiskProfile.CUSTOM
        assert custom.min_probability == CONSERVATIVE_PRESET.min_probability

    def test_infer_risk_profile(self) -> None:
        assert infer_risk_profile(0.30, 0.08, (2.0, 5.0)) is RiskProfile.AGGRESSIVE
        assert infer_risk_profile(0.31, 0.08, (2.0, 5.0)) is RiskProfile.CUSTOM

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("sports", frozenset()),
            ("markets", frozenset()),
            ("max_signals", 0),
            ("max_signals", 31),
            ("min_probability", 0.2),
            ("min_ev", 0.3),
            ("odds_min", 1.0),
            ("odds_max", 101.0),
            ("prematch_minutes", 45),
            ("timezone", "Mars/Olympus"),
            ("timezone", ""),
            ("bank", Decimal(0)),
            ("unit", Decimal(-1)),
            ("daily_loss_limit", Decimal("NaN")),
        ],
    )
    def test_rejects_invalid_values(self, field: str, value: object) -> None:
        with pytest.raises(ValidationFailedError) as excinfo:
            replace(UserSettingsView(), **{field: value})
        assert field_of(excinfo) == field

    def test_odds_range_must_be_ordered(self) -> None:
        with pytest.raises(ValidationFailedError) as excinfo:
            UserSettingsView(odds_min=2.0, odds_max=2.0)
        assert field_of(excinfo) == "odds_max"

    def test_unit_must_not_exceed_bank(self) -> None:
        UserSettingsView(bank=Decimal(1000), unit=Decimal(10))
        with pytest.raises(ValidationFailedError) as excinfo:
            UserSettingsView(bank=Decimal(10), unit=Decimal(20))
        assert field_of(excinfo) == "unit"

    def test_optional_features_can_be_switched_off(self) -> None:
        settings = UserSettingsView(prematch_minutes=None, quiet_hours=None)
        assert settings.prematch_minutes is None

    def test_quiet_hours_may_wrap_midnight_but_not_be_empty(self) -> None:
        QuietHours(time(23, 0), time(7, 0))
        with pytest.raises(ValidationFailedError):
            QuietHours(time(8, 0), time(8, 0))


# --------------------------------------------------------------------------- results
class TestSignalView:
    def test_derived_values(self) -> None:
        signal = make_signal()
        assert signal.fixture_label == "Arsenal — Chelsea"
        assert signal.fair_odds == pytest.approx(1 / 0.61)
        assert not signal.negative_ev
        assert make_signal(ev=-0.02).negative_ev

    @pytest.mark.parametrize(
        ("overrides", "field"),
        [
            ({"odds": 1.0}, "odds"),
            ({"probability": 0.0}, "probability"),
            ({"probability": 1.2}, "probability"),
            ({"stake_units": Decimal(-1)}, "stake_units"),
            ({"version": 0}, "version"),
            ({"kickoff": datetime(2026, 10, 22, 19, 0)}, "kickoff"),  # noqa: DTZ001
            ({"kickoff": KICKOFF.astimezone(ZoneInfo("Asia/Tashkent"))}, "kickoff"),
        ],
    )
    def test_rejects_broken_numbers(self, overrides: dict[str, object], field: str) -> None:
        with pytest.raises(ValidationFailedError) as excinfo:
            make_signal(**overrides)
        assert field_of(excinfo) == field

    def test_selection_must_belong_to_the_market(self) -> None:
        with pytest.raises(DomainValidationError):
            make_signal(market=Market.btts(), selection=Selection.OVER)


def test_cost_estimate_budget() -> None:
    window = DateWindow(TODAY, TODAY)
    fits = CostEstimate(window=window, fixtures_found=4, api_requests=6, budget_remaining=6)
    over = replace(fits, api_requests=7)
    assert fits.within_budget
    assert not over.within_budget


def test_analysis_result_shortfall() -> None:
    window = DateWindow(TODAY, TODAY)
    result = AnalysisResult(
        query_id="q1",
        window=window,
        signals=(),
        fixtures_analyzed=4,
        matched=3,
        requested=10,
        api_requests_used=2,
    )
    assert result.shortfall
    assert not replace(result, requested=None).shortfall
    assert not replace(result, requested=3).shortfall


def test_recalc_diff_reports_change_and_requires_utc() -> None:
    signal = make_signal()
    diff = RecalcDiff(
        signal=signal,
        before=numbers(1.85),
        after=numbers(1.72),
        verdict=RecalcVerdict.BELOW_THRESHOLD,
        recalculated_at=datetime(2026, 10, 21, 9, 32, tzinfo=UTC),
    )
    assert diff.changed
    assert not replace(diff, after=numbers(1.85)).changed
    with pytest.raises(ValidationFailedError):
        replace(diff, recalculated_at=datetime(2026, 10, 21, 9, 32))  # noqa: DTZ001


def test_quote_and_move_helpers() -> None:
    quote = MarketQuote(market=Market.btts(), selection=Selection.YES, probability=0.5)
    move = OddsMove(market=Market.btts(), selection=Selection.YES, opening=2.0, current=1.8)
    assert quote.fair_odds == pytest.approx(2.0)
    assert move.change == pytest.approx(-0.1)


def test_fixture_option_label() -> None:
    option = FixtureOption(
        fixture_id="fx",
        sport=Sport.FOOTBALL,
        league="Serie A",
        home_team="Inter",
        away_team="Milan",
        kickoff=KICKOFF,
    )
    assert option.label == "Inter — Milan"


def test_progress_and_quota() -> None:
    progress = AnalysisProgress(stage=AnalysisStage.ANALYSING, done=12, total=30)
    quota = ApiQuota(provider="api-football", sport=None, used=120, limit=100, reserved_manual=30)
    assert progress.done < progress.total
    assert quota.remaining == 0


# --------------------------------------------------------------------------- other inputs
class TestInputs:
    def test_fixture_list_request_bounds(self) -> None:
        FixtureListRequest(dates=DateSpec.today())
        for kwargs in ({"page": -1}, {"page_size": 0}, {"page_size": 21}, {"sports": frozenset()}):
            with pytest.raises(ValidationFailedError):
                FixtureListRequest(dates=DateSpec.today(), **kwargs)  # type: ignore[arg-type]

    def test_template_names_and_schedules(self) -> None:
        query = AnalysisQuery(dates=DateSpec.weekend())
        schedule = TemplateSchedule(weekdays=frozenset({5}), at=time(10, 0))
        assert TemplateDraft(name="Dam olish · APL", query=query, schedule=schedule).name
        for name in ("  ", "x" * 65):
            with pytest.raises(ValidationFailedError):
                TemplateDraft(name=name, query=query)
        for days in (frozenset(), frozenset({0}), frozenset({8})):
            with pytest.raises(ValidationFailedError):
                TemplateSchedule(weekdays=days, at=time(10, 0))

    def test_watch_target_needs_an_id(self) -> None:
        with pytest.raises(ValidationFailedError):
            WatchTarget(WatchKind.TEAM, " ")

    @pytest.mark.parametrize(
        "kwargs",
        [{"odds_taken": 1.0}, {"odds_taken": float("nan")}, {"stake": Decimal(0)}],
    )
    def test_bet_draft_rejects_bad_numbers(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValidationFailedError):
            BetDraft(signal_id=1, **kwargs)  # type: ignore[arg-type]

    def test_result_marks(self) -> None:
        ResultMarkRequest(signal_id=1, mark=ResultMark.WON)
        ResultMarkRequest(signal_id=1, mark=ResultMark.CASHOUT, cashout_profit_units=Decimal("0.4"))
        bad: list[dict[str, object]] = [
            {"mark": ResultMark.HALF_WON},
            {"mark": ResultMark.CASHOUT},
            {"mark": ResultMark.LOST, "cashout_profit_units": Decimal(1)},
            {"mark": ResultMark.CASHOUT, "cashout_profit_units": Decimal("Infinity")},
        ]
        for kwargs in bad:
            with pytest.raises(ValidationFailedError):
                ResultMarkRequest(signal_id=1, **kwargs)  # type: ignore[arg-type]

    def test_report_periods(self) -> None:
        request = ReportRequest.last_days(7, TODAY)
        assert (request.date_from, request.date_to) == (date(2026, 10, 15), TODAY)
        assert ReportRequest(date_to=TODAY).date_from is None
        with pytest.raises(ValidationFailedError):
            ReportRequest(date_from=TODAY, date_to=TODAY - timedelta(days=1))
        with pytest.raises(ValidationFailedError):
            ReportRequest.last_days(0, TODAY)

    def test_relative_report_periods_are_resolved_by_the_service(self) -> None:
        assert ReportRequest.recent(7).resolve(TODAY) == (date(2026, 10, 15), TODAY)
        assert ReportRequest.all_time().resolve(TODAY) == (None, TODAY)
        assert ReportRequest.last_days(7, TODAY).resolve(date(2030, 1, 1)) == (
            date(2026, 10, 15),
            TODAY,
        )
        for bad in ({"recent_days": 0}, {"recent_days": 7, "date_to": TODAY}):
            with pytest.raises(ValidationFailedError) as excinfo:
                ReportRequest(**bad)  # type: ignore[arg-type]
            assert field_of(excinfo) == "recent_days"


class TestEntityIds:
    @pytest.mark.parametrize(
        ("value", "valid"),
        [
            ("af:l39", True),
            ("x" * MAX_ENTITY_ID_BYTES, True),
            ("x" * 41, False),
            ("", False),
            (" ", False),
            ("ü" * 21, False),
        ],
    )
    def test_byte_budget(self, value: str, *, valid: bool) -> None:
        assert is_valid_entity_id(value) is valid

    def test_inputs_and_outputs_validate_ids(self) -> None:
        long_id = "x" * (MAX_ENTITY_ID_BYTES + 1)
        builders = [
            lambda: AnalysisQuery(
                dates=DateSpec.today(), scope=ScopeKind.TEAMS, scope_ids=frozenset({long_id})
            ),
            lambda: FixtureListRequest(dates=DateSpec.today(), team_ids=frozenset({long_id})),
            lambda: FixtureListRequest(dates=DateSpec.today(), league_ids=frozenset({long_id})),
            lambda: UserSettingsView(league_ids=frozenset({long_id})),
            lambda: WatchTarget(WatchKind.LEAGUE, long_id),
            lambda: make_signal(fixture_id=long_id),
            lambda: LeagueOption(league_id=long_id, sport=Sport.FOOTBALL, name="L"),
            lambda: TeamOption(team_id=long_id, sport=Sport.FOOTBALL, name="T", league="L"),
            lambda: FixtureOption(
                fixture_id=long_id,
                sport=Sport.FOOTBALL,
                league="L",
                home_team="A",
                away_team="B",
                kickoff=KICKOFF,
            ),
        ]
        for build in builders:
            with pytest.raises(ValidationFailedError):
                build()


class TestRecommendationFields:
    def test_defaults(self) -> None:
        signal = make_signal()
        assert signal.source is SignalSource.ANALYSIS
        assert not signal.retro
        assert not signal.recommended

    def test_recommended_signals_have_value(self) -> None:
        at = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
        assert make_signal(recommended_at=at).recommended
        with pytest.raises(ValidationFailedError):
            make_signal(recommended_at=at, no_value=True)
        with pytest.raises(ValidationFailedError):
            make_signal(recommended_at=datetime(2026, 10, 21, 9, 0))  # noqa: DTZ001

    def test_retro_signals_are_never_recommended(self) -> None:
        assert make_signal(source=SignalSource.RETRO).retro
        with pytest.raises(ValidationFailedError):
            make_signal(
                source=SignalSource.RETRO,
                recommended_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
            )

    def test_sport_option(self) -> None:
        option = SportOption(
            sport=Sport.TENNIS, available=False, experimental=False, supported_markets=frozenset()
        )
        assert not option.available
