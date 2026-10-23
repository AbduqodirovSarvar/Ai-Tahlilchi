"""The T5 bot fix round end to end: card ⭐ by id, paging edge cases, bank beyond 100 bets."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from aiogram import Dispatcher

from app.bot import texts
from app.bot.callbacks import BankCb, CardCb, ResultCb, TplCb, WatchCb
from app.bot.handlers.watch import CARD_AWAY, CARD_HOME, CARD_LEAGUE, _card_options
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    MarketType,
    ResultMark,
    Services,
    TemplateDraft,
    WatchKind,
    WatchTarget,
)
from tests.bot.fakes import ADMIN_ID, FIXED_NOW
from tests.bot.samples import make_signal
from tests.e2e.bot_client import Client, answers, shown
from tests.e2e.real_world import ADMIN, RealBot


class SpyCatalog:
    """Delegates to the real catalog and records which lookups were made."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.calls.append(name)
        return getattr(self._inner, name)


async def _first_signal(services: Services):  # type: ignore[no-untyped-def]
    result = await services.analysis.analyze(
        ADMIN_ID,
        AnalysisQuery(dates=DateSpec.next_days(3), min_probability=0.3, min_ev=0.0),
    )
    assert result.signals
    return result.signals[0]


# --------------------------------------------------------------------------- ⭐ on a card
async def test_the_card_star_uses_the_ids_on_the_card_and_searches_nothing(
    admin: Client, dispatcher: Dispatcher, services: Services
) -> None:
    signal = await _first_signal(services)
    assert signal.home_team_id and signal.away_team_id and signal.league_id
    spy = SpyCatalog(services.catalog)
    dispatcher["services"] = replace(services, catalog=spy)  # type: ignore[arg-type]

    methods = await admin.press_data(CardCb(action="watch", signal_id=signal.signal_id).pack(), 30)
    asked = admin.last_message_id  # the choice keyboard is a new message under the card
    choice = next(
        d for _, d in admin.live_buttons(asked) if d.startswith("wl:card") and f":{CARD_HOME}:" in d
    )
    await admin.press_data(choice, asked)

    assert spy.calls == []  # not a single catalog lookup: the ids come from the card
    assert shown(methods)
    followed = await services.watchlist.list_watchlist(ADMIN_ID)
    assert [i.target.entity_id for i in followed] == [signal.home_team_id]
    assert followed[0].target.kind is WatchKind.TEAM


async def test_each_card_choice_follows_exactly_the_entity_it_names(
    admin: Client, services: Services
) -> None:
    signal = await _first_signal(services)

    await admin.press_data(CardCb(action="watch", signal_id=signal.signal_id).pack(), 31)
    for code in (CARD_HOME, CARD_AWAY, CARD_LEAGUE):
        data = WatchCb(action="card", kind=code, key=str(signal.signal_id)).pack()
        await admin.press_data(data, 31)

    targets = {i.target for i in await services.watchlist.list_watchlist(ADMIN_ID)}
    assert targets == {
        WatchTarget(WatchKind.TEAM, signal.home_team_id),  # type: ignore[arg-type]
        WatchTarget(WatchKind.TEAM, signal.away_team_id),  # type: ignore[arg-type]
        WatchTarget(WatchKind.LEAGUE, signal.league_id),  # type: ignore[arg-type]
    }


def test_a_card_without_ids_offers_only_what_it_can_name() -> None:
    bare = make_signal(home_team_id=None, away_team_id=None, league_id=None)
    partial = make_signal(home_team_id="t1", away_team_id=None, league_id="l1")

    assert _card_options(bare) == {}
    assert sorted(_card_options(partial)) == [CARD_HOME, CARD_LEAGUE]
    assert _card_options(partial)[CARD_HOME].option_id == "t1"


# --------------------------------------------------------------------------- paging edges
async def test_a_page_beyond_the_end_shows_the_last_page_not_an_error(
    admin: Client, services: Services
) -> None:
    for index in range(12):
        await services.templates.save_template(
            ADMIN_ID, TemplateDraft(name=f"Shablon {index:02d}", query=_query())
        )
    for name in ("Arsenal", "Barcelona"):
        (team,) = await services.catalog.search_teams(name, limit=1)
        await services.watchlist.add_watch(ADMIN_ID, WatchTarget(WatchKind.TEAM, team.team_id))

    watch = shown(await admin.press_data(WatchCb(action="list", page=99).pack(), 40))
    templates = shown(await admin.press_data(TplCb(action="list", value=99).pack(), 41))

    assert "Arsenal" in watch[0]
    assert "Shablon 11" in templates[0]
    assert "Shablon 00" not in templates[0]  # the last page, not the first
    assert [t for t, _ in admin.live_buttons(41) if "/" in t] == ["2/2"]


async def test_a_negative_page_shows_the_first_page(admin: Client, services: Services) -> None:
    for index in range(12):
        await services.templates.save_template(
            ADMIN_ID, TemplateDraft(name=f"Shablon {index:02d}", query=_query())
        )

    templates = shown(await admin.press_data(TplCb(action="list", value=-3).pack(), 42))

    assert "Shablon 00" in templates[0]


def _query() -> AnalysisQuery:
    return AnalysisQuery(
        dates=DateSpec.next_days(3), min_probability=0.5, min_ev=0.05, markets=frozenset(MarketType)
    )


# --------------------------------------------------------------------------- bank, > 100 bets
@pytest.mark.slow
async def test_an_open_bet_beyond_the_first_hundred_still_opens_and_settles(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(10_000), unit=Decimal(10))
    signal = (await rb.signals())[0]
    first = await rb.bet(signal, 1)
    for _ in range(100):  # the first bet is now the 101st newest: outside list_bets' page
        await rb.bet(signal, 1)
    rb.finish_matches()
    admin = rb.client()

    overview = shown(await admin.send(texts.MENU_BETS))
    opened = shown(await admin.press_data(BankCb(action="bet", id=first.bet_id).pack(), 50))
    await admin.press_data(BankCb(action="mark", id=first.bet_id, value="w").pack(), 50)

    open_label = texts.BTN_BANK_OPEN.format(count=101)  # the count is the bank's, not the page's
    assert open_label in [t for t, _ in admin.live_buttons(_overview_id(admin, open_label))]
    assert overview
    assert opened and "O'yin tugagach" in opened[0]
    assert (await rb.services.signals.get_signal(ADMIN, signal.signal_id)).result is ResultMark.WON
    assert (await rb.services.bets.get_bet(ADMIN, first.bet_id)).outcome is ResultMark.WON


def _overview_id(admin: Client, label: str) -> int:
    return admin.find_button(label)[0]


@pytest.mark.slow
async def test_pressing_a_result_on_an_already_settled_bet_changes_nothing(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    await rb.settings(bank=Decimal(1000), unit=Decimal(10))
    bet = await rb.bet(signal, 10, odds=2.0)
    rb.finish_matches()
    admin = rb.client()
    await admin.press_data(BankCb(action="mark", id=bet.bet_id, value="w").pack(), 50)

    methods = await admin.press_data(BankCb(action="mark", id=bet.bet_id, value="l").pack(), 50)

    assert (await rb.services.bets.get_bet(ADMIN, bet.bet_id)).outcome is ResultMark.WON
    assert shown(methods) or answers(methods)


@pytest.mark.slow
async def test_a_bank_refusal_names_the_real_reason(real_bot: RealBot) -> None:
    rb = real_bot
    signal = (await rb.signals())[0]
    bet = await rb.bet(signal, 10)
    admin = rb.client()  # the match has not started yet

    methods = await admin.press_data(BankCb(action="mark", id=bet.bet_id, value="w").pack(), 50)

    assert [a.text for a in answers(methods)] == [texts.BANK_MARK_REFUSED]


# --------------------------------------------------------------------------- results, retro
async def test_a_retro_signal_cannot_be_marked_and_the_reason_says_so(
    admin: Client, services: Services
) -> None:
    today = FIXED_NOW.date()
    result = await services.analysis.analyze(
        ADMIN_ID,
        AnalysisQuery(
            dates=DateSpec.between(today - timedelta(days=3), today - timedelta(days=1)),
            include_no_value=True,
        ),
    )
    retro = next(s for s in result.signals if s.retro)

    methods = await admin.press_data(
        ResultCb(action="mark", signal_id=retro.signal_id, value="w").pack(), 60
    )

    assert [a.text for a in answers(methods)] == [texts.RESULT_MARK_RETRO]
