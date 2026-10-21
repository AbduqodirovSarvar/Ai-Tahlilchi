"""In-memory TTL cache for provider responses (plan §9: the same data is not re-requested).

Keys are built from the endpoint and its parameters, so ``fixtures?date=..`` asked twice
within the TTL costs one request. The cache is per process and bounded; it is an
optimisation only (losing it on restart costs requests, never correctness).
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

type MonotonicClock = Callable[[], float]
type QueryParams = Mapping[str, str | int]

DEFAULT_MAX_ENTRIES: Final = 2_048


def cache_key(path: str, params: QueryParams) -> str:
    """Canonical key: path plus parameters sorted by name (order of kwargs is irrelevant)."""
    query = "&".join(f"{name}={params[name]}" for name in sorted(params))
    return f"{path}?{query}"


@dataclass(frozen=True, slots=True)
class _Entry[V]:
    value: V
    expires_at: float


class TtlCache[V]:
    """Least-recently-used cache whose entries expire ``ttl`` seconds after being stored."""

    def __init__(
        self, *, clock: MonotonicClock = time.monotonic, max_entries: int = DEFAULT_MAX_ENTRIES
    ) -> None:
        """Create an empty cache using ``clock`` (seconds, monotonic) for expiry."""
        if max_entries < 1:
            msg = f"max_entries must be >= 1 (got {max_entries})"
            raise ValueError(msg)
        self._clock = clock
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _Entry[V]] = OrderedDict()

    def get(self, key: str) -> V | None:
        """The cached value, or ``None`` when absent or expired (expired entries are dropped)."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.value

    def set(self, key: str, value: V, *, ttl: float) -> None:
        """Store ``value`` for ``ttl`` seconds; a non-positive ttl stores nothing."""
        if ttl <= 0:
            self._entries.pop(key, None)
            return
        self._entries[key] = _Entry(value, self._clock() + ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def discard(self, key: str) -> None:
        """Drop one entry if present."""
        self._entries.pop(key, None)

    def clear(self) -> None:
        """Drop every entry."""
        self._entries.clear()

    def __len__(self) -> int:
        """Number of stored entries (expired ones included until next access)."""
        return len(self._entries)
