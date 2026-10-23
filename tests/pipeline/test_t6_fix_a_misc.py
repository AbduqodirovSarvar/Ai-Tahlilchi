"""T6 fix round A: secret redaction, the outbox error paths and the clock."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.pipeline.alerts import alert_throttled
from app.pipeline.clock import system_clock
from app.pipeline.ledger import MAX_ATTEMPTS, Ledger, LedgerKey
from app.pipeline.outbox import deliver_watch_event
from app.pipeline.redaction import REDACTED, register_secrets, scrub_text
from tests.pipeline.test_learning import Clock, context

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
BOT_TOKEN = "123456789:AAH3k2j4h5g6f7d8s9a0qwertyuiopASDFGH"
GEMINI_KEY = "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"


# ---------------------------------------------------------------------------- redaction
@pytest.fixture(autouse=True)
def _no_registered_secrets() -> Any:
    register_secrets(())
    yield
    register_secrets(())


@pytest.mark.parametrize(
    "text",
    [
        f"HTTP 401 for https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        f"GET https://x.example/v1/models?key={GEMINI_KEY}&alt=json failed",
        f"bad key {GEMINI_KEY}",
        "request failed: Authorization: Bearer abcdef0123456789abcdef",
        "headers {'x-apisports-key': 'deadbeefdeadbeefdeadbeef'}".replace("'", ""),
        "https://hc-ping.com/ping?token=11111111-2222-3333-4444-555555555555",
    ],
)
def test_credential_shapes_are_masked(text: str) -> None:
    cleaned = scrub_text(text)

    for secret in (
        BOT_TOKEN,
        GEMINI_KEY,
        "abcdef0123456789abcdef",
        "deadbeefdeadbeef",
        "11111111-2222",
    ):
        assert secret not in cleaned
    assert REDACTED in cleaned


def test_a_registered_secret_value_is_masked_wherever_it_appears() -> None:
    register_secrets(["super-secret-value", "short"])  # "short" is under the minimum length

    cleaned = scrub_text("failed with super-secret-value, short and super-secret-value again")

    assert "super-secret-value" not in cleaned
    assert "short" in cleaned


def test_ordinary_text_passes_unchanged_and_is_cut_to_the_limit() -> None:
    assert scrub_text("timeout after 20 s on odds?league=39&season=2026") == (
        "timeout after 20 s on odds?league=39&season=2026"
    )
    assert len(scrub_text("x" * 5000)) == 500
    assert scrub_text("") == ""


@given(st.text(max_size=300))
def test_scrubbing_never_grows_the_text_and_is_idempotent(text: str) -> None:
    once = scrub_text(text)
    assert len(once) <= 500
    assert scrub_text(once) == once


async def test_errors_are_scrubbed_before_they_reach_the_ledger_and_the_status(
    ledger: Ledger,
) -> None:
    await ledger.record_error("odds", f"fetch failed: {GEMINI_KEY}", now=NOW)
    failure = LedgerKey("send", NOW.date(), 1, "x")
    await ledger.fail(failure, now=NOW, detail=f"bot{BOT_TOKEN} down")

    [(_, component, message)] = await ledger.recent_errors()
    entry = await ledger.get(failure)

    assert component == "odds"
    assert GEMINI_KEY not in message
    assert entry is not None
    assert entry.detail is not None
    assert BOT_TOKEN not in entry.detail


async def test_a_digest_stored_in_the_ledger_is_kept_verbatim(ledger: Ledger) -> None:
    key = LedgerKey("digest", NOW.date(), 1, "stored")
    payload = '{"cards": [' + '{"a": 1},' * 400 + "]}"  # far over the error limit

    await ledger.complete(key, now=NOW, detail=payload)

    entry = await ledger.get(key)
    assert entry is not None
    assert entry.detail == payload


@dataclass
class RecordingAlerts:
    alerts: list[tuple[str, str]]

    async def alert_admin(self, component: str, message: str, at: datetime) -> None:  # noqa: ARG002
        self.alerts.append((component, message))


async def test_admin_alerts_never_carry_a_credential(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    ctx = context(sessions, ledger, Clock(NOW))
    delivery = RecordingAlerts([])

    sent = await alert_throttled(
        ctx,
        delivery,  # type: ignore[arg-type]
        "odds",
        f"HTTPStatusError for https://x.example/odds?apikey={GEMINI_KEY}",
    )

    assert sent
    [(_, message)] = delivery.alerts
    assert GEMINI_KEY not in message
    assert "HTTPStatusError" in message  # the useful part survives


# ---------------------------------------------------------------------------- outbox
@dataclass
class WatchDeliveryDouble:
    answer: object = "100:1"
    error: Exception | None = None
    calls: int = 0

    async def watch_event(self, user_id: int, message: object, key: LedgerKey) -> object:  # noqa: ARG002
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.answer


def watch_message() -> Any:
    return SimpleNamespace(event="result", fixture=SimpleNamespace(id=5))


WATCH_KEY = LedgerKey("watch.result", NOW.date(), 1, "5")


async def test_a_delivered_watch_event_is_marked_done_and_not_sent_twice(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    ctx = context(sessions, ledger, Clock(NOW))
    delivery = WatchDeliveryDouble()

    first = await deliver_watch_event(ctx, delivery, 1, watch_message(), WATCH_KEY)  # type: ignore[arg-type]
    second = await deliver_watch_event(ctx, delivery, 1, watch_message(), WATCH_KEY)  # type: ignore[arg-type]

    assert (first, second) == (True, False)
    assert delivery.calls == 1
    assert await ledger.is_done(WATCH_KEY)


@pytest.mark.parametrize(
    "delivery",
    [WatchDeliveryDouble(answer=None), WatchDeliveryDouble(error=ConnectionError("telegram down"))],
)
async def test_an_undelivered_watch_event_is_a_recorded_failure_that_never_raises(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger, delivery: WatchDeliveryDouble
) -> None:
    clock = Clock(NOW)
    ctx = context(sessions, ledger, clock)

    sent = await deliver_watch_event(ctx, delivery, 1, watch_message(), WATCH_KEY)  # type: ignore[arg-type]

    assert sent is False
    entry = await ledger.get(WATCH_KEY)
    assert entry is not None
    assert entry.attempts == 1
    assert not await ledger.is_done(WATCH_KEY)
    # still inside the backoff the key cannot be claimed; after it the event is retried
    assert await deliver_watch_event(ctx, delivery, 1, watch_message(), WATCH_KEY) is False  # type: ignore[arg-type]
    assert delivery.calls == 1
    clock.now += timedelta(days=1)
    assert await deliver_watch_event(ctx, delivery, 1, watch_message(), WATCH_KEY) is False  # type: ignore[arg-type]
    assert delivery.calls == 2


async def test_a_watch_event_stops_being_retried_after_the_last_attempt(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    clock = Clock(NOW)
    ctx = context(sessions, ledger, clock)
    delivery = WatchDeliveryDouble(answer=None)

    for _ in range(MAX_ATTEMPTS + 2):
        await deliver_watch_event(ctx, delivery, 1, watch_message(), WATCH_KEY)  # type: ignore[arg-type]
        clock.now += timedelta(days=1)

    assert delivery.calls == MAX_ATTEMPTS


# ---------------------------------------------------------------------------- clock
def test_the_system_clock_is_aware_utc_and_moves_forward() -> None:
    before = datetime.now(UTC)
    now = system_clock()
    after = datetime.now(UTC)

    assert now.utcoffset() == timedelta(0)
    assert before <= now <= after
