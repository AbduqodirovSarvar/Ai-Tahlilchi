"""Structured logging setup.

All output (our structlog loggers and stdlib loggers of libraries such as aiogram) goes to
stdout through one formatter: JSON lines in production, coloured key-value lines in
development.

Secrets are kept out of logs in three layers:

1. Tracebacks never include frame locals (aiogram keeps the bot token in a local ``url``).
2. Every configured secret *value* is replaced by ``***`` inside any string of the record,
   including the event text and rendered tracebacks (primary defence).
3. Values under credential-like *keys* are masked, recursively (catches secrets that are
   not configured settings, e.g. a header passed in by a client).
"""

import logging
import re
import sys
from collections.abc import Iterable
from typing import Final

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.config import AppEnv, LogLevel

REDACTED: Final = "***"
# Shorter values would scrub ordinary words/numbers from every log line; real credentials
# (bot tokens, API keys) are far longer.
MIN_SCRUBBED_SECRET_LENGTH: Final = 8

# Libraries that log full request URLs at INFO (httpx: `HTTP Request: POST https://...`); a URL can
# carry a credential (healthchecks.io ping path, Telegram bot token), so they stay at WARNING
# whatever the application level is. Application code logs the outcome of its calls itself.
QUIET_LIBRARY_LOGGERS: Final = ("httpx", "httpcore", "urllib3", "google_genai", "aiohttp.client")

BODY_SUMMARY_LIMIT: Final = 300
"""Longest piece of a third-party response body that may reach a log line."""
_HTML_BLOCKS: Final = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_TAGS: Final = re.compile(r"<[^>]*>")
_WHITESPACE: Final = re.compile(r"\s+")


def summarise_body(body: object, limit: int = BODY_SUMMARY_LIMIT) -> str:
    """A short single-line summary of a response body for logs (HTML tags stripped).

    A firewall or proxy answers with a whole HTML page where JSON was expected; logging it
    verbatim made a 38 KB line on every restart (T6-F-5).
    """
    text = _HTML_TAGS.sub(" ", _HTML_BLOCKS.sub(" ", str(body)))
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... (+{len(text) - limit} chars)"


_SENSITIVE_KEYS: Final = frozenset(
    {"token", "secret", "password", "api_key", "apikey", "authorization"}
)
# Suffix rules deliberately do not match plural counters such as `prompt_tokens`.
_SENSITIVE_KEY_SUFFIXES: Final = ("_token", "_key", "_secret", "_password", "_apikey")


def is_sensitive_key(key: str) -> bool:
    """Whether a key name denotes a credential (exact names and ``*_token``-style suffixes)."""
    normalised = key.strip().lower().replace("-", "_")
    return normalised in _SENSITIVE_KEYS or normalised.endswith(_SENSITIVE_KEY_SUFFIXES)


class SecretRedactor:
    """structlog processor that masks credentials by key name and by known secret value."""

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        """Remember secret values to scrub; longest first so overlapping ones fully vanish."""
        self._secret_values = sorted(
            {value for value in secret_values if len(value) >= MIN_SCRUBBED_SECRET_LENGTH},
            key=len,
            reverse=True,
        )

    def __call__(
        self, _logger: WrappedLogger, _method_name: str, event_dict: EventDict
    ) -> EventDict:
        """Return the event with every secret masked."""
        return {key: self._redact_item(key, value) for key, value in event_dict.items()}

    def _redact_item(self, key: object, value: object) -> object:
        if isinstance(key, str) and is_sensitive_key(key):
            return REDACTED
        return self._redact_value(value)

    def _redact_value(self, value: object) -> object:
        if isinstance(value, str):
            return self._scrub(value)
        if isinstance(value, dict):
            return {key: self._redact_item(key, item) for key, item in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self._redact_value(item) for item in value]
        return value

    def _scrub(self, text: str) -> str:
        for secret in self._secret_values:
            text = text.replace(secret, REDACTED)
        return text


def redact_secrets(_logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Key-based redaction only (no known secret values); see :class:`SecretRedactor`."""
    return SecretRedactor()(_logger, method_name, event_dict)


def _shared_processors() -> list[Processor]:
    """Processors applied to both structlog and foreign (stdlib) log records."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]


def _renderer_chain(app_env: AppEnv, redactor: SecretRedactor) -> list[Processor]:
    """Turn exceptions into data, redact, then render the event as a line of text.

    Redaction runs after exception rendering so traceback text is scrubbed too.
    """
    if app_env == "prod":
        exception_renderer: Processor = structlog.processors.ExceptionRenderer(
            structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
        )
        return [exception_renderer, redactor, structlog.processors.JSONRenderer()]
    # Plain tracebacks (format_exc_info): no locals, fast, and safe on non-UTF-8 consoles
    # (Windows cp1252), unlike the optional `rich` formatter structlog would pick up.
    return [
        structlog.processors.format_exc_info,
        redactor,
        structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback),
    ]


def configure_logging(
    *, level: LogLevel, app_env: AppEnv, secret_values: Iterable[str] = ()
) -> None:
    """Configure structlog and the stdlib root logger. Safe to call more than once.

    Args:
        level: minimum level for every logger.
        app_env: ``prod`` renders JSON, ``dev`` human-readable lines.
        secret_values: configured credentials to scrub from all output.
    """
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
            *_renderer_chain(app_env, SecretRedactor(secret_values)),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in QUIET_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger; pass ``__name__`` so records show their origin."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


__all__ = [
    "REDACTED",
    "SecretRedactor",
    "configure_logging",
    "get_logger",
    "is_sensitive_key",
    "redact_secrets",
]
