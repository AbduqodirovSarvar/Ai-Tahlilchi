"""Tests for app.logging."""

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from app.logging import REDACTED, configure_logging, get_logger, redact_secrets


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """configure_logging mutates process-global state; put it back after each test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers, root.level = handlers, level
    structlog.reset_defaults()


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
