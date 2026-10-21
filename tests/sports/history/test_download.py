"""Downloading and caching season files (app.sports.history.download); no real network."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from tenacity import wait_none

from app.errors import ExternalServiceError
from app.sports.history import download
from app.sports.history.download import (
    CURRENT_SEASON_MAX_AGE,
    MAX_ATTEMPTS,
    cache_path,
    create_client,
    current_season,
    download_season,
    looks_like_csv,
    needs_download,
    season_end,
    season_url,
    write_atomically,
)

CSV = b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\nE0,16/08/2024,A,B,1,0\n"
URL = "https://www.football-data.co.uk/mmz4281/2425/E0.csv"
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "RETRY_WAIT", wait_none())


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with create_client() as http_client:
        yield http_client


def test_urls_and_cache_layout(tmp_path: Path) -> None:
    assert season_url("E0", 2024) == URL
    assert cache_path(tmp_path, "E0", 2024) == tmp_path / "2425" / "E0.csv"


@pytest.mark.parametrize(
    ("today", "season"),
    [(date(2026, 10, 21), 2026), (date(2026, 6, 30), 2025), (date(2026, 7, 1), 2026)],
)
def test_current_season(today: date, season: int) -> None:
    assert current_season(today) == season


def touch(path: Path, modified: datetime) -> Path:
    write_atomically(path, CSV)
    os.utime(path, (modified.timestamp(), modified.timestamp()))
    return path


def test_missing_file_needs_download(tmp_path: Path) -> None:
    assert needs_download(tmp_path / "x.csv", season=2020, now=NOW)


def test_file_saved_after_its_season_ended_is_cached_forever(tmp_path: Path) -> None:
    path = touch(tmp_path / "old.csv", season_end(2024) + timedelta(days=1))
    assert not needs_download(path, season=2024, now=NOW)


def test_file_cached_mid_season_is_fetched_again_after_the_season(tmp_path: Path) -> None:
    # Saved on 1 March 2025 while 2024/25 was running: it lacks the last results.
    path = touch(tmp_path / "partial.csv", datetime(2025, 3, 1, tzinfo=UTC))
    assert needs_download(path, season=2024, now=NOW)
    assert season_end(2024) == datetime(2025, 7, 1, tzinfo=UTC)


def test_current_season_is_refreshed_when_stale(tmp_path: Path) -> None:
    fresh = touch(tmp_path / "fresh.csv", NOW - timedelta(hours=1))
    stale = touch(tmp_path / "stale.csv", NOW - CURRENT_SEASON_MAX_AGE - timedelta(minutes=1))
    assert not needs_download(fresh, season=2026, now=NOW)
    assert needs_download(stale, season=2026, now=NOW)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (CSV, True),
        (b"\xef\xbb\xbf" + CSV, True),
        (b"<!DOCTYPE html><html>Web Page Blocked</html>", False),
        (b"", False),
        (b"just text without commas\n", False),
    ],
)
def test_looks_like_csv(content: bytes, expected: bool) -> None:
    assert looks_like_csv(content) is expected


@respx.mock
async def test_download_writes_the_file(client: httpx.AsyncClient, tmp_path: Path) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=CSV))
    destination = tmp_path / "2425" / "E0.csv"
    assert await download_season(client, "E0", 2024, destination) is True
    assert destination.read_bytes() == CSV
    assert route.calls.last.request.headers["User-Agent"].startswith("ai-tahlilchi")
    assert not list(tmp_path.rglob("*.partial"))  # noqa: ASYNC240 - tiny test dir


@respx.mock
async def test_unpublished_season_returns_false(client: httpx.AsyncClient, tmp_path: Path) -> None:
    respx.get(URL).mock(return_value=httpx.Response(404))
    assert await download_season(client, "E0", 2024, tmp_path / "E0.csv") is False
    assert not (tmp_path / "E0.csv").exists()


@respx.mock
async def test_block_page_is_not_cached(client: httpx.AsyncClient, tmp_path: Path) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=b"<!DOCTYPE html><html>Gambling</html>")
    )
    with pytest.raises(ExternalServiceError, match="did not return a CSV"):
        await download_season(client, "E0", 2024, tmp_path / "E0.csv")
    assert not (tmp_path / "E0.csv").exists()


@respx.mock
async def test_client_error_is_reported(client: httpx.AsyncClient, tmp_path: Path) -> None:
    respx.get(URL).mock(return_value=httpx.Response(403))
    with pytest.raises(ExternalServiceError, match="HTTP 403"):
        await download_season(client, "E0", 2024, tmp_path / "E0.csv")


@respx.mock
async def test_server_errors_are_retried_then_succeed(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectTimeout("slow"),
            httpx.Response(200, content=CSV),
        ]
    )
    assert await download_season(client, "E0", 2024, tmp_path / "E0.csv") is True
    assert route.call_count == 3


@respx.mock
async def test_persistent_server_error_gives_up(client: httpx.AsyncClient, tmp_path: Path) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(502))
    with pytest.raises(ExternalServiceError, match="HTTP 502"):
        await download_season(client, "E0", 2024, tmp_path / "E0.csv")
    assert route.call_count == MAX_ATTEMPTS


@respx.mock
async def test_network_failure_mentions_proxy_hint(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("certificate verify failed"))
    with pytest.raises(ExternalServiceError, match="SSL_CERT_FILE"):
        await download_season(client, "E0", 2024, tmp_path / "E0.csv")


def test_write_atomically_replaces_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "E0.csv"
    write_atomically(path, b"old")
    write_atomically(path, b"new")
    assert path.read_bytes() == b"new"
