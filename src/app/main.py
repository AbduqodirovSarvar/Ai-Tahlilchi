"""Process entrypoint: ``python -m app.main``.

Composition root: loads settings, configures logging, validates what the bot needs and runs
long polling until SIGINT/SIGTERM (aiogram stops polling and closes the HTTP session).
"""

import asyncio
import sys

from aiogram import Bot

from app.bot.dispatcher import build_dispatcher
from app.config import Settings, get_settings
from app.errors import ConfigurationError
from app.logging import configure_logging, get_logger

# Not `__name__`: under `python -m app.main` that would be "__main__".
logger = get_logger("app.main")

EXIT_CONFIGURATION_ERROR = 2


async def run_bot(settings: Settings) -> None:
    """Validate bot settings and poll Telegram until the process is asked to stop.

    Raises:
        ConfigurationError: the bot token or the admin whitelist is missing.
    """
    token = settings.require_secret("telegram_bot_token")
    admin_ids = settings.require_admin_ids()
    dispatcher = build_dispatcher(admin_ids)
    bot = Bot(token=token)
    logger.info("bot_starting", app_env=settings.app_env, admin_count=len(admin_ids))
    await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    logger.info("bot_stopped")


def main() -> int:
    """Run the application and return a process exit code."""
    settings = get_settings()
    configure_logging(level=settings.log_level, app_env=settings.app_env)
    try:
        asyncio.run(run_bot(settings))
    except ConfigurationError as exc:
        logger.critical("startup_failed", reason=str(exc))
        return EXIT_CONFIGURATION_ERROR
    return 0


if __name__ == "__main__":
    sys.exit(main())
