"""Production wiring of the data aggregator (Football-Data.org + football-data.co.uk)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.clients.football_data_org import create_football_data_client
from app.config import Settings
from app.domain import CallRecorder
from app.sports.aggregator import DataAggregatorService, OddsCsvLoader
from app.sports.history.download import create_client


@asynccontextmanager
async def open_data_aggregator(
    settings: Settings, recorder: CallRecorder | None = None
) -> AsyncIterator[DataAggregatorService]:
    """The aggregator with both HTTP clients, closed on exit.

    Raises:
        ConfigurationError: ``FOOTBALL_DATA_ORG_KEY`` is not set.
    """
    key = settings.require_secret("football_data_org_key")
    client, api_http = create_football_data_client(
        key,
        requests_per_minute=settings.football_data_requests_per_minute,
        recorder=recorder,
    )
    csv_http = create_client()
    try:
        yield DataAggregatorService(client, OddsCsvLoader(csv_http, settings.odds_csv_cache_dir))
    finally:
        await csv_http.aclose()
        await api_http.aclose()
