"""PNG charts for the bot (bank history, calibration, segment yield) drawn with matplotlib.

Pure functions from data to PNG bytes. They use a ``Figure`` directly (no ``pyplot``), so there
is no global state and they are safe to call from worker threads: the handlers run them with
``asyncio.to_thread`` because rendering blocks. One light surface (Telegram shows the image on
either theme), a single accent ink for the data, recessive grid, thin marks, text in ink
colours only; a reference line is dashed gray.
"""

import io
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from matplotlib.axes import Axes
from matplotlib.figure import Figure

from app.bot import texts
from app.services.interfaces import CalibrationBucket, SegmentStat

SURFACE: Final = "#ffffff"
INK: Final = "#1f2937"
MUTED: Final = "#6b7280"
GRID: Final = "#e5e7eb"
ACCENT: Final = "#2563eb"
GOOD: Final = "#15803d"
BAD: Final = "#b91c1c"
FIGSIZE: Final = (8.0, 4.5)
DPI: Final = 120
LINE_WIDTH: Final = 2.0
BAR_WIDTH: Final = 0.6
PERCENT: Final = 100
MAX_X_LABELS: Final = 10


def _plain(text: str) -> str:
    """Escape ``$`` so matplotlib draws provider names literally instead of as mathtext."""
    return text.replace("$", r"\$")


def _frame(title: str) -> tuple[Figure, Axes]:
    """A figure on the light surface with a title and a quiet grid."""
    figure = Figure(figsize=FIGSIZE, dpi=DPI, facecolor=SURFACE)
    axes = figure.add_subplot(111, facecolor=SURFACE)
    axes.set_title(_plain(title), color=INK, fontsize=13, loc="left", pad=12)
    axes.grid(True, color=GRID, linewidth=0.8)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(GRID)
    axes.tick_params(colors=MUTED, labelsize=9)
    return figure, axes


def _png(figure: Figure) -> bytes:
    buffer = io.BytesIO()
    figure.tight_layout()
    figure.savefig(buffer, format="png", facecolor=SURFACE)
    return buffer.getvalue()


def bank_chart(points: Sequence[tuple[date, Decimal]], start: Decimal | None) -> bytes:
    """Bank balance at the end of each settled day, with the starting bank as a dashed line.

    Raises:
        ValueError: ``points`` is empty (there is nothing to draw).
    """
    if not points:
        msg = "a bank chart needs at least one point"
        raise ValueError(msg)
    figure, axes = _frame(texts.CHART_BANK_TITLE)
    positions = list(range(len(points)))
    balances = [float(balance) for _, balance in points]
    axes.plot(
        positions,
        balances,
        color=ACCENT,
        linewidth=LINE_WIDTH,
        marker="o",
        markersize=5,
        markerfacecolor=ACCENT,
        markeredgecolor=SURFACE,
        markeredgewidth=1.5,
    )
    if start is not None:
        axes.axhline(float(start), color=MUTED, linewidth=1.0, linestyle="--")
        axes.annotate(
            texts.CHART_BANK_START,
            (0, float(start)),
            color=MUTED,
            fontsize=9,
            xytext=(0, 4),
            textcoords="offset points",
        )
    last = balances[-1]
    axes.annotate(
        f"{last:,.0f}".replace(",", " "),
        (positions[-1], last),
        color=INK,
        fontsize=10,
        xytext=(0, 8),
        textcoords="offset points",
        ha="center",
    )
    step = max(1, -(-len(points) // MAX_X_LABELS))
    shown = positions[::step]
    axes.set_xticks(shown)
    axes.set_xticklabels([points[i][0].strftime("%d.%m") for i in shown])
    axes.margins(x=0.08)
    axes.yaxis.set_major_formatter(lambda value, _pos: f"{value:,.0f}".replace(",", " "))
    return _png(figure)


def calibration_chart(buckets: Sequence[CalibrationBucket]) -> bytes:
    """Promised probability against the share that really won, per bucket (diagonal = perfect).

    Raises:
        ValueError: ``buckets`` is empty.
    """
    if not buckets:
        msg = "a calibration chart needs at least one bucket"
        raise ValueError(msg)
    figure, axes = _frame(texts.CHART_CALIBRATION_TITLE)
    predicted = [bucket.predicted * PERCENT for bucket in buckets]
    actual = [bucket.actual * PERCENT for bucket in buckets]
    axes.plot([0, PERCENT], [0, PERCENT], color=MUTED, linewidth=1.0, linestyle="--")
    axes.plot(
        predicted,
        actual,
        color=ACCENT,
        linewidth=LINE_WIDTH,
        marker="o",
        markersize=6,
        markerfacecolor=ACCENT,
        markeredgecolor=SURFACE,
        markeredgewidth=1.5,
    )
    for bucket, x, y in zip(buckets, predicted, actual, strict=True):
        axes.annotate(
            f"n={bucket.count}",
            (x, y),
            color=MUTED,
            fontsize=8,
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
        )
    axes.set_xlim(0, PERCENT)
    axes.set_ylim(0, PERCENT)
    axes.set_xlabel(texts.CHART_CALIBRATION_X, color=MUTED, fontsize=9)
    axes.set_ylabel(texts.CHART_CALIBRATION_Y, color=MUTED, fontsize=9)
    return _png(figure)


def segments_chart(segments: Sequence[SegmentStat], title: str) -> bytes:
    """Yield per slice as horizontal bars (green above zero, red below, labelled).

    Raises:
        ValueError: ``segments`` is empty.
    """
    if not segments:
        msg = "a segments chart needs at least one slice"
        raise ValueError(msg)
    figure, axes = _frame(title)
    names = [_plain(segment.key) for segment in segments]
    values = [(segment.yield_rate or 0.0) * PERCENT for segment in segments]
    colors = [GOOD if value >= 0 else BAD for value in values]
    axes.barh(names, values, color=colors, height=BAR_WIDTH)
    axes.axvline(0, color=MUTED, linewidth=1.0)
    axes.invert_yaxis()
    for index, (value, segment) in enumerate(zip(values, segments, strict=True)):
        axes.annotate(
            f"{value:+.1f}%  (n={segment.signals})",
            (value, index),
            color=INK,
            fontsize=9,
            xytext=(6 if value >= 0 else -6, 0),
            textcoords="offset points",
            ha="left" if value >= 0 else "right",
            va="center",
        )
    axes.set_xlabel(texts.CHART_YIELD_X, color=MUTED, fontsize=9)
    axes.margins(x=0.25)
    return _png(figure)
