"""F0.7: check what the API-Football plan really gives us, before relying on it.

Usage (needs ``API_FOOTBALL_KEY`` in ``.env``):

    uv run python scripts/probe_api.py                       # all ten tracked leagues
    uv run python scripts/probe_api.py --leagues 39,140      # fewer leagues, fewer requests

For each league it asks the current season's fixtures (is the season open on this plan?)
and the odds of the next match day (which bookmakers: 1xBet, Pinnacle, Bet365?). Once per
run it checks xG (``fixtures/statistics``), injuries and line-ups, and how the provider
quotes Asian handicap lines (see ``app.sports.football_mapping``). Cost: about
``1 + 2 x leagues + 3`` requests (≈ 24 for ten leagues) of the daily 100.

Outputs: ``docs/api_probe_report.md`` and sanitized responses (account data removed; the key
is a header and never part of a body) in ``tests/fixtures/api_football/recorded/`` to
replace the synthetic test fixtures.

Exit codes: 0 probe finished, 1 provider/network failure, 2 no key / bad arguments.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from app.clients.api_football import (
    ApiFootballClient,
    ApiFootballError,
    create_api_football_client,
    validate_records,
)
from app.clients.api_football_models import (
    Bookmaker,
    Envelope,
    FixtureItem,
    OddsItem,
    TeamStatisticsItem,
)
from app.clients.cache import QueryParams
from app.clients.quota import InMemoryUsageStore, RequestPurpose
from app.config import get_settings
from app.domain import BOOKMAKER_1XBET, BOOKMAKER_BET365, BOOKMAKER_PINNACLE
from app.errors import ConfigurationError, ExternalServiceError
from app.logging import configure_logging, get_logger
from app.sports.football_leagues import DEFAULT_LEAGUES, TrackedLeague, season_for
from app.sports.football_mapping import (
    HandicapConvention,
    bookmaker_slug,
    expected_goals,
    fixture_status,
)

logger = get_logger("scripts.probe_api")

EXIT_OK: Final = 0
EXIT_PROVIDER_ERROR: Final = 1
EXIT_CONFIGURATION_ERROR: Final = 2
DEFAULT_REPORT: Final = Path("docs/api_probe_report.md")
DEFAULT_RECORD_DIR: Final = Path("tests/fixtures/api_football/recorded")
WANTED_BOOKMAKERS: Final = (BOOKMAKER_1XBET, BOOKMAKER_PINNACLE, BOOKMAKER_BET365)
PRIVATE_FIELDS: Final = frozenset({"account", "email", "firstname", "lastname"})
# A two-way price pair from one bookmaker has an overround of roughly 1-15%.
PLAUSIBLE_OVERROUND: Final = (1.0, 1.15)
NO_KEY_MESSAGE: Final = (
    "API_FOOTBALL_KEY is not set: the probe needs a key (F0.2). Add it to .env and run "
    "`uv run python scripts/probe_api.py` again. Nothing was requested."
)


@dataclass
class LeagueProbe:
    """What one league's current season looks like on this plan."""

    league: TrackedLeague
    name: str | None = None
    error: str | None = None
    fixtures: int = 0
    finished: int = 0
    upcoming: int = 0
    odds_day: date | None = None
    bookmakers: tuple[str, ...] = ()
    finished_ids: tuple[int, ...] = ()
    upcoming_ids: tuple[int, ...] = ()

    @property
    def season_open(self) -> bool:
        """Whether the current season could be read."""
        return self.error is None


@dataclass
class ProbeReport:
    """Everything the probe found, rendered by :func:`render_report`."""

    season: int
    checked_at: datetime
    plan: str | None = None
    leagues: list[LeagueProbe] = field(default_factory=list)
    xg: bool | None = None
    injuries: bool | None = None
    lineups: bool | None = None
    handicap: dict[HandicapConvention, tuple[int, int]] = field(default_factory=dict)
    requests_used: int = 0
    notes: list[str] = field(default_factory=list)


def sanitize(payload: JsonValue) -> JsonValue:
    """Drop account/personal fields (``/status`` returns the owner's name and e-mail)."""
    if isinstance(payload, dict):
        return {key: sanitize(value) for key, value in payload.items() if key not in PRIVATE_FIELDS}
    if isinstance(payload, list):
        return [sanitize(item) for item in payload]
    return payload


class Recorder:
    """Saves sanitized response bodies for re-recording the test fixtures."""

    def __init__(self, directory: Path | None) -> None:
        """Record into ``directory``; ``None`` disables recording."""
        self._directory = directory

    def save(self, endpoint: str, params: QueryParams, envelope: Envelope) -> None:
        """Write one response as ``<endpoint>_<params>.json``."""
        if self._directory is None:
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        suffix = "_".join(f"{name}-{params[name]}" for name in sorted(params)) or "all"
        name = f"{endpoint.replace('/', '-')}_{suffix}.json"
        body = sanitize(envelope.model_dump(mode="json"))
        (self._directory / name).write_text(json.dumps(body, indent=2), encoding="utf-8")


async def fetch(
    client: ApiFootballClient, recorder: Recorder, endpoint: str, params: QueryParams
) -> Envelope:
    """One manual-purpose request, recorded."""
    envelope = await client.envelope(endpoint, params, purpose=RequestPurpose.MANUAL, cache_ttl=0)
    recorder.save(endpoint, params, envelope)
    return envelope


async def probe_league(
    client: ApiFootballClient, recorder: Recorder, league: TrackedLeague, season: int, today: date
) -> LeagueProbe:
    """Season access and bookmakers of one league (2 requests)."""
    result = LeagueProbe(league)
    params: dict[str, str | int] = {"league": league.api_id, "season": season}
    try:
        envelope = await fetch(client, recorder, "fixtures", params)
    except ApiFootballError as exc:
        result.error = "; ".join(exc.errors.values())
        return result
    items = validate_records(envelope, FixtureItem, endpoint="fixtures")
    summarize_fixtures(result, items, today)
    if result.odds_day is not None:
        odds_params = {**params, "date": result.odds_day.isoformat()}
        try:
            odds = await fetch(client, recorder, "odds", odds_params)
        except ApiFootballError as exc:
            result.error = f"odds: {'; '.join(exc.errors.values())}"
            return result
        records = validate_records(odds, OddsItem, endpoint="odds")
        names = {bookmaker_slug(b.name) for item in records for b in item.bookmakers}
        result.bookmakers = tuple(sorted(names))
    return result


def summarize_fixtures(result: LeagueProbe, items: Sequence[FixtureItem], today: date) -> None:
    """Counts, a sample of finished/upcoming ids and the next match day with odds."""
    result.fixtures = len(items)
    if items:
        result.name = items[0].league.name
    finished = [item for item in items if item.fixture.status.short in {"FT", "AET", "PEN"}]
    upcoming = sorted(
        (item for item in items if item.fixture.date.astimezone(UTC).date() >= today),
        key=lambda item: item.fixture.date,
    )
    upcoming = [item for item in upcoming if _is_scheduled(item.fixture.status.short)]
    result.finished, result.upcoming = len(finished), len(upcoming)
    result.finished_ids = tuple(item.fixture.id for item in finished[-3:])
    result.upcoming_ids = tuple(item.fixture.id for item in upcoming[:3])
    if upcoming:
        result.odds_day = upcoming[0].fixture.date.astimezone(UTC).date()


def _is_scheduled(code: str) -> bool:
    try:
        return fixture_status(code).value == "scheduled"
    except ValueError:
        return False


def handicap_evidence(items: Sequence[OddsItem]) -> dict[HandicapConvention, tuple[int, int]]:
    """For each reading of AH values, how many Home/Away pairs have a plausible overround.

    The right reading pairs more lines (the wrong one only pairs the 0 line).
    """
    books = [_handicap_prices(bookmaker) for item in items for bookmaker in item.bookmakers]
    evidence: dict[HandicapConvention, tuple[int, int]] = {}
    for convention in HandicapConvention:
        overrounds = [value for prices in books for value in _pair_overrounds(prices, convention)]
        plausible = sum(
            PLAUSIBLE_OVERROUND[0] <= value <= PLAUSIBLE_OVERROUND[1] for value in overrounds
        )
        evidence[convention] = (plausible, len(overrounds))
    return evidence


def _pair_overrounds(
    prices: dict[tuple[str, float], float], convention: HandicapConvention
) -> list[float]:
    overrounds = []
    for (side, number), odd in prices.items():
        away_number = number if convention is HandicapConvention.HOME_LINE else -number
        away = prices.get(("away", away_number))
        if side == "home" and away is not None:
            overrounds.append(1 / odd + 1 / away)
    return overrounds


def _handicap_prices(bookmaker: Bookmaker) -> dict[tuple[str, float], float]:
    prices: dict[tuple[str, float], float] = {}
    values = [
        value
        for bet in bookmaker.bets
        if bet.name.casefold() == "asian handicap"
        for value in bet.values
    ]
    for value in values:
        parts = str(value.value).casefold().split()
        try:
            side, number, odd = parts[0], float(parts[1]), float(value.odd)
        except (IndexError, ValueError):
            continue
        prices[(side, number)] = odd
    return prices


async def probe_context(client: ApiFootballClient, recorder: Recorder, report: ProbeReport) -> None:
    """xG, injuries, line-ups and AH quoting on sample fixtures (≤ 4 requests)."""
    finished = next((lg.finished_ids[-1] for lg in report.leagues if lg.finished_ids), None)
    upcoming = next((lg.upcoming_ids[0] for lg in report.leagues if lg.upcoming_ids), None)
    if finished is not None:
        stats = await fetch(client, recorder, "fixtures/statistics", {"fixture": finished})
        items = validate_records(stats, TeamStatisticsItem, endpoint="fixtures/statistics")
        report.xg = bool(expected_goals(items))
        lineups = await fetch(client, recorder, "fixtures/lineups", {"fixture": finished})
        report.lineups = bool(lineups.records)
    if upcoming is not None:
        injuries = await fetch(client, recorder, "injuries", {"fixture": upcoming})
        report.injuries = bool(injuries.records)
        odds = await fetch(client, recorder, "odds", {"fixture": upcoming})
        report.handicap = handicap_evidence(validate_records(odds, OddsItem, endpoint="odds"))


async def run_probe(
    client: ApiFootballClient,
    leagues: Sequence[TrackedLeague],
    *,
    recorder: Recorder,
    now: datetime,
) -> ProbeReport:
    """Run every check; provider errors of single leagues are reported, not raised."""
    report = ProbeReport(season=season_for(now.date()), checked_at=now)
    status = await fetch(client, recorder, "status", {})
    report.plan = _plan_name(status)
    for league in leagues:
        report.leagues.append(
            await probe_league(client, recorder, league, report.season, now.date())
        )
    await probe_context(client, recorder, report)
    report.requests_used = (await client.api.quota.status()).counts.requests_used
    return report


def _plan_name(status: Envelope) -> str | None:
    record = status.records[0] if status.records else None
    if not isinstance(record, dict):
        return None
    subscription = record.get("subscription")
    if isinstance(subscription, dict) and isinstance(subscription.get("plan"), str):
        return str(subscription["plan"])
    return None


def _yes_no(value: bool | None) -> str:
    return {True: "ha", False: "yo'q", None: "tekshirilmadi"}[value]


def render_report(report: ProbeReport) -> str:
    """The Markdown report (Uzbek, like every project document)."""
    lines = [
        "# API-Football tekshiruv hisoboti (F0.7)",
        "",
        f"- Sana: {report.checked_at:%Y-%m-%d %H:%M} UTC · mavsum: {report.season}",
        f"- Tarif: {report.plan or 'aniqlanmadi'} · sarflangan so'rovlar: {report.requests_used}",
        f"- xG (`fixtures/statistics`): {_yes_no(report.xg)}",
        f"- Jarohatlar (`injuries`): {_yes_no(report.injuries)}",
        f"- Tarkiblar (`fixtures/lineups`): {_yes_no(report.lineups)}",
        "",
        "| Liga (id) | Joriy mavsum | O'yinlar | Tugagan | Kelgusi | Koeff. sanasi |"
        " 1xBet | Pinnacle | Bet365 | Bukmekerlar |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for league in report.leagues:
        found = [_yes_no(book in league.bookmakers) for book in WANTED_BOOKMAKERS]
        access = "ochiq" if league.season_open else f"yopiq: {league.error}"
        lines.append(
            f"| {league.name or '?'} ({league.league.api_id}) | {access} | {league.fixtures} |"
            f" {league.finished} | {league.upcoming} | {league.odds_day or '-'} |"
            f" {' | '.join(found)} | {', '.join(league.bookmakers) or '-'} |"
        )
    lines += ["", "## Osiyo forasi (AH) yozilishi", ""]
    for convention, (plausible, total) in report.handicap.items():
        lines.append(f"- `{convention}`: {plausible}/{total} juftlik ishonchli marjada")
    if not report.handicap:
        lines.append("- tekshirilmadi (kelgusi o'yin yoki koeffitsient topilmadi)")
    lines += ["", "Ko'proq ishonchli juftlik bergan yozilish to'g'ri (ADR 0007).", ""]
    lines += [f"- {note}" for note in report.notes]
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Command line options."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--leagues", help="comma-separated API-Football league ids")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    parser.add_argument("--no-record", action="store_true", help="do not save responses")
    return parser.parse_args(argv)


def select_leagues(option: str | None) -> list[TrackedLeague]:
    """Tracked leagues to probe (all by default).

    Raises:
        ConfigurationError: an id is not a tracked league.
    """
    if not option:
        return list(DEFAULT_LEAGUES)
    by_id = {league.api_id: league for league in DEFAULT_LEAGUES}
    chosen: list[TrackedLeague] = []
    for part in option.split(","):
        league_id = int(part) if part.strip().isdigit() else -1
        if league_id not in by_id:
            msg = f"--leagues: {part.strip()!r} is not a tracked API-Football league id"
            raise ConfigurationError(msg)
        chosen.append(by_id[league_id])
    return chosen


async def run(args: argparse.Namespace, api_key: str, now: datetime) -> ProbeReport:
    """Build the client, run the probe, write the report."""
    client, http = create_api_football_client(api_key, InMemoryUsageStore())
    try:
        recorder = Recorder(None if args.no_record else args.record_dir)
        report = await run_probe(client, select_leagues(args.leagues), recorder=recorder, now=now)
    finally:
        await http.aclose()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(report), encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    """Entry point; returns the process exit code."""
    configure_logging(level="INFO", app_env="dev")
    try:
        settings = get_settings()
        configure_logging(level="INFO", app_env="dev", secret_values=settings.secret_values())
        args = parse_args(sys.argv[1:] if argv is None else argv)
        api_key = settings.require_secret("api_football_key")
    except ConfigurationError as exc:
        reason = NO_KEY_MESSAGE if "API_FOOTBALL_KEY" in str(exc) else str(exc)
        logger.error("probe_not_run", reason=reason)
        return EXIT_CONFIGURATION_ERROR
    try:
        select_leagues(args.leagues)
        report = asyncio.run(run(args, api_key, now or datetime.now(UTC)))
    except ConfigurationError as exc:
        logger.error("probe_not_run", reason=str(exc))
        return EXIT_CONFIGURATION_ERROR
    except ExternalServiceError as exc:
        logger.error("probe_failed", reason=str(exc))
        return EXIT_PROVIDER_ERROR
    logger.info("probe_finished", report=str(args.report), requests=report.requests_used)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
