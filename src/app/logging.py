"""Structured logging setup.

All output (our structlog loggers and stdlib loggers of libraries such as aiogram) goes to
stdout through one formatter: JSON lines in production, coloured key-value lines in
development. Values of keys that look like credentials are masked before rendering, as a
last line of defence against leaking secrets into logs.
"""

import logging
import sys
from typing import Final

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.config import AppEnv, LogLevel

REDACTED: Final = "***"
_SENSITIVE_KEY_PARTS: Final = ("token", "secret", "password", "api_key", "apikey", "authorization")


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Mask values whose key names suggest a credential."""
    for key in event_dict:
        if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
            event_dict[key] = REDACTED
    return event_dict


def _shared_processors() -> list[Processor]:
    """Processors applied to both structlog and foreign (stdlib) log records."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_secrets,
    ]


def _renderer_chain(app_env: AppEnv) -> list[Processor]:
    """Final processors that turn an event dict into a line of text."""
    if app_env == "prod":
        return [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
    # Plain tracebacks: deterministic, fast, and safe on non-UTF-8 consoles (Windows cp1252),
    # unlike the optional `rich` formatter structlog would otherwise pick up.
    return [structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)]


def configure_logging(*, level: LogLevel, app_env: AppEnv) -> None:
    """Configure structlog and the stdlib root logger. Safe to call more than once."""
    shared = _shared_processors()
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *_renderer_chain(app_env),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger; pass ``__name__`` so records show their origin."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


__all__ = ["REDACTED", "configure_logging", "get_logger", "redact_secrets"]
