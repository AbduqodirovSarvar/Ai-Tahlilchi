"""Offline Telegram test doubles: a session that records requests instead of sending them,
update builders and a harness that drives the real dispatcher like a user would.
"""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Iterable
from datetime import UTC, datetime
from typing import Any, override

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from app.bot.dispatcher import build_dispatcher
from app.bot.jobs import BackgroundJobs
from app.bot.middleware import RateLimitMiddleware
from app.services.fake import create_fake_services
from app.services.interfaces import Services

ADMIN_ID = 1001
STRANGER_ID = 2002
FIXED_NOW = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
"""Wednesday 21.10.2026, 11:00 in Tashkent."""


def fixed_clock() -> datetime:
    """Deterministic clock shared by the bot and the fake services."""
    return FIXED_NOW


class RecordingSession(BaseSession):
    """Session that stores every API call and answers with a plausible result."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    @override
    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        self.requests.append(method)
        return Message(
            message_id=len(self.requests),
            date=datetime.now(UTC),
            chat=Chat(id=getattr(method, "chat_id", 0) or 0, type="private"),
            text=getattr(method, "text", None),
        ).as_(bot)

    @override
    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise NotImplementedError
        yield b""

    @override
    async def close(self) -> None:
        return None


class ScriptedSession(RecordingSession):
    """Fails the first requests as scripted (``None`` entries succeed), then behaves normally."""

    def __init__(self, script: Iterable[Exception | None] = ()) -> None:
        super().__init__()
        self.script: deque[Exception | None] = deque(script)

    @override
    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        failure = self.script.popleft() if self.script else None
        if failure is not None:
            self.requests.append(method)
            raise failure
        return await super().make_request(bot, method, timeout)


async def feed(dispatcher: Dispatcher, bot: Bot, update: Update) -> None:
    """Deliver ``update`` and wait for the background jobs it started (an analysis run).

    Jobs that were already running before (a blocked analysis in a concurrency test) are
    not waited for, so a later tap is still delivered while they run.
    """
    jobs: BackgroundJobs = dispatcher["jobs"]
    before = jobs.tasks
    await dispatcher.feed_update(bot, update)
    await asyncio.gather(*(jobs.tasks - before))


def _user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def text_update(user_id: int, text: str, update_id: int = 1) -> Update:
    """Build a private-chat text message update as Telegram would deliver it."""
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=_user(user_id),
        text=text,
    )
    return Update(update_id=update_id, message=message)


def bind(update: Update, bot: Bot) -> Update:
    """``update`` with every nested object mounted to ``bot`` (as aiogram does on receive)."""
    return Update.model_validate(update.model_dump(), context={"bot": bot})


def callback_update(
    user_id: int, data: str, message_id: int, text: str = "", update_id: int = 1
) -> Update:
    """Build a button press on bot message ``message_id``."""
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        text=text or "x",
    )
    callback = CallbackQuery(
        id=f"cb{update_id}",
        from_user=_user(user_id),
        chat_instance="ci",
        message=message,
        data=data,
    )
    return Update(update_id=update_id, callback_query=callback)


class BotHarness:
    """Drives the assembled dispatcher and tracks what the user would see.

    Message ids follow :class:`RecordingSession`: a sent message gets the index of its
    request (1-based); edits keep the id they target.
    """

    def __init__(self, bot: Bot, session: RecordingSession, services: Services | None = None):
        self.bot = bot
        self.session = session
        self.services = services or create_fake_services(clock=fixed_clock)
        self.dispatcher = build_dispatcher(
            frozenset({ADMIN_ID}),
            self.services,
            clock=fixed_clock,
            rate_limit=RateLimitMiddleware(capacity=10_000),
        )
        self.update_id = 0
        self.seen = 0
        self.texts: dict[int, str] = {}
        self.markups: dict[int, InlineKeyboardMarkup | None] = {}

    def restart(self) -> None:
        """Simulate a bot restart: a new dispatcher with empty in-memory FSM storage."""
        self.dispatcher = build_dispatcher(
            frozenset({ADMIN_ID}),
            self.services,
            clock=fixed_clock,
            rate_limit=RateLimitMiddleware(capacity=10_000),
        )

    def _track(self) -> list[TelegramMethod[Any]]:
        new = self.session.requests[self.seen :]
        for offset, method in enumerate(new, start=self.seen + 1):
            if isinstance(method, SendMessage):
                self.texts[offset] = method.text
                markup = method.reply_markup
                self.markups[offset] = markup if isinstance(markup, InlineKeyboardMarkup) else None
            elif isinstance(method, EditMessageText) and method.message_id is not None:
                self.texts[method.message_id] = method.text
                markup = method.reply_markup
                self.markups[method.message_id] = (
                    markup if isinstance(markup, InlineKeyboardMarkup) else None
                )
            elif isinstance(method, EditMessageReplyMarkup) and method.message_id is not None:
                self.markups[method.message_id] = None
        self.seen = len(self.session.requests)
        return new

    async def send(self, text: str) -> list[TelegramMethod[Any]]:
        """The user types ``text``; returns the API calls the bot made."""
        self.update_id += 1
        await feed(self.dispatcher, self.bot, text_update(ADMIN_ID, text, self.update_id))
        return self._track()

    def find_button(self, label: str) -> tuple[int, str]:
        """``(message_id, callback_data)`` of the newest button whose text contains ``label``."""
        for message_id in sorted(self.markups, reverse=True):
            markup = self.markups[message_id]
            buttons = [] if markup is None else [b for row in markup.inline_keyboard for b in row]
            for button in buttons:
                if label in button.text and button.callback_data:
                    return message_id, button.callback_data
        msg = f"no button containing {label!r}"
        raise AssertionError(msg)

    async def press(self, label: str) -> list[TelegramMethod[Any]]:
        """The user taps the newest button containing ``label``."""
        message_id, data = self.find_button(label)
        return await self.press_data(data, message_id)

    async def press_data(self, data: str, message_id: int) -> list[TelegramMethod[Any]]:
        """Tap a button with explicit callback data on message ``message_id``."""
        self.update_id += 1
        update = callback_update(
            ADMIN_ID, data, message_id, self.texts.get(message_id, ""), self.update_id
        )
        await feed(self.dispatcher, self.bot, update)
        return self._track()

    @property
    def last_text(self) -> str:
        """Text of the most recently sent or edited message."""
        for method in reversed(self.session.requests):
            if isinstance(method, SendMessage | EditMessageText):
                return method.text
        return ""


def answers(methods: list[TelegramMethod[Any]]) -> list[AnswerCallbackQuery]:
    """Callback answers among ``methods``."""
    return [method for method in methods if isinstance(method, AnswerCallbackQuery)]


def sent_texts(methods: list[TelegramMethod[Any]]) -> list[str]:
    """Texts of sent and edited messages among ``methods``."""
    return [method.text for method in methods if isinstance(method, SendMessage | EditMessageText)]
