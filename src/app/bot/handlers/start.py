"""``/start``: liveness check proving the whole chain (Telegram -> bot -> reply) works."""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.bot import texts


async def handle_start(message: Message) -> None:
    """Reply with a fixed confirmation text."""
    await message.answer(texts.START_OK)


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="start")
    router.message.register(handle_start, CommandStart())
    return router
