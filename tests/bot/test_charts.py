"""Chart rendering: valid PNGs for any sane input, errors for no data."""

import struct
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.bot.charts import bank_chart, calibration_chart, segments_chart
from app.services.interfaces import CalibrationBucket, SegmentStat

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
START = date(2026, 10, 1)


def png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(PNG_SIGNATURE)
    return struct.unpack(">II", data[16:24])  # type: ignore[return-value]


def points(count: int) -> list[tuple[date, Decimal]]:
    return [(START + timedelta(days=i), Decimal(100_000 + i * 1_500)) for i in range(count)]


def bucket(lower: float, predicted: float, actual: float, count: int = 12) -> CalibrationBucket:
    return CalibrationBucket(
        lower=lower, upper=lower + 0.1, count=count, predicted=predicted, actual=actual
    )


def segment(key: str, yield_rate: float | None, signals: int = 8) -> SegmentStat:
    return SegmentStat(
        key=key,
        signals=signals,
        profit_units=Decimal(1),
        yield_rate=yield_rate,
        clv=None,
        win_rate=0.5,
    )


@pytest.mark.parametrize("count", [1, 2, 9, 60])
def test_bank_chart_is_a_png_for_any_history_length(count: int) -> None:
    data = bank_chart(points(count), Decimal(100_000))

    assert png_size(data) == (960, 540)


def test_bank_chart_without_a_starting_bank_and_with_a_losing_run() -> None:
    losing = [(START + timedelta(days=i), Decimal(100_000 - i * 9_000)) for i in range(5)]

    assert png_size(bank_chart(losing, None))[0] > 0


def test_bank_chart_needs_a_point() -> None:
    with pytest.raises(ValueError, match="at least one point"):
        bank_chart([], Decimal(1))


@given(st.lists(st.integers(min_value=-(10**9), max_value=10**9), min_size=1, max_size=40))
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_bank_chart_never_fails_on_any_balances(balances: list[int]) -> None:
    series = [(START + timedelta(days=i), Decimal(value)) for i, value in enumerate(balances)]

    assert bank_chart(series, None).startswith(PNG_SIGNATURE)


def test_calibration_chart_draws_buckets() -> None:
    data = calibration_chart(
        [bucket(0.5, 0.55, 0.5), bucket(0.6, 0.65, 0.7), bucket(0.7, 0.75, 0.6)]
    )

    assert png_size(data) == (960, 540)
    with pytest.raises(ValueError, match="at least one bucket"):
        calibration_chart([])


def test_segments_chart_handles_missing_yield_and_negative_bars() -> None:
    data = segments_chart(
        [segment("Premier League", 0.08), segment("La Liga", -0.12), segment("Serie A", None)],
        "Liga bo'yicha",
    )

    assert data.startswith(PNG_SIGNATURE)
    with pytest.raises(ValueError, match="at least one slice"):
        segments_chart([], "x")
