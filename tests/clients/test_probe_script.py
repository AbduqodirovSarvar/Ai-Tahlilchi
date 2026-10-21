"""scripts/probe_api.py: no-key exit, full offline run over respx, sanitizing, AH evidence."""

import importlib.util
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import ModuleType

import pytest
import respx

from app.clients.api_football import API_FOOTBALL_CONFIG, create_api_football_client
from app.clients.api_football_models import OddsItem
from app.config import get_settings
from app.sports.football_mapping import HandicapConvention
from tests.clients.conftest import BASE_URL, TEST_KEY, load

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2024, 10, 20, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "probe_api", REPO_ROOT / "scripts" / "probe_api.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_cwd(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)  # no developer .env, outputs land in tmp
    # The free-plan pacing (10/min) would make the probe wait 6 s per request.
    fast = replace(API_FOOTBALL_CONFIG, requests_per_minute=60_000, burst=100)
    monkeypatch.setattr(
        script, "create_api_football_client", partial(create_api_football_client, config=fast)
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_without_key_exits_cleanly_and_requests_nothing(script: ModuleType, tmp_path: Path) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        code = script.main(["--report", str(tmp_path / "r.md")])
        assert router.calls.call_count == 0
    assert code == script.EXIT_CONFIGURATION_ERROR
    assert not (tmp_path / "r.md").exists()


def test_unknown_league_is_a_configuration_error(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_FOOTBALL_KEY", TEST_KEY)
    assert script.main(["--leagues", "39,abc"]) == script.EXIT_CONFIGURATION_ERROR


def mock_provider(router: respx.MockRouter) -> None:
    router.get("/status").respond(json=load("status.json"))
    router.get("/fixtures", params={"league": "140"}).respond(json=load("error_plan.json"))
    router.get("/fixtures").respond(json=load("fixtures_league_39_2024.json"))
    router.get("/odds").respond(json=load("odds_fixture_1208100.json"))
    router.get("/fixtures/statistics").respond(json=load("statistics_fixture_1208021.json"))
    router.get("/fixtures/lineups").respond(json=load("lineups_fixture_1208100.json"))
    router.get("/injuries").respond(json=load("injuries_fixture_1208100.json"))


def test_full_probe_writes_report_and_sanitized_recordings(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_FOOTBALL_KEY", TEST_KEY)
    report_path, record_dir = tmp_path / "report.md", tmp_path / "recorded"
    with respx.mock(base_url=BASE_URL) as router:
        mock_provider(router)
        code = script.main(
            ["--leagues", "39,140", "--report", str(report_path), "--record-dir", str(record_dir)],
            now=NOW,
        )
        calls = router.calls.call_count
    assert code == script.EXIT_OK
    assert calls == 1 + 2 + 1 + 4  # status, EPL fixtures+odds, La Liga (closed), context
    report = report_path.read_text(encoding="utf-8")
    assert "Tarif: Free" in report
    assert "| Premier League (39) | ochiq | 8 |" in report
    assert "yopiq: Free plans do not have access" in report
    assert "xG (`fixtures/statistics`): ha" in report
    # Two bookmakers x three lines read as home lines; only the 0-lines pair otherwise.
    assert "`home_line`: 6/6" in report
    assert "`own_line`: 2/2" in report
    recorded = {path.name for path in record_dir.iterdir()}
    assert "status_all.json" in recorded
    assert "fixtures_league-39_season-2024.json" in recorded
    status = (record_dir / "status_all.json").read_text(encoding="utf-8")
    assert "user@example.com" not in status
    assert TEST_KEY not in "".join(
        (record_dir / name).read_text(encoding="utf-8") for name in recorded
    )


def test_no_record_and_provider_failure(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_FOOTBALL_KEY", TEST_KEY)
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/status").respond(json=load("error_token.json"))
        code = script.main(["--leagues", "39", "--no-record"], now=NOW)
    assert code == script.EXIT_PROVIDER_ERROR
    assert not (tmp_path / "tests").exists()


def test_probe_with_no_upcoming_fixtures(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_FOOTBALL_KEY", TEST_KEY)
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/status").respond(json={"response": [], "errors": []})
        router.get("/fixtures").respond(json=load("fixtures_empty.json"))
        code = script.main(["--leagues", "39", "--no-record", "--report", "r.md"], now=NOW)
    assert code == script.EXIT_OK
    report = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert "Tarif: aniqlanmadi" in report
    assert "tekshirilmadi (kelgusi" in report


def test_sanitize_removes_private_fields_recursively(script: ModuleType) -> None:
    body = {"response": [{"account": {"email": "a@b.c"}, "requests": {"current": 1}}]}
    assert script.sanitize(body) == {"response": [{"requests": {"current": 1}}]}


def test_handicap_evidence_prefers_the_right_reading(script: ModuleType) -> None:
    item = OddsItem.model_validate(
        {
            "league": {"id": 39, "season": 2024},
            "fixture": {"id": 1},
            "bookmakers": [
                {
                    "id": 4,
                    "name": "Pinnacle",
                    "bets": [
                        {
                            "id": 4,
                            "name": "Asian Handicap",
                            "values": [
                                {"value": "Home -1", "odd": "2.60"},
                                {"value": "Away -1", "odd": "1.50"},
                                {"value": "Away +1", "odd": "4.00"},
                                {"value": "broken", "odd": "x"},
                            ],
                        },
                        {"id": 1, "name": "Match Winner", "values": []},
                    ],
                }
            ],
        }
    )
    evidence = script.handicap_evidence([item])
    assert evidence[HandicapConvention.HOME_LINE] == (1, 1)
    assert evidence[HandicapConvention.OWN_LINE] == (0, 1)


def test_recordings_are_valid_json(script: ModuleType, tmp_path: Path) -> None:
    from app.clients.api_football_models import Envelope  # noqa: PLC0415

    recorder = script.Recorder(tmp_path)
    recorder.save("fixtures/lineups", {}, Envelope.model_validate(load("lineups_empty.json")))
    saved = json.loads((tmp_path / "fixtures-lineups_all.json").read_text(encoding="utf-8"))
    assert saved["get"] == "fixtures/lineups"
