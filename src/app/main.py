"""Process entrypoint: ``python -m app.main``.

Composition root: loads settings, configures logging, validates what the bot needs and runs
long polling until SIGINT/SIGTERM (aiogram stops polling and closes the HTTP session).

Exit codes: 0 normal stop, 1 unexpected error, 2 configuration error (fix ``.env``; retrying
will not help).
"""

import asyncio
import sys
from typing import Final

from aiogram import Bot
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError

from app.bot.dispatcher import build_dispatcher
from app.bot.messaging import install_flood_control
from app.config import Settings, get_settings
from app.errors import ConfigurationError
from app.logging import configure_logging, get_logger
from app.services.fake import create_fake_services
from app.services.interfaces import Services

# Not `__name__`: under `python -m app.main` that would be "__main__".
logger = get_logger("app.main")

EXIT_OK: Final = 0
EXIT_UNEXPECTED_ERROR: Final = 1
EXIT_CONFIGURATION_ERROR: Final = 2


async def connect_bot(token: str) -> Bot:
    """Create the bot and prove the token is accepted by Telegram before polling.

    Raises:
        ConfigurationError: the token is malformed or rejected by Telegram.
    """
    try:
        bot = Bot(token=token)
    except TokenValidationError:
        msg = (
            "TELEGRAM_BOT_TOKEN is malformed; expected the '<digits>:<text>' token from @BotFather"
        )
        raise ConfigurationError(msg) from None
    try:
        await bot.get_me()
    except TelegramUnauthorizedError:
        await bot.session.close()
        msg = "Telegram rejected TELEGRAM_BOT_TOKEN (unauthorized); check or regenerate it"
        raise ConfigurationError(msg) from None
    except BaseException:
        await bot.session.close()
        raise
    return bot


def build_services(settings: Settings) -> Services:
    """The service implementation selected by ``APP_SERVICES`` (only ``fake`` until T4)."""
    logger.warning("services_backend", backend=settings.app_services, sample_data=True)
    return create_fake_services()


async def run_bot(settings: Settings) -> None:
    """Validate bot settings and poll Telegram until the process is asked to stop.

    Raises:
        ConfigurationError: the bot token or the admin whitelist is missing or invalid.
    """
    token = settings.require_secret("telegram_bot_token")
    admin_ids = settings.require_admin_ids()
    dispatcher = build_dispatcher(admin_ids, build_services(settings))
    bot = await connect_bot(token)
    install_flood_control(bot)
    logger.info("bot_starting", app_env=settings.app_env, admin_count=len(admin_ids))
    await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    logger.info("bot_stopped")


def main() -> int:
    """Run the application and return a process exit code."""
    # Safe defaults first, so even an invalid .env produces a structured log line.
    configure_logging(level="INFO", app_env="prod")
    try:
        settings = get_settings()
        configure_logging(
            level=settings.log_level,
            app_env=settings.app_env,
            secret_values=settings.secret_values(),
        )
        asyncio.run(run_bot(settings))
    except ConfigurationError as exc:
        logger.critical("startup_failed", reason=str(exc))
        return EXIT_CONFIGURATION_ERROR
    except Exception:
        # Last resort for the process: record the full (scrubbed) traceback and let the
        # supervisor (Docker restart policy) decide whether to restart.
        logger.exception("fatal_error")
        return EXIT_UNEXPECTED_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
