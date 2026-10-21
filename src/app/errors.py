"""Project exception hierarchy.

Every error raised deliberately by the application derives from :class:`AppError`, so
entrypoints can tell expected failures (log + clean exit / user-facing message) apart from
programming bugs (full traceback). Layers subclass the most specific base that fits instead
of raising bare built-in exceptions.
"""


class AppError(Exception):
    """Base class for all expected, application-level failures."""


class ConfigurationError(AppError):
    """Required configuration is missing or invalid.

    Raised lazily, when a component that needs the setting is started, so the rest of the
    application keeps working without it.
    """


class ExternalServiceError(AppError):
    """An external dependency (data provider, LLM, Telegram) failed or misbehaved."""


class DataError(AppError):
    """Input or stored data is missing, malformed or inconsistent."""
