"""Heartbeat client: pings the right URL, retries briefly, never raises, never logs the URL."""

import httpx
import pytest
import respx
from pydantic import HttpUrl

from app.config import Settings
from app.logging import configure_logging
from app.ops.heartbeat import MAX_DETAIL_CHARS, Heartbeat, PingKind

URL = "https://hc-ping.com/11111111-2222-3333-4444-555555555555"


def heartbeat(url: str | None = URL, *, attempts: int = 3) -> Heartbeat:
    return Heartbeat(url, attempts=attempts, backoff=0.0)


@respx.mock
@pytest.mark.parametrize(
    ("kind", "suffix"),
    [(PingKind.SUCCESS, ""), (PingKind.START, "/start"), (PingKind.FAIL, "/fail")],
)
async def test_pings_the_endpoint_of_each_kind(kind: PingKind, suffix: str) -> None:
    route = respx.post(URL + suffix).respond(200, text="OK")

    assert await heartbeat().ping(kind, "settle ok") is True

    assert route.call_count == 1
    assert route.calls.last.request.content == b"settle"  # a short code, never free text


@respx.mock
async def test_trailing_slash_in_the_configured_url_is_tolerated() -> None:
    route = respx.post(URL + "/fail").respond(200)
    assert await heartbeat(URL + "/").ping(PingKind.FAIL) is True
    assert route.called


async def test_without_a_url_it_is_a_silent_noop() -> None:
    beat = heartbeat(None)
    assert beat.enabled is False
    assert await beat.ping() is False


@respx.mock
async def test_retries_a_server_error_then_succeeds() -> None:
    route = respx.post(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200)])

    assert await heartbeat().ping() is True
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_the_attempts_without_raising() -> None:
    route = respx.post(URL).respond(500)

    assert await heartbeat(attempts=3).ping() is False
    assert route.call_count == 3


@respx.mock
@pytest.mark.parametrize("error", [httpx.ConnectTimeout("t"), httpx.ConnectError("c")])
async def test_network_errors_never_propagate(error: Exception) -> None:
    respx.post(URL).mock(side_effect=error)
    assert await heartbeat(attempts=2).ping() is False


async def test_an_invalid_url_never_propagates() -> None:
    assert await heartbeat("http://[bad", attempts=1).ping() is False


@respx.mock
async def test_the_secret_url_never_reaches_the_logs(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="DEBUG", app_env="prod")
    respx.post(URL).mock(side_effect=httpx.ConnectError(f"cannot reach {URL}"))

    await heartbeat(attempts=2).ping()

    output = capsys.readouterr().out
    assert "heartbeat_failed" in output
    assert "11111111-2222" not in output


@respx.mock
async def test_long_detail_is_cut_to_a_short_code() -> None:
    route = respx.post(URL).respond(200)
    await heartbeat().ping(detail="x" * 5000)
    assert len(route.calls.last.request.content) == MAX_DETAIL_CHARS


@respx.mock
@pytest.mark.parametrize(
    ("detail", "sent"),
    [
        ("settle: ConnectError https://api.example/odds?key=SECRET123456", b"settle"),
        ("daily.digest failed: boom", b"daily.digest"),
        ("   ", b""),
        (None, b""),
    ],
)
async def test_internal_error_text_never_leaves_the_host(detail: str | None, sent: bytes) -> None:
    route = respx.post(URL + "/fail").respond(200)
    await heartbeat().ping(PingKind.FAIL, detail)
    assert route.calls.last.request.content == sent
    assert b"SECRET" not in route.calls.last.request.content


def test_from_settings_uses_healthchecks_url() -> None:
    enabled = Settings(_env_file=None, healthchecks_url=HttpUrl(URL))  # type: ignore[call-arg]
    assert Heartbeat.from_settings(enabled).enabled is True
    assert Heartbeat.from_settings(Settings(_env_file=None)).enabled is False  # type: ignore[call-arg]
