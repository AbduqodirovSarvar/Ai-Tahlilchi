"""F3 gate checks and the Markdown backtest report (text only; files are written by callers).

The gate follows decision #11 (ishlab_chiqish_rejasi.md, F3): every criterion is judged on
the hold-out season only and, where a sampling interval exists, on its 95% CI. A criterion
met by its point estimate but not by its interval is shown as "ahamiyatsiz" (not
significant) and does **not** count as passed.

The report is in Uzbek like every project document; identifiers and numbers stay as is.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

from app.models.backtest.experiment import ChosenParameters, Evaluation, ExperimentResult
from app.models.backtest.metrics import BettingSummary, Interval

# F3 gate (decision #11): thresholds as written in the plan.
GATE_MIN_BETS: Final = 500
GATE_MAX_CALIBRATION_DEVIATION: Final = 0.05
GATE_MIN_BUCKET_SIZE: Final = 30
GATE_ROI_CI_FLOOR: Final = -0.02
GATE_MIN_LEAGUES: Final = 3
GATE_MIN_LEAGUE_BETS: Final = 100
TOP_GRID_ROWS: Final = 10


class GateStatus(StrEnum):
    """Outcome of one gate criterion."""

    PASSED = "✅"
    NOT_SIGNIFICANT = "ahamiyatsiz"
    FAILED = "❌"


@dataclass(frozen=True, slots=True, kw_only=True)
class GateCheck:
    """One F3 gate criterion: point estimate, interval, sample size and status."""

    name: str
    status: GateStatus
    point: str
    interval: str
    n: str
    detail: str

    @property
    def passed(self) -> bool:
        """Only a fully met criterion passes ("ahamiyatsiz" does not)."""
        return self.status is GateStatus.PASSED


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
    """The F3 gate criteria (decision #11) evaluated on the hold-out season."""
    return (
        _calibration_check(evaluation),
        _clv_check(evaluation.summary),
        _roi_check(evaluation.summary),
        _league_check(evaluation.by_league),
        _baseline_check(evaluation, chosen),
    )


def gate_passed(checks: Sequence[GateCheck]) -> bool:
    """The gate passes only when every criterion is fully met."""
    return all(check.passed for check in checks)


def _calibration_check(evaluation: Evaluation) -> GateCheck:
    judged = [bucket for bucket in evaluation.calibration if bucket.n >= GATE_MIN_BUCKET_SIZE]
    worst = max((abs(bucket.deviation) for bucket in judged), default=None)
    ok = worst is not None and worst <= GATE_MAX_CALIBRATION_DEVIATION
    return GateCheck(
        name=f"Kalibratsiya: har 10% oraliqda (n >= {GATE_MIN_BUCKET_SIZE}) og'ish <= 5 p.p.",
        status=GateStatus.PASSED if ok else GateStatus.FAILED,
        point="-" if worst is None else f"eng katta {_pp(worst)}",
        interval="-",
        n=f"{len(judged)} oraliq / {sum(bucket.n for bucket in judged)} bashorat",
        detail="baholanadigan oraliq yo'q" if worst is None else "ansambl, partition bozorlar",
    )


def _clv_status(mean: float | None, interval: Interval | None) -> GateStatus:
    """Mean > 0 and CI low > 0: passed; mean > 0 only: not significant; else failed."""
    if mean is None or mean <= 0.0:
        return GateStatus.FAILED
    if interval is not None and interval.low > 0.0:
        return GateStatus.PASSED
    return GateStatus.NOT_SIGNIFICANT


def _roi_status(roi: float | None, interval: Interval | None) -> GateStatus:
    """ROI >= 0 and CI low >= floor: passed; ROI >= 0 only: not significant; else failed."""
    if roi is None or roi < 0.0:
        return GateStatus.FAILED
    if interval is not None and interval.low >= GATE_ROI_CI_FLOOR:
        return GateStatus.PASSED
    return GateStatus.NOT_SIGNIFICANT


def _clv_check(summary: BettingSummary) -> GateCheck:
    status = _clv_status(summary.mean_clv, summary.clv_ci)
    enough = summary.n_clv >= GATE_MIN_BETS
    return GateCheck(
        name=f">= {GATE_MIN_BETS} signal; CLV o'rtachasi > 0 va 95% CI pastki chegarasi > 0",
        status=status if enough else GateStatus.FAILED,
        point=_pct(summary.mean_clv),
        interval=_ci(summary.clv_ci) or "-",
        n=f"{summary.n_clv} / {summary.n_bets}",
        detail=(
            "faqat yopilish narxi bor stavkalar"
            + ("" if enough else f"; signal kam (< {GATE_MIN_BETS})")
        ),
    )


def _roi_check(summary: BettingSummary) -> GateCheck:
    point = summary.roi if summary.n_bets else None
    return GateCheck(
        name=f"ROI: nuqta >= 0% va 95% CI pastki chegarasi >= {_pct(GATE_ROI_CI_FLOOR)}",
        status=_roi_status(point, summary.roi_ci),
        point=_pct(point),
        interval=_ci(summary.roi_ci) or "-",
        n=str(summary.n_bets),
        detail="flat, olingan koeffitsientda",
    )


def _league_check(by_league: Mapping[str, BettingSummary]) -> GateCheck:
    eligible = {
        name: summary
        for name, summary in by_league.items()
        if summary.n_bets >= GATE_MIN_LEAGUE_BETS
    }
    good = [
        name
        for name, summary in eligible.items()
        if summary.mean_clv is not None and summary.mean_clv > 0.0
    ]
    return GateCheck(
        name=(
            f"Kamida {GATE_MIN_LEAGUES} ta ligada (har biri n >= {GATE_MIN_LEAGUE_BETS}) CLV > 0"
        ),
        status=GateStatus.PASSED if len(good) >= GATE_MIN_LEAGUES else GateStatus.FAILED,
        point=f"{len(good)} liga",
        interval="-",
        n=f"{len(eligible)} / {len(by_league)} liga n >= {GATE_MIN_LEAGUE_BETS}",
        detail=", ".join(good) or "-",
    )


def _baseline_check(evaluation: Evaluation, chosen: ChosenParameters) -> GateCheck:
    model, market = evaluation.log_loss, evaluation.baseline_log_loss
    comparable = math.isfinite(model) and math.isfinite(market)
    if chosen.weight == 0.0:
        status, detail = GateStatus.FAILED, "w = 0 tanlandi: DC bozorga ma'lumot qo'shmaydi"
    elif comparable and model < market:
        status, detail = GateStatus.PASSED, f"w = {chosen.weight:g} ansambl bozordan yaxshi"
    else:
        status, detail = GateStatus.FAILED, "model bozordan yaxshi emas: bozor modeli rejimi"
    return GateCheck(
        name="Sof bozor (w = 0) bazaviy varianti bilan solishtirish (log-loss)",
        status=status,
        point=f"{model:.4f} vs {market:.4f}" if comparable else "-",
        interval="-",
        n="hold-out partition bozorlar",
        detail=detail,
    )


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
    verdict = "✅ bajarildi" if gate_passed(checks) else "❌ bajarilmadi"
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
    if chosen.boundary_weight:
        items.append(
            f"⚠️ Chegaraviy w = {chosen.weight:g} tanlandi (grid chekkasi): "
            + (
                "sof bozor modeli eng yaxshi; DC qo'shimcha ma'lumot bermadi."
                if chosen.weight == 0.0
                else "sof DC; grid kengaytirilsa yaxshiroq w bo'lishi mumkin."
            )
        )
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
        ("Yopilish narxi bor stavkalar (CLV qamrovi)", f"{summary.n_clv} / {summary.n_bets}"),
        (
            f"Yopilishsiz stavkalar ROI, olingan narxda ({summary.n_without_closing} ta)",
            _pct(summary.roi_without_closing),
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
    rows = [
        (check.name, check.point, check.interval, check.n, check.status.value, check.detail)
        for check in checks
    ]
    header = ("Mezon", "Nuqta bahosi", "95% CI", "n", "Holat", "Izoh")
    note = (
        "Darvoza faqat barcha mezonlar ✅ bo'lsa o'tadi. 'ahamiyatsiz' = nuqta bahosi "
        "bajarilgan, lekin 95% CI bajarilmagan (qarorlar jurnali #11): ✅ hisoblanmaydi."
    )
    return "## F3 darvozasi (hold-out, qaror #11)\n\n" + _table(header, rows) + "\n\n" + note


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2%}"


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _pp(value: float, *, signed: bool = False) -> str:
    return f"{value * 100:+.1f} p.p." if signed else f"{value * 100:.1f} p.p."


def _ci(interval: Interval | None) -> str:
    return "" if interval is None else f"[{interval.low:+.2%}; {interval.high:+.2%}]"
