"""Walk-forward backtest of the model (F3.7-F3.8): grid on validation, one hold-out run.

Usage:

    uv run python scripts/backtest.py --synthetic           # synthetic efficient market
    uv run python scripts/backtest.py                       # history in DB_PATH
    uv run python scripts/backtest.py --leagues E0,SP1 --xis 0.0019 --weights 0.5,1

Real-data mode reads finished fixtures and odds from the application database (fill it with
``scripts/import_history.py`` first). Seasons that still have unplayed fixtures are never
used as the hold-out. The report goes to ``docs/backtest/<date>-<label>.md`` with a
calibration and an equity PNG next to it. Methodology: docs/adr/0005-backtest-methodology.md.

Exit codes: 0 report written, 2 nothing to evaluate (empty database, missing schema, too
few seasons) or bad arguments.
"""

import argparse
import asyncio
import io
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import matplotlib as mpl

mpl.use("Agg")  # files only; never open a window

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.engine import create_engine, create_session_factory
from app.db.mappers import fixture_from_row, odds_from_row, result_from_row
from app.db.schema import FixtureRow, OddsSnapshotRow
from app.domain import FixtureStatus, HistoryBatch
from app.logging import configure_logging, get_logger
from app.models import ModelInputError
from app.models.backtest import (
    ExperimentConfig,
    ExperimentResult,
    GridSpec,
    RunMetadata,
    SyntheticConfig,
    build_dataset,
    dataset_fingerprint,
    generate_history,
    render_report,
    run_experiment,
)

logger = get_logger("scripts.backtest")

EXIT_OK: Final = 0
EXIT_NOTHING_TO_EVALUATE: Final = 2
NO_HISTORY_MESSAGE: Final = "no history in DB - run scripts/import_history.py first"
DEFAULT_OUT_DIR: Final = Path("docs/backtest")
SYNTHETIC_NOTE: Final = (
    "Bu **sintetik** ma'lumotdagi backtest: bozor ta'rifiga ko'ra samarali (narx = haqiqiy "
    "ehtimollik + marja), shuning uchun kutilgan natija ROI ~ -marja va ahamiyatli ustunlik "
    "yo'qligi. U backtest vositasini tekshiradi, modelni emas. **Haqiqiy ma'lumotdagi F3 "
    "darvozasi hali kutilmoqda** (qarorlar jurnali #7: football-data.co.uk bu mashinada "
    "bloklangan; serverda `scripts/import_history.py` + `scripts/backtest.py` bilan bajariladi)."
)
# Colours of the reference dataviz palette (categorical slots 1-2, secondary text ink).
SERIES_1: Final = "#2a78d6"
SERIES_2: Final = "#eb6834"
INK_SECONDARY: Final = "#52514e"
GRID_INK: Final = "#e4e3df"
FIGURE_DPI: Final = 120


@dataclass(frozen=True, slots=True)
class Loaded:
    """Data to backtest and how it was obtained."""

    batch: HistoryBatch
    holdout_season: int | None
    parameters: dict[str, str]


EMPTY: Final = Loaded(HistoryBatch(), None, {})


def float_list(text: str) -> tuple[float, ...]:
    """``"0.1,0.2"`` -> ``(0.1, 0.2)``."""
    try:
        values = tuple(float(part) for part in text.split(",") if part.strip())
    except ValueError as exc:
        msg = f"expected comma-separated numbers, got {text!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    if not values:
        msg = "expected at least one number"
        raise argparse.ArgumentTypeError(msg)
    return values


def parse_args(argv: Sequence[str], *, default_db: Path) -> argparse.Namespace:
    """Command-line options."""
    grid = GridSpec()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--synthetic", action="store_true", help="synthetic efficient market")
    parser.add_argument("--db-path", type=Path, default=default_db)
    parser.add_argument("--leagues", default="", help="league external ids, e.g. E0,SP1")
    parser.add_argument("--label", default=None, help="report name suffix")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--seed", type=int, default=ExperimentConfig().seed)
    parser.add_argument("--xis", type=float_list, default=grid.xis)
    parser.add_argument("--weights", type=float_list, default=grid.weights)
    parser.add_argument("--min-evs", type=float_list, default=grid.min_evs)
    parser.add_argument("--min-probs", type=float_list, default=grid.min_probabilities)
    parser.add_argument("--refit-days", type=int, default=ExperimentConfig().refit_days)
    parser.add_argument("--resamples", type=int, default=ExperimentConfig().n_resamples)
    parser.add_argument("--holdout-season", type=int, default=None)
    parser.add_argument("--synthetic-leagues", type=int, default=SyntheticConfig().leagues)
    parser.add_argument("--synthetic-teams", type=int, default=SyntheticConfig().teams_per_league)
    parser.add_argument("--synthetic-seasons", type=int, default=SyntheticConfig().seasons)
    return parser.parse_args(argv)


async def _has_schema(engine: AsyncEngine) -> bool:
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    return {"fixtures", "odds_snapshots"} <= tables


async def load_history(db_path: Path, leagues: frozenset[str]) -> Loaded:
    """Finished fixtures, results and odds from a database file that exists (may be empty)."""
    engine = create_engine(db_path)
    try:
        if not await _has_schema(engine):
            return EMPTY
        async with create_session_factory(engine)() as session:
            fixture_rows = (
                await session.scalars(
                    select(FixtureRow).options(
                        selectinload(FixtureRow.league),
                        selectinload(FixtureRow.home_team),
                        selectinload(FixtureRow.away_team),
                    )
                )
            ).all()
            odds_rows = (await session.scalars(select(OddsSnapshotRow))).all()
    finally:
        await engine.dispose()
    return _to_batch(fixture_rows, odds_rows, leagues)


def _to_batch(
    fixture_rows: Sequence[FixtureRow],
    odds_rows: Sequence[OddsSnapshotRow],
    leagues: frozenset[str],
) -> Loaded:
    selected = [row for row in fixture_rows if not leagues or row.league.external_id in leagues]
    incomplete = {row.season for row in selected if row.status is not FixtureStatus.FINISHED}
    finished = [row for row in selected if row.status is FixtureStatus.FINISHED]
    complete = sorted({row.season for row in finished} - incomplete)
    refs = {row.id: fixture_from_row(row).ref for row in finished}
    batch = HistoryBatch(
        fixtures=tuple(fixture_from_row(row) for row in finished),
        results=tuple(result for row in finished if (result := result_from_row(row)) is not None),
        odds=tuple(
            odds_from_row(row, fixture=refs[row.fixture_id])
            for row in odds_rows
            if row.fixture_id in refs
        ),
    )
    holdout = complete[-1] if complete else None
    parameters = {"incomplete seasons (not hold-out)": str(sorted(incomplete) or "-")}
    return Loaded(batch, holdout, parameters)


def synthetic_history(args: argparse.Namespace) -> Loaded:
    """Generated history and its parameters."""
    config = SyntheticConfig(
        leagues=args.synthetic_leagues,
        teams_per_league=args.synthetic_teams,
        seasons=args.synthetic_seasons,
        seed=args.seed,
    )
    parameters = {
        f"synthetic.{name}": str(getattr(config, name))
        for name in (
            "leagues",
            "teams_per_league",
            "seasons",
            "first_season",
            "seed",
            "intercept",
            "home_advantage",
            "rho",
            "strength_sd",
            "season_drift_sd",
            "match_shock_sd",
        )
    }
    parameters["synthetic.bookmakers"] = ", ".join(
        f"{p.bookmaker}/{p.kind}/{p.margin:.1%}" for p in config.bookmakers
    )
    return Loaded(generate_history(config), None, parameters)


def git_commit() -> str:
    """Current commit, suffixed ``-dirty`` with uncommitted changes; ``unknown`` without git."""
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        head = subprocess.run(  # noqa: S603 - fixed argv, no user input
            [git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(  # noqa: S603 - fixed argv, no user input
            [git, "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{head}-dirty" if status else head


def plot_calibration(result: ExperimentResult, path: Path) -> None:
    """Predicted vs observed frequency per probability bucket (hold-out)."""
    buckets = result.holdout.calibration
    figure, axes = plt.subplots(figsize=(6, 6))
    axes.plot([0, 1], [0, 1], color=INK_SECONDARY, linewidth=1, linestyle="--", label="ideal")
    axes.plot(
        [b.mean_predicted for b in buckets],
        [b.observed_rate for b in buckets],
        color=SERIES_1,
        linewidth=2,
        marker="o",
        markersize=8,
        label="ansambl (hold-out)",
    )
    for bucket in buckets:
        axes.annotate(
            f"n={bucket.n}",
            (bucket.mean_predicted, bucket.observed_rate),
            textcoords="offset points",
            xytext=(6, -12),
            fontsize=8,
            color=INK_SECONDARY,
        )
    axes.set(xlim=(0, 1), ylim=(0, 1), xlabel="bashorat ehtimolligi", ylabel="haqiqiy chastota")
    axes.set_title(f"Kalibratsiya, mavsum {result.holdout.season}")
    _finish(figure, axes, path)


def plot_equity(result: ExperimentResult, path: Path) -> None:
    """Cumulative profit of the hold-out bets, flat and 1/4 Kelly, in units."""
    bets = result.holdout.bets
    flat, kelly = [0.0], [0.0]
    for bet in bets:
        flat.append(flat[-1] + bet.flat_profit)
        kelly.append(kelly[-1] + bet.kelly_profit)
    figure, axes = plt.subplots(figsize=(8, 4.5))
    axes.axhline(0, color=INK_SECONDARY, linewidth=1)
    axes.plot(flat, color=SERIES_1, linewidth=2, label="flat (1 birlik)")
    axes.plot(kelly, color=SERIES_2, linewidth=2, label="Kelly 1/4 (100 birlik bank)")
    axes.set(xlabel="stavka tartib raqami", ylabel="jami foyda, birlik")
    axes.set_title(f"Foyda egri chizig'i, mavsum {result.holdout.season} ({len(bets)} stavka)")
    _finish(figure, axes, path)


def _finish(figure: Figure, axes: Axes, path: Path) -> None:
    axes.grid(color=GRID_INK, linewidth=0.8)
    axes.spines[["top", "right"]].set_visible(False)
    axes.legend(frameon=False)
    figure.tight_layout()
    figure.savefig(path, dpi=FIGURE_DPI)
    plt.close(figure)


def experiment_config(args: argparse.Namespace, holdout: int | None) -> ExperimentConfig:
    """Experiment settings from the command line."""
    return ExperimentConfig(
        grid=GridSpec(
            xis=args.xis,
            weights=args.weights,
            min_evs=args.min_evs,
            min_probabilities=args.min_probs,
        ),
        refit_days=args.refit_days,
        seed=args.seed,
        n_resamples=args.resamples,
        holdout_season=args.holdout_season if args.holdout_season is not None else holdout,
    )


def efficiency_note(result: ExperimentResult) -> str:
    """Sanity verdict for an efficient market: ROI must be consistent with the closing EV."""
    summary = result.holdout.summary
    expected, interval = summary.mean_closing_ev, summary.roi_ci
    if expected is None or interval is None or summary.mean_clv is None:
        return "Sanity tekshiruvi: hold-out da signal yetarli emas."
    consistent = "ha" if interval.contains(expected) else "YO'Q"
    edge = "BOR (xato belgisi!)" if interval.low > 0.0 else "yo'q"
    return (
        "Sanity tekshiruvi (samarali bozor): kutilgan ROI (yopilish taqsimotida) "
        f"{expected:+.2%}, haqiqiy ROI {summary.roi:+.2%} [{interval.low:+.2%}; "
        f"{interval.high:+.2%}], {summary.n_bets} ta signal. CI kutilganni o'z ichiga oladi: "
        f"{consistent}; ahamiyatli musbat ustunlik: {edge}; o'rtacha CLV "
        f"{summary.mean_clv:+.2%}."
    )


def write_report(
    args: argparse.Namespace, loaded: Loaded, result: ExperimentResult, commit: str
) -> Path:
    """Markdown report plus PNGs; returns the report path."""
    now = datetime.now(UTC)
    label = args.label or ("synthetic" if args.synthetic else "real")
    stem = f"{now.date().isoformat()}-{label}"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    figures = {
        "Kalibratsiya": f"{stem}-calibration.png",
        "Foyda egri chizig'i": f"{stem}-equity.png",
    }
    plot_calibration(result, args.out_dir / figures["Kalibratsiya"])
    plot_equity(result, args.out_dir / figures["Foyda egri chizig'i"])
    league_names = {fixture.league.name for fixture in loaded.batch.fixtures}
    meta = RunMetadata(
        label=label,
        mode="sintetik" if args.synthetic else "haqiqiy (DB)",
        generated_at=now,
        seed=args.seed,
        data_fingerprint=dataset_fingerprint(loaded.batch),
        git_commit=commit,
        n_fixtures=len(loaded.batch.fixtures),
        leagues=tuple(sorted(league_names)),
        seasons=tuple(sorted({fixture.season for fixture in loaded.batch.fixtures})),
        parameters=loaded.parameters,
        figures=figures,
        notes=(SYNTHETIC_NOTE, efficiency_note(result)) if args.synthetic else (),
    )
    path: Path = args.out_dir / f"{stem}.md"
    path.write_text(render_report(result, meta), encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")
    configure_logging(level="INFO", app_env="dev")
    args = parse_args(sys.argv[1:] if argv is None else argv, default_db=get_settings().db_path)
    commit = git_commit()  # before any file is written, so "-dirty" means code changes
    if args.synthetic:
        loaded = synthetic_history(args)
    else:
        leagues = frozenset(code.strip() for code in args.leagues.split(",") if code.strip())
        # A missing file would be created empty by SQLite; treat it as "no history".
        exists = args.db_path.is_file()
        loaded = asyncio.run(load_history(args.db_path, leagues)) if exists else EMPTY
    if not loaded.batch.fixtures:
        logger.error("backtest_aborted", reason=NO_HISTORY_MESSAGE, db_path=str(args.db_path))
        return EXIT_NOTHING_TO_EVALUATE
    try:
        result = run_experiment(
            build_dataset(loaded.batch), experiment_config(args, loaded.holdout_season)
        )
    except ModelInputError as exc:
        logger.error("backtest_aborted", reason=str(exc))
        return EXIT_NOTHING_TO_EVALUATE
    path = write_report(args, loaded, result, commit)
    summary = result.holdout.summary
    logger.info(
        "backtest_finished",
        report=str(path),
        holdout_season=result.split.holdout,
        bets=summary.n_bets,
        roi=round(summary.roi, 4),
        mean_clv=None if summary.mean_clv is None else round(summary.mean_clv, 4),
        configurations=result.n_configurations,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
