"""🗄 Baza (``/baza``): browse what the database holds, and the administrator's refresh.

Handlers stay thin: they read the tap, ask :class:`DataBrowserService` (never the database or the
provider) and hand the DTOs to the formatters. Navigation inside one message is a pure function
of the :class:`~app.bot.browse_nav.BrowseNav` record (:func:`apply_action`), so double taps and
buttons of an older screen are harmless: a token that does not match means a newer screen was
opened (stale), a screen code that does not match only redraws the screen on display.
"""

from dataclasses import dataclass, replace
from datetime import date
from typing import Final

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import texts
from app.bot.browse_card_ui import (
    ODDS_PAGE_SIZE,
    card_keyboard,
    format_fixture_card,
    format_odds_history,
    odds_keyboard,
)
from app.bot.browse_nav import (
    SCOPE_ALL,
    SCOPE_LEAGUE,
    SCOPE_TEAM,
    SCREEN_CALENDAR,
    SCREEN_FIXTURE,
    SCREEN_FIXTURES,
    SCREEN_LEAGUES,
    SCREEN_ODDS,
    SCREEN_TEAMS,
    BrowseNav,
    load_nav,
    open_screen,
    save_nav,
)
from app.bot.browse_ui import (
    BROWSE_PAGE_SIZE,
    calendar_bounds,
    calendar_keyboard,
    fixtures_keyboard,
    format_fixtures,
    format_leagues,
    format_overview,
    format_teams,
    has_data,
    initial_month,
    leagues_keyboard,
    overview_keyboard,
    teams_keyboard,
)
from app.bot.callbacks import DbCb
from app.bot.draft import StaleDraftError
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.bot.paging import paginate
from app.bot.refresh_ui import format_refresh_report, refresh_done_keyboard, refresh_error_text
from app.bot.states import BrowseFlow
from app.logging import get_logger
from app.services.errors import ServiceError
from app.services.interfaces import (
    MAX_STORED_TEAM_SEARCH_LENGTH,
    Services,
    StoredFixtureRequest,
    StoredOddsRequest,
    StoredTeamPage,
    StoredTeamRequest,
)

logger = get_logger(__name__)

COMMAND: Final = "baza"
REFRESH_JOB: Final = "db_refresh"
"""Name of the background refresh task: one refresh at a time for the whole bot."""


@dataclass(frozen=True, slots=True)
class Screen:
    """A rendered screen: its navigation record, text and keyboard."""

    nav: BrowseNav
    text: str
    markup: InlineKeyboardMarkup


def _message_of(callback: CallbackQuery) -> Message:
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        raise StaleDraftError
    return message


# --------------------------------------------------------------------------- navigation
def _open_fixtures(nav: BrowseNav, item: tuple[str, str]) -> BrowseNav:
    """A league or team was picked: its fixtures, every day, first page."""
    scope = SCOPE_LEAGUE if nav.screen == SCREEN_LEAGUES else SCOPE_TEAM
    return replace(
        nav,
        screen=SCREEN_FIXTURES,
        scope=scope,
        scope_id=item[0],
        scope_name=item[1],
        parent_page=nav.page,
        day=0,
        page=0,
        ids=(),
        labels=(),
    )


def _pick(nav: BrowseNav, index: int) -> BrowseNav:
    item = nav.pick(index)
    if item is None:
        return nav
    if nav.screen == SCREEN_FIXTURES:
        return replace(nav, screen=SCREEN_FIXTURE, fixture_id=item[0])
    return _open_fixtures(nav, item)


def _back(nav: BrowseNav) -> BrowseNav:
    """One step back: odds → card → fixtures → the list it was opened from."""
    match nav.screen:
        case "odds":
            return replace(nav, screen=SCREEN_FIXTURE)
        case "fixture" | "calendar":
            return replace(nav, screen=SCREEN_FIXTURES)
        case "fixtures" if nav.scope == SCOPE_LEAGUE:
            return replace(nav, screen=SCREEN_LEAGUES, page=nav.parent_page)
        case "fixtures" if nav.scope == SCOPE_TEAM:
            return replace(nav, screen=SCREEN_TEAMS, page=nav.parent_page)
        case _:
            return nav


def apply_action(nav: BrowseNav, action: str, value: int) -> BrowseNav:
    """The record after a button of the screen on display (pure, no I/O)."""
    result = nav
    match action:
        case "pick":
            result = _pick(nav, value)
        case "back":
            result = _back(nav)
        case "page":
            result = replace(nav, page=value)
        case "opage":
            result = replace(nav, odds_page=value)
        case "day":
            result = replace(nav, screen=SCREEN_FIXTURES, day=value, page=0)
        case "cal":
            result = replace(nav, screen=SCREEN_CALENDAR, cal_month=0)
        case "month":
            result = replace(nav, cal_month=value)
        case "odds":
            result = replace(nav, screen=SCREEN_ODDS, odds_page=0)
    return result


# --------------------------------------------------------------------------- rendering
async def _overview_screen(flow: FlowContext) -> tuple[str, InlineKeyboardMarkup]:
    overview = await flow.services.browser.database_overview(flow.user_id)
    zone = await flow.zone()
    return (
        format_overview(overview, zone, flow.clock()),
        overview_keyboard(with_data=has_data(overview)),
    )


async def _leagues_screen(flow: FlowContext, nav: BrowseNav) -> Screen:
    leagues = await flow.services.browser.list_stored_leagues(flow.user_id)
    page = paginate(leagues, nav.page, BROWSE_PAGE_SIZE)
    shown = replace(
        nav,
        screen=SCREEN_LEAGUES,
        page=page.number,
        ids=tuple(league.league_id for league in page.items),
        labels=tuple(league.name for league in page.items),
    )
    text = format_leagues(page, len(leagues), await flow.zone())
    return Screen(shown, text, leagues_keyboard(page, shown))


async def _team_page(flow: FlowContext, nav: BrowseNav) -> StoredTeamPage:
    """The teams page asked for; a page past the end (list shrank) falls back to the last."""
    browser = flow.services.browser

    def request(page: int) -> StoredTeamRequest:
        return StoredTeamRequest(text=nav.query, page=page, page_size=BROWSE_PAGE_SIZE)

    result = await browser.list_stored_teams(flow.user_id, request(nav.page))
    if not result.items and result.total and result.page_count:
        result = await browser.list_stored_teams(flow.user_id, request(result.page_count - 1))
    return result


async def _teams_screen(flow: FlowContext, nav: BrowseNav) -> Screen:
    result = await _team_page(flow, nav)
    shown = replace(
        nav,
        screen=SCREEN_TEAMS,
        page=result.page,
        ids=tuple(team.team_id for team in result.items),
        labels=tuple(team.name for team in result.items),
    )
    return Screen(shown, format_teams(result, nav.query), teams_keyboard(result, shown))


def _fixture_request(nav: BrowseNav, page: int) -> StoredFixtureRequest:
    day = date.fromordinal(nav.day) if nav.day else None
    scope_ids = frozenset() if nav.scope_id is None else frozenset({nav.scope_id})
    return StoredFixtureRequest(
        date_from=day,
        date_to=day,
        league_ids=scope_ids if nav.scope == SCOPE_LEAGUE else frozenset(),
        team_ids=scope_ids if nav.scope == SCOPE_TEAM else frozenset(),
        page=page,
        page_size=BROWSE_PAGE_SIZE,
    )


async def _fixtures_screen(flow: FlowContext, nav: BrowseNav) -> Screen:
    browser = flow.services.browser
    result = await browser.list_stored_fixtures(flow.user_id, _fixture_request(nav, nav.page))
    if not result.items and result.total and result.page_count:
        last = _fixture_request(nav, result.page_count - 1)
        result = await browser.list_stored_fixtures(flow.user_id, last)
    shown = replace(
        nav,
        screen=SCREEN_FIXTURES,
        page=result.page,
        ids=tuple(fixture.fixture_id for fixture in result.items),
        labels=tuple(fixture.label for fixture in result.items),
    )
    zone = await flow.zone()
    return Screen(
        shown,
        format_fixtures(result, shown, zone),
        fixtures_keyboard(result, shown, await flow.today()),
    )


async def _card_screen(flow: FlowContext, nav: BrowseNav) -> Screen:
    detail = await flow.services.browser.get_stored_fixture(flow.user_id, _fixture_id(nav))
    shown = replace(nav, screen=SCREEN_FIXTURE)
    text = format_fixture_card(detail, await flow.zone())
    return Screen(shown, text, card_keyboard(shown))


def _fixture_id(nav: BrowseNav) -> str:
    if nav.fixture_id is None:
        raise StaleDraftError
    return nav.fixture_id


async def _odds_screen(flow: FlowContext, nav: BrowseNav) -> Screen:
    browser = flow.services.browser
    fixture_id = _fixture_id(nav)
    page = await browser.list_stored_odds(
        flow.user_id,
        _odds_request(fixture_id, nav.odds_page),
    )
    if not page.items and page.total and page.page_count:
        page = await browser.list_stored_odds(
            flow.user_id, _odds_request(fixture_id, page.page_count - 1)
        )
    detail = await browser.get_stored_fixture(flow.user_id, fixture_id)
    shown = replace(nav, screen=SCREEN_ODDS, odds_page=page.page)
    text = format_odds_history(page, detail.fixture, await flow.zone())
    return Screen(shown, text, odds_keyboard(page, shown))


def _odds_request(fixture_id: str, page: int) -> StoredOddsRequest:
    return StoredOddsRequest(fixture_id=fixture_id, page=page, page_size=ODDS_PAGE_SIZE)


async def _calendar_screen(flow: FlowContext, nav: BrowseNav) -> Screen:
    overview = await flow.services.browser.database_overview(flow.user_id)
    zone = await flow.zone()
    today = await flow.today()
    bounds = calendar_bounds(overview, zone, today)
    month = nav.cal_month or initial_month(nav, bounds, today)
    shown = replace(nav, screen=SCREEN_CALENDAR, cal_month=month)
    return Screen(shown, texts.DB_CALENDAR_PROMPT, calendar_keyboard(shown, month, bounds, today))


async def render(flow: FlowContext, nav: BrowseNav) -> Screen:
    """Draw the screen ``nav`` names, reading what it shows from the browser service."""
    match nav.screen:
        case "teams":
            return await _teams_screen(flow, nav)
        case "fixtures":
            return await _fixtures_screen(flow, nav)
        case "fixture":
            return await _card_screen(flow, nav)
        case "odds":
            return await _odds_screen(flow, nav)
        case "calendar":
            return await _calendar_screen(flow, nav)
        case _:
            return await _leagues_screen(flow, nav)


# --------------------------------------------------------------------------- entry points
async def _leave_search(flow: FlowContext) -> None:
    """Forget a pending team search (only this feature's own state is cleared)."""
    if await flow.state.get_state() == BrowseFlow.team_query.state:
        await flow.state.set_state(None)


async def open_overview(message: Message, flow: FlowContext) -> None:
    """🗄 Baza / ``/baza``: tables, size, freshness and the section buttons."""
    await _leave_search(flow)
    text, markup = await _overview_screen(flow)
    await send_html(message, text, markup)


async def on_menu(callback: CallbackQuery, flow: FlowContext) -> None:
    """🗄 Baza button: back to the overview in the same message."""
    await answer_callback(callback)
    await _leave_search(flow)
    text, markup = await _overview_screen(flow)
    await edit_html(_message_of(callback), text, markup)


async def _enter(callback: CallbackQuery, flow: FlowContext, nav: BrowseNav) -> None:
    """Open a new screen family in the callback's message (new token)."""
    await answer_callback(callback)
    await _leave_search(flow)
    opened = await open_screen(flow.state, nav)
    screen = await render(flow, opened)
    await save_nav(flow.state, screen.nav)
    await edit_html(_message_of(callback), screen.text, screen.markup)


async def on_leagues(callback: CallbackQuery, flow: FlowContext) -> None:
    """🏆 Ligalar."""
    await _enter(callback, flow, BrowseNav(screen=SCREEN_LEAGUES))


async def on_teams(callback: CallbackQuery, flow: FlowContext) -> None:
    """👥 Jamoalar (every team; 🔎 narrows it)."""
    await _enter(callback, flow, BrowseNav(screen=SCREEN_TEAMS))


async def on_all(callback: CallbackQuery, flow: FlowContext) -> None:
    """⚽ Hamma o'yinlar: every stored fixture, day by day."""
    await _enter(callback, flow, BrowseNav(screen=SCREEN_FIXTURES, scope=SCOPE_ALL))


async def on_search(callback: CallbackQuery, flow: FlowContext) -> None:
    """🔎 Qidirish: ask for a team name."""
    await answer_callback(callback)
    await flow.state.set_state(BrowseFlow.team_query)
    await send_html(
        _message_of(callback),
        texts.DB_TEAM_SEARCH_PROMPT.format(min_length=texts.DB_TEAM_SEARCH_MIN_LENGTH),
    )


async def on_team_query(message: Message, flow: FlowContext) -> None:
    """The typed team name: a fresh team list filtered by it."""
    query = " ".join((message.text or "").split())
    if len(query) < texts.DB_TEAM_SEARCH_MIN_LENGTH:
        await send_html(
            message,
            texts.DB_TEAM_SEARCH_TOO_SHORT.format(min_length=texts.DB_TEAM_SEARCH_MIN_LENGTH),
        )
        return
    if len(query) > MAX_STORED_TEAM_SEARCH_LENGTH:
        await send_html(
            message, texts.DB_TEAM_SEARCH_TOO_LONG.format(max_length=MAX_STORED_TEAM_SEARCH_LENGTH)
        )
        return
    await flow.state.set_state(None)
    opened = await open_screen(flow.state, BrowseNav(screen=SCREEN_TEAMS, query=query))
    screen = await render(flow, opened)
    await save_nav(flow.state, screen.nav)
    await send_html(message, screen.text, screen.markup)


async def on_screen_action(callback: CallbackQuery, callback_data: DbCb, flow: FlowContext) -> None:
    """A button of the screen on display (see the module doc for the stale rules)."""
    nav = await load_nav(flow.state)
    if nav is None or nav.token != callback_data.t:
        await answer_callback(callback, texts.DB_STALE_SCREEN, alert=True)
        return
    await answer_callback(callback)
    target = (
        apply_action(nav, callback_data.action, callback_data.n) if nav.owns(callback_data) else nav
    )
    screen = await render(flow, target)
    await save_nav(flow.state, screen.nav)
    await edit_html(_message_of(callback), screen.text, screen.markup)


async def on_noop(callback: CallbackQuery) -> None:
    """Display-only cells (month name, weekday letters, out-of-span days)."""
    await answer_callback(callback)


# --------------------------------------------------------------------------- refresh
async def on_refresh(callback: CallbackQuery, flow: FlowContext) -> None:
    """🔄 Bazani yangilash: one refresh at a time, reported in a message of its own."""
    if any(task.get_name() == REFRESH_JOB for task in flow.jobs.tasks):
        await answer_callback(callback, texts.DB_REFRESH_ALREADY, alert=True)
        return
    await answer_callback(callback)
    progress = await send_html(_message_of(callback), texts.DB_REFRESHING)
    flow.jobs.spawn(_refresh_job(progress, flow.services, flow.user_id), name=REFRESH_JOB)


async def _refresh_job(progress: Message, services: Services, user_id: int) -> None:
    """The refresh itself; its outcome (or the reason it did not run) replaces the wait message."""
    try:
        text = format_refresh_report(await services.browser.refresh_database(user_id))
    except ServiceError as error:
        logger.info("db_refresh_rejected", error_type=type(error).__name__, detail=str(error))
        text = refresh_error_text(error)
    except Exception:
        logger.exception("db_refresh_failed", user_id=user_id)
        text = texts.UNEXPECTED_ERROR
    try:
        await edit_html(progress, text, refresh_done_keyboard())
    except TelegramAPIError as error:
        logger.warning("db_refresh_report_failed", error=error.message)


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="browse")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(open_overview, F.text == texts.MENU_DB)
    router.message.register(open_overview, Command(COMMAND))
    router.message.register(on_team_query, TYPED_INPUT, StateFilter(BrowseFlow.team_query))
    for action, handler in (
        ("menu", on_menu),
        ("leagues", on_leagues),
        ("teams", on_teams),
        ("all", on_all),
        ("search", on_search),
        ("refresh", on_refresh),
    ):
        router.callback_query.register(handler, DbCb.filter(F.action == action))
    router.callback_query.register(on_noop, DbCb.filter(F.action == "noop"))
    router.callback_query.register(on_screen_action, DbCb.filter())
    return router
