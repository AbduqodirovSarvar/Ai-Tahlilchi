"""Thin async wrapper over the ``google-genai`` SDK for schema-constrained JSON answers.

The wrapper owns everything transport-related so callers see one method,
:meth:`GeminiClient.generate_json`:

* structured output: ``response_mime_type=application/json`` + ``response_schema`` (the
  answer is never regex-parsed; the caller validates it against the same schema);
* low ``temperature`` and a hard per-request timeout (SDK level and ``asyncio`` level);
* retry with jittered exponential backoff on *transient* failures only (HTTP 429, 5xx,
  timeouts, transport errors); anything else fails fast;
* token usage of every successful call is returned and logged as metrics. The API key and
  the prompt text are never logged (only sizes).
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from time import monotonic
from typing import Final, Protocol

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import Settings
from app.errors import ExternalServiceError
from app.logging import get_logger

logger = get_logger(__name__)

_MS_PER_SECOND: Final = 1000
# The SDK enforces its own timeout; asyncio backs it up for a hung connection.
_ASYNCIO_TIMEOUT_MARGIN_SECONDS: Final = 1.0

type Sleep = Callable[[float], Awaitable[None]]


class GeminiError(ExternalServiceError):
    """A Gemini call failed for good (non-transient error, or transient and out of attempts)."""


@dataclass(frozen=True, slots=True)
class GeminiConfig:
    """Request and retry tuning; the defaults suit a small batched review call."""

    temperature: float = 0.2
    timeout_seconds: float = 30.0
    max_attempts: int = 3
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 20.0


@dataclass(frozen=True, slots=True)
class GeminiUsage:
    """Token counts the API reported for one successful call."""

    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class GeminiResponse:
    """Raw JSON text of the answer, the model that produced it and the tokens it cost."""

    text: str
    model: str
    usage: GeminiUsage
    attempts: int


class JsonGenerator(Protocol):
    """What the LLM service needs from a model backend (lets tests inject a fake)."""

    @property
    def model(self) -> str:
        """Model name recorded with every result."""
        ...

    async def generate_json(
        self, *, system_instruction: str, contents: str, response_schema: type[BaseModel]
    ) -> GeminiResponse:
        """Return the model's JSON answer constrained to ``response_schema``."""
        ...


def is_transient(error: BaseException) -> bool:
    """Whether retrying the same request can plausibly succeed."""
    if isinstance(error, genai_errors.APIError):
        return (
            error.code
            in {
                HTTPStatus.TOO_MANY_REQUESTS,
                HTTPStatus.REQUEST_TIMEOUT,
            }
            or error.code >= HTTPStatus.INTERNAL_SERVER_ERROR
        )
    return isinstance(error, TimeoutError | httpx.TransportError)


class GeminiClient:
    """Async client for one model; safe to share between tasks."""

    def __init__(
        self,
        sdk_client: genai.Client,
        model: str,
        config: GeminiConfig = GeminiConfig(),  # noqa: B008 - frozen, shared default is safe
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Wrap an already configured SDK client (the key lives only inside it)."""
        self._sdk = sdk_client
        self._model = model
        self._config = config
        self._sleep = sleep

    @property
    def model(self) -> str:
        """Model name used for every request."""
        return self._model

    async def generate_json(
        self, *, system_instruction: str, contents: str, response_schema: type[BaseModel]
    ) -> GeminiResponse:
        """Ask for a JSON answer matching ``response_schema``; retry transient failures.

        Raises:
            GeminiError: non-transient API error, retries exhausted, or an empty answer.
        """
        cfg = self._config
        request_config = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=cfg.temperature,
            response_mime_type="application/json",
            response_schema=response_schema,
            http_options=genai_types.HttpOptions(timeout=int(cfg.timeout_seconds * _MS_PER_SECOND)),
        )
        retrying = AsyncRetrying(
            stop=stop_after_attempt(cfg.max_attempts),
            wait=wait_exponential_jitter(
                initial=cfg.backoff_initial_seconds,
                max=cfg.backoff_max_seconds,
                jitter=cfg.backoff_initial_seconds,
            ),
            retry=retry_if_exception(is_transient),
            sleep=self._sleep,
            reraise=True,
        )
        started = monotonic()
        attempts = 0
        try:
            async for attempt in retrying:
                with attempt:
                    attempts += 1
                    response = await self._call(contents, request_config, attempts)
        except (genai_errors.APIError, TimeoutError, httpx.TransportError) as exc:
            logger.warning(
                "gemini_call_failed",
                model=self._model,
                attempts=attempts,
                error_type=type(exc).__name__,
                status=getattr(exc, "code", None),
            )
            msg = f"Gemini request failed after {attempts} attempt(s): {type(exc).__name__}"
            raise GeminiError(msg) from exc
        return self._finish(response, attempts, monotonic() - started)

    async def _call(
        self, contents: str, config: genai_types.GenerateContentConfig, attempt: int
    ) -> genai_types.GenerateContentResponse:
        """One request; the outer ``asyncio`` timeout backs up the SDK's own."""
        logger.debug("gemini_request", model=self._model, attempt=attempt, chars=len(contents))
        timeout = self._config.timeout_seconds + _ASYNCIO_TIMEOUT_MARGIN_SECONDS
        async with asyncio.timeout(timeout):
            return await self._sdk.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )

    def _finish(
        self, response: genai_types.GenerateContentResponse, attempts: int, seconds: float
    ) -> GeminiResponse:
        """Extract text and usage; an empty answer is a failure, not a valid result."""
        text = response.text
        if text is None or not text.strip():
            msg = "Gemini returned an empty answer"
            raise GeminiError(msg)
        meta = response.usage_metadata
        usage = GeminiUsage(input_tokens=0, output_tokens=0)
        if meta is not None:
            usage = GeminiUsage(
                input_tokens=meta.prompt_token_count or 0,
                # Thinking tokens are billed as output, so they count against the budget.
                output_tokens=(meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0),
            )
        logger.info(
            "gemini_call",
            model=self._model,
            attempts=attempts,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=round(seconds * _MS_PER_SECOND),
        )
        return GeminiResponse(text=text, model=self._model, usage=usage, attempts=attempts)


def create_gemini_client(settings: Settings) -> GeminiClient:
    """Build the production client from settings.

    Raises:
        ConfigurationError: ``GEMINI_API_KEY`` is not set.
    """
    api_key = settings.require_secret("gemini_api_key")
    return GeminiClient(genai.Client(api_key=api_key), settings.gemini_model)


__all__ = [
    "GeminiClient",
    "GeminiConfig",
    "GeminiError",
    "GeminiResponse",
    "GeminiUsage",
    "JsonGenerator",
    "create_gemini_client",
    "is_transient",
]
