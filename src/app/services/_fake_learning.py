"""In-memory learning journal behind :class:`FakeLearningService` (contract v1.8)."""

from dataclasses import replace

from app.services.interfaces import (
    DEFAULT_PAGE_SIZE,
    LearningChangeView,
    LearningMode,
    LearningPage,
    RollbackOutcome,
)

FAKE_HOLD_DAYS = 30


class FakeLearningState:
    """Journal, mode and the set of entries whose rollback is refused as stale."""

    def __init__(self) -> None:
        """Start in shadow mode with an empty journal."""
        self.mode: LearningMode = LearningMode.SHADOW
        self.missed: bool = False
        self.journal: list[LearningChangeView] = []
        self.stale: set[int] = set()

    def add(self, change: LearningChangeView) -> None:
        """Append an entry (tests build the journal they need)."""
        self.journal.append(change)


class FakeLearningService:
    """Fake :class:`~app.services.interfaces.LearningService`."""

    def __init__(self, state: FakeLearningState) -> None:
        """Share the journal with the fake system status (tests seed ``state``)."""
        self.state = state

    async def list_changes(
        self,
        user_id: int,  # noqa: ARG002 - the journal is system-wide
        *,
        page: int = 0,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> LearningPage:
        """See :meth:`LearningService.list_changes`."""
        ordered = sorted(self.state.journal, key=lambda c: (c.at, c.change_id), reverse=True)
        count = max(1, -(-len(ordered) // page_size))
        number = min(max(page, 0), count - 1)
        items = tuple(ordered[number * page_size : (number + 1) * page_size])
        return LearningPage(
            items=items,
            page=number,
            page_count=count,
            total=len(ordered),
            mode=self.state.mode,
            hold_days=FAKE_HOLD_DAYS,
        )

    async def rollback_change(
        self,
        user_id: int,  # noqa: ARG002 - the journal is system-wide
        change_id: int,
    ) -> RollbackOutcome:
        """See :meth:`LearningService.rollback_change`."""
        for index, change in enumerate(self.state.journal):
            if change.change_id != change_id:
                continue
            if change.rolled_back:
                return RollbackOutcome.ALREADY_ROLLED_BACK
            if not change.undoable:
                return RollbackOutcome.NOT_UNDOABLE
            if change_id in self.state.stale:
                return RollbackOutcome.STALE
            self.state.journal[index] = replace(change, rolled_back=True)
            return RollbackOutcome.ROLLED_BACK
        return RollbackOutcome.NOT_FOUND
