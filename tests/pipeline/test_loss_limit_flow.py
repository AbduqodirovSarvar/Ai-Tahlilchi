"""Loss limits stop new signals (plan 6.9) in the digest and in templates, not manual analysis."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.db.enums import Outcome
from app.db.repo import create_bet, ensure_user, settle_bet
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.daily import run_due_digests
from app.pipeline.ledger import LedgerKey, backoff
from app.pipeline.limit_notice import LIMIT_DETAIL, NOTICE_JOB
from app.pipeline.limits import LimitPeriod
from app.pipeline.templates import run_due_templates
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    TemplateDraft,
    TemplateSchedule,
)
from app.services.interfaces import LimitPeriod as ServicePeriod
from tests.pipeline.doubles import RecordingDelivery
from tests.pipeline.harness import Env

USER = 5005
D = Decimal
TODAY = date(2026, 10, 21)  # the seeded world's "today" (a Wednesday)
PERMISSIVE = {"min_probability": 0.30, "min_ev": 0.0}


async def tune(env: Env, **changes: object) -> None:
    """Loose thresholds, no quiet hours, plus ``changes`` (e.g. the loss limits)."""
    services = env.system.services
    settings = await services.settings.get_settings(USER)
    values = {**PERMISSIVE, "quiet_hours": None, **changes}
    await services.settings.update_settings(USER, replace(settings, **values))


async def lose(env: Env, amount: str, settled_at: datetime) -> None:
    """A settled losing bet of ``amount`` money units."""
    async with env.sessions.begin() as session:
        await ensure_user(session, USER, timezone="Asia/Tashkent")
        bet = await create_bet(
            session,
            user_id=USER,
            signal_id=None,
            odds_taken=2.0,
            stake=D(amount),
            placed_at=settled_at - timedelta(hours=2),
        )
        await settle_bet(
            session,
            bet.id,
            user_id=USER,
            outcome=Outcome.LOSS,
            profit=-D(amount),
            settled_at=settled_at,
        )


def morning() -> datetime:
    """Earlier today (Tashkent 10:00), before the clock's "now" of 11:00."""
    return datetime(2026, 10, 21, 5, 0, tzinfo=UTC)


async def test_the_digest_at_the_exact_daily_limit_sends_the_notice_instead(
    seeded_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await tune(seeded_env, daily_loss_limit=D("100.00"))
    await lose(seeded_env, "100.00", morning())
    runs: list[int] = []
    original = AnalysisEngine.run

    async def counting(self: AnalysisEngine, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        runs.append(1)
        return await original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(AnalysisEngine, "run", counting)
    delivery = RecordingDelivery()

    report = await run_due_digests(seeded_env.ctx, delivery)

    assert report.sent == (USER,)
    [(_, message, key)] = delivery.digests
    assert message.outcome.cards == ()
    assert message.loss_limit is not None
    assert message.loss_limit.period is LimitPeriod.DAY
    assert (message.loss_limit.limit, message.loss_limit.loss) == (D("100.00"), D("100.00"))
    assert runs == []  # no analysis, no LLM, no new signals
    entry = await seeded_env.ctx.ledger.get(key)
    assert entry is not None
    assert entry.detail == LIMIT_DETAIL


async def test_one_cent_below_the_limit_the_digest_is_normal(seeded_env: Env) -> None:
    await tune(seeded_env, daily_loss_limit=D("100.00"))
    await lose(seeded_env, "99.99", morning())
    delivery = RecordingDelivery()

    await run_due_digests(seeded_env.ctx, delivery)

    [(_, message, _)] = delivery.digests
    assert message.loss_limit is None
    assert message.outcome.cards


async def test_the_user_is_told_once_a_day_however_many_deliveries_are_blocked(
    seeded_env: Env,
) -> None:
    await tune(seeded_env, daily_loss_limit=D(50))
    await lose(seeded_env, "80", morning())
    services = seeded_env.system.services
    query = AnalysisQuery(dates=DateSpec.next_days(2), **PERMISSIVE)
    schedule = TemplateSchedule(weekdays=frozenset(range(1, 8)), at=time(11, 0))
    for name in ("Birinchi", "Ikkinchi"):
        await services.templates.save_template(
            USER, TemplateDraft(name=name, query=query, schedule=schedule)
        )
    delivery = RecordingDelivery()

    await run_due_digests(seeded_env.ctx, delivery)
    templates = await run_due_templates(seeded_env.ctx, delivery)
    await run_due_templates(seeded_env.ctx, delivery)

    assert len(delivery.digests) == 1  # one notice for the digest and both templates
    assert len(templates.sent) == 2  # both templates ran their course (silently)
    notice_key = LedgerKey(NOTICE_JOB, TODAY, USER, "notice")
    assert await seeded_env.ctx.ledger.is_done(notice_key)


async def test_a_template_blocked_by_the_limit_sends_the_notice_and_no_signals(
    seeded_env: Env,
) -> None:
    await tune(seeded_env, daily_loss_limit=D(50))
    await lose(seeded_env, "50", morning())
    services = seeded_env.system.services
    await services.templates.save_template(
        USER,
        TemplateDraft(
            name="Tezkor",
            query=AnalysisQuery(dates=DateSpec.today(), **PERMISSIVE),
            schedule=TemplateSchedule(weekdays=frozenset(range(1, 8)), at=time(11, 0)),
        ),
    )
    delivery = RecordingDelivery()

    await run_due_templates(seeded_env.ctx, delivery)

    [(_, message, _)] = delivery.digests
    assert message.label is None  # the notice is not a template digest
    assert message.loss_limit is not None
    assert message.outcome.cards == ()


async def test_the_next_local_day_starts_clean(seeded_env: Env) -> None:
    await tune(seeded_env, daily_loss_limit=D(50))
    await lose(seeded_env, "70", morning())
    delivery = RecordingDelivery()
    await run_due_digests(seeded_env.ctx, delivery)

    seeded_env.clock.now = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)  # Thursday 11:00 local
    await run_due_digests(seeded_env.ctx, delivery)

    assert [m.loss_limit is not None for _, m, _ in delivery.digests] == [True, False]


async def test_the_weekly_limit_blocks_until_the_week_turns(seeded_env: Env) -> None:
    await tune(seeded_env, weekly_loss_limit=D(100))
    await lose(seeded_env, "60", datetime(2026, 10, 19, 6, 0, tzinfo=UTC))  # Monday
    await lose(seeded_env, "40", morning())  # Wednesday: the week reaches 100
    delivery = RecordingDelivery()
    await run_due_digests(seeded_env.ctx, delivery)

    seeded_env.clock.now = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)  # Saturday, same ISO week
    await run_due_digests(seeded_env.ctx, delivery)
    seeded_env.clock.now = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)  # next Monday
    await run_due_digests(seeded_env.ctx, delivery)

    kinds = [m.loss_limit.period if m.loss_limit else None for _, m, _ in delivery.digests]
    assert kinds == [LimitPeriod.WEEK, LimitPeriod.WEEK, None]


async def test_an_unconfirmed_notice_is_retried_like_any_digest(seeded_env: Env) -> None:
    await tune(seeded_env, daily_loss_limit=D(50))
    await lose(seeded_env, "70", morning())
    delivery = RecordingDelivery()
    delivery.answers = [None]  # not delivered

    first = await run_due_digests(seeded_env.ctx, delivery)
    seeded_env.clock.advance(backoff(1))
    second = await run_due_digests(seeded_env.ctx, delivery)
    third = await run_due_digests(seeded_env.ctx, delivery)

    assert first.failed == (USER,)
    assert second.sent == (USER,)
    assert third.sent == third.failed == ()
    assert all(m.loss_limit is not None for _, m, _ in delivery.digests)


async def test_through_the_notifier_the_notice_is_a_digest_with_no_signals(
    seeded_env: Env,
) -> None:
    await tune(seeded_env, daily_loss_limit=D(50))
    await lose(seeded_env, "75.50", morning())

    await run_due_digests(seeded_env.ctx, seeded_env.system.delivery)

    [sent] = [n for n in seeded_env.notifier.sent if n.method == "daily_digest"]
    digest = sent.payload
    assert digest.result.signals == ()  # type: ignore[attr-defined]
    notice = digest.loss_limit  # type: ignore[attr-defined]
    assert (notice.period, notice.limit, notice.loss) == (
        ServicePeriod.DAY,
        D(50),
        D("75.50"),
    )


async def test_a_manual_analysis_still_works_but_carries_the_banner(seeded_env: Env) -> None:
    await tune(seeded_env, daily_loss_limit=D(50))
    services = seeded_env.system.services
    query = AnalysisQuery(dates=DateSpec.next_days(2), **PERMISSIVE)
    clean = await services.analysis.analyze(USER, query)
    await lose(seeded_env, "50", morning())

    limited = await services.analysis.analyze(USER, query)

    assert clean.loss_limit is None
    assert limited.signals  # custom analysis is never blocked
    assert limited.loss_limit is not None
    assert limited.loss_limit.period is ServicePeriod.DAY
    bank = await services.bets.bank_summary(USER)
    assert bank.loss_limit_reached  # the same rule the /bank screen shows
    assert bank.loss_today == D(50)


async def test_a_look_back_carries_no_banner(seeded_env: Env) -> None:
    await tune(seeded_env, daily_loss_limit=D(50))
    await lose(seeded_env, "80", morning())

    result = await seeded_env.system.services.analysis.analyze(
        USER,
        AnalysisQuery(dates=DateSpec.between(date(2026, 10, 14), date(2026, 10, 16)), **PERMISSIVE),
    )

    assert result.retro
    assert result.loss_limit is None
