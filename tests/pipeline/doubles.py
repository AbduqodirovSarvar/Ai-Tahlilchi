"""Test doubles of the pipeline's ports shared by the feedback-loop tests."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.db.repo.records import SavedQueryDraft
from app.llm.inputs import PostmortemInput
from app.llm.schemas import PostmortemCategory, PostmortemResult
from app.pipeline.delivery import DailyDelivery, NullDelivery
from app.pipeline.ledger import LedgerKey


class Crash(BaseException):
    """Raised to model the process dying at a precise point."""


@dataclass
class RecordingDelivery(NullDelivery):
    """Records every digest-style message; ``fail_if`` may raise for chosen messages.

    ``answers`` scripts the confirmation of the next digests (a string reference, ``None`` for
    "not delivered"); once empty every digest is confirmed.
    """

    digests: list[tuple[int, DailyDelivery, LedgerKey]] = field(default_factory=list)
    alerts: list[tuple[str, str]] = field(default_factory=list)
    fail_if: Callable[[DailyDelivery], BaseException | None] = lambda _message: None
    answers: list[str | None] = field(default_factory=list)

    async def daily(self, user_id: int, message: DailyDelivery, key: LedgerKey) -> str | None:
        """Record the digest, then fail, answer ``None`` or confirm it as scripted."""
        error = self.fail_if(message)
        if error is not None:
            raise error
        self.digests.append((user_id, message, key))
        return self.answers.pop(0) if self.answers else f"{user_id}:{len(self.digests)}"

    async def alert_admin(self, component: str, message: str, at: datetime) -> None:  # noqa: ARG002
        """Record the alert."""
        self.alerts.append((component, message))

    def labels(self) -> list[str | None]:
        """The template labels of the recorded digests, in order."""
        return [message.label for _, message, _ in self.digests]


def draft_query(**changes: object) -> SavedQueryDraft:
    """A valid saved query (today's matches of all leagues) with ``changes`` applied."""
    from app.db.enums import DateMode, QueryScope, SortOrder  # noqa: PLC0415

    values: dict[str, object] = {
        "user_id": 1,
        "name": "Shablon",
        "date_mode": DateMode.TODAY,
        "days": None,
        "date_from": None,
        "date_to": None,
        "sports": ("football",),
        "min_prob": 0.30,
        "min_ev": 0.0,
        "scope": QueryScope.ALL,
        "scope_ids": (),
        "markets": ("1x2", "totals"),
        "max_signals": 10,
        "sort": SortOrder.EV,
    }
    values.update(changes)
    return SavedQueryDraft(**values)  # type: ignore[arg-type]


@dataclass
class ScriptedAnalyst:
    """A post-mortem analyst that answers from a script and records the cases it was shown.

    ``script`` items: a :class:`PostmortemResult`, ``None`` (no answer) or an exception to
    raise. When the script is exhausted ``default`` answers.
    """

    script: list[PostmortemResult | BaseException | None] = field(default_factory=list)
    default: PostmortemResult | None = None
    cases: list[PostmortemInput] = field(default_factory=list)

    async def classify(self, case: PostmortemInput) -> PostmortemResult | None:
        """Record ``case`` and give the next scripted answer."""
        self.cases.append(case)
        answer = self.script.pop(0) if self.script else self.default
        if isinstance(answer, BaseException):
            raise answer
        return answer


def verdict(
    category: PostmortemCategory = PostmortemCategory.MISSING_INFO,
    reason: str = "Asosiy hujumchi o'ynamagan.",
) -> PostmortemResult:
    """A post-mortem answer."""
    return PostmortemResult(
        category=category, reason=reason, prompt_version="postmortem.v2", model="test"
    )


def ids(items: Sequence[tuple[int, int]]) -> list[int]:
    """The second element of each ``(user, template)`` pair."""
    return [second for _, second in items]
