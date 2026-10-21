"""Tests for app.logging."""

import json
import logging
from typing import Literal

import pytest
import structlog

from app.logging import (
    REDACTED,
    SecretRedactor,
    configure_logging,
    get_logger,
    is_sensitive_key,
    redact_secrets,
)


def last_json_line(output: str) -> dict[str, object]:
    line = output.strip().splitlines()[-1]
    parsed: dict[str, object] = json.loads(line)
    return parsed


def test_prod_renders_json_with_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", app_env="prod")

    get_logger("tests.prod").info("signal_sent", fixture_id=101)

    record = last_json_line(capsys.readouterr().out)
    assert record["event"] == "signal_sent"
    assert record["fixture_id"] == 101
    assert record["level"] == "info"
    assert record["logger"] == "tests.prod"
    assert str(record["timestamp"]).endswith("Z")


def test_prod_renders_stdlib_records_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", app_env="prod")

    logging.getLogger("aiogram.test").warning("from a library")

    record = last_json_line(capsys.readouterr().out)
    assert record["event"] == "from a library"
    assert record["level"] == "warning"


def test_prod_renders_exceptions_as_structured_data(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", app_env="prod")
    error = ValueError("bad data")

    get_logger("tests.exc").error("failed", exc_info=error)

    record = last_json_line(capsys.readouterr().out)
    assert "bad data" in json.dumps(record["exception"])


def test_level_filters_lower_records(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", app_env="prod")

    get_logger("tests.level").info("hidden")

    assert capsys.readouterr().out == ""


def test_dev_renders_human_readable_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="DEBUG", app_env="dev")

    get_logger("tests.dev").debug("hello", user_id=7)

    output = capsys.readouterr().out
    assert "hello" in output
    assert "user_id" in output
    with pytest.raises(json.JSONDecodeError):
        json.loads(output.strip())


def test_secrets_are_redacted_in_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", app_env="prod")

    get_logger("tests.redact").info("calling_api", api_key="k-123", bot_token="42:XYZ")

    output = capsys.readouterr().out
    assert "k-123" not in output
    assert "42:XYZ" not in output


@pytest.mark.parametrize(
    "key", ["token", "TELEGRAM_BOT_TOKEN", "api_key", "gemini_apikey", "Authorization", "password"]
)
def test_redact_secrets_masks_sensitive_keys(key: str) -> None:
    result = redact_secrets(None, "info", {"event": "x", key: "value"})
    assert result[key] == REDACTED


def test_redact_secrets_keeps_ordinary_keys() -> None:
    event = {"event": "x", "fixture_id": 5, "user_id": 7}
    assert redact_secrets(None, "info", dict(event)) == event


def test_dev_renders_plain_ascii_tracebacks(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", app_env="dev")
    try:
        raise ValueError("bad data")
    except ValueError:
        get_logger("tests.dev_exc").exception("failed")

    output = capsys.readouterr().out
    assert "Traceback (most recent call last)" in output
    assert "ValueError: bad data" in output


# --- Secret redaction edge cases (QA, T1) -------------------------------------------------

SECRET = "42:SUPERSECRET"


@pytest.mark.parametrize("app_env", ["dev", "prod"])
def test_secrets_are_redacted_in_every_renderer(
    capsys: pytest.CaptureFixture[str], app_env: Literal["dev", "prod"]
) -> None:
    configure_logging(level="INFO", app_env=app_env)

    get_logger("tests.redact").info("calling_api", telegram_bot_token=SECRET)

    output = capsys.readouterr().out
    assert SECRET not in output
    assert REDACTED in output


def test_secrets_bound_as_context_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", app_env="prod")
    structlog.contextvars.bind_contextvars(api_key=SECRET)
    try:
        get_logger("tests.ctx").info("with_context")
        get_logger("tests.ctx").bind(client_secret=SECRET).info("with_bound_logger")
    finally:
        structlog.contextvars.clear_contextvars()

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert SECRET not in json.dumps(lines)
    assert [line["api_key"] for line in lines] == [REDACTED, REDACTED]
    assert lines[1]["client_secret"] == REDACTED


def test_redaction_never_masks_the_event_name() -> None:
    # Only keys are inspected: an event *named* after a token stays readable.
    event = {"event": "token_refreshed", "user_id": 7}

    assert redact_secrets(None, "info", dict(event)) == event


def test_secrets_nested_in_mappings_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", app_env="prod")

    get_logger("tests.nested").info("http_call", headers={"Authorization": f"Bearer {SECRET}"})

    assert SECRET not in capsys.readouterr().out


def test_prod_exception_logs_do_not_render_frame_locals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", app_env="prod")

    def connect() -> None:
        url = f"https://api.telegram.org/bot{SECRET}/getMe"
        raise ConnectionError(len(url))

    try:
        connect()
    except ConnectionError:
        get_logger("tests.locals").exception("request_failed")

    output = capsys.readouterr().out
    assert "request_failed" in output
    assert SECRET not in output


# --- T1 fix round: key rules, nesting, value scrubbing ----------------------------------------

CONFIGURED_SECRET = "AIzaSy-CONFIGURED-KEY-0123456789"


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "bot_token",
        "TELEGRAM_BOT_TOKEN",
        "api_key",
        "apikey",
        "gemini_api_key",
        "x-api-key",
        "Authorization",
        "password",
        "db_password",
        "secret",
        "client_secret",
    ],
)
def test_credential_like_keys_are_sensitive(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize(
    "key",
    ["prompt_tokens", "completion_tokens", "tokens_used", "total_tokens", "event", "user_id"],
)
def test_token_counters_and_ordinary_keys_are_not_sensitive(key: str) -> None:
    assert not is_sensitive_key(key)


def test_token_counters_stay_visible_in_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", app_env="prod")

    get_logger("tests.llm").info("gemini_call", prompt_tokens=120, completion_tokens=30)

    record = last_json_line(capsys.readouterr().out)
    assert record["prompt_tokens"] == 120
    assert record["completion_tokens"] == 30


def test_nested_mappings_and_sequences_are_redacted() -> None:
    event = {
        "event": "http_call",
        "headers": {"Authorization": "Bearer abc", "accept": "json"},
        "attempts": [{"api_key": "k1", "status": 429}, ("x", {"token": "t"})],
    }

    result = redact_secrets(None, "info", event)

    assert result["headers"] == {"Authorization": REDACTED, "accept": "json"}
    assert result["attempts"] == [{"api_key": REDACTED, "status": 429}, ["x", {"token": REDACTED}]]


def test_configured_secret_values_are_scrubbed_from_any_string() -> None:
    redactor = SecretRedactor([CONFIGURED_SECRET])
    event = {
        "event": f"calling with {CONFIGURED_SECRET}",
        "url": f"https://v3.football.api-sports.io/odds?apiKey={CONFIGURED_SECRET}&page=2",
        "nested": {"note": [f"key={CONFIGURED_SECRET}"]},
        "count": 3,
    }

    result = redactor(None, "info", event)

    assert CONFIGURED_SECRET not in str(result)
    assert result["url"] == f"https://v3.football.api-sports.io/odds?apiKey={REDACTED}&page=2"
    assert result["count"] == 3


def test_overlapping_secrets_are_fully_scrubbed() -> None:
    redactor = SecretRedactor(["ABCDEFGH", "ABCDEFGH-LONGER"])

    assert redactor(None, "info", {"event": "ABCDEFGH-LONGER"})["event"] == REDACTED


def test_very_short_secret_values_are_not_used_for_scrubbing() -> None:
    redactor = SecretRedactor(["42"])

    assert redactor(None, "info", {"event": "fixture 42"})["event"] == "fixture 42"


@pytest.mark.parametrize("app_env", ["dev", "prod"])
def test_configured_secrets_are_scrubbed_from_events_and_tracebacks(
    capsys: pytest.CaptureFixture[str], app_env: Literal["dev", "prod"]
) -> None:
    configure_logging(level="INFO", app_env=app_env, secret_values=[CONFIGURED_SECRET])

    try:
        msg = f"request to https://host/odds?apiKey={CONFIGURED_SECRET} failed"
        raise ConnectionError(msg)
    except ConnectionError:
        get_logger("tests.scrub").exception(f"failed {CONFIGURED_SECRET}")
    logging.getLogger("aiogram.test").warning("stdlib %s", CONFIGURED_SECRET)

    output = capsys.readouterr().out
    assert "ConnectionError" in output
    assert CONFIGURED_SECRET not in output
    assert output.count(REDACTED) >= 3
