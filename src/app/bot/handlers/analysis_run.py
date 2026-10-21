"""Running a confirmed analysis: live progress message, then result pages.

Shared with the card/pagination handlers: :func:`send_result_page` renders one page of a
stored result.
"""

import time
from collections.abc import Callable, Sequence
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from app.bot import keyboards, texts
from app.bot.formatters import (
    format_results_summary,
    ordered_for_display,
    result_page,
    window_label,
)
from app.bot.messaging import edit_html, send_html
from app.bot.session import WizardSession
from app.bot.states import AnalysisFlow, StoredResults, drop_draft, load_draft, save_results
from app.bot.wizard import render, summary_fields
from app.logging import get_logger
from app.services.errors import ServiceError
from app.services.interfaces import AnalysisProgress, AnalysisStage, SignalView

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

    def __init__(self, message: Message, monotonic: Monotonic = time.monotonic) -> None:
        """``message`` is the progress message to edit."""
        self._message = message
        self._monotonic = monotonic
        self._last_text = message.text or ""
        self._last_edit = monotonic()

    async def __call__(self, progress: AnalysisProgress) -> None:
        """Show ``progress`` unless an edit happened very recently (final stage always)."""
        template = texts.PROGRESS_STAGES[progress.stage.value]
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


async def run_analysis(session: WizardSession) -> None:
    """▶️ Boshlash: run the query with live progress, then show the results."""
    draft = session.draft
    query = draft.to_query()
    await session.answer()
    await session.state.set_state(AnalysisFlow.running)
    wizard_message = session.message
    summary = texts.WIZARD_CONFIRM.split("\n", 1)[0].format(
        **summary_fields(draft, window_label(query.dates.resolve(session.ctx.today)))
    )
    await edit_html(wizard_message, f"{summary}\n{texts.WIZARD_STARTED}")
    progress_message = await send_html(wizard_message, texts.PROGRESS.format(done=0, total="…"))
    services, user_id = session.ctx.services, session.ctx.user_id
    reporter = ProgressReporter(progress_message)
    try:
        result = await services.analysis.analyze(user_id, query, progress=reporter)
    except ServiceError:
        # Back to the confirmation so the user can narrow the query; the error middleware
        # then explains what went wrong.
        await edit_html(progress_message, texts.CANCELLED)
        await session.show(await render("confirm", draft, session.ctx))
        raise
    await _finish_wizard(session, draft.message_id)
    done = result.fixtures_analyzed
    # No-op when the service already reported the final stage.
    await reporter(AnalysisProgress(stage=AnalysisStage.FINISHED, done=done, total=done))
    await send_html(wizard_message, format_results_summary(result))
    ordered = ordered_for_display(result.signals, session.ctx.zone)
    await save_results(session.state, StoredResults(tuple(s.signal_id for s in ordered)))
    await send_result_page(wizard_message, ordered, session.ctx.zone, 0)


async def _finish_wizard(session: WizardSession, message_id: int | None) -> None:
    """Drop the draft unless the user already started a new wizard meanwhile."""
    try:
        current = await load_draft(session.state)
    except LookupError:
        return
    if current.message_id == message_id:
        await drop_draft(session.state)


async def send_result_page(
    message: Message, signals: Sequence[SignalView], zone: ZoneInfo, page: int
) -> None:
    """Day headers and cards of one page, then "➡️ Yana N ta" while more remain."""
    for item in result_page(signals, zone, page, RESULTS_PAGE_SIZE):
        markup = (
            None
            if item.signal is None
            else keyboards.card_keyboard(item.signal.signal_id, retro=item.signal.retro)
        )
        await send_html(message, item.text, markup)
    shown = min((page + 1) * RESULTS_PAGE_SIZE, len(signals))
    remaining = len(signals) - shown
    if remaining > 0:
        await send_html(
            message,
            texts.RESULTS_MORE.format(shown=shown, total=len(signals)),
            keyboards.more_results_keyboard(page + 1, min(RESULTS_PAGE_SIZE, remaining)),
        )
    elif page > 0:
        await send_html(message, texts.RESULTS_END.format(total=len(signals)))
