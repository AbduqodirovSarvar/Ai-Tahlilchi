"""T6 fix round A: the detail handlers when Telegram no longer exposes the card message."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.bot.callbacks import CardCb
from app.bot.handlers.detail import on_back_to_card, on_detail
from app.services.fake import create_fake_services
from app.services.interfaces import DateSpec

NOW = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
USER = 1001


def gone_callback() -> Any:
    """A callback whose message is inaccessible: answering still works, editing is skipped."""
    answered: list[object] = []

    async def answer(*_args: object, **_kwargs: object) -> None:
        answered.append(1)

    return SimpleNamespace(message=None, answer=answer, answered=answered)


async def first_signal_id() -> tuple[Any, str]:
    from app.services.interfaces import AnalysisQuery  # noqa: PLC0415

    services = create_fake_services(clock=lambda: NOW)
    result = await services.analysis.analyze(
        USER, AnalysisQuery(dates=DateSpec.next_days(3), include_no_value=True)
    )
    return services, result.signals[0].signal_id


async def test_detail_without_an_accessible_message_only_answers_the_tap() -> None:
    services, signal_id = await first_signal_id()
    callback = gone_callback()

    await on_detail(callback, CardCb(action="detail", signal_id=signal_id), services, USER)

    assert callback.answered == [1]


async def test_back_to_card_without_an_accessible_message_only_answers_the_tap() -> None:
    services, signal_id = await first_signal_id()
    callback = gone_callback()

    await on_back_to_card(
        callback, CardCb(action="card", signal_id=signal_id), services, USER, lambda: NOW
    )

    assert callback.answered == [1]
