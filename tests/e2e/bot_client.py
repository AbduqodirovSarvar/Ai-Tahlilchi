"""QA doubles for driving the real bot dispatcher as one or several Telegram users.

Unlike ``tests.bot.fakes.BotHarness`` (one admin, strictly sequential), a :class:`Client` is
bound to a user *and* a chat, so several users, group chats and concurrent taps can share
one dispatcher. :class:`YieldingSession` yields to the event loop on every API call, the
way a real HTTP round trip does; without it, concurrent updates never interleave.
"""

import asyncio
import copy
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, override

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from app.bot.dispatcher import build_dispatcher
from app.bot.middleware import RateLimitMiddleware
from app.services.fake import create_fake_services
from app.services.interfaces import Services
from tests.bot.fakes import FIXED_NOW, RecordingSession, fixed_clock

CALLBACK_DATA_LIMIT = 64
TELEGRAM_TEXT_LIMIT = 4096


class YieldingSession(RecordingSession):
    """Records requests like :class:`RecordingSession`, but suspends like real I/O."""

    @override
    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        await asyncio.sleep(0)
        return await super().make_request(bot, method, timeout)


def make_dispatcher(admin_ids: frozenset[int], services: Services | None = None) -> Dispatcher:
    """The production dispatcher over fake services, fixed clock, no rate limiting."""
    return build_dispatcher(
        admin_ids,
        services or create_fake_services(clock=fixed_clock),
        clock=fixed_clock,
        rate_limit=RateLimitMiddleware(capacity=10_000),
    )


def services_of(dispatcher: Dispatcher) -> Services:
    """The ``Services`` bundle the dispatcher injects into handlers."""
    services: Services = dispatcher["services"]
    return services


@dataclass
class Client:
    """One Telegram user in one chat, talking to a shared dispatcher."""

    dispatcher: Dispatcher
    bot: Bot
    session: RecordingSession
    user_id: int
    chat_id: int | None = None
    chat_type: str = "private"
    texts: dict[int, str] = field(default_factory=dict)
    markups: dict[int, InlineKeyboardMarkup | None] = field(default_factory=dict)
    _update_id: int = 0

    def __post_init__(self) -> None:
        if self.chat_id is None:
            self.chat_id = self.user_id

    # ------------------------------------------------------------------ updates
    def _next_update_id(self) -> int:
        self._update_id += 1
        return self.user_id * 100_000 + self._update_id

    def _chat(self) -> Chat:
        assert self.chat_id is not None
        return Chat(id=self.chat_id, type=self.chat_type)

    def _user(self) -> User:
        return User(id=self.user_id, is_bot=False, first_name=f"U{self.user_id}")

    async def send(self, text: str) -> list[TelegramMethod[Any]]:
        """The user types ``text``; returns the API calls the bot made for this update."""
        update_id = self._next_update_id()
        message = Message(
            message_id=update_id,
            date=FIXED_NOW,
            chat=self._chat(),
            from_user=self._user(),
            text=text,
        )
        return await self._feed(Update(update_id=update_id, message=message))

    async def press_data(self, data: str, message_id: int) -> list[TelegramMethod[Any]]:
        """Tap a button with ``data`` on bot message ``message_id``."""
        update_id = self._next_update_id()
        message = Message(
            message_id=message_id,
            date=FIXED_NOW,
            chat=self._chat(),
            text=self.texts.get(message_id) or "x",
        )
        callback = CallbackQuery(
            id=f"cb{update_id}",
            from_user=self._user(),
            chat_instance="ci",
            message=message,
            data=data,
        )
        return await self._feed(Update(update_id=update_id, callback_query=callback))

    async def press(self, label: str) -> list[TelegramMethod[Any]]:
        """Tap the newest button whose text contains ``label``."""
        message_id, data = self.find_button(label)
        return await self.press_data(data, message_id)

    async def _feed(self, update: Update) -> list[TelegramMethod[Any]]:
        before = len(self.session.requests)
        bound = Update.model_validate(update.model_dump(), context={"bot": self.bot})
        await self.dispatcher.feed_update(self.bot, bound)
        # With concurrent clients other users' calls may interleave: keep ours only.
        new = [
            (index, method)
            for index, method in enumerate(self.session.requests[before:], start=before + 1)
            if self._mine(method, bound)
        ]
        self._track(new)
        return [method for _, method in new]

    def _mine(self, method: TelegramMethod[Any], update: Update) -> bool:
        if isinstance(method, AnswerCallbackQuery):
            callback = update.callback_query
            return callback is not None and method.callback_query_id == callback.id
        return getattr(method, "chat_id", None) == self.chat_id

    def _track(self, new: list[tuple[int, TelegramMethod[Any]]]) -> None:
        for index, method in new:
            if isinstance(method, SendMessage):
                self.texts[index] = method.text
                markup = method.reply_markup
                self.markups[index] = markup if isinstance(markup, InlineKeyboardMarkup) else None
            elif isinstance(method, EditMessageText) and method.message_id is not None:
                self.texts[method.message_id] = method.text
                markup = method.reply_markup
                self.markups[method.message_id] = (
                    markup if isinstance(markup, InlineKeyboardMarkup) else None
                )
            elif isinstance(method, EditMessageReplyMarkup) and method.message_id is not None:
                self.markups[method.message_id] = None

    # ------------------------------------------------------------------ what the user sees
    def find_button(self, label: str) -> tuple[int, str]:
        """``(message_id, callback_data)`` of the newest button containing ``label``."""
        for message_id in sorted(self.markups, reverse=True):
            for button in buttons_of(self.markups[message_id]):
                if label in button.text and button.callback_data:
                    return message_id, button.callback_data
        msg = f"no button containing {label!r}"
        raise AssertionError(msg)

    async def press_option(self, label: str) -> list[TelegramMethod[Any]]:
        """Tap the newest button labelled exactly ``label`` (a ✅/▫️ prefix is allowed)."""
        for message_id in sorted(self.markups, reverse=True):
            for button in buttons_of(self.markups[message_id]):
                text = button.text
                if button.callback_data and (text == label or text.endswith(f" {label}")):
                    return await self.press_data(button.callback_data, message_id)
        msg = f"no button labelled {label!r}"
        raise AssertionError(msg)

    def has_button(self, label: str) -> bool:
        """True when some live keyboard has a button containing ``label``."""
        try:
            self.find_button(label)
        except AssertionError:
            return False
        return True

    @property
    def last_message_id(self) -> int:
        """Id of the most recently sent or edited message of this chat."""
        return max(self.texts)

    @property
    def last_text(self) -> str:
        """Text of the most recently sent message (or the latest edit)."""
        return self.texts[self.last_message_id]

    def live_buttons(self, message_id: int) -> list[tuple[str, str]]:
        """``(text, callback_data)`` of every button on message ``message_id``."""
        return [
            (button.text, button.callback_data)
            for button in buttons_of(self.markups.get(message_id))
            if button.callback_data
        ]

    # ------------------------------------------------------------------ FSM
    def storage_key(self) -> StorageKey:
        """FSM key of this user in this chat."""
        assert self.chat_id is not None
        return StorageKey(bot_id=self.bot.id, chat_id=self.chat_id, user_id=self.user_id)

    async def state(self) -> str | None:
        """Current FSM state name."""
        return await self.dispatcher.storage.get_state(self.storage_key())


def buttons_of(markup: InlineKeyboardMarkup | None) -> list[Any]:
    """Flat list of inline buttons."""
    return [] if markup is None else [b for row in markup.inline_keyboard for b in row]


def answers(methods: list[TelegramMethod[Any]]) -> list[AnswerCallbackQuery]:
    """Callback answers among ``methods``."""
    return [method for method in methods if isinstance(method, AnswerCallbackQuery)]


def shown(methods: list[TelegramMethod[Any]]) -> list[str]:
    """Texts of sent and edited messages among ``methods``."""
    return [method.text for method in methods if isinstance(method, SendMessage | EditMessageText)]


def sent(methods: list[TelegramMethod[Any]]) -> list[SendMessage]:
    """Sent messages among ``methods``."""
    return [method for method in methods if isinstance(method, SendMessage)]


@dataclass
class Snapshot:
    """FSM storage and client view at one moment, to branch many taps from one screen."""

    storage: Any
    texts: dict[int, str]
    markups: dict[int, InlineKeyboardMarkup | None]

    @classmethod
    def take(cls, client: Client) -> "Snapshot":
        """Copy what a tap could change."""
        storage = client.dispatcher.storage
        assert isinstance(storage, MemoryStorage)
        return cls(copy.deepcopy(storage.storage), dict(client.texts), dict(client.markups))

    def restore(self, client: Client) -> None:
        """Put the copy back (services are not rolled back: taps stay read-mostly)."""
        storage = client.dispatcher.storage
        assert isinstance(storage, MemoryStorage)
        storage.storage = copy.deepcopy(self.storage)
        client.texts, client.markups = dict(self.texts), dict(self.markups)


# --------------------------------------------------------------------------- HTML checks
_ALLOWED_TAGS = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a"}
    | {"tg-spoiler", "span", "blockquote", "tg-emoji"}
)
_TOKEN = re.compile(
    r"<(?P<tag>/?[A-Za-z][A-Za-z-]*)(?:\s[^<>]*)?>"
    r"|&(?:lt|gt|amp|quot|#\d+|#x[0-9A-Fa-f]+);"
    r"|(?P<stray>[<>&])"
)


class _TagBalance(HTMLParser):
    """Stack-checks tags the way Telegram's HTML parse mode does (no implicit closing)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.problems: list[str] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALLOWED_TAGS:
            self.problems.append(f"unsupported tag <{tag}>")
        self.stack.append(tag)

    @override
    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"unbalanced </{tag}>")
            return
        self.stack.pop()


def telegram_html_problems(text: str) -> list[str]:
    """Why Telegram would reject ``text`` with ``parse_mode=HTML`` (empty list: valid)."""
    problems = [
        f"stray {match.group('stray')!r} at {match.start()}"
        for match in _TOKEN.finditer(text)
        if match.group("stray")
    ]
    if len(text) > TELEGRAM_TEXT_LIMIT:
        problems.append(f"too long: {len(text)}")
    parser = _TagBalance()
    parser.feed(text)
    parser.close()
    problems.extend(parser.problems)
    problems.extend(f"unclosed <{tag}>" for tag in parser.stack)
    return problems
