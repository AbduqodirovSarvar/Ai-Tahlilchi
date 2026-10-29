"""T6 fix round A: the response cache is bounded, sweeps what expired, and caches reduced bodies."""

import pytest

from app.clients.cache import DEFAULT_MAX_ENTRIES, TtlCache
from app.clients.quota import RequestPurpose
from tests.clients.conftest import FakeTime

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
