"""Typed application settings loaded from environment variables and an optional ``.env``.

This is the only module allowed to read the process environment. Secrets are optional at
load time so tests, CI and keyless development work without a ``.env``; a component that
needs a secret asks for it via :meth:`Settings.require_secret`, which fails with a clear
:class:`~app.errors.ConfigurationError` only at the moment the component starts.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, HttpUrl, SecretStr, TypeAdapter, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.errors import ConfigurationError

JOBS_DB_NAME: Final = "jobs.db"

type AppEnv = Literal["dev", "prod"]
type ServicesBackend = Literal["fake", "real"]
type HandicapConventionName = Literal["home_line", "own_line"]
type StakingModeName = Literal["flat", "kelly"]
type LearningModeName = Literal["off", "shadow", "apply"]
type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
type SecretName = Literal["telegram_bot_token", "api_football_key", "gemini_api_key"]

_HTTP_URL: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)


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
    gemini_api_key: SecretStr | None = None
    gemini_model: str = Field(default="gemini-flash-latest", min_length=1)

    # Not `TZ`: the POSIX variable is often set by the OS (e.g. ":/etc/localtime") and also
    # changes the process clock; this one only affects how times are shown to users.
    display_tz: str = "Asia/Tashkent"
    db_path: Path = Path("data/app.db")
    # How API-Football writes Asian handicap numbers (see docs/api_probe_report.md); set it
    # from the F0 probe BEFORE the first real odds are stored and never change it afterwards:
    # stored lines cannot be re-read. APP_SERVICES=real refuses to start unless it is set
    # explicitly (see `require_explicit_handicap_convention`).
    handicap_convention: HandicapConventionName = "home_line"
    # Which implementation of the service contract the bot talks to: the in-memory sample
    # data ("fake", the default until API keys exist) or the pipeline-backed one ("real").
    app_services: ServicesBackend = "fake"
    # How many units a recommendation stakes: one flat unit, or a capped quarter Kelly of the
    # user's bank (falls back to flat for a user without a bank and unit).
    staking_mode: StakingModeName = "flat"
    # Bookmakers stored besides Pinnacle and the user's own (comma-separated, e.g.
    # "bet365,unibet"); empty by default: every extra one multiplies the odds history (ADR 0018).
    ingest_extra_bookmakers: Annotated[frozenset[str], NoDecode] = frozenset()
    # The weekly learning loop: "off" does nothing, "shadow" (default) computes and journals what it
    # would change without touching any analysis, "apply" also lets segment rules and the
    # calibration steer analysis. Switching to "apply" is a human decision (ADR 0013).
    learning_mode: LearningModeName = "shadow"
    # Days the loop leaves a segment or the calibration alone after a person rolled a change back.
    learning_manual_hold_days: int = Field(default=30, ge=0, le=365)
    # The ping URL's path is the credential of the check, so it is a secret (never logged).
    healthchecks_url: SecretStr | None = None
    # Baked into the image by the build (`--build-arg APP_VERSION=... GIT_SHA=...`); shown in the
    # startup log and /holat so "which build is running" is never a guess.
    app_version: str | None = None
    git_sha: str = "unknown"
    # The in-app watchdog restarts the process (exit code 3, Docker restarts it) when the
    # scheduler's tick has not run for this many minutes; the scheduler is stuck or dead.
    watchdog_stall_minutes: float = Field(default=10.0, ge=1.0, le=1440.0)
    # Outbound HTTPS behind a TLS-intercepting corporate proxy (src/app/tls.py): either trust the
    # OS certificate store, or a PEM file with the proxy's CA. Both unset = library defaults.
    tls_ca_bundle: Path | None = None
    tls_use_system_trust_store: bool = False

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        """Accept ``info`` as well as ``INFO``."""
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("healthchecks_url")
    @classmethod
    def _check_healthchecks_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject a malformed URL at startup without echoing it (it is a credential)."""
        if value is not None:
            try:
                _HTTP_URL.validate_python(value.get_secret_value())
            except ValidationError:
                msg = "must be an http(s) URL such as https://hc-ping.com/<uuid>"
                raise ValueError(msg) from None
        return value

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

    @field_validator("ingest_extra_bookmakers", mode="before")
    @classmethod
    def _parse_extra_bookmakers(cls, value: object) -> object:
        """Accept ``INGEST_EXTRA_BOOKMAKERS=bet365, Unibet`` (names are matched lower-case)."""
        if isinstance(value, str):
            parts = [part.strip().lower() for part in value.split(",")]
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
    def jobs_db_path(self) -> Path:
        """The scheduler's own SQLite file, next to the application database.

        Separate because APScheduler stores pickles in it: it is rebuilt on every start, kept
        out of backups, and never restored from someone else's data.
        """
        return self.db_path.with_name(JOBS_DB_NAME)

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

    def require_explicit_handicap_convention(self) -> None:
        """Refuse real services while ``HANDICAP_CONVENTION`` was never chosen on purpose.

        The default is only a guess; odds stored under a wrong convention are silently wrong
        for good, so the operator must have looked at the F0 probe result and said so (setting
        ``HANDICAP_CONVENTION=home_line`` explicitly is the escape hatch).

        Raises:
            ConfigurationError: the variable is absent from the environment and ``.env``.
        """
        if "handicap_convention" not in self.model_fields_set:
            msg = (
                "HANDICAP_CONVENTION is not set; set it from the F0 probe result "
                "(home_line or own_line) before the first real odds are stored, "
                "and do not change it afterwards (RUNBOOK.md). "
                "Set HANDICAP_CONVENTION=home_line explicitly to accept the default."
            )
            raise ConfigurationError(msg)

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
