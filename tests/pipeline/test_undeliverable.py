"""Contract v1.6: a user who blocked the bot is "handled", not retried five times."""

from pathlib import Path

from app.pipeline.config import PipelineConfig
from app.pipeline.delivery import UNDELIVERABLE_REFERENCE
from app.pipeline.ledger import backoff
from app.pipeline.settle import run_settlement
from app.services.interfaces import DeliveryKey, MessageRef, SettledNotice, UndeliverableAware
from tests.integration.t4_support import USER, FlakyNotifier, open_bare, permissive
from tests.integration.test_t4_hand_league import KICKOFF, MODEL_WEIGHT
from tests.integration.test_t4_resilience import two_finished_signals

CONFIG = PipelineConfig(model_weight=MODEL_WEIGHT)


class BlockedUserNotifier(FlakyNotifier):
    """Refuses every message of ``blocked`` users and says so through the v1.6 protocol."""

    def __init__(self, blocked: set[int]) -> None:
        super().__init__()
        self.blocked = blocked
        self.attempts = 0

    def is_undeliverable(self, user_id: int) -> bool:
        return user_id in self.blocked

    async def send_result_settled(
        self, user_id: int, notice: SettledNotice, *, key: DeliveryKey
    ) -> MessageRef | None:
        self.attempts += 1
        if user_id in self.blocked:
            return None
        return await super().send_result_settled(user_id, notice, key=key)


def test_the_blocked_user_notifier_satisfies_the_protocol() -> None:
    assert isinstance(BlockedUserNotifier(set()), UndeliverableAware)
    assert not isinstance(FlakyNotifier(), UndeliverableAware)


async def test_a_blocked_user_is_tried_once_and_never_alerted(tmp_path: Path) -> None:
    notifier = BlockedUserNotifier({USER})
    async with open_bare(
        tmp_path / "blocked.db",
        None,
        config=CONFIG,
        notifier=notifier,
        now=KICKOFF.replace(hour=8),
    ) as bare:
        await permissive(bare.system.services, USER)
        await two_finished_signals(bare)
        reports = []
        for attempt in range(1, 8):
            reports.append(await run_settlement(bare.ctx, bare.system.delivery))
            bare.clock.advance(backoff(attempt))

    assert notifier.attempts == 2  # one try per result message, then the key is done
    assert sum(r.announce_failed for r in reports) == 0
    assert [n for n in notifier.sent if n.method == "alert_admin"] == []


async def test_an_unknown_failure_is_still_retried(tmp_path: Path) -> None:
    """Only a notifier that vouches for "permanent" stops the retries."""
    notifier = BlockedUserNotifier(set())
    notifier.fail["result_settled"] = 1
    async with open_bare(
        tmp_path / "flaky.db",
        None,
        config=CONFIG,
        notifier=notifier,
        now=KICKOFF.replace(hour=8),
    ) as bare:
        await permissive(bare.system.services, USER)
        await two_finished_signals(bare)
        first = await run_settlement(bare.ctx, bare.system.delivery)
        bare.clock.advance(backoff(1))
        second = await run_settlement(bare.ctx, bare.system.delivery)

    assert first.announce_failed == 1
    assert second.notified == 1


def test_the_undeliverable_reference_is_not_a_reply_target() -> None:
    from app.services.real.notifier import _decode  # noqa: PLC0415

    assert _decode(UNDELIVERABLE_REFERENCE) is None
