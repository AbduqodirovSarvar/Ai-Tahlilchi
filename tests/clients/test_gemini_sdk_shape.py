"""GeminiClient against the REAL ``google-genai`` SDK with a faked HTTP transport.

``test_gemini.py`` uses a hand-made fake SDK, so it cannot notice that a call would be
rejected by the installed SDK. Here the real ``genai.Client`` builds the request and parses
the response; only the wire (``httpx.MockTransport``) is replaced. No network is touched.
"""

import json
from collections.abc import Callable

import httpx
import pytest
import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel

from app.clients.gemini import GeminiClient, GeminiConfig, GeminiError, is_transient
from app.llm.schemas import PostmortemOutput, ReviewResponse

KEY = "AIza-conformance-secret-key-0123456789"
MODEL = "gemini-flash-latest"
FAST = GeminiConfig(max_attempts=3, backoff_initial_seconds=0.0, backoff_max_seconds=0.0)

type Handler = Callable[[httpx.Request], httpx.Response]


def answer(
    text: str = '{"reviews": []}', usage: dict[str, int] | None = None, *, with_usage: bool = True
) -> httpx.Response:
    body: dict[str, object] = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
        ]
    }
    if with_usage:
        body["usageMetadata"] = usage or {
            "promptTokenCount": 11,
            "candidatesTokenCount": 5,
            "thoughtsTokenCount": 3,
            "totalTokenCount": 19,
        }
    return httpx.Response(200, json=body)


def error(code: int, status: str = "RESOURCE_EXHAUSTED") -> httpx.Response:
    return httpx.Response(code, json={"error": {"code": code, "message": "nope", "status": status}})


async def no_sleep(_: float) -> None:
    return None


def make_client(handler: Handler) -> GeminiClient:
    sdk = genai.Client(
        api_key=KEY,
        http_options=genai_types.HttpOptions(
            async_client_args={"transport": httpx.MockTransport(handler)}
        ),
    )
    return GeminiClient(sdk, MODEL, FAST, sleep=no_sleep)


async def ask(client: GeminiClient, schema: type[BaseModel] = ReviewResponse) -> object:
    return await client.generate_json(
        system_instruction="SYSTEM RULES", contents="USER DATA", response_schema=schema
    )


async def test_real_sdk_builds_the_structured_request_we_intend() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer()

    result = await make_client(handler).generate_json(
        system_instruction="SYSTEM RULES", contents="USER DATA", response_schema=ReviewResponse
    )

    (request,) = seen
    body = json.loads(request.content)
    config = body["generationConfig"]
    assert request.url.path.endswith(f"models/{MODEL}:generateContent")
    assert config["responseMimeType"] == "application/json"
    assert config["temperature"] == 0.2
    assert body["systemInstruction"]["parts"][0]["text"] == "SYSTEM RULES"
    assert body["contents"][0]["parts"][0]["text"] == "USER DATA"
    item = config["responseSchema"]["properties"]["reviews"]["items"]
    assert set(item["properties"]) == {"candidate_id", "verdict", "risk_flags", "izoh"}
    assert item["properties"]["verdict"]["enum"] == ["tasdiq", "ehtiyot", "rad"]
    assert len(item["properties"]["risk_flags"]["items"]["enum"]) == 8
    # Thinking tokens are billed as output.
    assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 8)  # type: ignore[attr-defined]


async def test_postmortem_schema_is_accepted_by_the_sdk_and_has_only_enum_and_text() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return answer('{"category": "omadsizlik", "reason": "x"}')

    await make_client(handler).generate_json(
        system_instruction="s", contents="c", response_schema=PostmortemOutput
    )

    schema = seen[0]["generationConfig"]["responseSchema"]  # type: ignore[index]
    assert set(schema["properties"]) == {"category", "reason"}


async def test_key_travels_in_a_header_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer()

    await ask(make_client(handler))

    request = seen[0]
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)
    assert KEY.encode() not in request.content


async def test_429_is_retried_through_the_real_sdk_error_classes() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return error(429) if len(calls) < 3 else answer()

    result = await ask(make_client(handler))

    assert len(calls) == 3
    assert result.attempts == 3  # type: ignore[attr-defined]


@pytest.mark.parametrize("code", [400, 401, 403, 404])
async def test_permanent_http_errors_fail_fast_without_leaking_the_key(code: int) -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return error(code, "INVALID_ARGUMENT")

    with structlog.testing.capture_logs() as logs, pytest.raises(GeminiError) as caught:
        await ask(make_client(handler))

    assert len(calls) == 1
    assert KEY not in str(caught.value)
    assert KEY not in str(logs)
    assert "USER DATA" not in str(logs)


async def test_server_errors_exhaust_the_attempts_then_raise_gemini_error() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return error(503, "UNAVAILABLE")

    with pytest.raises(GeminiError):
        await ask(make_client(handler))

    assert len(calls) == FAST.max_attempts


async def test_blocked_prompt_without_candidates_is_an_empty_answer_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(GeminiError, match="empty"):
        await ask(make_client(handler))


async def test_missing_usage_metadata_gives_zero_tokens_not_a_crash() -> None:
    result = await ask(make_client(lambda _: answer(with_usage=False)))

    assert result.usage.total_tokens == 0  # type: ignore[attr-defined]


def test_sdk_exposes_every_usage_field_the_client_reads() -> None:
    fields = genai_types.GenerateContentResponseUsageMetadata.model_fields
    assert {"prompt_token_count", "candidates_token_count", "thoughts_token_count"} <= set(fields)
    assert "timeout" in genai_types.HttpOptions.model_fields


@pytest.mark.xfail(
    strict=True,
    reason="QA T4-llm F-1: production SDK transport is aiohttp; its ClientError family is "
    "neither retried nor wrapped in GeminiError (gemini.py:100-111, 170)",
)
async def test_aiohttp_transport_error_is_wrapped_as_gemini_error() -> None:
    aiohttp = pytest.importorskip("aiohttp")

    class Boom:
        def __init__(self) -> None:
            self.aio = self
            self.models = self

        async def generate_content(self, **_: object) -> object:
            raise aiohttp.ClientConnectionError("reset by peer")

    client = GeminiClient(Boom(), MODEL, FAST, sleep=no_sleep)  # type: ignore[arg-type]

    with pytest.raises(GeminiError):
        await ask(client)


@pytest.mark.xfail(
    strict=True,
    reason="QA T4-llm F-2: APIError.code may be None; is_transient raises TypeError "
    "instead of classifying (gemini.py:109)",
)
def test_is_transient_survives_an_api_error_without_status_code() -> None:
    assert is_transient(genai_errors.APIError(0, {})) is False
