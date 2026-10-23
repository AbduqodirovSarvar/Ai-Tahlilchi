"""T6 fix round A: the response cache is bounded, sweeps what expired, and caches reduced bodies."""

import copy
from datetime import date, timedelta

import pytest
import respx

from app.clients.api_football import BookmakerFilter, bookmaker_shape, league_shape
from app.clients.cache import DEFAULT_MAX_ENTRIES, TtlCache
from app.clients.quota import RequestPurpose
from tests.clients.conftest import FakeTime, Harness, load

AUTO = RequestPurpose.AUTOMATIC


# ---------------------------------------------------------------------------- the cache
def make(clock: FakeTime, **kwargs: int) -> TtlCache[str]:
    return TtlCache(clock=clock.monotonic, **kwargs)


def test_defaults_are_the_documented_bounds() -> None:
    assert DEFAULT_MAX_ENTRIES == 128


def test_expired_entries_nobody_asks_for_again_are_swept_on_the_next_set(
    fake_time: FakeTime,
) -> None:
    cache = make(fake_time)
    for index in range(10):
        cache.set(f"old{index}", "x", ttl=60, size=100)
    fake_time.seconds += 61

    cache.set("fresh", "y", ttl=60, size=100)

    assert len(cache) == 1
    assert cache.estimated_bytes == 100
    assert cache.get("fresh") == "y"


def test_the_entry_count_is_capped_oldest_used_first(fake_time: FakeTime) -> None:
    cache = make(fake_time, max_entries=3)
    for key in "abc":
        cache.set(key, key, ttl=60)
    assert cache.get("a") == "a"  # touching "a" makes "b" the least recently used

    cache.set("d", "d", ttl=60)

    assert [cache.get(k) for k in "abcd"] == ["a", None, "c", "d"]


def test_the_estimated_bytes_are_capped_and_tracked_through_every_exit(
    fake_time: FakeTime,
) -> None:
    cache = make(fake_time, max_bytes=1000)
    cache.set("a", "a", ttl=60, size=400)
    cache.set("b", "b", ttl=60, size=400)
    cache.set("c", "c", ttl=60, size=400)  # 1200 > 1000: "a" goes

    assert cache.get("a") is None
    assert cache.estimated_bytes == 800
    cache.set("b", "b2", ttl=60, size=100)  # replacing re-counts
    assert cache.estimated_bytes == 500
    cache.discard("b")
    assert cache.estimated_bytes == 400
    fake_time.seconds += 61
    assert cache.get("c") is None  # expired on access
    assert cache.estimated_bytes == 0
    cache.set("d", "d", ttl=60, size=10)
    cache.clear()
    assert (len(cache), cache.estimated_bytes) == (0, 0)


def test_a_value_larger_than_the_whole_budget_is_not_stored_and_flushes_nothing(
    fake_time: FakeTime,
) -> None:
    cache = make(fake_time, max_bytes=1000)
    cache.set("keep", "k", ttl=60, size=300)

    cache.set("huge", "h", ttl=60, size=1001)

    assert cache.get("huge") is None
    assert cache.get("keep") == "k"


def test_a_non_positive_ttl_drops_the_entry_and_its_bytes(fake_time: FakeTime) -> None:
    cache = make(fake_time)
    cache.set("a", "a", ttl=60, size=50)

    cache.set("a", "a", ttl=0, size=50)

    assert (len(cache), cache.estimated_bytes) == (0, 0)


@pytest.mark.parametrize(("entries", "size"), [(0, 1), (1, 0)])
def test_nonsense_bounds_are_refused(entries: int, size: int) -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        TtlCache(max_entries=entries, max_bytes=size)


# ---------------------------------------------------------------------------- reduced bodies
def wide_day(day_index: int, leagues: int = 40, per_league: int = 12) -> dict[str, object]:
    """A day of the world: ``leagues`` leagues, only league 39 is wanted."""
    template = load("fixtures_date_2024-10-26.json")
    assert isinstance(template, dict)
    record = template["response"][0]  # type: ignore[index]
    response = []
    for league in range(leagues):
        for number in range(per_league):
            item = copy.deepcopy(record)
            item["league"]["id"] = 39 if league == 0 else 1000 + league
            item["fixture"]["id"] = day_index * 100_000 + league * 100 + number
            response.append(item)
    return {**template, "response": response, "results": len(response)}


async def test_the_cache_stays_flat_over_200_simulated_days(
    harness: Harness, router: respx.MockRouter
) -> None:
    days = iter(range(200))
    router.get("/fixtures").mock(
        side_effect=lambda request: respx.MockResponse(json=wide_day(next(days)))  # noqa: ARG005
    )
    sizes = []
    for index in range(200):
        harness.time.seconds += 24 * 3600  # one day: everything cached earlier has expired
        harness.time.now += timedelta(days=1)
        items = await harness.football.fixtures_by_date(
            date(2024, 10, 26) + timedelta(days=index), purpose=AUTO, league_ids={39}
        )
        assert {item.league.id for item in items} == {39}
        sizes.append((len(harness.cache), harness.cache.estimated_bytes))

    entries, estimated = zip(*sizes, strict=True)
    assert max(entries) == 1  # expired days are swept, not accumulated
    assert max(estimated) < min(estimated) * 1.1  # and the memory estimate is flat day to day
    assert max(estimated) < 1_000_000  # one reduced day, nowhere near the 32 MB budget


async def test_a_league_filtered_day_is_cached_smaller_than_the_whole_world(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures").mock(
        side_effect=lambda request: respx.MockResponse(json=wide_day(0))  # noqa: ARG005
    )

    await harness.football.fixtures_by_date(date(2024, 10, 26), purpose=AUTO, league_ids={39})
    reduced = harness.cache.estimated_bytes
    harness.cache.clear()
    await harness.football.fixtures_by_date(date(2024, 10, 27), purpose=AUTO)
    whole = harness.cache.estimated_bytes

    assert reduced * 20 < whole  # 1 league of 40


async def test_a_narrow_body_is_never_served_to_a_wider_request(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").mock(
        side_effect=lambda request: respx.MockResponse(json=wide_day(0))  # noqa: ARG005
    )
    day = date(2024, 10, 26)

    narrow = await harness.football.fixtures_by_date(day, purpose=AUTO, league_ids={39})
    again = await harness.football.fixtures_by_date(day, purpose=AUTO, league_ids={39})
    wide = await harness.football.fixtures_by_date(day, purpose=AUTO, league_ids={39, 1001})

    assert len(narrow) == len(again) == 12
    assert len(wide) == 24
    assert route.call_count == 2  # the repeat was a cache hit, the wider set was a new key


async def test_an_error_body_is_passed_through_unreduced_and_not_cached(
    harness: Harness, router: respx.MockRouter
) -> None:
    from app.clients.api_football import ApiFootballError  # noqa: PLC0415

    router.get("/fixtures").respond(json=load("error_plan.json"))

    with pytest.raises(ApiFootballError):
        await harness.football.fixtures_by_date(date(2024, 10, 26), purpose=AUTO, league_ids={39})

    assert len(harness.cache) == 0


def test_the_league_shape_ignores_records_without_a_league() -> None:
    shape = league_shape({39})
    body = {"response": [{"league": {"id": 39}}, {"league": {"id": 2}}, {"nope": 1}, "junk"]}

    assert shape.apply(body) == {"response": [{"league": {"id": 39}}]}
    assert shape.apply("not an envelope") == "not an envelope"
    assert shape.apply({"response": "oops"}) == {"response": "oops"}


def test_the_bookmaker_shape_keeps_only_the_accepted_names() -> None:
    shape = bookmaker_shape(BookmakerFilter("p", lambda name: name.lower() == "pinnacle"))
    body = {
        "response": [
            {"fixture": {"id": 1}, "bookmakers": [{"name": "Pinnacle"}, {"name": "Bet365"}, {}]},
            {"fixture": {"id": 2}},
        ]
    }

    assert shape.apply(body) == {
        "response": [
            {"fixture": {"id": 1}, "bookmakers": [{"name": "Pinnacle"}]},
            {"fixture": {"id": 2}},
        ]
    }


async def test_odds_are_reduced_to_the_wanted_bookmakers_before_they_are_cached(
    harness: Harness, router: respx.MockRouter
) -> None:
    body = load("odds_fixture_1208100.json")
    assert isinstance(body, dict)
    record = body["response"][0]  # type: ignore[index]
    record["bookmakers"] = [
        {**copy.deepcopy(record["bookmakers"][0]), "id": index, "name": name}
        for index, name in enumerate(["Pinnacle", "Bet365", "1xBet", "Bwin", "Unibet"])
    ]
    router.get("/odds").respond(json=body)
    wanted = BookmakerFilter("pin+1x", lambda name: name in {"Pinnacle", "1xBet"})

    items = await harness.football.odds_by_fixture(1208100, purpose=AUTO, bookmakers=wanted)

    assert [b.name for b in items[0].bookmakers] == ["Pinnacle", "1xBet"]
