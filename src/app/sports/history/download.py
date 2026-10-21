"""Download and cache football-data.co.uk season files.

Complete seasons never change, so a cached file is reused forever; the current season is
re-downloaded when the cached copy is older than :data:`CURRENT_SEASON_MAX_AGE`. Files are
written atomically, and a response that is not a CSV (for example a proxy or web-filter
block page) is rejected instead of being cached.

TLS is verified with httpx's defaults (certifi, or ``SSL_CERT_FILE`` when set, e.g. for a
corporate proxy CA). Verification is never disabled.
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tenacity.wait import wait_base

from app.errors import ExternalServiceError
from app.logging import get_logger
from app.sports.history.football_data import season_code

logger = get_logger(__name__)

BASE_URL: Final = "https://www.football-data.co.uk/mmz4281"
CURRENT_SEASON_MAX_AGE: Final = timedelta(hours=12)
# European football seasons start in July/August; earlier months belong to the season
# that started the previous year.
SEASON_START_MONTH: Final = 7
REQUEST_TIMEOUT_SECONDS: Final = 30.0
MAX_ATTEMPTS: Final = 3
RETRY_WAIT: wait_base = wait_exponential(multiplier=1, min=1, max=10)
USER_AGENT: Final = "ai-tahlilchi/0.1 (historical data import)"
HTTP_NOT_FOUND: Final = 404
HTTP_SERVER_ERROR: Final = 500
_SNIFF_BYTES: Final = 512


class _TransientHTTPError(Exception):
    """A response worth retrying (5xx)."""


def season_url(league_code: str, season: int) -> str:
    """Download URL of one league season."""
    return f"{BASE_URL}/{season_code(season)}/{league_code}.csv"


def cache_path(cache_dir: Path, league_code: str, season: int) -> Path:
    """Local copy of one league season, mirroring the URL layout."""
    return cache_dir / season_code(season) / f"{league_code}.csv"


def current_season(today: date) -> int:
    """Start year of the season in progress on ``today``."""
    return today.year if today.month >= SEASON_START_MONTH else today.year - 1


def needs_download(path: Path, *, season: int, now: datetime) -> bool:
    """Whether the cached file is missing, or belongs to the current season and is stale."""
    if not path.is_file():
        return True
    if season < current_season(now.date()):
        return False
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
    return now - modified > CURRENT_SEASON_MAX_AGE


def looks_like_csv(content: bytes) -> bool:
    """Cheap check that a response is a football-data CSV, not an HTML/error page."""
    head = content[:_SNIFF_BYTES].lstrip(b"\xef\xbb\xbf").lstrip()
    first_line = head.split(b"\n", 1)[0]
    return not head.lower().startswith(b"<") and b"," in first_line and b"Date" in first_line


def create_client() -> httpx.AsyncClient:
    """HTTP client for football-data downloads (the caller closes it)."""
    return httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


async def download_season(
    client: httpx.AsyncClient, league_code: str, season: int, destination: Path
) -> bool:
    """Download one season file to ``destination``; ``False`` if the server has none (404).

    Raises:
        ExternalServiceError: network/TLS failure, HTTP error or a non-CSV response.
    """
    url = season_url(league_code, season)
    response = await _get(client, url)
    if response.status_code == HTTP_NOT_FOUND:
        logger.info("season_file_not_published", url=url)
        return False
    if response.is_error:
        msg = f"{url} answered HTTP {response.status_code}"
        raise ExternalServiceError(msg)
    if not looks_like_csv(response.content):
        msg = f"{url} did not return a CSV (blocked by a proxy or web filter?)"
        raise ExternalServiceError(msg)
    await asyncio.to_thread(write_atomically, destination, response.content)
    logger.info("season_file_downloaded", url=url, bytes=len(response.content))
    return True


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET with retries on transport errors and 5xx responses."""
    retrying = AsyncRetrying(
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=RETRY_WAIT,
        retry=retry_if_exception_type((httpx.TransportError, _TransientHTTPError)),
        reraise=True,
    )
    try:
        return await retrying(_get_once, client, url)
    except _TransientHTTPError as exc:
        msg = f"{url} kept failing with HTTP {exc.args[0]} after {MAX_ATTEMPTS} attempts"
        raise ExternalServiceError(msg) from exc
    except httpx.TransportError as exc:
        msg = f"cannot reach {url}: {exc} (behind a TLS-inspecting proxy? set SSL_CERT_FILE)"
        raise ExternalServiceError(msg) from exc


async def _get_once(client: httpx.AsyncClient, url: str) -> httpx.Response:
    response = await client.get(url)
    if response.status_code >= HTTP_SERVER_ERROR:
        raise _TransientHTTPError(response.status_code)
    return response


def write_atomically(path: Path, content: bytes) -> None:
    """Write ``content`` so that readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.partial")
    partial.write_bytes(content)
    partial.replace(path)
