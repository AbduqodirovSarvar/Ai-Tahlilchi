"""In-memory TTL cache for provider responses (plan §9: the same data is not re-requested).

Keys are built from the endpoint and its parameters, so ``fixtures?date=..`` asked twice
within the TTL costs one request. The cache is per process; it is an optimisation only
(losing it on restart costs requests, never correctness).

It is bounded three ways, because provider bodies are large (one day of fixtures is
megabytes of parsed JSON) and a long-running process must stay flat: expired entries are
swept on every ``set`` (an entry nobody asks for again would otherwise live until evicted),
the number of entries is capped, and so is their approximate memory (``max_bytes``, in
estimated bytes of the parsed value; the caller supplies the estimate).
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

type MonotonicClock = Callable[[], float]
type QueryParams = Mapping[str, str | int]

DEFAULT_MAX_ENTRIES: Final = 128
DEFAULT_MAX_BYTES: Final = 32 * 1024 * 1024


def cache_key(path: str, params: QueryParams) -> str:
    """Canonical key: path plus parameters sorted by name (order of kwargs is irrelevant)."""
    query = "&".join(f"{name}={params[name]}" for name in sorted(params))
    return f"{path}?{query}"


@dataclass(frozen=True, slots=True)
class _Entry[V]:
    value: V
    expires_at: float
    size: int


class TtlCache[V]:
    """Least-recently-used cache whose entries expire ``ttl`` seconds after being stored."""

    def __init__(
        self,
        *,
        clock: MonotonicClock = time.monotonic,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """Create an empty cache using ``clock`` (seconds, monotonic) for expiry."""
        if max_entries < 1 or max_bytes < 1:
            msg = f"max_entries and max_bytes must be >= 1 (got {max_entries}, {max_bytes})"
            raise ValueError(msg)
        self._clock = clock
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._bytes = 0
        self._entries: OrderedDict[str, _Entry[V]] = OrderedDict()

    def get(self, key: str) -> V | None:
        """The cached value, or ``None`` when absent or expired (expired entries are dropped)."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            self.discard(key)
            return None
        self._entries.move_to_end(key)
        return entry.value

    def set(self, key: str, value: V, *, ttl: float, size: int = 0) -> None:
        """Store ``value`` for ``ttl`` seconds; ``size`` is its estimated memory in bytes.

        A non-positive ttl stores nothing, and neither does a value larger than the whole
        byte budget (it would only flush every other entry).
        """
        self.discard(key)
        if ttl <= 0 or size > self._max_bytes:
            return
        self._sweep_expired()
        self._entries[key] = _Entry(value, self._clock() + ttl, size)
        self._bytes += size
        while len(self._entries) > self._max_entries or self._bytes > self._max_bytes:
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= evicted.size

    def _sweep_expired(self) -> None:
        """Drop every expired entry (at most ``max_entries`` of them: a cheap scan)."""
        now = self._clock()
        for key in [k for k, entry in self._entries.items() if entry.expires_at <= now]:
            self.discard(key)

    def discard(self, key: str) -> None:
        """Drop one entry if present."""
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= entry.size

    def clear(self) -> None:
        """Drop every entry."""
        self._entries.clear()
        self._bytes = 0

    @property
    def estimated_bytes(self) -> int:
        """Sum of the sizes given to :meth:`set` for the entries currently stored."""
        return self._bytes

    def __len__(self) -> int:
        """Number of stored entries (expired ones included until the next ``set``)."""
        return len(self._entries)
