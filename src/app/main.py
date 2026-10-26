"""Process entrypoint: ``python -m app.main``.

Composition root: loads settings, configures logging, validates what the bot needs and runs
long polling until SIGINT/SIGTERM (aiogram stops polling and closes the HTTP session).

Exit codes: 0 normal stop, 1 unexpected error, 2 configuration error (fix ``.env``; retrying
will not help), 3 the watchdog found the scheduler stalled (a restart is the cure).
"""

import asyncio
import os
import ssl
import sys
from typing import Final

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import ClientDecodeError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError

from app.bot.dispatcher import build_dispatcher
from app.bot.messaging import install_flood_control
from app.bot.notifier import TelegramNotifier
from app.bot.recovery import recover_running_analyses
from app.bot.startup_notice import announce_startup
from app.bot.storage import SqliteStorage, fsm_db_path
from app.config import Settings, get_settings
from app.errors import ConfigurationError
from app.logging import configure_logging, get_logger, summarise_body
from app.ops.heartbeat import Heartbeat
from app.ops.version import get_version
from app.ops.watchdog import EXIT_STALLED, SchedulerStalledError, Watchdog
from app.pipeline.redaction import register_secrets
from app.pipeline.runtime import open_runtime
from app.scheduler import PipelineJobs, PipelineScheduler
from app.services.fake import create_fake_services
from app.services.interfaces import Services
from app.services.real import create_real_services
from app.tls import get_ssl_context

# Not `__name__`: under `python -m app.main` that would be "__main__".
logger = get_logger("app.main")

EXIT_OK: Final = 0
EXIT_UNEXPECTED_ERROR: Final = 1
EXIT_CONFIGURATION_ERROR: Final = 2
SECONDS_PER_MINUTE: Final = 60


class TrustedAiohttpSession(AiohttpSession):
    """aiogram session whose connector verifies against the configured SSL context.

    aiogram exposes no public parameter for this (its connector is built from
    ``_connector_init`` with a certifi-only context), so the supported extension point is a
    subclass that replaces that one entry; verification stays on.
    """

    def __init__(self, ssl_context: ssl.SSLContext) -> None:
        """Trust ``ssl_context`` instead of the certifi default."""
        super().__init__()
        self._connector_init["ssl"] = ssl_context


def build_bot_session() -> AiohttpSession | None:
    """The session for the configured trust, or ``None`` for aiogram's default."""
    context = get_ssl_context()
    return None if context is None else TrustedAiohttpSession(context)


async def connect_bot(token: str) -> Bot:
    """Create the bot and prove the token is accepted by Telegram before polling.

    Raises:
        ConfigurationError: the token is malformed or rejected by Telegram, or something in
            the network (a firewall page instead of JSON) stands between us and Telegram.
    """
    try:
        bot = Bot(token=token, session=build_bot_session())
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
    except ClientDecodeError as error:
        await bot.session.close()
        msg = (
            "unexpected content from api.telegram.org (not JSON): the network or a firewall is "
            "blocking Telegram, see RUNBOOK 'Telegram is unreachable'. "
            f"Answer: {summarise_body(error.data)}"
        )
        raise ConfigurationError(msg) from None
    except BaseException:
        await bot.session.close()
        raise
    return bot


def build_services(settings: Settings) -> Services:
    """The in-memory sample services (``APP_SERVICES=fake``, the default without API keys)."""
    logger.warning("services_backend", backend=settings.app_services, sample_data=True)
    return create_fake_services()


async def run_bot(settings: Settings) -> None:
    """Validate bot settings and poll Telegram until the process is asked to stop.

    Raises:
        ConfigurationError: the bot token or the admin whitelist is missing or invalid.
    """
    token = settings.require_secret("telegram_bot_token")
    admin_ids = settings.require_admin_ids()
    if settings.app_services == "real":
        await run_real(settings, token, admin_ids)
        return
    dispatcher = build_dispatcher(
        admin_ids, build_services(settings), storage=SqliteStorage(fsm_db_path(settings.db_path))
    )
    await poll(dispatcher, token, admin_ids, settings)


async def poll(
    dispatcher: Dispatcher, token: str, admin_ids: frozenset[int], settings: Settings
) -> None:
    """Connect the bot and poll until the process is asked to stop."""
    bot = await connect_bot(token)
    install_flood_control(bot)
    logger.info("bot_starting", app_env=settings.app_env, admin_count=len(admin_ids))
    try:
        await recover_running_analyses(bot, dispatcher.storage)
        await announce_startup(
            bot,
            admin_ids,
            build=get_version(settings).label,
            backend=settings.app_services,
        )
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await dispatcher.storage.close()
    logger.info("bot_stopped")


async def run_real(settings: Settings, token: str, admin_ids: frozenset[int]) -> None:
    """Bot over the pipeline-backed services, with the scheduler running beside it.

    The scheduler starts in the dispatcher's startup phase, after the bot is connected and the
    :class:`TelegramNotifier` is bound to it: a job that fires at once (a missed run inside its
    grace time) can then deliver its messages instead of finding no bot. It is stopped (running
    jobs finish) and the database closed on every exit. The watchdog turns a stalled scheduler
    into a restart (exit code 3).

    Raises:
        ConfigurationError: ``HANDICAP_CONVENTION`` was never set explicitly.
        SchedulerStalledError: the watchdog found the scheduler no longer ticking.
    """
    settings.require_explicit_handicap_convention()
    async with open_runtime(settings) as runtime:
        notifier = TelegramNotifier(admin_ids=admin_ids, default_zone=settings.display_timezone)
        system = create_real_services(
            runtime.ctx, notifier=notifier, build=get_version(settings).label
        )
        notifier.attach_settings(system.services.settings)
        logger.info("services_backend", backend="real", sample_data=False)
        scheduler = PipelineScheduler(
            PipelineJobs(runtime.ctx, system.delivery, heartbeat=Heartbeat.from_settings(settings)),
            db_path=settings.jobs_db_path,
            timezone=settings.display_timezone,
        )
        dispatcher = build_dispatcher(
            admin_ids,
            system.services,
            storage=SqliteStorage(fsm_db_path(settings.db_path)),
        )
        dispatcher["backend"] = "real"
        watchdog = Watchdog(
            scheduler.seconds_since_tick,
            stall_after=settings.watchdog_stall_minutes * SECONDS_PER_MINUTE,
            request_stop=dispatcher.stop_polling,
            force_exit=lambda: os._exit(EXIT_STALLED),  # last resort when the orderly stop hangs
        )
        watchdog_task: asyncio.Task[None] | None = None

        async def start_scheduler() -> None:
            nonlocal watchdog_task
            scheduler.start()
            watchdog_task = asyncio.create_task(watchdog.run(), name="watchdog")

        dispatcher.startup.register(notifier.on_startup)
        dispatcher.startup.register(start_scheduler)
        try:
            await poll(dispatcher, token, admin_ids, settings)
        finally:
            if watchdog_task is not None and not watchdog.stalled:
                watchdog_task.cancel()
            await scheduler.stop()
        if watchdog.stalled:
            msg = "the scheduler stopped ticking; restarting the process"
            raise SchedulerStalledError(msg)


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
        register_secrets(settings.secret_values())  # bot token, API keys, healthchecks URL
        build = get_version(settings)
        logger.info(
            "app_started", version=build.version, sha=build.git_sha, services=settings.app_services
        )
        asyncio.run(run_bot(settings))
    except SchedulerStalledError as exc:
        logger.critical("scheduler_stalled_exit", reason=str(exc))
        return EXIT_STALLED
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
