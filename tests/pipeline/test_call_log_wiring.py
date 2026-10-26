"""Provider call log in the pipeline: weekly pruning and the production wiring."""

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.clients.api_football import create_api_football_client as real_create
from app.config import Settings
from app.db.engine import create_engine, create_session_factory
from app.db.repo import count_calls, record_call
from app.db.schema import Base, ProviderCallRow
from app.domain import CallOutcome, CallRecord, Provider
from app.pipeline import retention
from app.pipeline.config import PipelineConfig
from app.pipeline.redaction import register_secrets
from app.pipeline.retention import run_retention
from app.pipeline.runtime import open_runtime
from tests.integration.t4_support import open_bare
from tests.pipeline.world import START

KEY = "football-key-0123456789abcdef"


def record(age: timedelta) -> CallRecord:
    return CallRecord(
        at=START - age,
        provider=Provider.GEMINI,
        operation="Review",
        outcome=CallOutcome.OK,
        duration_ms=10,
        request="prompt",
    )


async def seed(bare_sessions, *ages: timedelta) -> None:  # type: ignore[no-untyped-def]
    async with bare_sessions.begin() as session:
        for age in ages:
            await record_call(session, record(age))


async def test_retention_prunes_calls_older_than_the_configured_period(tmp_path: Path) -> None:
    config = PipelineConfig(call_log_retention=timedelta(days=10))
    async with open_bare(tmp_path / "ret.db", config=config) as bare:
        await seed(bare.sessions, timedelta(days=1), timedelta(days=9), timedelta(days=11))

        report = await run_retention(bare.ctx)

        async with bare.sessions() as session:
            assert await count_calls(session) == 2
    assert report.calls_deleted == 1
    assert report.deleted == 0


async def test_retention_default_is_thirty_days_and_runs_in_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retention, "DELETE_BATCH", 3)
    async with open_bare(tmp_path / "batch.db") as bare:
        old = timedelta(days=45)
        await seed(bare.sessions, *[old] * 7, timedelta(days=29))

        report = await run_retention(bare.ctx)

        async with bare.sessions() as session:
            remaining = (await session.scalars(select(ProviderCallRow))).all()
    assert report.calls_deleted == 7
    assert len(remaining) == 1


async def test_retention_is_idempotent_within_the_week(tmp_path: Path) -> None:
    async with open_bare(tmp_path / "idem.db") as bare:
        await seed(bare.sessions, timedelta(days=45))

        first = await run_retention(bare.ctx)
        again = await run_retention(bare.ctx)

    assert (first.calls_deleted, again.skipped) == (1, True)


def test_the_retention_setting_has_a_default_and_a_floor() -> None:
    assert Settings(_env_file=None).call_log_retention_days == 30  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="call_log_retention_days"):
        Settings(_env_file=None, call_log_retention_days=0)  # type: ignore[call-arg]


async def test_the_runtime_gives_the_football_client_a_scrubbing_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "wired.db"
    engine = create_engine(db_path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    seen: dict[str, object] = {}

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen["recorder"] = kwargs.get("recorder")
        return real_create(*args, **kwargs)

    monkeypatch.setattr("app.pipeline.runtime.create_api_football_client", spy)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        db_path=db_path,
        api_football_key=SecretStr(KEY),
        call_log_retention_days=12,
    )
    register_secrets([KEY])
    try:
        async with open_runtime(settings) as runtime:
            assert runtime.ctx.config.call_log_retention == timedelta(days=12)
            recorder = seen["recorder"]
            assert recorder is not None
            await recorder.record(  # type: ignore[attr-defined]
                CallRecord(
                    at=START,
                    provider=Provider.API_FOOTBALL,
                    operation="fixtures",
                    outcome=CallOutcome.ERROR,
                    duration_ms=1,
                    request=f"x-apisports-key: {KEY}",
                    response=f"echo {KEY}",
                    error=f"url ?key={KEY}",
                )
            )
    finally:
        register_secrets(())

    async with create_session_factory(engine)() as session:
        stored = (await session.execute(select(ProviderCallRow))).scalar_one()
    await engine.dispose()
    assert KEY not in repr((stored.request, stored.response, stored.error))
    assert stored.response == "echo ***"


async def test_the_runtime_without_keys_still_opens_and_has_no_provider(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "bare.db")  # type: ignore[call-arg]

    async with open_runtime(settings) as runtime:
        assert runtime.http is None
        assert isinstance(runtime.ctx.config.call_log_retention, timedelta)
