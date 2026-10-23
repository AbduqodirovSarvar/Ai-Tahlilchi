"""Heartbeat client: pings the right URL, retries briefly, never raises, never logs the URL."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.logging import configure_logging
from app.ops.heartbeat import Heartbeat, PingKind

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
    assert route.calls.last.request.content == b"settle ok"


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
@pytest.mark.parametrize(
    "detail",
    [
        "x" * 5000,
        "settle: OperationalError: no such table: bets /data/app.db",
        "line one\nline two",
        "päss",
    ],
)
async def test_free_form_or_long_detail_is_withheld(detail: str) -> None:
    route = respx.post(URL).respond(200)
    await heartbeat().ping(detail=detail)
    assert route.calls.last.request.content == b"detail_withheld"


@respx.mock
async def test_short_codes_are_sent_as_they_are() -> None:
    route = respx.post(URL + "/fail").respond(200)
    await heartbeat().ping(PingKind.FAIL, "refresh: ApiError")
    assert route.calls.last.request.content == b"refresh: ApiError"


def test_from_settings_uses_healthchecks_url() -> None:
    enabled = Settings(_env_file=None, healthchecks_url=SecretStr(URL))  # type: ignore[call-arg]
    assert Heartbeat.from_settings(enabled).enabled is True
    assert Heartbeat.from_settings(Settings(_env_file=None)).enabled is False  # type: ignore[call-arg]


class _Recorder(BaseHTTPRequestHandler):
    seen: ClassVar[list[str]] = []

    def do_POST(self) -> None:
        self.seen.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        """Keep the test server silent."""


async def test_a_real_ping_never_logs_the_url_path_in_prod_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The httpx INFO line `HTTP Request: POST <url>` would leak the check's credential."""
    secret_path = "/aaaa1111-bbbb-2222-cccc-333333333333"
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}{secret_path}"
    try:
        settings = Settings(_env_file=None, healthchecks_url=SecretStr(url))  # type: ignore[call-arg]
        configure_logging(level="DEBUG", app_env="prod", secret_values=settings.secret_values())
        assert await Heartbeat.from_settings(settings).ping(PingKind.SUCCESS, "settle ok") is True
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        configure_logging(level="INFO", app_env="dev")

    output = capsys.readouterr().out
    assert _Recorder.seen == [secret_path]
    assert "heartbeat_sent" in output
    assert "aaaa1111" not in output


def test_the_ping_url_is_a_secret_value() -> None:
    settings = Settings(_env_file=None, healthchecks_url=SecretStr(URL))  # type: ignore[call-arg]
    assert URL in settings.secret_values()
    assert URL not in repr(settings)
