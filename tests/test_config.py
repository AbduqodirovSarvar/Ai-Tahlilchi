"""Tests for app.config."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.errors import ConfigurationError


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
    with pytest.raises(ValidationError, match="IANA time zone"):
        make_settings(display_tz="Mars/Olympus")


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


# --- ADMIN_IDS edge cases through the real source (environment / .env), not init kwargs ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,", frozenset({1})),  # trailing comma
        (",1", frozenset({1})),  # leading comma
        (" 1 ", frozenset({1})),
        ("\t1\t,\n2", frozenset({1, 2})),  # tabs / newline from a pasted list
        ("1 , 2", frozenset({1, 2})),
        ("00012", frozenset({12})),
        ("123456789012", frozenset({123456789012})),  # ids beyond 32 bits are real
    ],
)
def test_admin_ids_from_environment_tolerates_separators_and_spaces(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: frozenset[int]
) -> None:
    monkeypatch.setenv("ADMIN_IDS", raw)

    assert make_settings().admin_ids == expected


@pytest.mark.parametrize(
    "raw",
    [
        "-0",
        "-1",
        "0,1",
        "1;2",  # wrong separator must not silently become one id or be dropped
        "1 2",
        "[1,2]",  # JSON form is not supported (NoDecode)
        "1e3",
        "0x10",
        "١٢",  # non-ASCII digits
        "@admin",
    ],
)
def test_admin_ids_from_environment_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("ADMIN_IDS", raw)

    with pytest.raises(ValidationError, match="admin_ids"):
        make_settings()


@pytest.mark.parametrize("raw", [",", " , ", ",,,"])
def test_admin_ids_of_only_separators_is_an_empty_whitelist(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("ADMIN_IDS", raw)
    settings = make_settings()

    assert settings.admin_ids == frozenset()
    with pytest.raises(ConfigurationError, match="ADMIN_IDS"):
        settings.require_admin_ids()


def test_whitespace_only_token_in_env_file_is_treated_as_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=   \nADMIN_IDS=1\n", encoding="utf-8")

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN is not set"):
        settings.require_secret("telegram_bot_token")


def test_environment_overrides_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ADMIN_IDS=1\nLOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.setenv("ADMIN_IDS", "2")

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.admin_ids == frozenset({2})
    assert settings.log_level == "DEBUG"


def test_empty_environment_variable_falls_back_to_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # docker compose passes `KEY=` through as an empty variable; it must not mask .env.
    env_file = tmp_path / ".env"
    env_file.write_text("ADMIN_IDS=7\n", encoding="utf-8")
    monkeypatch.setenv("ADMIN_IDS", "")

    assert Settings(_env_file=env_file).admin_ids == frozenset({7})  # type: ignore[call-arg]


def test_secret_values_never_appear_in_validation_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    # A failing setting next to a secret must not echo the secret in the startup error.
    monkeypatch.setenv("GEMINI_API_KEY", "SUPERSECRET-123")
    monkeypatch.setenv("ADMIN_IDS", "abc")

    with pytest.raises(ValidationError) as excinfo:
        make_settings()

    assert "SUPERSECRET-123" not in str(excinfo.value)


# --- T1 fix round ---------------------------------------------------------------------------


def test_display_timezone_is_read_from_display_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY_TZ", "Europe/London")
    monkeypatch.setenv("TZ", ":/etc/localtime")  # the POSIX variable must not interfere

    assert make_settings().display_timezone == ZoneInfo("Europe/London")


@pytest.mark.parametrize("raw", ["debug", " Warning ", "INFO"])
def test_log_level_is_case_insensitive(raw: str) -> None:
    assert make_settings(log_level=raw).log_level == raw.strip().upper()


def test_secret_values_lists_only_configured_secrets() -> None:
    settings = make_settings(telegram_bot_token="42:TOKEN-VALUE", gemini_api_key="  ")

    assert settings.secret_values() == frozenset({"42:TOKEN-VALUE"})


def test_get_settings_reports_invalid_values_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HEALTHCHECKS_URL", "not-a-url-with-key-SECRET123")
    monkeypatch.setenv("DISPLAY_TZ", "Mars/SECRET456")
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationError) as caught:
            get_settings()
    finally:
        get_settings.cache_clear()

    message = str(caught.value)
    assert "HEALTHCHECKS_URL" in message
    assert "DISPLAY_TZ" in message
    assert "SECRET123" not in message
    assert "SECRET456" not in message
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_handicap_convention_defaults_to_home_line_and_rejects_typos() -> None:
    assert make_settings().handicap_convention == "home_line"
    assert make_settings(handicap_convention="own_line").handicap_convention == "own_line"
    with pytest.raises(ValidationError):
        make_settings(handicap_convention="wrong")
