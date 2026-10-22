"""Daily request/token budget for the free Gemini tier, plus an in-memory request journal.

The guard is deliberately conservative: it counts every HTTP request that reached the API
(also failed ones and every retry inside one logical call) and compares *estimated*
next-call tokens against the remaining allowance, so the free quota is not exhausted by a
retry storm. Two counters are kept: ``requests`` (HTTP attempts, what the provider's quota
sees, and what the daily cap applies to) and ``logical_requests`` (calls of the service).

Concurrency: :meth:`LlmBudget.reserve` checks and books the allowance in one synchronous
step. The event loop cannot switch tasks inside it, so no lock is needed, and parallel
callers cannot all pass the check before any of them has recorded its usage (the old
``allow`` + ``await`` + ``record`` gap). The booking is settled with the real usage after
the call. The day rolls over at UTC midnight; the
provider's own reset time differs, which only makes this guard stricter at times.

State is per process (in memory). A restart forgets today's usage, which at most lets one
extra day's worth through; persistence can be added behind the same interface if it matters.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Final

from app.logging import get_logger

logger = get_logger(__name__)

# Defaults sit far below the free-tier allowance: the product needs 1-2 small calls a day.
DEFAULT_MAX_REQUESTS_PER_DAY: Final = 100
DEFAULT_MAX_TOKENS_PER_DAY: Final = 250_000
JOURNAL_CAPACITY: Final = 500

type Clock = Callable[[], datetime]


class CallOutcome(StrEnum):
    """How one model request ended."""

    OK = "ok"
    INVALID_OUTPUT = "invalid_output"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class LlmLimits:
    """Daily allowance."""

    max_requests_per_day: int = DEFAULT_MAX_REQUESTS_PER_DAY
    max_tokens_per_day: int = DEFAULT_MAX_TOKENS_PER_DAY


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One request as the budget saw it (never contains prompt text or keys)."""

    at: datetime
    purpose: str
    prompt_version: str
    model: str | None
    input_tokens: int
    output_tokens: int
    outcome: CallOutcome
    # HTTP requests behind this logical call (retries inside the client count).
    attempts: int = 1
    # True when the provider reported no usage and the tokens are a character-based guess.
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Usage of the current day."""

    day: date
    requests: int
    tokens: int
    logical_requests: int = 0


@dataclass(frozen=True, slots=True)
class Reservation:
    """A booked slice of today's allowance, to be settled or released exactly once."""

    day: date
    tokens: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LlmBudget:
    """Tracks today's usage and answers "may I make another call of this size?"."""

    def __init__(self, limits: LlmLimits = LlmLimits(), clock: Clock = _utc_now) -> None:  # noqa: B008
        """Start with an empty day and journal."""
        self._limits = limits
        self._clock = clock
        self._day = clock().date()
        self._requests = 0
        self._logical = 0
        self._tokens = 0
        self._journal: deque[JournalEntry] = deque(maxlen=JOURNAL_CAPACITY)

    def _roll_day(self) -> None:
        today = self._clock().date()
        if today != self._day:
            self._day, self._requests, self._logical, self._tokens = today, 0, 0, 0

    def _fits(self, estimated_tokens: int) -> bool:
        self._roll_day()
        fits = (
            self._requests < self._limits.max_requests_per_day
            and self._tokens + estimated_tokens <= self._limits.max_tokens_per_day
        )
        if not fits:
            logger.warning(
                "llm_budget_exhausted",
                requests=self._requests,
                tokens=self._tokens,
                estimated_tokens=estimated_tokens,
            )
        return fits

    def allow(self, estimated_tokens: int) -> bool:
        """Whether one more request of about ``estimated_tokens`` fits (does not book it)."""
        return self._fits(estimated_tokens)

    def reserve(self, estimated_tokens: int) -> Reservation | None:
        """Atomically check and book one request; ``None`` when it does not fit."""
        if not self._fits(estimated_tokens):
            return None
        self._requests += 1
        self._logical += 1
        self._tokens += estimated_tokens
        return Reservation(day=self._day, tokens=estimated_tokens)

    def settle(self, reservation: Reservation, entry: JournalEntry) -> None:
        """Replace the booked estimate with the real usage and journal the call.

        A reservation made on an earlier day is only journalled: its day is gone.
        """
        self._roll_day()
        if reservation.day == self._day:
            self._tokens += entry.input_tokens + entry.output_tokens - reservation.tokens
            self._requests += max(entry.attempts - 1, 0)
        self._journal.append(entry)

    def release(self, reservation: Reservation) -> None:
        """Give a booking back (the call never completed, e.g. the task was cancelled)."""
        self._roll_day()
        if reservation.day == self._day:
            self._requests = max(self._requests - 1, 0)
            self._logical = max(self._logical - 1, 0)
            self._tokens = max(self._tokens - reservation.tokens, 0)

    def record(self, entry: JournalEntry) -> None:
        """Count a finished request that was not reserved first, and journal it."""
        self._roll_day()
        self._requests += max(entry.attempts, 1)
        self._logical += 1
        self._tokens += entry.input_tokens + entry.output_tokens
        self._journal.append(entry)

    def snapshot(self) -> BudgetSnapshot:
        """Today's usage."""
        self._roll_day()
        return BudgetSnapshot(
            day=self._day,
            requests=self._requests,
            tokens=self._tokens,
            logical_requests=self._logical,
        )

    def journal(self) -> tuple[JournalEntry, ...]:
        """Most recent requests, oldest first."""
        return tuple(self._journal)

    def now(self) -> datetime:
        """The budget's clock, so journal timestamps match the day accounting."""
        return self._clock()
