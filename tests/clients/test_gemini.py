"""GeminiClient against a fake SDK: structured request, retry policy, usage, secrecy."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import structlog
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel

from app.clients.gemini import (
    GeminiClient,
    GeminiConfig,
    GeminiError,
    GeminiResponse,
    create_gemini_client,
    is_transient,
)
from app.config import Settings
from app.errors import ConfigurationError

SECRET = "AIza-super-secret-key-value"
DEFAULT_CONFIG = GeminiConfig()


class Answer(BaseModel):
    value: int


def api_error(code: int) -> genai_errors.APIError:
    cls = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return cls(code, {"error": {"message": "boom", "status": "X"}})


def response(
    text: str | None = '{"value": 1}', prompt: int = 11, out: int = 7, thoughts: int | None = None
) -> Any:
    usage = SimpleNamespace(
        prompt_token_count=prompt, candidates_token_count=out, thoughts_token_count=thoughts
    )
    return SimpleNamespace(text=text, usage_metadata=usage)


class FakeSdk:
    """Plays a script of results (responses or exceptions) and records the calls."""

    def __init__(self, *script: object, hang: bool = False) -> None:
        self.script = list(script)
        self.hang = hang
        self.calls: list[dict[str, Any]] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.hang:
            await asyncio.sleep(30)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_client(
    sdk: FakeSdk, config: GeminiConfig = DEFAULT_CONFIG
) -> tuple[GeminiClient, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return GeminiClient(cast(genai.Client, sdk), "gemini-test", config, sleep=fake_sleep), sleeps


async def ask(client: GeminiClient) -> GeminiResponse:
    return await client.generate_json(
        system_instruction="sys", contents="user data", response_schema=Answer
    )


async def test_request_is_structured_and_usage_is_reported() -> None:
    sdk = FakeSdk(response(prompt=100, out=20, thoughts=5))
    client, _ = make_client(sdk)

    result = await ask(client)

    assert result.text == '{"value": 1}'
    assert (result.usage.input_tokens, result.usage.output_tokens) == (100, 25)
    assert result.usage.total_tokens == 125
    assert result.attempts == 1
    call = sdk.calls[0]
    assert call["model"] == "gemini-test"
    assert call["contents"] == "user data"
    config = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is Answer
    assert config.temperature == pytest.approx(0.2)
    assert config.system_instruction == "sys"
    assert config.http_options.timeout == 30_000


async def test_429_then_success_retries_with_backoff() -> None:
    sdk = FakeSdk(api_error(429), api_error(503), response())
    client, sleeps = make_client(sdk)

    result = await ask(client)

    assert result.attempts == 3
    assert len(sdk.calls) == 3
    assert len(sleeps) == 2


async def test_retries_are_bounded() -> None:
    sdk = FakeSdk(api_error(500), api_error(500), api_error(500), response())
    client, _ = make_client(sdk)

    with pytest.raises(GeminiError, match="3 attempt"):
        await ask(client)
    assert len(sdk.calls) == 3


@pytest.mark.parametrize("code", [400, 401, 403, 404])
async def test_permanent_errors_fail_fast(code: int) -> None:
    sdk = FakeSdk(api_error(code), response())
    client, sleeps = make_client(sdk)

    with pytest.raises(GeminiError):
        await ask(client)
    assert len(sdk.calls) == 1
    assert sleeps == []


async def test_transport_error_is_retried() -> None:
    sdk = FakeSdk(httpx.ConnectError("down"), response())
    client, _ = make_client(sdk)

    assert (await ask(client)).attempts == 2


async def test_hung_request_times_out_and_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.clients.gemini._ASYNCIO_TIMEOUT_MARGIN_SECONDS", 0.0)
    sdk = FakeSdk(hang=True)
    config = GeminiConfig(timeout_seconds=0.01, max_attempts=2, backoff_initial_seconds=0.0)
    client, _ = make_client(sdk, config)

    with pytest.raises(GeminiError, match="TimeoutError"):
        await ask(client)
    assert len(sdk.calls) == 2


@pytest.mark.parametrize("text", [None, "", "   "])
async def test_empty_answer_is_an_error(text: str | None) -> None:
    client, _ = make_client(FakeSdk(response(text=text)))

    with pytest.raises(GeminiError, match="empty"):
        await ask(client)


async def test_missing_usage_metadata_counts_as_zero() -> None:
    client, _ = make_client(FakeSdk(SimpleNamespace(text="{}", usage_metadata=None)))

    assert (await ask(client)).usage.total_tokens == 0


def test_is_transient_classification() -> None:
    assert is_transient(api_error(429))
    assert is_transient(api_error(408))
    assert is_transient(api_error(502))
    assert not is_transient(api_error(400))
    assert is_transient(TimeoutError())
    assert is_transient(httpx.ReadTimeout("slow"))
    assert not is_transient(ValueError("x"))


async def test_logs_metrics_but_not_the_prompt() -> None:
    client, _ = make_client(FakeSdk(response(prompt=42, out=3)))

    with structlog.testing.capture_logs() as logs:
        await ask(client)

    event = next(entry for entry in logs if entry["event"] == "gemini_call")
    assert event["input_tokens"] == 42
    assert event["output_tokens"] == 3
    assert "user data" not in str(logs)


async def test_failure_log_has_no_prompt() -> None:
    client, _ = make_client(FakeSdk(api_error(400)))

    with structlog.testing.capture_logs() as logs, pytest.raises(GeminiError):
        await ask(client)

    assert any(entry["event"] == "gemini_call_failed" for entry in logs)
    assert "user data" not in str(logs)


def test_create_client_requires_key() -> None:
    with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
        create_gemini_client(Settings(_env_file=None))


def test_create_client_uses_configured_model() -> None:
    settings = Settings.model_validate(
        {"gemini_api_key": SECRET, "gemini_model": "gemini-x"}, strict=False
    )

    client = create_gemini_client(settings)

    assert client.model == "gemini-x"
