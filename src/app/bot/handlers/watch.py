"""⭐ Sevimlilar (plan §6.7, F7.3): list, add by name or from a card, notification switches.

Entries are always looked up in a fresh ``list_watchlist`` when a button is pressed (buttons
carry ``crc32`` keys, see :mod:`app.bot.watch_ui`), so an entry removed elsewhere is reported
as gone instead of toggling another one. The pick lists of the add flow belong to their
message and are refused as stale once another message replaced them.
"""

from dataclasses import replace
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from app.bot import texts
from app.bot.callbacks import CardCb, WatchCb
from app.bot.custom_ui import CustomOption
from app.bot.draft import StaleDraftError
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.formatters import escape
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.keyboards import markup_of, single_button
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.bot.states import WatchFlow, leave_flow
from app.bot.watch_ui import (
    MAX_PICK_OPTIONS,
    PICK_LEAGUE,
    PICK_TEAM,
    WatchDraft,
    ask_keyboard,
    find_item,
    flag_of,
    format_item,
    format_watchlist,
    item_keyboard,
    pick_keyboard,
    pick_text,
    watchlist_keyboard,
)
from app.logging import get_logger
from app.services.interfaces import (
    LeagueOption,
    SignalView,
    WatchItem,
    WatchKind,
    WatchTarget,
)

logger = get_logger(__name__)

WATCH_KEY: Final = "watch"
COMMAND: Final = "kuzat"
TEAM_HITS: Final = 5
CARD_HOME: Final = "h"
CARD_AWAY: Final = "a"
CARD_LEAGUE: Final = "l"


# --------------------------------------------------------------------------- storage
async def _save(flow: FlowContext, draft: WatchDraft, *, waiting: bool = False) -> None:
    await flow.state.update_data({WATCH_KEY: draft.to_data()})
    if waiting:
        await flow.state.set_state(WatchFlow.team_query)
    else:
        await leave_flow(flow.state)


async def _load(flow: FlowContext) -> WatchDraft:
    data = await flow.state.get_data()
    return WatchDraft.from_data(data.get(WATCH_KEY))


async def _leave(flow: FlowContext) -> None:
    """Forget the add flow (the list screen is shown instead).

    Only the watchlist's own state is cleared: opening ⭐ while an analysis runs must not wipe
    the "already running" guard of that analysis (QA F-5).
    """
    data = await flow.state.get_data()
    data.pop(WATCH_KEY, None)
    state = await flow.state.get_state()
    if state is not None and state.startswith(f"{WatchFlow.__name__}:"):
        await leave_flow(flow.state)
    await flow.state.set_data(data)


def _message_of(callback: CallbackQuery) -> Message:
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        raise StaleDraftError
    return message


# --------------------------------------------------------------------------- list
async def _show_list(
    message: Message, flow: FlowContext, *, page: int = 0, notice: str | None = None
) -> None:
    items = await flow.services.watchlist.list_watchlist(flow.user_id)
    text = format_watchlist(items, page)
    await edit_html(
        message, f"{notice}\n\n{text}" if notice else text, watchlist_keyboard(items, page)
    )


async def open_watchlist(message: Message, flow: FlowContext) -> None:
    """⭐ Sevimlilar: the list with its add buttons."""
    await _leave(flow)
    items = await flow.services.watchlist.list_watchlist(flow.user_id)
    await send_html(message, format_watchlist(items), watchlist_keyboard(items))


async def on_list(callback: CallbackQuery, callback_data: WatchCb, flow: FlowContext) -> None:
    """⬅️ Sevimlilarga, or a page of the list (◀️ ▶️)."""
    await answer_callback(callback)
    await _leave(flow)
    await _show_list(_message_of(callback), flow, page=callback_data.page)


async def _item(callback: CallbackQuery, data: WatchCb, flow: FlowContext) -> WatchItem | None:
    """The entry a button names; when it is gone the user is told and the list refreshed."""
    items = await flow.services.watchlist.list_watchlist(flow.user_id)
    item = find_item(items, data.kind, data.key)
    if item is None:
        await answer_callback(callback, texts.WATCH_GONE, alert=True)
        await _show_list(_message_of(callback), flow)
    return item


async def on_open(callback: CallbackQuery, callback_data: WatchCb, flow: FlowContext) -> None:
    """An entry of the list: its notification switches."""
    item = await _item(callback, callback_data, flow)
    if item is None:
        return
    await answer_callback(callback)
    await edit_html(
        _message_of(callback), format_item(item), item_keyboard(item, callback_data.page)
    )


async def on_notify(callback: CallbackQuery, callback_data: WatchCb, flow: FlowContext) -> None:
    """Switch one notification of an entry."""
    item = await _item(callback, callback_data, flow)
    flag = flag_of(callback_data.flag)
    if item is None or flag is None:
        return
    chosen = item.notifications ^ {flag}
    saved = await flow.services.watchlist.set_notifications(
        flow.user_id, item.target, frozenset(chosen)
    )
    await answer_callback(callback, texts.WATCH_STORED)
    page = callback_data.page
    await edit_html(_message_of(callback), format_item(saved), item_keyboard(saved, page))


async def on_remove(callback: CallbackQuery, callback_data: WatchCb, flow: FlowContext) -> None:
    """🗑 Olib tashlash."""
    item = await _item(callback, callback_data, flow)
    if item is None:
        return
    await flow.services.watchlist.remove_watch(flow.user_id, item.target)
    await answer_callback(callback)
    notice = texts.WATCH_REMOVED.format(name=escape(item.name))
    await _show_list(_message_of(callback), flow, page=callback_data.page, notice=notice)


# --------------------------------------------------------------------------- adding
async def on_add_team(callback: CallbackQuery, flow: FlowContext) -> None:
    """Add a team: ask for its name."""
    message = _message_of(callback)
    await answer_callback(callback)
    await edit_html(message, texts.WATCH_ASK_TEAM, ask_keyboard())
    await _save(flow, WatchDraft(message_id=message.message_id), waiting=True)


async def on_add_league(callback: CallbackQuery, flow: FlowContext) -> None:
    """Add a league: pick from the tracked leagues of the user's sports."""
    message = _message_of(callback)
    settings = await flow.settings()
    leagues: list[LeagueOption] = []
    for sport in sorted(settings.sports):
        leagues.extend(await flow.services.catalog.list_leagues(sport))
    await answer_callback(callback)
    if not leagues:
        await edit_html(message, texts.WATCH_NO_LEAGUES, ask_keyboard())
        return
    options = tuple(
        CustomOption(league.league_id, league.name, league.sport.value, league.name)
        for league in leagues[:MAX_PICK_OPTIONS]
    )
    draft = WatchDraft(message_id=message.message_id, kind=PICK_LEAGUE, options=options)
    await edit_html(message, pick_text(draft), pick_keyboard(draft))
    await _save(flow, draft)


async def _add(flow: FlowContext, kind: WatchKind, option: CustomOption) -> WatchItem:
    return await flow.services.watchlist.add_watch(
        flow.user_id, WatchTarget(kind=kind, entity_id=option.option_id)
    )


async def on_pick(callback: CallbackQuery, callback_data: WatchCb, flow: FlowContext) -> None:
    """A team or league of a pick list: follow it and show its switches."""
    draft = await _load(flow)
    message = _message_of(callback)
    if message.message_id != draft.message_id:
        raise StaleDraftError
    kind = WatchKind.TEAM if draft.kind == PICK_TEAM else WatchKind.LEAGUE
    if not callback_data.key.isdigit():
        raise StaleDraftError
    option = draft.option(int(callback_data.key))
    item = await _add(flow, kind, option)
    await answer_callback(callback)
    await _leave(flow)
    text = f"{texts.WATCH_ADDED.format(name=escape(item.name))}\n\n{format_item(item)}"
    await edit_html(message, text, item_keyboard(item))


async def _team_options(flow: FlowContext, query: str) -> tuple[CustomOption, ...]:
    teams = await flow.services.catalog.search_teams(query, limit=TEAM_HITS)
    return tuple(
        CustomOption(team.team_id, f"{team.name} ({team.league})", team.sport.value, team.name)
        for team in teams
    )


async def _follow_or_pick(
    message: Message, flow: FlowContext, query: str, draft: WatchDraft
) -> None:
    """Follow a single clear hit at once, offer several, or say nothing was found."""
    options = await _team_options(flow, query)
    exact = [option for option in options if option.name.casefold() == query.casefold()]
    if not options:
        await send_html(message, texts.WATCH_NOT_FOUND.format(query=escape(query)))
        return
    if len(options) == 1 or len(exact) == 1:
        item = await _add(flow, WatchKind.TEAM, (exact or list(options))[0])
        await _leave(flow)
        text = f"{texts.WATCH_ADDED.format(name=escape(item.name))}\n\n{format_item(item)}"
        await send_html(message, text, item_keyboard(item))
        return
    shown = replace(draft, kind=PICK_TEAM, query=query, options=options)
    sent = await send_html(message, pick_text(shown), pick_keyboard(shown))
    await _save(flow, replace(shown, message_id=sent.message_id))


async def on_team_text(message: Message, flow: FlowContext) -> None:
    """The team name typed in the add flow."""
    draft = await _load(flow)
    await _follow_or_pick(message, flow, (message.text or "").strip(), draft)


async def on_kuzat(message: Message, command: CommandObject, flow: FlowContext) -> None:
    """``/kuzat Barcelona``: follow a team by name; without a name, open the list."""
    query = " ".join((command.args or "").split())
    if not query:
        await open_watchlist(message, flow)
        return
    await _follow_or_pick(message, flow, query, WatchDraft())


# --------------------------------------------------------------------------- from a card
def _card_options(signal: SignalView) -> dict[str, CustomOption]:
    """The catalog entries behind a card: its two teams and its league.

    The card carries the catalog ids (contract v1.7), so nothing is searched by name; a card
    from a source without ids simply offers fewer choices.
    """
    candidates = (
        (CARD_HOME, signal.home_team_id, signal.home_team),
        (CARD_AWAY, signal.away_team_id, signal.away_team),
        (CARD_LEAGUE, signal.league_id, signal.league),
    )
    return {
        code: CustomOption(entity_id, name, signal.sport.value, name)
        for code, entity_id, name in candidates
        if entity_id
    }


def _card_keyboard(
    signal_id: int, options: dict[str, CustomOption], followed: set[str]
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for code, option in options.items():
        template = texts.WATCH_CARD_ON if option.option_id in followed else texts.WATCH_CARD_OFF
        label = template.format(name=option.name)
        icon = "🏆 " if code == CARD_LEAGUE else "👕 "
        rows.append(
            [single_button(icon + label, WatchCb(action="card", kind=code, key=str(signal_id)))]
        )
    return rows


async def _followed_ids(flow: FlowContext) -> set[str]:
    items = await flow.services.watchlist.list_watchlist(flow.user_id)
    return {item.target.entity_id for item in items}


async def on_card_watch(callback: CallbackQuery, callback_data: CardCb, flow: FlowContext) -> None:
    """⭐ on a card: offer its two teams and its league."""
    signal = await flow.services.signals.get_signal(flow.user_id, callback_data.signal_id)
    options = _card_options(signal)
    if not options:
        names = f"{signal.home_team}, {signal.away_team}"
        await answer_callback(callback, texts.WATCH_CARD_UNKNOWN.format(names=names), alert=True)
        return
    await answer_callback(callback)
    rows = _card_keyboard(signal.signal_id, options, await _followed_ids(flow))
    await send_html(
        _message_of(callback),
        texts.WATCH_CARD_ASK.format(fixture=escape(signal.fixture_label)),
        markup_of(rows),
    )


async def on_card_choice(
    callback: CallbackQuery, callback_data: WatchCb, flow: FlowContext
) -> None:
    """Follow or unfollow what a card's choice names (a second tap undoes the first)."""
    if not callback_data.key.isdigit():
        raise StaleDraftError
    signal = await flow.services.signals.get_signal(flow.user_id, int(callback_data.key))
    options = _card_options(signal)
    option = options.get(callback_data.kind)
    if option is None:
        raise StaleDraftError
    kind = WatchKind.LEAGUE if callback_data.kind == CARD_LEAGUE else WatchKind.TEAM
    target = WatchTarget(kind=kind, entity_id=option.option_id)
    followed = await _followed_ids(flow)
    if option.option_id in followed:
        await flow.services.watchlist.remove_watch(flow.user_id, target)
        followed.discard(option.option_id)
        await answer_callback(callback, texts.WATCH_REMOVED_ALERT)
    else:
        await flow.services.watchlist.add_watch(flow.user_id, target)
        followed.add(option.option_id)
        await answer_callback(callback, texts.WATCH_ADDED_ALERT)
    rows = _card_keyboard(signal.signal_id, options, followed)
    await edit_html(
        _message_of(callback),
        texts.WATCH_CARD_ASK.format(fixture=escape(signal.fixture_label)),
        markup_of(rows),
    )


# --------------------------------------------------------------------------- router
def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="watch")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(open_watchlist, F.text == texts.MENU_WATCHLIST)
    router.message.register(on_kuzat, Command(COMMAND))
    router.message.register(on_team_text, TYPED_INPUT, StateFilter(WatchFlow.team_query))
    router.callback_query.register(on_card_watch, CardCb.filter(F.action == "watch"))
    router.callback_query.register(on_card_choice, WatchCb.filter(F.action == "card"))
    router.callback_query.register(on_list, WatchCb.filter(F.action == "list"))
    router.callback_query.register(on_open, WatchCb.filter(F.action == "open"))
    router.callback_query.register(on_notify, WatchCb.filter(F.action == "notify"))
    router.callback_query.register(on_remove, WatchCb.filter(F.action == "remove"))
    router.callback_query.register(on_add_team, WatchCb.filter(F.action == "add_team"))
    router.callback_query.register(on_add_league, WatchCb.filter(F.action == "add_league"))
    router.callback_query.register(on_pick, WatchCb.filter(F.action == "add"))
    return router
