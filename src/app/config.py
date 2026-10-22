"""Typed application settings loaded from environment variables and an optional ``.env``.

This is the only module allowed to read the process environment. Secrets are optional at
load time so tests, CI and keyless development work without a ``.env``; a component that
needs a secret asks for it via :meth:`Settings.require_secret`, which fails with a clear
:class:`~app.errors.ConfigurationError` only at the moment the component starts.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, HttpUrl, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.errors import ConfigurationError

type AppEnv = Literal["dev", "prod"]
type ServicesBackend = Literal["fake", "real"]
type HandicapConventionName = Literal["home_line", "own_line"]
type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
type SecretName = Literal[
    "telegram_bot_token", "api_football_key", "odds_api_key", "gemini_api_key"
]


class Settings(BaseSettings):
    """Process-wide configuration. Field names map to upper-case environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # `KEY=` in .env means "not set", so optional fields fall back to their defaults.
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    app_env: AppEnv = "dev"
    log_level: LogLevel = "INFO"

    telegram_bot_token: SecretStr | None = None
    admin_ids: Annotated[frozenset[int], NoDecode] = frozenset()

    api_football_key: SecretStr | None = None
    odds_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    gemini_model: str = Field(default="gemini-flash-latest", min_length=1)

    # Not `TZ`: the POSIX variable is often set by the OS (e.g. ":/etc/localtime") and also
    # changes the process clock; this one only affects how times are shown to users.
    display_tz: str = "Asia/Tashkent"
    db_path: Path = Path("data/app.db")
    # How API-Football writes Asian handicap numbers (see docs/api_probe_report.md); set it
    # before the first real odds are stored, stored lines cannot be re-read afterwards.
    handicap_convention: HandicapConventionName = "home_line"
    # Which implementation of the service contract the bot talks to: the in-memory sample
    # data ("fake", the default until API keys exist) or the pipeline-backed one ("real").
    app_services: ServicesBackend = "fake"
    healthchecks_url: HttpUrl | None = None

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        """Accept ``info`` as well as ``INFO``."""
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        """Accept the comma-separated form used in ``.env`` (``ADMIN_IDS=1,2``)."""
        if isinstance(value, int):
            return frozenset({value})
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
            return frozenset(part for part in parts if part)
        return value

    @field_validator("admin_ids")
    @classmethod
    def _check_admin_ids_positive(cls, value: frozenset[int]) -> frozenset[int]:
        """Telegram user ids are positive; anything else is a typo, not a user."""
        if any(user_id <= 0 for user_id in value):
            msg = "ADMIN_IDS must contain positive Telegram user ids"
            raise ValueError(msg)
        return value

    @field_validator("display_tz")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        """Fail at startup rather than at the first time conversion."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = "must be an IANA time zone name such as Asia/Tashkent"
            raise ValueError(msg) from exc
        return value

    @property
    def display_timezone(self) -> ZoneInfo:
        """Time zone used only when presenting times to users (storage is always UTC)."""
        return ZoneInfo(self.display_tz)

    def secret_values(self) -> frozenset[str]:
        """All configured secret values, so logging can scrub them from any output."""
        values = (getattr(self, name) for name in type(self).model_fields)
        return frozenset(
            value.get_secret_value()
            for value in values
            if isinstance(value, SecretStr) and value.get_secret_value().strip()
        )

    def require_secret(self, name: SecretName) -> str:
        """Return the plain value of a secret, or explain how to configure it.

        Raises:
            ConfigurationError: the secret is not configured.
        """
        secret: SecretStr | None = getattr(self, name)
        if secret is None or not secret.get_secret_value().strip():
            msg = f"{name.upper()} is not set; add it to .env (see .env.example)"
            raise ConfigurationError(msg)
        return secret.get_secret_value()

    def require_admin_ids(self) -> frozenset[int]:
        """Return the bot whitelist, refusing to run a bot that nobody may use.

        Raises:
            ConfigurationError: the whitelist is empty.
        """
        if not self.admin_ids:
            msg = "ADMIN_IDS is empty; add your Telegram user id to .env (see .env.example)"
            raise ConfigurationError(msg)
        return self.admin_ids


def describe_validation_error(error: ValidationError) -> str:
    """Summarise invalid settings by variable name and reason, never by value.

    Pydantic's own message echoes the input, which may be a secret or a URL with a key.
    """
    problems = sorted(
        {
            f"{'.'.join(str(part) for part in issue['loc']).upper()}: {issue['msg']}"
            for issue in error.errors(include_url=False, include_input=False)
        }
    )
    return "Invalid settings in environment/.env: " + "; ".join(problems)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process.

    Raises:
        ConfigurationError: a variable is set to an invalid value.
    """
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigurationError(describe_validation_error(exc)) from None
