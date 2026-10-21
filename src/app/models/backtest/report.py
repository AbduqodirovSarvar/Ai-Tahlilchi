"""F3 gate checks and the Markdown backtest report (text only; files are written by callers).

The report is in Uzbek like every project document; identifiers and numbers stay as is.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from app.models.backtest.experiment import ChosenParameters, Evaluation, ExperimentResult
from app.models.backtest.metrics import BettingSummary, Interval

# F3 gate (ishlab_chiqish_rejasi.md): thresholds as written in the plan.
GATE_MIN_BETS: Final = 500
GATE_MAX_CALIBRATION_DEVIATION: Final = 0.05
GATE_GATED_EV: Final = 0.05
GATE_MIN_LEAGUES: Final = 3
# "Positive or close to zero" per league: ROI no worse than this.
GATE_LEAGUE_ROI_FLOOR: Final = -0.02
# Buckets with fewer forecasts are too noisy to judge (SE of a rate ~ 0.5/sqrt(n) > 5 pp).
GATE_MIN_BUCKET_SIZE: Final = 100
TOP_GRID_ROWS: Final = 10


@dataclass(frozen=True, slots=True)
class GateCheck:
    """One F3 gate criterion and whether the hold-out result meets it."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunMetadata:
    """Reproducibility record of one run (``figures``: caption -> relative path)."""

    label: str
    mode: str
    generated_at: datetime
    seed: int
    data_fingerprint: str
    git_commit: str
    n_fixtures: int
    leagues: tuple[str, ...]
    seasons: tuple[int, ...]
    parameters: Mapping[str, str] = field(default_factory=dict)
    figures: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


def gate_checks(evaluation: Evaluation, chosen: ChosenParameters) -> tuple[GateCheck, ...]:
    """The F3 gate criteria evaluated on the hold-out season."""
    return (
        _calibration_check(evaluation),
        _closing_roi_check(evaluation.summary),
        _gated_ev_check(evaluation.summary, chosen),
        _league_check(evaluation.by_league),
    )


def _calibration_check(evaluation: Evaluation) -> GateCheck:
    judged = [bucket for bucket in evaluation.calibration if bucket.n >= GATE_MIN_BUCKET_SIZE]
    worst = max((abs(bucket.deviation) for bucket in judged), default=None)
    if worst is None:
        detail = "baholanadigan oraliq yo'q"
    else:
        detail = (
            f"eng katta og'ish {_pp(worst)} ({len(judged)} ta oraliq, n >= {GATE_MIN_BUCKET_SIZE})"
        )
    passed = worst is not None and worst <= GATE_MAX_CALIBRATION_DEVIATION
    return GateCheck("Kalibratsiya: har 10% oraliqda og'ish <= 5 p.p.", passed, detail)


def _closing_roi_check(summary: BettingSummary) -> GateCheck:
    roi = summary.roi_at_closing
    passed = summary.n_closing >= GATE_MIN_BETS and roi is not None and roi >= 0.0
    detail = (
        f"{summary.n_closing} ta signal, ROI (yopilishda) {_pct(roi)} "
        f"{_ci(summary.roi_at_closing_ci)}"
    )
    name = f"Yopilish koeffitsientida >= {GATE_MIN_BETS} signal va ROI >= 0%"
    return GateCheck(name, passed, detail)


def _gated_ev_check(summary: BettingSummary, chosen: ChosenParameters) -> GateCheck:
    name = "EV >= 5% filtrida ROI musbat"
    if chosen.min_ev < GATE_GATED_EV:
        detail = (
            f"tanlangan min EV {_pct(chosen.min_ev)} < 5%: mezon bu konfiguratsiyada tekshirilmagan"
        )
        return GateCheck(name, False, detail)
    detail = f"ROI {_pct(summary.roi)} {_ci(summary.roi_ci)}, {summary.n_bets} ta signal"
    return GateCheck(name, summary.n_bets > 0 and summary.roi > 0.0, detail)


def _league_check(by_league: Mapping[str, BettingSummary]) -> GateCheck:
    good = [
        name
        for name, summary in by_league.items()
        if summary.n_bets and summary.roi >= GATE_LEAGUE_ROI_FLOOR
    ]
    detail = f"{len(good)} ta liga ROI >= {_pct(GATE_LEAGUE_ROI_FLOOR)}: {', '.join(good) or '-'}"
    name = f"Kamida {GATE_MIN_LEAGUES} ta ligada musbat yoki nolga yaqin"
    return GateCheck(name, len(good) >= GATE_MIN_LEAGUES, detail)


def render_report(result: ExperimentResult, meta: RunMetadata) -> str:
    """The full Markdown report."""
    checks = gate_checks(result.holdout, result.chosen)
    sections = [
        _header(meta, checks),
        _reproducibility(meta, result),
        _method(result),
        _grid_section(result),
        _evaluation_section("Hold-out natijasi (bir marta baholandi)", result.holdout),
        _evaluation_section(
            "Validatsiya (tanlangan konfiguratsiya, optimistik)", result.validation
        ),
        _figures(meta),
        _gate_section(checks),
    ]
    return "\n\n".join(sections) + "\n"


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows),
    ]
    return "\n".join(lines)


def _bullets(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _header(meta: RunMetadata, checks: Sequence[GateCheck]) -> str:
    verdict = "✅ bajarildi" if all(check.passed for check in checks) else "❌ bajarilmadi"
    parts = [f"# Backtest hisoboti: {meta.label}"]
    if meta.notes:
        parts.append("\n".join(f"> {note}" for note in meta.notes))
    parts.append(
        _bullets(
            [
                f"**Rejim:** {meta.mode}",
                f"**Sana:** {meta.generated_at.isoformat(timespec='seconds')}",
                f"**F3 darvozasi (shu ma'lumotda):** {verdict}",
            ]
        )
    )
    return "\n\n".join(parts)


def _reproducibility(meta: RunMetadata, result: ExperimentResult) -> str:
    config = result.config
    rows: list[tuple[str, str]] = [
        ("Git commit", f"`{meta.git_commit}`"),
        ("Ma'lumot izi (SHA-256)", f"`{meta.data_fingerprint}`"),
        ("Seed (bootstrap)", f"`{meta.seed}`"),
        ("O'yinlar (tugagan)", str(meta.n_fixtures)),
        ("Ligalar", ", ".join(meta.leagues)),
        ("Mavsumlar", ", ".join(str(season) for season in meta.seasons)),
        ("Refit oralig'i", f"{config.refit_days} kun"),
        ("Tarix oynasi", f"{config.max_history_days} kun"),
        ("Koeffitsient oralig'i", f"{config.odds_min:.2f}-{config.odds_max:.2f}"),
        ("Bootstrap qayta tanlashlar", str(config.n_resamples)),
        *((key, f"`{value}`") for key, value in meta.parameters.items()),
    ]
    return "## Qayta olinadiganlik\n\n" + _table(("Parametr", "Qiymat"), rows)


def _method(result: ExperimentResult) -> str:
    config = result.config
    pricing, split = config.pricing, result.split
    train = ", ".join(str(season) for season in split.train)
    fallback = ", ".join(config.market_model.fallback) or "-"
    items = [
        "**Walk-forward:** har bir o'yin o'z ligasining `as_of` dan **qat'iy oldingi** "
        "o'yinlari bilan fit qilingan Dixon-Coles modeli bilan bashorat qilinadi "
        "(`as_of` = refit sanasi <= o'yin sanasi; sana = UTC kickoff sanasi). Fitga `as_of` "
        "yoki undan keyingi o'yin kirsa model xato beradi; testda har bir fit tekshiriladi.",
        f"**Bo'linish:** train (faqat tarix) {train} · validatsiya {split.validation} · "
        f"hold-out {split.holdout} (bir marta).",
        f"**Stavka narxi:** `{pricing.bookmaker}` **{pricing.kind}**. **CLV** va bozor bazasi: "
        f"`{pricing.bookmaker}` **{pricing.closing_kind}** (de-vig: "
        f"{config.market_model.method}). Darvozadagi 'yopilishdagi ROI' o'sha stavkalarni "
        "yopilish narxida qayta hisoblaydi.",
        f"**Bozor modeli:** ustuvor {', '.join(config.market_model.priority)}, zaxira "
        f"{fallback}; bashorat vaqtida faqat **{pricing.kind}** narxlari ishlatiladi.",
        "**Ansambl:** `p = w * p_DC + (1 - w) * p_bozor` (to'liq natija taqsimoti bo'yicha); "
        "har o'yindan EV bo'yicha eng yaxshi **bitta** stavka.",
        f"**Stavka hajmi:** flat 1 birlik va Kelly x {config.staking.kelly_multiplier:g} "
        f"(maks. {config.staking.max_fraction:.0%}) {config.staking.bank:g} birlik "
        "**o'zgarmas** bankdan.",
        "**Baholash:** log-loss va Brier (bozor bazasi = de-vig qilingan yopilish), "
        "kalibratsiya, ROI/CLV uchun bootstrap 95% CI (seed bilan).",
    ]
    return "## Metodologiya\n\n" + _bullets(items)


def _grid_section(result: ExperimentResult) -> str:
    chosen, config = result.chosen, result.config
    top = sorted(result.rows, key=lambda row: row.roi, reverse=True)[:TOP_GRID_ROWS]
    items = [
        f"Sinalgan konfiguratsiyalar soni: **{result.n_configurations}** "
        "(xi x w x min EV x min ehtimollik). Eng yaxshi validatsiya ROI si ko'p sinov "
        "(multiple testing) sababli **optimistik**; haqiqiy baho - hold-out.",
        "1-bosqich: `(xi, w)` validatsiya log-loss bo'yicha; 2-bosqich: chegaralar ROI "
        f"bo'yicha (>= {config.min_validation_bets} signal).",
        f"**Tanlangan:** xi = {chosen.xi:g}, w = {chosen.weight:g}, "
        f"min EV = {_pct(chosen.min_ev)}, min ehtimollik = {_rate(chosen.min_probability)}.",
    ]
    if chosen.underpowered:
        items.append(
            f"⚠️ Hech bir chegara validatsiyada >= {config.min_validation_bets} signal "
            "bermadi: eng ko'p signalli konfiguratsiya tanlandi."
        )
    rows = [
        (
            f"{row.xi:g}",
            f"{row.weight:g}",
            _pct(row.min_ev),
            _rate(row.min_probability),
            row.n_bets,
            _pct(row.roi),
            f"{row.log_loss:.4f}",
        )
        for row in top
    ]
    header = ("xi", "w", "min EV", "min p", "signallar", "ROI", "log-loss (ansambl)")
    return (
        f"## Parametr tanlash (validatsiya {result.split.validation})\n\n"
        + _bullets(items)
        + f"\n\nValidatsiya ROI bo'yicha eng yaxshi {len(top)} ta qator:\n\n"
        + _table(header, rows)
    )


def _evaluation_section(title: str, evaluation: Evaluation) -> str:
    skipped = ", ".join(f"{reason}: {count}" for reason, count in evaluation.skipped.items())
    counts = (
        f"Bashoratlar: {evaluation.predictions} · o'tkazib yuborilgan: {skipped or "yo'q"} · "
        f"fitlar: {evaluation.fits} (muvaffaqiyatsiz: {evaluation.fit_failures})"
    )
    return "\n\n".join(
        [
            f"## {title}: mavsum {evaluation.season}",
            counts,
            _summary_table(evaluation.summary),
            "**Proper scoring (bozor nusxasiga o'rtacha; kichik = yaxshi):**",
            _score_table(evaluation),
            "**Kalibratsiya (ansambl):**",
            _calibration_table(evaluation),
            "**Liga kesimi:**",
            _slice_table(evaluation.by_league),
            "**Bozor kesimi:**",
            _slice_table(evaluation.by_market),
        ]
    )


def _summary_table(summary: BettingSummary) -> str:
    kelly = (
        f"{_pct(summary.kelly_roi)} · {_pct(summary.kelly_bank_return)} · "
        f"{summary.kelly_max_drawdown:.2f}"
    )
    rows = [
        ("Signallar", summary.n_bets),
        ("ROI / yield (flat)", f"{_pct(summary.roi)} {_ci(summary.roi_ci)}"),
        ("Foyda (flat, birlik)", f"{summary.profit:+.2f}"),
        ("Hit rate (foyda bilan yopilgan)", _rate(summary.hit_rate)),
        ("Maksimal pasayish (flat, birlik)", f"{summary.max_drawdown:.2f}"),
        (
            f"ROI yopilish narxida ({summary.n_closing} ta)",
            f"{_pct(summary.roi_at_closing)} {_ci(summary.roi_at_closing_ci)}",
        ),
        (
            f"CLV: o'rtacha log(odds / yopilish adolatli) ({summary.n_clv} ta)",
            f"{_pct(summary.mean_clv)} {_ci(summary.clv_ci)}",
        ),
        ("Yopilishni yutgan ulush", _rate(summary.beat_close_rate)),
        ("Yopilish taqsimotida kutilgan EV", _pct(summary.mean_closing_ev)),
        ("Kelly 1/4: ROI · bank daromadi · maks. pasayish", kelly),
    ]
    return _table(("Ko'rsatkich", "Qiymat"), rows)


def _score_table(evaluation: Evaluation) -> str:
    header = (
        "Guruh",
        "n",
        "log-loss ansambl",
        "log-loss DC",
        "log-loss bozor (yopilish)",
        "Brier ansambl",
        "Brier DC",
        "Brier bozor",
    )
    rows = [
        (
            row.group,
            row.n_markets,
            *(
                f"{value:.4f}"
                for value in (
                    row.log_loss_ensemble,
                    row.log_loss_model,
                    row.log_loss_market,
                    row.brier_ensemble,
                    row.brier_model,
                    row.brier_market,
                )
            ),
        )
        for row in evaluation.scores
    ]
    return _table(header, rows) if rows else "_baholanadigan bozor yo'q_"


def _calibration_table(evaluation: Evaluation) -> str:
    rows = [
        (
            f"{bucket.lower:.0%}-{bucket.upper:.0%}",
            bucket.n,
            _rate(bucket.mean_predicted),
            _rate(bucket.observed_rate),
            _pp(bucket.deviation, signed=True),
        )
        for bucket in evaluation.calibration
    ]
    header = ("Oraliq", "n", "o'rtacha bashorat", "haqiqiy chastota", "og'ish")
    return _table(header, rows) if rows else "_bashorat yo'q_"


def _slice_table(slices: Mapping[str, BettingSummary]) -> str:
    if not slices:
        return "_signal yo'q_"
    rows = [
        (name, summary.n_bets, f"{_pct(summary.roi)} {_ci(summary.roi_ci)}", _pct(summary.mean_clv))
        for name, summary in slices.items()
    ]
    return _table(("Kesim", "signallar", "ROI (95% CI)", "CLV"), rows)


def _figures(meta: RunMetadata) -> str:
    if not meta.figures:
        return "## Grafiklar\n\n_grafik yaratilmagan_"
    images = "\n\n".join(f"![{name}]({path})" for name, path in meta.figures.items())
    return f"## Grafiklar\n\n{images}"


def _gate_section(checks: Sequence[GateCheck]) -> str:
    rows = [(check.name, "✅" if check.passed else "❌", check.detail) for check in checks]
    return "## F3 darvozasi (hold-out)\n\n" + _table(("Mezon", "Natija", "Tafsilot"), rows)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2%}"


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _pp(value: float, *, signed: bool = False) -> str:
    return f"{value * 100:+.1f} p.p." if signed else f"{value * 100:.1f} p.p."


def _ci(interval: Interval | None) -> str:
    return "" if interval is None else f"[{interval.low:+.2%}; {interval.high:+.2%}]"
