"""scripts/llm_eval.py: refuses to run without a key; offline run over recorded answers."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import BaseModel

from app.clients.gemini import GeminiResponse, GeminiUsage
from app.config import Settings
from app.llm.evaluation import load_golden_cases

REPO_ROOT = Path(__file__).resolve().parents[2]
ALL_CASES = load_golden_cases(REPO_ROOT / "tests" / "llm" / "golden")
CASES = {candidate.candidate_id: case for case in ALL_CASES for candidate in case.candidates}


@pytest.fixture
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("llm_eval", REPO_ROOT / "scripts" / "llm_eval.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordedClient:
    """Stands in for GeminiClient: answers each request with the case's recorded answer."""

    model = "recorded-model"

    async def generate_json(
        self,
        *,
        system_instruction: str,  # noqa: ARG002
        contents: str,
        response_schema: type[BaseModel],  # noqa: ARG002
        operation: str | None = None,  # noqa: ARG002
    ) -> GeminiResponse:
        block = contents.split("<<<DATA>>>")[1].split("<<<END_DATA>>>", maxsplit=1)[0]
        first_id = json.loads(block)["candidates"][0]["id"]
        return GeminiResponse(
            text=CASES[first_id].recorded_text,
            model=self.model,
            usage=GeminiUsage(input_tokens=10, output_tokens=5),
            attempts=1,
        )


def test_without_key_exits_2_with_clear_message(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(script, "get_settings", lambda: Settings(_env_file=None))

    code = script.main([])

    assert code == script.EXIT_CONFIGURATION_ERROR == 2
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().out


def test_offline_run_writes_report_and_skips_recorded_only_cases(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings.model_validate({"gemini_api_key": "AIza-test-key-0123456789"})
    monkeypatch.setattr(script, "get_settings", lambda: settings)
    monkeypatch.setattr(script, "create_gemini_client", lambda _settings: RecordedClient())

    code = script.main(["--report-dir", str(tmp_path)])

    assert code == script.EXIT_OK
    (report,) = tmp_path.glob("*.md")
    text = report.read_text(encoding="utf-8")
    live_cases = [case for case in ALL_CASES if not case.recorded_only]
    assert f"**{len(live_cases)}/{len(live_cases)}**" in text
    assert "recorded_hallucinated_player" not in text
    assert "recorded-model" in text
