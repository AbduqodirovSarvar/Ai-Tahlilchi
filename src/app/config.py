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

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.errors import ConfigurationError

type AppEnv = Literal["dev", "prod"]
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

    tz: str = "Asia/Tashkent"
    db_path: Path = Path("data/app.db")
    healthchecks_url: HttpUrl | None = None

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

    @field_validator("tz")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        """Fail at startup rather than at the first time conversion."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"TZ={value!r} is not a known IANA time zone"
            raise ValueError(msg) from exc
        return value

    @property
    def display_timezone(self) -> ZoneInfo:
        """Time zone used only when presenting times to users (storage is always UTC)."""
        return ZoneInfo(self.tz)

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process."""
    return Settings()
