"""QA (T5 bot): per-user isolation and access control of every new button, REAL services.

User B (a second whitelisted user) replays callback_data copied from user A's keyboards. Each
button must either be refused or act on B's own (empty) data; A's bets, templates, watchlist,
signals and notification switches must come out untouched and nothing of A's may leak into
B's replies. Strangers (not whitelisted) get no API call at all, and the plain-text team
search cannot be reached by them nor flooded by an admin.
"""

from datetime import time
from decimal import Decimal

import pytest

from app.bot import texts
from app.bot.callbacks import (
    BankCb,
    BetCb,
    CardCb,
    CustomCb,
    ExpressCb,
    ReportCb,
    TplCb,
    WatchCb,
)
from app.bot.middleware import RateLimitMiddleware
from app.bot.watch_ui import entity_key
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    ResultMark,
    SignalState,
    TemplateDraft,
    TemplateSchedule,
    WatchKind,
    WatchNotification,
    WatchTarget,
)
from tests.e2e.bot_client import Client, answers, shown
from tests.e2e.real_world import ADMIN, SECOND, STRANGER, RealBot

pytestmark = pytest.mark.slow

SECRET_TEMPLATE = "SECRET-TPL-A"
SECRET_STAKE = 123_457


def _everything_said(methods: list[object]) -> str:
    """All visible text of a reply: messages, edits and callback alerts."""
    alerts = [a.text or "" for a in answers(methods)]  # type: ignore[arg-type]
    return "\n".join([*shown(methods), *alerts])  # type: ignore[arg-type]


class Owner:
    """What user A owns, created through the real services."""

    def __init__(self) -> None:
        self.signal_id = 0
        self.bet_id = 0
        self.template_id = 0
        self.team_id = ""
        self.team_name = ""
        self.fixture_label = ""


async def _own_things(rb: RealBot) -> Owner:
    svc = rb.services
    owner = Owner()
    await rb.settings(ADMIN, bank=Decimal(1000), unit=Decimal(10))
    signal = (await rb.signals())[0]
    owner.signal_id, owner.fixture_label = signal.signal_id, signal.fixture_label
    bet = await rb.bet(signal, SECRET_STAKE)
    owner.bet_id = bet.bet_id
    query = AnalysisQuery(dates=DateSpec.next_days(3), min_probability=0.4, min_ev=0.02)
    schedule = TemplateSchedule(weekdays=frozenset({1, 3}), at=time(9, 0))
    template = await svc.templates.save_template(
        ADMIN, TemplateDraft(name=SECRET_TEMPLATE, query=query, schedule=schedule)
    )
    owner.template_id = template.template_id
    (team,) = (await svc.catalog.search_teams(signal.home_team, limit=1))[:1]
    owner.team_id, owner.team_name = team.team_id, team.name
    target = WatchTarget(kind=WatchKind.TEAM, entity_id=team.team_id)
    await svc.watchlist.add_watch(ADMIN, target)
    await svc.watchlist.set_notifications(ADMIN, target, frozenset({WatchNotification.LINEUPS}))
    return owner


def _forged(owner: Owner) -> list[str]:
    """callback_data of every new button that names one of A's objects."""
    key = entity_key(owner.team_id)
    return [
        BankCb(action="bet", id=owner.bet_id).pack(),
        BankCb(action="mark", id=owner.bet_id, value="w").pack(),
        BankCb(action="mark", id=owner.bet_id, value="l").pack(),
        BankCb(action="mark", id=owner.bet_id, value="c").pack(),
        TplCb(action="open", id=owner.template_id).pack(),
        TplCb(action="run", id=owner.template_id).pack(),
        TplCb(action="delete", id=owner.template_id).pack(),
        TplCb(action="delete_yes", id=owner.template_id).pack(),
        TplCb(action="schedule", id=owner.template_id).pack(),
        TplCb(action="day", id=owner.template_id, value=2).pack(),
        TplCb(action="time", id=owner.template_id, value=1).pack(),
        TplCb(action="save", id=owner.template_id).pack(),
        TplCb(action="clear", id=owner.template_id).pack(),
        WatchCb(action="open", kind="t", key=key).pack(),
        WatchCb(action="notify", kind="t", key=key, flag="l").pack(),
        WatchCb(action="notify", kind="t", key=key, flag="o").pack(),
        WatchCb(action="remove", kind="t", key=key).pack(),
        WatchCb(action="card", kind="h", key=str(owner.signal_id)).pack(),
        CardCb(action="detail", signal_id=owner.signal_id).pack(),
        CardCb(action="card", signal_id=owner.signal_id).pack(),
        CardCb(action="bet", signal_id=owner.signal_id).pack(),
        CardCb(action="hide", signal_id=owner.signal_id).pack(),
        CardCb(action="watch", signal_id=owner.signal_id).pack(),
        CardCb(action="recalc", signal_id=owner.signal_id).pack(),
        BetCb(action="ok", signal_id=owner.signal_id).pack(),
        BetCb(action="force", signal_id=owner.signal_id).pack(),
        BetCb(action="custom", signal_id=owner.signal_id).pack(),
        ExpressCb(action="toggle", index=0).pack(),
        ExpressCb(action="calc").pack(),
        CustomCb(action="toggle", index=0).pack(),
        CustomCb(action="run").pack(),
        ReportCb(action="chart", period="all", group="sport").pack(),
        ReportCb(action="export").pack(),
    ]


async def test_forged_callbacks_of_another_user_change_nothing_and_leak_nothing(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    owner = await _own_things(rb)
    rb.finish_matches()  # even a started match must not let B settle A's bet
    intruder = rb.client(SECOND)
    leaks = (SECRET_TEMPLATE, str(SECRET_STAKE), owner.team_name, owner.fixture_label)

    for data in _forged(owner):
        methods = await intruder.press_data(data, message_id=90)
        said = _everything_said(methods)
        for secret in leaks:
            assert secret not in said, f"{data} leaked {secret!r}"

    svc = rb.services
    (bet,) = await svc.bets.list_bets(ADMIN)
    assert bet.outcome is None
    assert bet.stake == Decimal(SECRET_STAKE)
    (template,) = await svc.templates.list_templates(ADMIN)
    assert template.schedule == TemplateSchedule(weekdays=frozenset({1, 3}), at=time(9, 0))
    (watched,) = await svc.watchlist.list_watchlist(ADMIN)
    assert watched.notifications == frozenset({WatchNotification.LINEUPS})
    assert (await svc.signals.get_signal(ADMIN, owner.signal_id)).state is SignalState.ACTIVE
    assert not await svc.bets.list_bets(SECOND)
    assert not await svc.templates.list_templates(SECOND)
    assert not await svc.watchlist.list_watchlist(SECOND)


async def test_the_owner_still_works_after_the_intruder_replayed_everything(
    real_bot: RealBot,
) -> None:
    """Control: the same data from the real owner is accepted (the test above is not vacuous)."""
    rb = real_bot
    owner = await _own_things(rb)
    rb.finish_matches()
    admin = rb.client(ADMIN)

    methods = await admin.press_data(BankCb(action="mark", id=owner.bet_id, value="w").pack(), 90)

    assert texts.BANK_MARKED.format(result=texts.RESULT_LABELS[ResultMark.WON]) in "\n".join(
        shown(methods)
    )
    (bet,) = await rb.services.bets.list_bets(ADMIN)
    assert bet.outcome is ResultMark.WON


async def test_the_other_users_exports_and_reports_hold_only_their_own_rows(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    await _own_things(rb)
    rb.finish_matches()
    intruder = rb.client(SECOND)

    csv_bets = await rb.services.reports.export_bets(SECOND)
    csv_signals = await rb.services.reports.export(SECOND)
    report = await intruder.send("/hisobot")

    assert csv_bets.content.decode().strip().count("\n") == 0  # header only
    assert csv_signals.content.decode().strip().count("\n") == 0
    assert str(SECRET_STAKE) not in "\n".join(shown(report))
    assert texts.REPORT_EMPTY in "\n".join(shown(report))


# --------------------------------------------------------------------------- strangers
async def test_a_stranger_gets_no_reply_to_any_new_entry_point(real_bot: RealBot) -> None:
    rb = real_bot
    owner = await _own_things(rb)
    stranger = rb.client(STRANGER)
    typed = [
        texts.MENU_CUSTOM,
        texts.MENU_WATCHLIST,
        texts.MENU_TEMPLATES,
        texts.MENU_BETS,
        texts.MENU_REPORT,
        "/bank",
        "/hisobot",
        "/eksport",
        "/ekspress",
        "/kuzat Arsenal",
        "Arsenal Chelsea",
        "Arsenal - Chelsea",
        "a",
    ]

    before = len(rb.session.requests)
    for text in typed:
        assert await stranger.send(text) == []
    for data in _forged(owner):
        assert await stranger.press_data(data, message_id=90) == []

    assert len(rb.session.requests) == before


async def test_a_stranger_in_an_admins_group_chat_is_ignored_too(real_bot: RealBot) -> None:
    rb = real_bot
    await _own_things(rb)
    group = Client(
        rb.dispatcher, rb.bot, rb.session, ADMIN, chat_id=-100500, chat_type="supergroup"
    )

    assert await group.send(texts.MENU_BETS) == []
    assert await group.send("Arsenal") == []
    assert await group.press_data(BankCb(action="open").pack(), 91) == []


async def test_plain_text_from_a_stranger_never_reaches_the_catalog(
    real_bot: RealBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    rb = real_bot
    calls: list[str] = []
    original = rb.services.catalog.search_teams

    async def spy(query: str, **kwargs: object) -> object:
        calls.append(query)
        return await original(query, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(rb.services.catalog, "search_teams", spy, raising=False)
    monkeypatch.setattr(rb.services.catalog, "search_fixtures", spy, raising=False)

    await rb.client(STRANGER).send("Arsenal Chelsea")
    assert calls == []
    await rb.client(ADMIN).send("Arsenal Chelsea")
    assert calls  # the control: an admin's text does search


async def test_admin_text_flood_is_rate_limited_before_it_hits_the_catalog(
    seeded_template: object, tmp_path: object, bot: object, session: object
) -> None:
    from pathlib import Path  # noqa: PLC0415

    from tests.e2e.real_world import open_real_bot  # noqa: PLC0415

    template, world = seeded_template  # type: ignore[misc]
    limiter = RateLimitMiddleware(capacity=5, refill_per_second=0.0001)
    async with open_real_bot(
        template,
        world,
        Path(str(tmp_path)) / "flood.db",
        bot,
        session,
        rate_limit=limiter,  # type: ignore[arg-type]
    ) as rb:
        calls: list[str] = []
        original = rb.services.catalog.search_teams

        async def spy(query: str, **kwargs: object) -> object:
            calls.append(query)
            return await original(query, **kwargs)  # type: ignore[arg-type]

        rb.services.catalog.search_teams = spy  # type: ignore[method-assign,assignment]
        admin = rb.client(ADMIN)

        for number in range(60):
            await admin.send(f"xyz team {number}")

        assert 0 < len(calls) <= 5 * 12  # at most the burst, each text costs a bounded few
        assert len(calls) < 60 * 2


async def test_every_main_menu_label_is_routed_to_its_screen_not_to_the_text_search(
    real_bot: RealBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.bot.keyboards import MENU_TEXTS  # noqa: PLC0415

    rb = real_bot
    searched: list[str] = []
    original = rb.services.catalog.search_teams

    async def spy(query: str, **kwargs: object) -> object:
        searched.append(query)
        return await original(query, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(rb.services.catalog, "search_teams", spy, raising=False)
    admin = rb.client(ADMIN)

    for label in sorted(MENU_TEXTS):
        await admin.send("/start")
        await admin.send(label)

    assert searched == []
