"""Per-update wizard session: the glue that keeps analysis handlers to a few lines.

:class:`WizardSessionMiddleware` (inner, on the analysis router) builds a
:class:`WizardSession` for every matched update: the user's context, the stored draft and
the message to edit. For button presses it also rejects *stale* buttons — pressed on an old
wizard message, or after the draft was lost (bot restart, cancelled flow) — and it makes
sure every callback query is answered exactly once.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot import texts
from app.bot.draft import StaleDraftError, WizardDraft
from app.bot.messaging import answer_callback, callback_message, drop_keyboard, edit_html, send_html
from app.bot.states import drop_draft, load_draft, save_draft
from app.bot.wizard import Screen, WizardContext, render
from app.services.interfaces import Services

type Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
type Clock = Callable[[], datetime]


class WizardSession:
    """What an analysis handler works with: context, draft and how to show the next step."""

    def __init__(
        self,
        ctx: WizardContext,
        state: FSMContext,
        event: Message | CallbackQuery,
        draft: WizardDraft | None,
    ) -> None:
        """Built by :class:`WizardSessionMiddleware`; handlers never construct it."""
        self.ctx = ctx
        self.state = state
        self.event = event
        self._draft = draft
        self.answered = False

    @property
    def draft(self) -> WizardDraft:
        """The stored draft (raises :class:`StaleDraftError` when there is none)."""
        if self._draft is None:
            raise StaleDraftError
        return self._draft

    @property
    def message(self) -> Message:
        """The user's message, or the wizard message a button belongs to."""
        if isinstance(self.event, Message):
            return self.event
        message = callback_message(self.event)
        if message is None:
            raise StaleDraftError
        return message

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        """Answer the button press (no-op for text messages and on repeat calls)."""
        if isinstance(self.event, CallbackQuery) and not self.answered:
            self.answered = True
            await answer_callback(self.event, text, alert=alert)

    async def go(self, step: str, draft: WizardDraft, notice: str | None = None) -> None:
        """Render ``step`` and show it.

        A button press edits the wizard message; typed input gets a fresh message (and the
        old message loses its keyboard, so it cannot be pressed later).
        """
        screen = await render(step, draft, self.ctx, notice)
        await self.answer()
        await self.show(screen)

    async def show(self, screen: Screen) -> None:
        """Display an already rendered screen and store its draft and state."""
        message = self.message
        if isinstance(self.event, CallbackQuery):
            await edit_html(message, screen.text, screen.markup)
            message_id = message.message_id
        else:
            if message.bot is not None and self._draft is not None:
                await drop_keyboard(message.bot, message.chat.id, self._draft.message_id)
            sent = await send_html(message, screen.text, screen.markup)
            message_id = sent.message_id
        draft = WizardDraft.from_data({**screen.draft.to_data(), "message_id": message_id})
        self._draft = draft
        await save_draft(self.state, draft)
        await self.state.set_state(screen.state)

    async def cancel(self) -> None:
        """Leave the wizard; the wizard message loses its keyboard."""
        await drop_draft(self.state)
        await self.answer()
        if isinstance(self.event, CallbackQuery):
            await edit_html(self.message, texts.CANCELLED)
        else:
            await send_html(self.message, texts.CANCELLED)


class WizardSessionMiddleware(BaseMiddleware):
    """Inject ``session`` into analysis handlers; reject stale buttons."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Build the session, run the handler, answer the callback if it did not."""
        if not isinstance(event, Message | CallbackQuery):  # pragma: no cover - not registered
            return await handler(event, data)
        services: Services = data["services"]
        clock: Clock = data["clock"]
        user_id: int = data["user_id"]
        state: FSMContext = data["state"]
        settings = await services.settings.get_settings(user_id)
        ctx = WizardContext(
            services=services,
            user_id=user_id,
            zone=settings.zone,
            now=clock(),
            notices=data["notices"],
        )
        try:
            draft: WizardDraft | None = await load_draft(state)
        except StaleDraftError:
            draft = None
        if isinstance(event, CallbackQuery) and not _is_current(event, draft):
            await answer_callback(event, texts.STALE_BUTTON, alert=True)
            return None
        session = WizardSession(ctx, state, event, draft)
        data["session"] = session
        result = await handler(event, data)
        # On failure the error middleware answers instead (with the error as an alert).
        await session.answer()
        return result


def _is_current(callback: CallbackQuery, draft: WizardDraft | None) -> bool:
    """The button belongs to the wizard message the stored draft is shown in."""
    message = callback_message(callback)
    return draft is not None and message is not None and message.message_id == draft.message_id
