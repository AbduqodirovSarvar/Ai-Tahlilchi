"""Running a confirmed analysis: live progress message, then result pages.

Shared with the card/pagination handlers: :func:`send_result_page` renders one page of a
stored result.
"""

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from app.bot import keyboards, texts
from app.bot.draft import WizardDraft
from app.bot.formatters import (
    format_results_summary,
    ordered_for_display,
    result_page,
    window_label,
)
from app.bot.jobs import BackgroundJobs
from app.bot.messaging import edit_html, send_html
from app.bot.middleware import ErrorReply, error_reply
from app.bot.session import WizardSession
from app.bot.states import AnalysisFlow, drop_draft, load_draft, next_results
from app.bot.wizard import render, summary_fields
from app.logging import get_logger
from app.services.errors import ServiceError
from app.services.interfaces import (
    AnalysisProgress,
    AnalysisQuery,
    AnalysisResult,
    AnalysisStage,
    SignalView,
)

logger = get_logger(__name__)

RESULTS_PAGE_SIZE: Final = 5
"""Cards per page: each card is its own message (it carries its own buttons)."""
PROGRESS_MIN_INTERVAL: Final = 1.5
"""Seconds between progress edits; Telegram throttles frequent edits of one message."""

type Monotonic = Callable[[], float]


class ProgressReporter:
    """``ProgressCallback`` that edits the "⏳ Tahlil qilinmoqda… n/m" message.

    Edits are throttled and never raise: a failed edit must not abort the analysis.
    """

    def __init__(
        self,
        message: Message,
        monotonic: Monotonic = time.monotonic,
        *,
        stages: Mapping[str, str] = texts.PROGRESS_STAGES,
    ) -> None:
        """``message`` is the progress message to edit; ``stages`` are its texts per stage."""
        self._message = message
        self._stages = stages
        self._monotonic = monotonic
        self._last_text = message.text or ""
        self._last_edit = monotonic()

    async def __call__(self, progress: AnalysisProgress) -> None:
        """Show ``progress`` unless an edit happened very recently (final stage always)."""
        template = self._stages[progress.stage.value]
        text = template.format(done=progress.done, total=progress.total)
        final = progress.stage is AnalysisStage.FINISHED
        now = self._monotonic()
        if text == self._last_text or (not final and now - self._last_edit < PROGRESS_MIN_INTERVAL):
            return
        try:
            await edit_html(self._message, text)
        except TelegramAPIError as error:
            logger.debug("progress_edit_failed", error=error.message)
            return
        self._last_text, self._last_edit = text, now


@dataclass(frozen=True, slots=True)
class _Run:
    """What the background analysis needs; everything else is looked up under the lock."""

    session: WizardSession
    jobs: BackgroundJobs
    query: AnalysisQuery
    draft: WizardDraft
    progress_message: Message

    @property
    def wizard_message(self) -> Message:
        """The message the wizard is shown in (results are answered to its chat)."""
        return self.session.message


async def run_analysis(session: WizardSession, jobs: BackgroundJobs) -> None:
    """▶️ Boshlash: mark the user as running, then analyse in the background.

    Runs inside the user's update lock, so check-and-set of ``running`` is atomic: a second
    tap queues behind this handler and then finds the user already running. The analysis
    itself must not hold the lock (a long run would freeze every other button), so it is a
    background job that reports through the progress message.
    """
    draft = session.draft
    query = draft.to_query()
    await session.answer()
    if await session.state.get_state() == AnalysisFlow.running.state:
        await session.answer(texts.ALREADY_RUNNING, alert=True)
        return
    await session.state.set_state(AnalysisFlow.running)
    wizard_message = session.message
    summary = texts.WIZARD_RUN_SUMMARY.format(
        **summary_fields(draft, window_label(query.dates.resolve(session.ctx.today)))
    )
    await edit_html(wizard_message, f"{summary}\n{texts.WIZARD_STARTED}")
    progress_message = await send_html(wizard_message, texts.PROGRESS.format(done=0, total="…"))
    run = _Run(session, jobs, query, draft, progress_message)
    jobs.spawn(_execute(run), name=f"analysis:{session.ctx.user_id}")


async def _execute(run: _Run) -> None:
    """The analysis job: run, then show the results or explain the failure."""
    session = run.session
    reporter = ProgressReporter(run.progress_message)
    try:
        result = await session.ctx.services.analysis.analyze(
            session.ctx.user_id, run.query, progress=reporter
        )
        await _deliver(run, result, reporter)
    except ServiceError as error:
        logger.info("analysis_rejected", error_type=type(error).__name__, detail=str(error))
        reply = await error_reply(error, session.ctx.services, session.ctx.user_id)
        await _abort(run, reply, progress_text=texts.CANCELLED)
    except Exception:
        # Whatever it was, the user must not stay "running" under a frozen progress message.
        logger.exception("analysis_failed")
        await _abort(run, ErrorReply(texts.UNEXPECTED_ERROR), progress_text=texts.UNEXPECTED_ERROR)


async def _deliver(run: _Run, result: AnalysisResult, reporter: ProgressReporter) -> None:
    """Close the wizard, remember the result for paging and send summary and first page."""
    session = run.session
    ordered = ordered_for_display(result.signals, session.ctx.zone)
    async with run.jobs.lock(session.state.key):
        await _finish_wizard(session, run.draft.message_id)
        stored = await next_results(session.state, tuple(s.signal_id for s in ordered))
    done = result.fixtures_analyzed
    # No-op when the service already reported the final stage.
    await reporter(AnalysisProgress(stage=AnalysisStage.FINISHED, done=done, total=done))
    markup = (
        keyboards.recalc_query_keyboard(result.query_id)
        if result.signals and not result.retro
        else None
    )
    await send_html(run.wizard_message, format_results_summary(result), markup)
    await send_result_page(run.wizard_message, ordered, session.ctx.zone, 0, stored.token)


async def _abort(run: _Run, reply: ErrorReply, *, progress_text: str) -> None:
    """Close the progress message, give the user back the confirmation, explain the error."""
    await _quietly(edit_html(run.progress_message, progress_text))
    session = run.session
    async with run.jobs.lock(session.state.key):
        # A wizard the user started meanwhile is theirs now: leave it alone.
        if await _is_current_wizard(session, run.draft.message_id):
            await _restore_confirmation(run)
    await _quietly(send_html(run.wizard_message, reply.text, reply.markup))


async def _restore_confirmation(run: _Run) -> None:
    """Show the confirmation again so the user can narrow the query and retry."""
    session = run.session
    try:
        await session.show(await render("confirm", run.draft, session.ctx))
    except Exception:
        logger.exception("confirmation_restore_failed")
        await drop_draft(session.state)


async def _quietly(action: Awaitable[object]) -> None:
    """Best-effort Telegram call for the failure path (the user may have blocked the bot)."""
    try:
        await action
    except TelegramAPIError as error:
        logger.warning("analysis_cleanup_failed", error=error.message)


async def _is_current_wizard(session: WizardSession, message_id: int | None) -> bool:
    """The stored draft is the one shown in ``message_id`` (no newer wizard replaced it)."""
    try:
        current = await load_draft(session.state)
    except LookupError:
        return False
    return current.message_id == message_id


async def _finish_wizard(session: WizardSession, message_id: int | None) -> None:
    """Drop the draft unless the user already started a new wizard meanwhile."""
    if await _is_current_wizard(session, message_id):
        await drop_draft(session.state)


async def send_result_page(
    message: Message, signals: Sequence[SignalView], zone: ZoneInfo, page: int, token: int
) -> None:
    """Day headers and cards of one page, then "➡️ Yana N ta" while more remain.

    ``token`` is put on the "➡️ Yana" button so it can only continue this very result.
    """
    for item in result_page(signals, zone, page, RESULTS_PAGE_SIZE):
        markup = None if item.signal is None else keyboards.signal_keyboard(item.signal)
        await send_html(message, item.text, markup)
    shown = min((page + 1) * RESULTS_PAGE_SIZE, len(signals))
    remaining = len(signals) - shown
    if remaining > 0:
        await send_html(
            message,
            texts.RESULTS_MORE.format(shown=shown, total=len(signals)),
            keyboards.more_results_keyboard(page + 1, min(RESULTS_PAGE_SIZE, remaining), token),
        )
    elif page > 0:
        await send_html(message, texts.RESULTS_END.format(total=len(signals)))
