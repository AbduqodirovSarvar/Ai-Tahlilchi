"""Real learning journal service (contract v1.8): ``/ozgarishlar`` over ``learning_changes``."""

from typing import Any, Final

from app.logging import get_logger
from app.pipeline import data
from app.pipeline.learning import rollback_change
from app.services.interfaces import (
    DEFAULT_PAGE_SIZE,
    LearningChangeKind,
    LearningChangeView,
    LearningMode,
    LearningPage,
    RollbackOutcome,
)
from app.services.real.core import Core

logger = get_logger(__name__)

MAX_JOURNAL_ENTRIES: Final = 500
"""Entries read per request: the loop writes a handful a week, so this is years of history."""
SECONDS_PER_DAY: Final = 86_400


def change_view(change: data.LearningChange) -> LearningChangeView | None:
    """A journal row as a DTO; a row of a kind this version does not know is skipped."""
    try:
        kind = LearningChangeKind(change.kind)
    except ValueError:
        logger.warning("learning_change_kind_unknown", change_id=change.id, kind=change.kind)
        return None
    after: dict[str, Any] = change.after
    evidence = after.get("evidence")
    samples = after.get("n_samples")
    if samples is None and isinstance(evidence, dict):
        samples = evidence.get("n")
    return LearningChangeView(
        change_id=change.id,
        at=change.created_at,
        kind=kind,
        subject=change.subject,
        reason=change.reason,
        applied=change.applied,
        rolled_back=change.rolled_back,
        ev_floor=after.get("ev_threshold"),
        version=after.get("version"),
        samples=samples if isinstance(samples, int) else None,
    )


def mode_of(core: Core) -> LearningMode:
    """The configured mode as the service-level enum."""
    return LearningMode(core.ctx.learning_settings.mode.value)


class RealLearningService:
    """:class:`~app.services.interfaces.LearningService`."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def list_changes(
        self,
        user_id: int,  # noqa: ARG002 - the journal is system-wide
        *,
        page: int = 0,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> LearningPage:
        """See :meth:`LearningService.list_changes`."""
        async with self._core.ctx.sessions() as session:
            rows = await data.list_learning_changes(session, limit=MAX_JOURNAL_ENTRIES)
        views = [view for view in map(change_view, rows) if view is not None]
        count = max(1, -(-len(views) // page_size))
        number = min(max(page, 0), count - 1)
        hold = self._core.ctx.learning_settings.manual_hold
        return LearningPage(
            items=tuple(views[number * page_size : (number + 1) * page_size]),
            page=number,
            page_count=count,
            total=len(views),
            mode=mode_of(self._core),
            hold_days=int(hold.total_seconds() // SECONDS_PER_DAY),
        )

    async def rollback_change(
        self,
        user_id: int,
        change_id: int,
    ) -> RollbackOutcome:
        """See :meth:`LearningService.rollback_change`."""
        result = await rollback_change(self._core.ctx, change_id)
        logger.info(
            "learning_rollback_requested", user_id=user_id, change_id=change_id, result=result
        )
        return RollbackOutcome(result.value)
