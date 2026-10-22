"""Hand-made DTOs for notifier and formatter tests (no fake service involved).

Everything is fixed, so rendered messages can be compared to golden strings.
"""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from app.domain import Market, Selection, Sport
from app.services.interfaces import (
    AnalysisResult,
    ConfidenceLevel,
    DailyDigest,
    DateWindow,
    DaySummary,
    DeliveryKey,
    RecalcDiff,
    RecalcVerdict,
    SignalNumbers,
    SignalView,
)

KICKOFF = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
"""20:00 in Tashkent."""
NOW = datetime(2026, 10, 22, 9, 32, tzinfo=UTC)
"""14:32 in Tashkent."""
DAY = date(2026, 10, 22)


def make_signal(signal_id: int = 7, **overrides: object) -> SignalView:
    """A recommended 1X2 home signal; ``overrides`` replace any field."""
    base = SignalView(
        signal_id=signal_id,
        fixture_id="fx1",
        sport=Sport.FOOTBALL,
        league="Premier League",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff=KICKOFF,
        market=Market.match_result(),
        selection=Selection.HOME,
        odds=1.85,
        bookmaker="1xbet",
        probability=0.61,
        ev=0.129,
        confidence=ConfidenceLevel.HIGH,
        stake_units=Decimal("1.5"),
        recommended_at=NOW,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def numbers(**overrides: object) -> SignalNumbers:
    """Numbers of the plan's §6.4 example (1.85, EV +12.9%)."""
    base = SignalNumbers(
        odds=1.85,
        probability=0.61,
        ev=0.129,
        stake_units=Decimal("1.5"),
        confidence=ConfidenceLevel.HIGH,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_diff(
    *,
    after: SignalNumbers | None = None,
    verdict: RecalcVerdict = RecalcVerdict.BELOW_THRESHOLD,
    signal: SignalView | None = None,
) -> RecalcDiff:
    """The §6.4 example: odds 1.85 → 1.72, EV +12.9% → +4.9%, below the threshold."""
    return RecalcDiff(
        signal=signal or make_signal(),
        before=numbers(),
        after=after or numbers(odds=1.72, ev=0.049),
        verdict=verdict,
        recalculated_at=NOW,
    )


def make_digest(signals: tuple[SignalView, ...], **overrides: object) -> DailyDigest:
    """A digest of ``signals`` for ``DAY`` with a yesterday tally."""
    result = AnalysisResult(
        query_id="daily:2026-10-22",
        window=DateWindow(DAY, DAY),
        signals=signals,
        fixtures_analyzed=12,
        matched=len(signals),
        api_requests_used=0,
        requested=overrides.pop("requested", None),  # type: ignore[arg-type]
    )
    yesterday = DaySummary(
        day=date(2026, 10, 21),
        signals=6,
        won=4,
        lost=2,
        void=0,
        pending=0,
        profit_units=Decimal("2.3"),
        clv=None,
    )
    return DailyDigest(day=DAY, result=result, yesterday=overrides.pop("yesterday", yesterday))  # type: ignore[arg-type]


def key(job: str = "daily", subject: str = "all") -> DeliveryKey:
    """A delivery key for tests."""
    return DeliveryKey(job=job, day=DAY, user_id=1001, subject=subject)
