"""Start-up greeting to the admins: proof that the bot is up, which build and which backend."""

from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.bot import texts
from app.bot.messaging import send_html_to
from app.logging import get_logger

logger = get_logger(__name__)


async def announce_startup(bot: Bot, admin_ids: Iterable[int], *, build: str, backend: str) -> int:
    """Tell every admin the bot has started; returns how many were told.

    A failure for one admin (they never pressed /start, or blocked the bot) is logged and
    never raised: a greeting must not keep the bot from polling.
    """
    told = 0
    text = texts.STARTUP_NOTICE.format(build=build, backend=backend)
    for admin_id in sorted(admin_ids):
        try:
            await send_html_to(bot, admin_id, text)
            told += 1
        except TelegramAPIError as error:
            logger.warning("startup_notice_failed", user_id=admin_id, error=str(error))
    logger.info("startup_notice_sent", told=told)
    return told
