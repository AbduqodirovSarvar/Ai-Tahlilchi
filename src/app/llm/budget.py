"""Daily request/token budget for the free Gemini tier, plus an in-memory request journal.

The guard is deliberately conservative: it counts every request that reached the API (also
failed ones) and compares *estimated* next-call tokens against the remaining allowance, so
the free quota is not exhausted by a retry storm. The day rolls over at UTC midnight; the
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


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Usage of the current day."""

    day: date
    requests: int
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
        self._tokens = 0
        self._journal: deque[JournalEntry] = deque(maxlen=JOURNAL_CAPACITY)

    def _roll_day(self) -> None:
        today = self._clock().date()
        if today != self._day:
            self._day, self._requests, self._tokens = today, 0, 0

    def allow(self, estimated_tokens: int) -> bool:
        """Whether one more request of about ``estimated_tokens`` fits today's allowance."""
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

    def record(self, entry: JournalEntry) -> None:
        """Count a finished request and keep it in the journal."""
        self._roll_day()
        self._requests += 1
        self._tokens += entry.input_tokens + entry.output_tokens
        self._journal.append(entry)

    def snapshot(self) -> BudgetSnapshot:
        """Today's usage."""
        self._roll_day()
        return BudgetSnapshot(day=self._day, requests=self._requests, tokens=self._tokens)

    def journal(self) -> tuple[JournalEntry, ...]:
        """Most recent requests, oldest first."""
        return tuple(self._journal)

    def now(self) -> datetime:
        """The budget's clock, so journal timestamps match the day accounting."""
        return self._clock()
