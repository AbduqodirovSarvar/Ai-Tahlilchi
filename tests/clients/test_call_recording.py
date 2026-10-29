"""Call-log hooks of the API-Football and Gemini clients (no network, no database)."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx
import pytest
import respx
from google import genai

from app.clients.gemini import GeminiClient, GeminiError
from app.clients.http import ProviderHttpError, ProviderUnavailableError
from app.clients.quota import QuotaExceededError, RequestPurpose
from app.db.call_log import SqlCallRecorder
from app.db.engine import create_engine, create_session_factory
from app.domain import CallOutcome, CallRecord, Provider
from tests.clients.conftest import (
    EMPTY_BODY,
    TEST_CONFIG,
    TEST_KEY,
    FakeTime,
    Harness,
    build_harness,
)
from tests.clients.test_gemini import Answer, FakeSdk, api_error, response

AUTO = RequestPurpose.AUTOMATIC
PARAMS = {"date": "2024-10-26", "league": 39}


@dataclass
class ListRecorder:
    calls: list[CallRecord] = field(default_factory=list)

    async def record(self, call: CallRecord) -> None:
        self.calls.append(call)


def harness_with(
    recorder: ListRecorder, fake_time: FakeTime, http: httpx.AsyncClient, **kwargs: int
) -> Harness:
    return build_harness(fake_time, http, recorder=recorder, **kwargs)


# ------------------------------------------------------------------ API-Football


async def test_a_sent_request_is_recorded_with_params_body_status_and_cost(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    recorder = ListRecorder()
    harness = harness_with(recorder, fake_time, http_client)
    body = EMPTY_BODY
    router.get("/fixtures").respond(json=body)

    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)

    (call,) = recorder.calls
    assert call.provider is TEST_CONFIG.log_provider
    assert call.operation == "fixtures"
    assert call.outcome is CallOutcome.OK
    assert json.loads(call.request) == PARAMS
    assert call.response is not None
    assert json.loads(call.response) == body
    assert call.http_status == 200
    assert call.quota_cost == 1
    assert call.size_bytes == len(call.response.encode())
    assert call.duration_ms >= 0
    assert call.at == fake_time.utc()
    assert call.error is None


async def test_the_api_key_is_never_part_of_a_record(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    recorder = ListRecorder()
    harness = harness_with(recorder, fake_time, http_client)
    router.get("/fixtures").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=EMPTY_BODY)]
    )

    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)

    assert recorder.calls
    for call in recorder.calls:
        assert TEST_KEY not in repr(call)


async def test_a_cache_answer_is_recorded_as_cached_and_free(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    recorder = ListRecorder()
    harness = harness_with(recorder, fake_time, http_client)
    router.get("/fixtures").respond(json=EMPTY_BODY)

    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)

    assert [c.outcome for c in recorder.calls] == [CallOutcome.OK, CallOutcome.CACHED]
    cached = recorder.calls[1]
    assert (cached.quota_cost, cached.response, cached.http_status) == (0, None, 200)
    assert json.loads(cached.request) == PARAMS


async def test_a_refused_request_is_recorded_as_quota_blocked(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    recorder = ListRecorder()
    harness = harness_with(recorder, fake_time, http_client, daily_limit=1)
    route = router.get("/fixtures").respond(json=EMPTY_BODY)
    await harness.api.get_json("fixtures", {"date": "2024-10-25"}, purpose=AUTO)

    with pytest.raises(QuotaExceededError):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)

    assert route.call_count == 1
    blocked = recorder.calls[-1]
    assert blocked.outcome is CallOutcome.QUOTA_BLOCKED
    assert (blocked.quota_cost, blocked.response, blocked.http_status) == (0, None, None)
    assert blocked.error is not None
    assert "left today" in blocked.error


async def test_every_attempt_of_a_retried_request_is_recorded_and_charged(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    recorder = ListRecorder()
    harness = harness_with(recorder, fake_time, http_client)
    router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(429, text="slow down"),
            httpx.Response(503),
            httpx.Response(200, json=EMPTY_BODY),
        ]
    )

    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)

    assert [c.outcome for c in recorder.calls] == [
        CallOutcome.ERROR,
        CallOutcome.ERROR,
        CallOutcome.OK,
    ]
    assert [c.http_status for c in recorder.calls] == [429, 503, 200]
    assert [c.quota_cost for c in recorder.calls] == [1, 1, 1]
    assert recorder.calls[0].error == "HTTP 429"
    assert recorder.calls[0].response == "slow down"


async def test_a_client_error_is_recorded_with_its_status_and_the_call_still_raises(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    recorder = ListRecorder()
    harness = harness_with(recorder, fake_time, http_client)
    router.get("/fixtures").respond(404, text="not here")

    with pytest.raises(ProviderHttpError):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)

    (call,) = recorder.calls
    assert (call.outcome, call.http_status, call.response) == (
        CallOutcome.ERROR,
        404,
        "not here",
    )
    assert call.error is not None
    assert "HTTP 404" in call.error


async def test_a_body_that_is_not_json_is_recorded_as_an_error(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    recorder = ListRecorder()
    harness = harness_with(recorder, fake_time, http_client)
    router.get("/fixtures").respond(200, text="<html>firewall</html>")

    with pytest.raises(ProviderHttpError):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)

    (call,) = recorder.calls
    assert call.outcome is CallOutcome.ERROR
    assert call.response == "<html>firewall</html>"
    assert call.error is not None
    assert "not JSON" in call.error


async def test_timeouts_and_transport_errors_are_recorded_without_a_status(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    recorder = ListRecorder()
    harness = harness_with(recorder, fake_time, http_client)
    router.get("/fixtures").mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.ConnectError("refused"),
            httpx.Response(200, json=EMPTY_BODY),
        ]
    )

    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)

    first, second, third = recorder.calls
    assert (first.outcome, first.http_status, first.response) == (CallOutcome.ERROR, None, None)
    assert first.error == "timeout: ReadTimeout"
    assert second.error == "transport error: ConnectError"
    assert third.outcome is CallOutcome.OK


async def test_exhausted_retries_leave_one_error_per_attempt(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    recorder = ListRecorder()
    harness = harness_with(recorder, fake_time, http_client)
    router.get("/fixtures").respond(502)

    with pytest.raises(ProviderUnavailableError):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)

    assert len(recorder.calls) == harness.api.config.max_attempts
    assert {c.outcome for c in recorder.calls} == {CallOutcome.ERROR}


async def test_a_broken_call_log_database_does_not_break_the_provider_call(
    fake_time: FakeTime,
    http_client: httpx.AsyncClient,
    router: respx.MockRouter,
    tmp_path: Path,
) -> None:
    engine = create_engine(tmp_path / "empty.db")  # no schema: every write fails
    recorder = SqlCallRecorder(create_session_factory(engine), scrub=lambda text: text)
    harness = build_harness(fake_time, http_client, recorder=recorder)
    router.get("/fixtures").respond(json=EMPTY_BODY)

    result = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    await engine.dispose()

    assert result.status_code == 200
    assert (await harness.quota.status()).counts.requests_used == 1


async def test_without_a_recorder_nothing_changes(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=EMPTY_BODY)

    result = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)

    assert result.status_code == 200


# ------------------------------------------------------------------ Gemini


def gemini_with(sdk: FakeSdk, recorder: ListRecorder) -> GeminiClient:
    async def no_sleep(_seconds: float) -> None:
        return None

    return GeminiClient(cast(genai.Client, sdk), "gemini-test", sleep=no_sleep, recorder=recorder)


async def ask(client: GeminiClient) -> object:
    return await client.generate_json(
        system_instruction="be brief", contents="the user data", response_schema=Answer
    )


async def test_a_gemini_call_records_the_whole_prompt_and_the_answer() -> None:
    recorder = ListRecorder()
    client = gemini_with(FakeSdk(response(text='{"value": 7}')), recorder)

    await ask(client)

    (call,) = recorder.calls
    assert call.provider is Provider.GEMINI
    assert call.operation == "Answer"  # the answer schema names the task
    assert call.outcome is CallOutcome.OK
    assert "be brief" in call.request
    assert "the user data" in call.request
    assert call.response == '{"value": 7}'
    assert (call.quota_cost, call.error, call.http_status) == (1, None, None)
    assert call.size_bytes == len(b'{"value": 7}')


async def test_retried_gemini_call_is_one_record_that_costs_every_attempt() -> None:
    recorder = ListRecorder()
    client = gemini_with(FakeSdk(api_error(429), api_error(503), response()), recorder)

    await ask(client)

    (call,) = recorder.calls
    assert (call.outcome, call.quota_cost) == (CallOutcome.OK, 3)


async def test_gemini_errors_are_recorded_and_still_raised() -> None:
    recorder = ListRecorder()
    client = gemini_with(FakeSdk(api_error(400)), recorder)

    with pytest.raises(GeminiError):
        await ask(client)

    (call,) = recorder.calls
    assert call.outcome is CallOutcome.ERROR
    assert call.response is None
    assert call.quota_cost == 1
    assert call.error is not None
    assert "ClientError" in call.error
    assert "the user data" in call.request


async def test_gemini_empty_answer_and_timeout_are_errors() -> None:
    recorder = ListRecorder()
    empty = gemini_with(FakeSdk(response(text="  ")), recorder)
    with pytest.raises(GeminiError):
        await ask(empty)
    timing_out = gemini_with(FakeSdk(TimeoutError(), TimeoutError(), TimeoutError()), recorder)
    with pytest.raises(GeminiError):
        await ask(timing_out)

    assert [c.outcome for c in recorder.calls] == [CallOutcome.ERROR, CallOutcome.ERROR]
    assert recorder.calls[1].quota_cost == 3
    assert recorder.calls[1].error is not None
    assert "TimeoutError" in recorder.calls[1].error


async def test_gemini_without_a_recorder_is_unchanged() -> None:
    client = GeminiClient(cast(genai.Client, FakeSdk(response())), "gemini-test")

    result = await ask(client)

    assert getattr(result, "text", None) == '{"value": 1}'
