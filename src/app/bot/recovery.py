"""Start-up recovery of conversations that were running when the bot stopped (QA F-1).

An analysis runs as a background task; a restart kills it, but the persistent FSM still says
"running", so the 🔍/▶️ buttons would answer "already running" for ever. At start-up those
states are cleared and each affected chat is told what happened and what to do next.
"""

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.base import BaseStorage

from app.bot import texts
from app.bot.messaging import send_html_to
from app.bot.storage import SqliteStorage
from app.logging import get_logger

logger = get_logger(__name__)


async def recover_running_analyses(bot: Bot, storage: BaseStorage) -> int:
    """Tell the users whose dead "running" state the storage cleared; returns how many were told.

    A message that cannot be sent (the user blocked the bot) is logged, never raised: the
    states are already cleared, so the user is not locked out either way.
    """
    if not isinstance(storage, SqliteStorage):
        return 0  # in-memory storage forgets everything on restart anyway
    chats = sorted(set(await storage.take_recovered()))
    told = 0
    for chat_id in chats:
        try:
            await send_html_to(bot, chat_id, texts.RESTART_RECOVERED)
            told += 1
        except TelegramAPIError as error:
            logger.warning("restart_notice_failed", chat_id=chat_id, error=str(error))
    if chats:
        logger.info("running_analyses_recovered", chats=len(chats), told=told)
    return told
