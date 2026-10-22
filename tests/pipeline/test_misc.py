"""LLM verdict effects, the refresh CLI, the default notifier, errors and main wiring."""

from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import main as main_module
from app.config import Settings
from app.db.enums import LlmVerdict
from app.pipeline import refresh as refresh_module
from app.pipeline.delivery import NullDelivery
from app.pipeline.errors import BudgetExhaustedError, ProviderUnavailableError
from app.pipeline.refresh import EXIT_CONFIGURATION, EXIT_OK, EXIT_UNEXPECTED, RefreshSummary
from app.pipeline.review import Review, ReviewCandidate, ReviewKey
from app.scheduler import PipelineJobs
from app.services.errors import BusyError, ServiceUnavailableError
from app.services.interfaces import AdminAlert, AnalysisQuery, DateSpec, Verdict
from app.services.real import create_real_services
from app.services.real.notifier import LogOnlyNotifier
from tests.pipeline.harness import Env, open_env
from tests.pipeline.world import World

USER = 3003


class Verdicts:
    """A reviewer giving every candidate the same verdict."""

    def __init__(self, verdict: LlmVerdict) -> None:
        self.verdict = verdict

    async def review(self, candidates: Sequence[ReviewCandidate]) -> Mapping[ReviewKey, Review]:
        return {c.key: Review(verdict=self.verdict, note="Izoh", risk_flags=("rotatsiya",)) for c in candidates}


async def analyse(env: Env, **changes: object) -> list:  # type: ignore[type-arg]
    query = AnalysisQuery(
        dates=DateSpec.next_days(3), min_probability=0.30, min_ev=0.0, **changes  # type: ignore[arg-type]
    )
    return list((await env.system.services.analysis.analyze(USER, query)).signals)


async def test_a_rejected_candidate_is_not_recommended(
    seeded_template: tuple[Path, World], tmp_path: Path
) -> None:
    template, world = seeded_template
    async with open_env(template, world, tmp_path / "a.db") as plain:
        baseline = await analyse(plain)
    async with open_env(
        template, world, tmp_path / "b.db", reviewer=Verdicts(LlmVerdict.REJECT)
    ) as env:
        regular = await analyse(env)
        custom = await analyse(env, include_no_value=True)

    assert baseline
    assert regular == []
    assert custom
    assert all(signal.no_value and not signal.recommended for signal in custom)
    assert any(signal.llm_verdict is Verdict.REJECT for signal in custom)


async def test_a_cautious_candidate_stays_recommended_with_its_note(
    seeded_template: tuple[Path, World], tmp_path: Path
) -> None:
    template, world = seeded_template
    async with open_env(
        template, world, tmp_path / "c.db", reviewer=Verdicts(LlmVerdict.CAUTION)
    ) as env:
        signals = await analyse(env)

    assert signals
    assert all(s.recommended and s.llm_verdict is Verdict.CAUTION for s in signals)
    assert all(s.llm_note == "Izoh" and "rotatsiya" in s.risk_flags for s in signals)


async def test_the_default_notifier_only_logs(seeded_env: Env) -> None:
    system = create_real_services(seeded_env.ctx)
    jobs = PipelineJobs(seeded_env.ctx, system.delivery)
    notifier = LogOnlyNotifier()
    await notifier.alert_admin(AdminAlert(component="x", message="y", at=datetime.now(UTC)))
    await jobs.run("recalc-tick")
    assert await NullDelivery().alert_admin("x", "y", datetime.now(UTC)) is None


def test_pipeline_errors_carry_their_numbers() -> None:
    budget = BudgetExhaustedError(5, 2)
    assert (budget.needed, budget.available) == (5, 2)
    outage = ProviderUnavailableError("api-football", "down")
    assert (outage.provider, outage.detail) == ("api-football", "down")
    assert ServiceUnavailableError("db", "locked").component == "db"
    assert ServiceUnavailableError("db").detail == ""
    assert BusyError(7).user_id == 7


# ------------------------------------------------------------------------------ refresh CLI
def settings(tmp_path: Path, **changes: object) -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "cli.db", **changes)  # type: ignore[call-arg, arg-type]


def test_refresh_cli_explains_a_missing_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.config.get_settings", lambda: settings(tmp_path))
    assert refresh_module.main() == EXIT_CONFIGURATION


def test_refresh_cli_runs_the_refresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []

    @asynccontextmanager
    async def runtime(_settings: Settings):  # type: ignore[no-untyped-def]
        class Holder:
            ctx = object()

        yield Holder()

    async def fake_refresh(ctx: object) -> RefreshSummary:
        calls.append(ctx)
        return RefreshSummary(already_done=True)

    monkeypatch.setattr(
        "app.config.get_settings", lambda: settings(tmp_path, api_football_key="k" * 20)
    )
    monkeypatch.setattr("app.pipeline.runtime.open_runtime", runtime)
    monkeypatch.setattr(refresh_module, "refresh", fake_refresh)

    assert refresh_module.main() == EXIT_OK
    assert len(calls) == 1


def test_refresh_cli_reports_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def broken() -> Settings:
        raise RuntimeError("boom")

    monkeypatch.setattr("app.config.get_settings", broken)
    assert refresh_module.main() == EXIT_UNEXPECTED


# ------------------------------------------------------------------------------ main
async def test_main_runs_the_scheduler_beside_the_real_services(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = []
    polled = []

    async def poll(dispatcher: object, token: str, admins: frozenset[int], _s: Settings) -> None:
        polled.append((token, admins))
        started.append(True)

    monkeypatch.setattr(main_module, "poll", poll)
    config = settings(tmp_path, app_services="real", telegram_bot_token="42:TEST", admin_ids="1")

    await main_module.run_bot(config)

    assert polled == [("42:TEST", frozenset({1}))]
    assert started == [True]

