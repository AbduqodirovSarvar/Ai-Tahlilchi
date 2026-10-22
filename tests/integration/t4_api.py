"""A tiny API-Football behind respx, wired to the real pipeline provider.

Used by the T4 QA tests that must count HTTP requests or see the real client's failure
behaviour (quota, 5xx) instead of a scripted provider.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import respx

from app.pipeline.provider import FootballProvider
from app.sports.football_leagues import DEFAULT_LEAGUES, season_for
from tests.clients.conftest import BASE_URL, TEST_KEY, FakeTime, Harness, build_harness

LEAGUE_IDS = [league.api_id for league in DEFAULT_LEAGUES]
PAGE_SIZE = 10


def record(  # noqa: PLR0913 - one column per field of an API-Football fixture
    fixture_id: int,
    league_id: int,
    kickoff: datetime,
    *,
    status: str = "NS",
    fulltime: tuple[int, int] | None = None,
    goals: tuple[int, int] | None = None,
) -> dict[str, object]:
    """A ``/fixtures`` record (``fulltime`` = regular time, ``goals`` includes extra time)."""

    def pair(score: tuple[int, int] | None) -> dict[str, int | None]:
        return (
            {"home": None, "away": None} if score is None else {"home": score[0], "away": score[1]}
        )

    return {
        "fixture": {"id": fixture_id, "date": kickoff.isoformat(), "status": {"short": status}},
        "league": {
            "id": league_id,
            "name": f"League {league_id}",
            "season": season_for(kickoff.date()),
        },
        "teams": {
            "home": {"id": fixture_id * 10 + 1, "name": f"Home {fixture_id}"},
            "away": {"id": fixture_id * 10 + 2, "name": f"Away {fixture_id}"},
        },
        "goals": pair(goals if goals is not None else fulltime),
        "score": {"fulltime": pair(fulltime), "halftime": {"home": None, "away": None}},
    }


def envelope(records: list[dict[str, object]], *, page: int = 1, pages: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "get": "x",
            "parameters": {},
            "errors": [],
            "results": len(records),
            "paging": {"current": page, "total": pages},
            "response": records,
        },
    )


def odds_record(
    fixture_id: int, league_id: int, season: int, one_x_bet: float
) -> dict[str, object]:
    """Pinnacle (fair 2.00/3.50/3.80) and 1xBet (home at ``one_x_bet``) 1X2 prices."""

    def bookmaker(identifier: int, name: str, home: float) -> dict[str, object]:
        return {
            "id": identifier,
            "name": name,
            "bets": [
                {
                    "id": 1,
                    "name": "Match Winner",
                    "values": [
                        {"value": "Home", "odd": f"{home:.2f}"},
                        {"value": "Draw", "odd": "3.50"},
                        {"value": "Away", "odd": "3.80"},
                    ],
                }
            ],
        }

    return {
        "league": {"id": league_id, "season": season},
        "fixture": {"id": fixture_id},
        "bookmakers": [bookmaker(4, "Pinnacle", 2.0), bookmaker(11, "1xBet", one_x_bet)],
    }


@dataclass
class Wire:
    """The state of the fake API: fixtures by id, and switches for failures."""

    fixtures: dict[int, dict[str, object]] = field(default_factory=dict)
    one_x_bet: float = 2.20
    odds_pages: int = 1
    fail_with: int | None = None
    seen: list[httpx.Request] = field(default_factory=list)

    def add(self, item: dict[str, object]) -> None:
        self.fixtures[item["fixture"]["id"]] = item  # type: ignore[index]

    def count(self, path: str) -> int:
        """How many requests went to ``path``."""
        return sum(1 for request in self.seen if request.url.path.endswith(path))

    def _fixtures(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with:
            return httpx.Response(self.fail_with)
        params = request.url.params
        if "ids" in params:
            wanted = {int(part) for part in params["ids"].split("-")}
            return envelope([r for i, r in self.fixtures.items() if i in wanted])
        if "date" in params:
            day = params["date"]
            return envelope([r for r in self.fixtures.values() if _date(r) == day])
        league = int(params["league"])
        return envelope([r for r in self.fixtures.values() if r["league"]["id"] == league])  # type: ignore[index]

    def _odds(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with:
            return httpx.Response(self.fail_with)
        params = request.url.params
        league, day = int(params["league"]), params["date"]
        page = int(params.get("page", "1"))
        rows = [
            odds_record(r["fixture"]["id"], league, r["league"]["season"], self.one_x_bet)  # type: ignore[index]
            for r in self.fixtures.values()
            if r["league"]["id"] == league and _date(r) == day  # type: ignore[index]
        ]
        share = rows[page - 1 :: self.odds_pages] if self.odds_pages > 1 else rows
        return envelope(share, page=page, pages=self.odds_pages)

    def _other(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(503) if self.fail_with else envelope([])

    def install(self, router: respx.MockRouter) -> None:
        def log(request: httpx.Request) -> None:
            self.seen.append(request)

        for path, handler in (
            ("/fixtures", self._fixtures),
            ("/odds", self._odds),
            ("/fixtures/statistics", self._other),
            ("/fixtures/lineups", self._other),
            ("/injuries", self._other),
        ):
            router.get(path).mock(side_effect=_logged(handler, log))


def _logged(handler, log):  # type: ignore[no-untyped-def]
    def call(request: httpx.Request) -> httpx.Response:
        log(request)
        return handler(request)

    return call


def _date(item: dict[str, object]) -> str:
    return str(item["fixture"]["date"])[:10]  # type: ignore[index]


@dataclass
class Api:
    """The wire, the real client over it, and the provider the pipeline uses."""

    wire: Wire
    harness: Harness
    provider: FootballProvider
    router: respx.MockRouter


@asynccontextmanager
async def api_over_respx(
    now: datetime, *, daily_limit: int = 100
) -> AsyncIterator[tuple[Api, FakeTime]]:
    """A real ``FootballProvider`` talking to the fake API; time only moves when told to."""
    fake_time = FakeTime(now=now)
    wire = Wire()
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        wire.install(router)
        async with httpx.AsyncClient(
            base_url=BASE_URL, headers={"x-apisports-key": TEST_KEY}
        ) as http:
            harness = build_harness(fake_time, http, daily_limit=daily_limit)
            provider = FootballProvider(harness.football, clock=fake_time.utc)
            yield Api(wire, harness, provider, router), fake_time
