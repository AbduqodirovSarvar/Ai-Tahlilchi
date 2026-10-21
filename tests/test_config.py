"""Tests for app.config."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.errors import ConfigurationError

SETTINGS_ENV_VARS = (
    "APP_ENV",
    "LOG_LEVEL",
    "TELEGRAM_BOT_TOKEN",
    "ADMIN_IDS",
    "API_FOOTBALL_KEY",
    "ODDS_API_KEY",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "TZ",
    "DB_PATH",
    "HEALTHCHECKS_URL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the developer's shell environment."""
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings(**overrides: object) -> Settings:
    """Build settings that never read a real `.env` file."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_load_without_env_file_or_secrets() -> None:
    settings = make_settings()

    assert settings.app_env == "dev"
    assert settings.log_level == "INFO"
    assert settings.telegram_bot_token is None
    assert settings.gemini_api_key is None
    assert settings.admin_ids == frozenset()
    assert settings.db_path == Path("data/app.db")
    assert settings.display_timezone == ZoneInfo("Asia/Tashkent")


def test_reads_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("ADMIN_IDS", "11, 22")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "42:TEST")
    monkeypatch.setenv("DB_PATH", "/data/app.db")

    settings = make_settings()

    assert settings.app_env == "prod"
    assert settings.admin_ids == frozenset({11, 22})
    assert settings.require_secret("telegram_bot_token") == "42:TEST"
    assert settings.db_path == Path("/data/app.db")


def test_empty_values_in_env_file_mean_unset(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=\nADMIN_IDS=\nHEALTHCHECKS_URL=\nLOG_LEVEL=DEBUG\n", encoding="utf-8"
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.telegram_bot_token is None
    assert settings.admin_ids == frozenset()
    assert settings.healthchecks_url is None
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7", frozenset({7})),
        ("1,2,3", frozenset({1, 2, 3})),
        (" 1 , 2 ,, ", frozenset({1, 2})),
        ("5,5", frozenset({5})),
        ("", frozenset()),
    ],
)
def test_admin_ids_parsing(raw: str, expected: frozenset[int]) -> None:
    assert make_settings(admin_ids=raw).admin_ids == expected


def test_admin_ids_accepts_single_integer() -> None:
    assert make_settings(admin_ids=9).admin_ids == frozenset({9})


def test_admin_ids_accepts_collection() -> None:
    assert make_settings(admin_ids=[1, 2]).admin_ids == frozenset({1, 2})


@given(st.frozensets(st.integers(min_value=1, max_value=2**53)))
def test_admin_ids_round_trip_from_comma_separated_text(ids: frozenset[int]) -> None:
    raw = ",".join(str(user_id) for user_id in ids)
    assert make_settings(admin_ids=raw).admin_ids == ids


@pytest.mark.parametrize("raw", ["abc", "1,two", "0", "-5", "1,-2"])
def test_admin_ids_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(admin_ids=raw)


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError, match="IANA"):
        make_settings(tz="Mars/Olympus")


@pytest.mark.parametrize(("field", "value"), [("app_env", "staging"), ("log_level", "LOUD")])
def test_invalid_literals_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_invalid_healthchecks_url_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(healthchecks_url="not a url")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_require_secret_explains_missing_value(value: str | None) -> None:
    settings = make_settings(gemini_api_key=value)

    with pytest.raises(ConfigurationError, match=r"GEMINI_API_KEY is not set"):
        settings.require_secret("gemini_api_key")


def test_require_admin_ids_rejects_empty_whitelist() -> None:
    with pytest.raises(ConfigurationError, match="ADMIN_IDS"):
        make_settings().require_admin_ids()


def test_require_admin_ids_returns_whitelist() -> None:
    assert make_settings(admin_ids="3").require_admin_ids() == frozenset({3})


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = make_settings(telegram_bot_token="42:SUPERSECRET")

    assert "SUPERSECRET" not in repr(settings)
    assert "SUPERSECRET" not in str(settings.model_dump())


def test_settings_are_immutable() -> None:
    settings = make_settings()

    with pytest.raises(ValidationError):
        settings.app_env = "prod"  # type: ignore[misc]


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
