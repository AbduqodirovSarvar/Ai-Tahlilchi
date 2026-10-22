"""GeminiClient failure classification: structural transient rule, wrapping, attempts, cost."""

import asyncio

import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel

from app.clients.gemini import GeminiClient, GeminiConfig, GeminiError, is_transient
from app.llm.schemas import ReviewResponse

MODEL = "gemini-test"
FAST = GeminiConfig(max_attempts=3, backoff_initial_seconds=0.0, backoff_max_seconds=0.0)


async def no_sleep(_: float) -> None:
    return None


class FakeSdk:
    """Minimal stand-in for ``genai.Client`` whose ``generate_content`` plays a script."""

    def __init__(self, *script: BaseException | genai_types.GenerateContentResponse) -> None:
        self.aio = self
        self.models = self
        self.script = list(script)
        self.calls = 0

    async def generate_content(self, **_: object) -> genai_types.GenerateContentResponse:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def client_for(sdk: FakeSdk) -> GeminiClient:
    return GeminiClient(sdk, MODEL, FAST, sleep=no_sleep)  # type: ignore[arg-type]


async def ask(client: GeminiClient, schema: type[BaseModel] = ReviewResponse) -> object:
    return await client.generate_json(system_instruction="s", contents="c", response_schema=schema)


def foreign(module: str, name: str, base: type[BaseException], **attrs: object) -> BaseException:
    """An exception of a class that claims to live in another package (aiohttp, ...)."""
    cls = type(name, (base,), {"__module__": module, **attrs})
    return cls("boom")


def ok_response() -> genai_types.GenerateContentResponse:
    return genai_types.GenerateContentResponse.model_validate(
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "{}"}]}}]}
    )


def test_transient_classification_is_structural() -> None:
    assert is_transient(ConnectionResetError())
    assert is_transient(OSError("network unreachable"))
    assert is_transient(foreign("aiohttp.client_exceptions", "ClientConnectionError", Exception))
    assert is_transient(foreign("httpcore", "ReadError", Exception))
    assert is_transient(foreign("aiohttp.client_exceptions", "Resp", Exception, status=503))
    assert not is_transient(foreign("aiohttp.client_exceptions", "Resp", Exception, status=404))
    assert not is_transient(foreign("aiohttp.client_exceptions", "InvalidURL", ValueError))
    assert not is_transient(foreign("somewhere.else", "ClientError", Exception))
    assert not is_transient(ValueError("bad request shape"))


@pytest.mark.parametrize("code", [None, 0])
def test_api_error_without_usable_code_is_not_transient(code: int | None) -> None:
    error = genai_errors.APIError(0, {})
    error.code = code  # type: ignore[assignment]  # the SDK can leave it unset

    assert is_transient(error) is False


async def test_aiohttp_style_error_is_retried_then_succeeds() -> None:
    boom = foreign("aiohttp.client_exceptions", "ServerDisconnectedError", Exception)
    sdk = FakeSdk(boom, boom, ok_response())

    result = await ask(client_for(sdk))

    assert sdk.calls == 3
    assert result.attempts == 3  # type: ignore[attr-defined]


async def test_unknown_exception_is_wrapped_not_leaked_and_not_retried() -> None:
    sdk = FakeSdk(RuntimeError("sdk bug"))

    with pytest.raises(GeminiError) as caught:
        await ask(client_for(sdk))

    assert sdk.calls == 1
    assert caught.value.attempts == 1
    assert isinstance(caught.value.__cause__, RuntimeError)


async def test_cancellation_is_never_swallowed() -> None:
    with pytest.raises(asyncio.CancelledError):
        await ask(client_for(FakeSdk(asyncio.CancelledError())))


async def test_exhausted_retries_report_the_attempts_made() -> None:
    boom = ConnectionResetError("reset")

    with pytest.raises(GeminiError) as caught:
        await ask(client_for(FakeSdk(boom, boom, boom)))

    assert caught.value.attempts == FAST.max_attempts


async def test_empty_answer_still_reports_the_tokens_it_cost() -> None:
    blocked = genai_types.GenerateContentResponse.model_validate(
        {"usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 0}}
    )

    with pytest.raises(GeminiError, match="empty") as caught:
        await ask(client_for(FakeSdk(blocked)))

    assert caught.value.attempts == 1
    assert caught.value.usage is not None
    assert caught.value.usage.input_tokens == 40
