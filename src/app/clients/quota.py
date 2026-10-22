"""Daily request quota per provider and sport, with a reserve kept for manual requests.

Plan §9: the free API-Football plan allows 100 requests a day; automatic jobs need about
25-43 of them and ~30% is kept for the user's manual requests (recalculation, custom
analysis, date ranges). The rule enforced here:

- a **manual** request may use anything that is left today;
- an **automatic** request may not touch the part of the reserve that manual requests have
  not used yet: ``automatic_available = left - max(reserve - manual_used, 0)``.

So automatic jobs can never starve the user, while unused manual capacity is not wasted
once the user has spent their reserve. Usage is persisted through a :class:`UsageStore`
(the database implementation lives in ``app.db.api_usage``) and keyed by UTC day, which
is when API-Sports resets its counters.
"""

import asyncio
import weakref
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Final, Protocol

from app.errors import ExternalServiceError

type UtcClock = Callable[[], datetime]

# Same value as ``app.services.interfaces.MANUAL_RESERVE_SHARE`` (clients cannot import
# services); the service layer passes its constant in when it builds the policy.
DEFAULT_MANUAL_RESERVE_SHARE: Final = 0.30
NOT_SPORT_SPECIFIC: Final = ""


class RequestPurpose(StrEnum):
    """Who asked for a request: a scheduled job or a user action."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class UsageKey:
    """One counter row: UTC day, provider and sport (``""`` when not sport-specific)."""

    day: date
    provider: str
    sport: str = NOT_SPORT_SPECIFIC


@dataclass(frozen=True, slots=True)
class UsageCounts:
    """Requests spent on one day; ``manual_used`` is the manual share of ``requests_used``."""

    requests_used: int = 0
    manual_used: int = 0


class UsageStore(Protocol):
    """Persistence of daily usage counters."""

    async def get(self, key: UsageKey) -> UsageCounts:
        """Current counters (zeros when nothing was spent yet)."""
        ...

    async def add(self, key: UsageKey, *, requests: int, manual: int) -> UsageCounts:
        """Increase the counters and return the new values."""
        ...

    async def raise_floor(self, key: UsageKey, requests_used: int) -> UsageCounts:
        """Set ``requests_used`` to at least ``requests_used`` (provider-reported usage)."""
        ...


class InMemoryUsageStore:
    """Process-local :class:`UsageStore` (tests, scripts and keyless development)."""

    def __init__(self) -> None:
        """Start with no usage."""
        self._counts: dict[UsageKey, UsageCounts] = {}

    async def get(self, key: UsageKey) -> UsageCounts:
        """See :meth:`UsageStore.get`."""
        return self._counts.get(key, UsageCounts())

    async def add(self, key: UsageKey, *, requests: int, manual: int) -> UsageCounts:
        """See :meth:`UsageStore.add`."""
        current = self._counts.get(key, UsageCounts())
        updated = UsageCounts(current.requests_used + requests, current.manual_used + manual)
        self._counts[key] = updated
        return updated

    async def raise_floor(self, key: UsageKey, requests_used: int) -> UsageCounts:
        """See :meth:`UsageStore.raise_floor`."""
        current = self._counts.get(key, UsageCounts())
        updated = replace(current, requests_used=max(current.requests_used, requests_used))
        self._counts[key] = updated
        return updated


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """Daily limit of one provider/sport and the share of it reserved for manual requests."""

    daily_limit: int
    manual_reserve_share: float = DEFAULT_MANUAL_RESERVE_SHARE

    def __post_init__(self) -> None:
        """Reject a negative limit or a share outside 0..1."""
        if self.daily_limit < 0:
            msg = f"daily_limit must be >= 0 (got {self.daily_limit})"
            raise ValueError(msg)
        if not 0 <= self.manual_reserve_share <= 1:
            msg = f"manual_reserve_share must be in 0..1 (got {self.manual_reserve_share})"
            raise ValueError(msg)

    @property
    def manual_reserve(self) -> int:
        """Requests per day that automatic jobs may never use."""
        return round(self.daily_limit * self.manual_reserve_share)

    def available(self, counts: UsageCounts, purpose: RequestPurpose) -> int:
        """How many more requests ``purpose`` may spend today."""
        left = max(self.daily_limit - counts.requests_used, 0)
        if purpose is RequestPurpose.MANUAL:
            return left
        reserve_left = max(self.manual_reserve - counts.manual_used, 0)
        return max(left - reserve_left, 0)


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """Today's usage of one provider/sport and what each purpose may still spend."""

    key: UsageKey
    counts: UsageCounts
    policy: QuotaPolicy

    @property
    def remaining(self) -> int:
        """Requests left today for anyone (manual requests may use all of them)."""
        return self.policy.available(self.counts, RequestPurpose.MANUAL)

    @property
    def automatic_available(self) -> int:
        """Requests automatic jobs may still spend today."""
        return self.policy.available(self.counts, RequestPurpose.AUTOMATIC)

    @property
    def reserve_remaining(self) -> int:
        """Unused part of the manual reserve."""
        return max(min(self.policy.manual_reserve - self.counts.manual_used, self.remaining), 0)


class QuotaExceededError(ExternalServiceError):
    """A request would exceed today's quota for its purpose (nothing was sent)."""

    def __init__(self, *, provider: str, purpose: RequestPurpose, needed: int, available: int):
        """Remember what was asked and what was left, for a user-facing message."""
        self.provider = provider
        self.purpose = purpose
        self.needed = needed
        self.available = available
        super().__init__(
            f"{provider}: {purpose} request(s) need {needed}, only {available} left today"
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class QuotaGuard:
    """Checks and charges the daily quota of one provider/sport before each request.

    Check-and-charge is atomic within the process (one asyncio lock); the application runs
    as a single process (ADR 0002), so no cross-process locking is needed.
    """

    def __init__(
        self,
        store: UsageStore,
        policy: QuotaPolicy,
        *,
        provider: str,
        sport: str = NOT_SPORT_SPECIFIC,
        clock: UtcClock = _utc_now,
    ) -> None:
        """Guard ``provider``/``sport`` usage stored in ``store`` under ``policy``."""
        self._store = store
        self._policy = policy
        self._provider = provider
        self._sport = sport
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def policy(self) -> QuotaPolicy:
        """The limit and reserve this guard enforces."""
        return self._policy

    def _key(self) -> UsageKey:
        return UsageKey(self._clock().astimezone(UTC).date(), self._provider, self._sport)

    async def status(self) -> QuotaStatus:
        """Today's counters and availability."""
        key = self._key()
        return QuotaStatus(key, await self._store.get(key), self._policy)

    async def ensure_available(self, requests: int, purpose: RequestPurpose) -> QuotaStatus:
        """Fail before a multi-request operation starts if it cannot finish within quota.

        Raises:
            QuotaExceededError: fewer than ``requests`` are available for ``purpose``.
        """
        status = await self.status()
        self._check(status.counts, requests, purpose)
        return status

    async def charge(self, purpose: RequestPurpose, requests: int = 1) -> QuotaStatus:
        """Count ``requests`` about to be sent, or refuse without counting anything.

        Raises:
            QuotaExceededError: the request would exceed the quota for ``purpose``.
        """
        async with self._lock:
            key = self._key()
            self._check(await self._store.get(key), requests, purpose)
            manual = requests if purpose is RequestPurpose.MANUAL else 0
            counts = await self._store.add(key, requests=requests, manual=manual)
            return QuotaStatus(key, counts, self._policy)

    async def reconcile(
        self, key: UsageKey, *, provider_limit: int, provider_remaining: int
    ) -> QuotaStatus:
        """Adopt the provider's own count for ``key``'s day when it is higher than ours.

        Another consumer of the same key (or requests lost in a crash) may have spent
        requests we never counted; the provider's numbers are authoritative upwards only,
        so a provider reset at a slightly different moment never hands out extra quota.

        ``key`` is the one :meth:`charge` returned for the request whose response carried
        these numbers. A response that arrives after UTC midnight describes the *previous*
        day's counter: applying it to the new day would wrongly burn the new day's quota,
        so it is ignored. The adopted count never exceeds the daily limit.
        """
        used = min(max(provider_limit - provider_remaining, 0), self._policy.daily_limit)
        async with self._lock:
            today = self._key()
            if key != today:
                return QuotaStatus(today, await self._store.get(today), self._policy)
            counts = await self._store.raise_floor(key, used)
            return QuotaStatus(key, counts, self._policy)

    def _check(self, counts: UsageCounts, requests: int, purpose: RequestPurpose) -> None:
        available = self._policy.available(counts, purpose)
        if requests > available:
            raise QuotaExceededError(
                provider=self._provider, purpose=purpose, needed=requests, available=available
            )


_SHARED_GUARDS: weakref.WeakKeyDictionary[
    UsageStore, dict[tuple[str, str, QuotaPolicy], QuotaGuard]
] = weakref.WeakKeyDictionary()


def shared_guard(
    store: UsageStore,
    policy: QuotaPolicy,
    *,
    provider: str,
    sport: str = NOT_SPORT_SPECIFIC,
    clock: UtcClock = _utc_now,
) -> QuotaGuard:
    """The one :class:`QuotaGuard` of (``store``, ``provider``, ``sport``, ``policy``).

    Check-and-charge is atomic only inside one guard (its lock). Two guards over the same
    counters could both pass the check and overspend, so production wiring obtains guards
    here: every client built over the same store shares the same lock. ``clock`` only
    applies when the guard is created.
    """
    guards = _SHARED_GUARDS.setdefault(store, {})
    key = (provider, sport, policy)
    guard = guards.get(key)
    if guard is None:
        guard = guards[key] = QuotaGuard(store, policy, provider=provider, sport=sport, clock=clock)
    return guard
