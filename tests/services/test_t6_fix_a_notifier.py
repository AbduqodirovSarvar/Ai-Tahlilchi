"""T6 fix round A: the notifier adapter's small paths (log-only notifier, references, alerts)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.pipeline.delivery import UNDELIVERABLE_REFERENCE
from app.pipeline.redaction import REDACTED
from app.services.interfaces import AdminAlert, DeliveryKey, MessageRef
from app.services.real.notifier import (
    LogOnlyNotifier,
    NotifierDelivery,
    _decode,
    _encode,
    _reference,
)

KEY = DeliveryKey(
    job="daily", day=datetime(2026, 10, 23, tzinfo=UTC).date(), user_id=1, subject="x"
)
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


async def test_the_log_only_notifier_logs_everything_and_delivers_nothing() -> None:
    notifier = LogOnlyNotifier()
    digest: Any = SimpleNamespace(result=SimpleNamespace(signals=(1, 2)))
    update: Any = SimpleNamespace(kind="h24")
    cancelled: Any = SimpleNamespace(reason="postponed")
    event: Any = SimpleNamespace(kind="lineups")
    summary: Any = SimpleNamespace(changes=())

    assert await notifier.send_daily_digest(1, digest, key=KEY) is None
    assert await notifier.send_signal_update(1, update, key=KEY) is None
    assert await notifier.send_signal_cancelled(1, cancelled, key=KEY) is None
    assert await notifier.send_watchlist_event(1, event, key=KEY) is None
    assert await notifier.send_result_settled(1, SimpleNamespace(), key=KEY) is None  # type: ignore[arg-type]
    assert await notifier.send_learning_summary(1, summary, key=KEY) is None
    await notifier.alert_admin(AdminAlert(component="odds", message="down", at=NOW))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, None),
        ("", None),
        ("no-colon", None),
        ("5:7", MessageRef(5, 7)),
        ("-100:42", MessageRef(-100, 42)),
    ],
)
def test_message_references_round_trip_or_are_ignored(
    text: str | None, expected: MessageRef | None
) -> None:
    assert _decode(text) == expected
    if expected is not None:
        assert _decode(_encode(expected)) == expected
    assert _encode(None) is None


@dataclass
class Unreachable:
    """A notifier that knows which users blocked the bot (contract v1.6)."""

    blocked: set[int] = field(default_factory=set)

    def is_undeliverable(self, user_id: int) -> bool:
        return user_id in self.blocked


def test_a_missing_confirmation_retries_unless_the_user_is_permanently_unreachable() -> None:
    notifier: Any = Unreachable(blocked={7})

    assert _reference(notifier, 7, None) == UNDELIVERABLE_REFERENCE
    assert _reference(notifier, 8, None) is None  # reachable: not delivered, retry
    assert _reference(notifier, 7, MessageRef(1, 2)) == "1:2"  # delivered wins
    assert _reference(LogOnlyNotifier(), 7, None) is None  # no such knowledge: retry


@dataclass
class RecordingAlerts:
    alerts: list[AdminAlert] = field(default_factory=list)

    async def alert_admin(self, alert: AdminAlert) -> None:
        self.alerts.append(alert)


async def test_the_admin_alert_is_forwarded_without_credentials() -> None:
    recorder = RecordingAlerts()
    delivery = NotifierDelivery(SimpleNamespace(), recorder)  # type: ignore[arg-type]

    await delivery.alert_admin("odds", "failed: https://x.example/?apikey=SECRETSECRET123", NOW)

    [alert] = recorder.alerts
    assert (alert.component, alert.at) == ("odds", NOW)
    assert "SECRETSECRET123" not in alert.message
    assert REDACTED in alert.message
