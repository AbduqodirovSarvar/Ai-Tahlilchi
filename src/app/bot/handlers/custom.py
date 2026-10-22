"""🔍 Maxsus tahlil (plan §6.3, F7.1): search a team or fixture by name, pick fixtures, analyse.

Flow: the menu button (or ``/tahlil Arsenal - Chelsea``, or plain text such as ``Arsenal
Chelsea``) leads to a list of teams or fixtures; a chosen team shows its next three fixtures;
the checked fixtures are analysed with ``include_no_value=True`` so every market's probability
is shown, flagged "Value topilmadi" when nothing passes the user's thresholds.

Buttons belong to the flow's one message (``CustomDraft.message_id``): a button of an older
message, or after a restart, is refused as stale. The analysis itself runs as a background
job (``BackgroundJobs``), like the wizard's, so the user's other buttons keep working.
"""

from collections.abc import Awaitable
from dataclasses import dataclass, replace
from typing import Final
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.callbacks import CustomCb
from app.bot.custom_ui import (
    KIND_FIXTURES,
    KIND_TEAMS,
    MAX_CUSTOM_FIXTURES,
    MAX_LISTED_FIXTURES,
    MAX_TEAM_HITS,
    SOURCE_DAY,
    SOURCE_QUERY,
    SOURCE_TEAM,
    UPCOMING_PER_TEAM,
    CustomDraft,
    CustomOption,
    by_fixture,
    format_custom_fixture,
    render_custom,
)
from app.bot.draft import StaleDraftError
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.formatters import (
    clock,
    escape,
    format_signal_card,
    price_lines,
    short_date,
)
from app.bot.handlers.analysis_run import ProgressReporter
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.keyboards import signal_keyboard
from app.bot.messaging import (
    answer_callback,
    callback_message,
    drop_keyboard,
    edit_html,
    send_html,
)
from app.bot.middleware import user_message_for
from app.bot.parsing import parse_fixture_text
from app.bot.states import CustomFlow, drop_draft
from app.bot.wizard import sport_options, supported_markets
from app.domain import Sport
from app.logging import get_logger
from app.services.errors import ServiceError
from app.services.interfaces import (
    MAX_FUTURE_DAYS,
    MIN_SEARCH_LENGTH,
    AnalysisQuery,
    AnalysisResult,
    DateSpec,
    FixtureListRequest,
    FixtureOption,
    ScopeKind,
    SortKey,
    TeamOption,
    UserSettingsView,
)

logger = get_logger(__name__)

CUSTOM_KEY: Final = "custom"
MIN_PAIR_WORDS: Final = 2
MAX_PAIR_WORDS: Final = 6
"""Longest word list tried as "home team + away team" without a separator."""
MAX_TYPED_LENGTH: Final = 100
"""Longer text is not a team name; it is cut before it reaches the catalog."""


@dataclass(frozen=True, slots=True)
class Resolution:
    """What a search produced: the next screen, or a notice when there is nothing to show."""

    draft: CustomDraft | None = None
    notice: str | None = None


# --------------------------------------------------------------------------- draft storage
async def _save(state: FSMContext, draft: CustomDraft, step: State = CustomFlow.pick) -> None:
    await state.update_data({CUSTOM_KEY: draft.to_data()})
    await state.set_state(step)


async def load_custom(state: FSMContext) -> CustomDraft:
    """The flow's draft.

    Raises:
        StaleDraftError: there is none (finished, cancelled or the bot restarted).
    """
    data = await state.get_data()
    return CustomDraft.from_data(data.get(CUSTOM_KEY))


async def drop_custom(state: FSMContext) -> None:
    """Leave the flow: forget its draft and state, keep everything else in FSM data."""
    data = await state.get_data()
    data.pop(CUSTOM_KEY, None)
    await state.set_state(None)
    await state.set_data(data)


def _current(callback: CallbackQuery, draft: CustomDraft) -> Message:
    """The flow message a button belongs to; an older message's button is stale."""
    message = callback_message(callback)
    if message is None or message.message_id != draft.message_id:
        raise StaleDraftError
    return message


def _option(draft: CustomDraft, index: int) -> CustomOption:
    if not 0 <= index < len(draft.options):
        raise StaleDraftError
    return draft.options[index]


# --------------------------------------------------------------------------- searching
def _fixture_option(fixture: FixtureOption, zone: ZoneInfo) -> CustomOption:
    local = fixture.kickoff.astimezone(zone)
    stamp = f"{short_date(local.date())} {clock(local.time())}"
    return CustomOption(
        option_id=fixture.fixture_id,
        label=f"{stamp} {fixture.label}",
        sport=fixture.sport.value,
        name=fixture.label,
    )


def _team_option(team: TeamOption) -> CustomOption:
    return CustomOption(
        option_id=team.team_id,
        label=f"{team.name} ({team.league})",
        sport=team.sport.value,
        name=team.name,
    )


def _fixtures_draft(
    fixtures: tuple[FixtureOption, ...],
    zone: ZoneInfo,
    *,
    source: str,
    title: str,
    total: int | None = None,
) -> CustomDraft:
    shown = fixtures[:MAX_LISTED_FIXTURES]
    options = tuple(_fixture_option(fixture, zone) for fixture in shown)
    return CustomDraft(
        kind=KIND_FIXTURES,
        source=source,
        title=title,
        options=options,
        truncated=max((total if total is not None else len(fixtures)) - len(shown), 0),
    )


async def _team_fixtures(
    flow: FlowContext, settings: UserSettingsView, team: CustomOption
) -> Resolution:
    """The next fixtures of ``team`` (checkbox list), or a notice when there are none."""
    request = FixtureListRequest(
        dates=DateSpec.next_days(MAX_FUTURE_DAYS),
        sports=frozenset({Sport(team.sport)}),
        team_ids=frozenset({team.option_id}),
        page_size=UPCOMING_PER_TEAM,
    )
    page = await flow.services.catalog.list_fixtures(flow.user_id, request)
    if not page.items:
        notice = texts.CUSTOM_NO_UPCOMING.format(team=escape(team.name), days=MAX_FUTURE_DAYS)
        return Resolution(notice=notice)
    draft = _fixtures_draft(page.items, settings.zone, source=SOURCE_TEAM, title=team.name)
    nearest = draft.options[0].option_id
    return Resolution(draft=replace(draft, selected=(nearest,)))


async def _fixtures_by_text(
    flow: FlowContext, settings: UserSettingsView, text: str
) -> Resolution | None:
    fixtures = await flow.services.catalog.search_fixtures(text, limit=MAX_LISTED_FIXTURES)
    if not fixtures:
        return None
    draft = _fixtures_draft(fixtures, settings.zone, source=SOURCE_QUERY, title=text)
    return Resolution(draft=draft)


async def _is_team(flow: FlowContext, name: str) -> bool:
    """True when ``name`` is exactly the name of a catalog team (case-insensitive)."""
    hits = await flow.services.catalog.search_teams(name, limit=1)
    return bool(hits) and hits[0].name.casefold() == name.casefold()


async def _two_team_fixtures(
    flow: FlowContext, settings: UserSettingsView, words: list[str]
) -> Resolution | None:
    """``Arsenal Chelsea`` without a separator: split the words into two exact team names."""
    if not MIN_PAIR_WORDS <= len(words) <= MAX_PAIR_WORDS:
        return None
    for cut in range(1, len(words)):
        home, away = " ".join(words[:cut]), " ".join(words[cut:])
        long_enough = len(home) >= MIN_SEARCH_LENGTH and len(away) >= MIN_SEARCH_LENGTH
        if long_enough and await _is_team(flow, home) and await _is_team(flow, away):
            return await _fixtures_by_text(flow, settings, f"{home} - {away}")
    return None


async def _by_team_name(
    flow: FlowContext, settings: UserSettingsView, text: str
) -> Resolution | None:
    teams = await flow.services.catalog.search_teams(text, limit=MAX_TEAM_HITS)
    exact = [team for team in teams if team.name.casefold() == text.casefold()]
    if not exact:
        pair = await _two_team_fixtures(flow, settings, text.split())
        if pair is not None:
            return pair
    if not teams:
        return None
    if len(teams) == 1 or len(exact) == 1:
        return await _team_fixtures(flow, settings, _team_option((exact or teams)[0]))
    options = tuple(_team_option(team) for team in teams)
    draft = CustomDraft(kind=KIND_TEAMS, source=SOURCE_QUERY, title=text, options=options)
    return Resolution(draft=draft)


async def resolve_text(flow: FlowContext, settings: UserSettingsView, text: str) -> Resolution:
    """What typed text means: ``A - B`` is a fixture search, else a team, else a fixture.

    Raises:
        ServiceError: the catalog rejected the text (too short).
    """
    cleaned = " ".join(text.split())[:MAX_TYPED_LENGTH]
    not_found = Resolution(notice=texts.CUSTOM_NOT_FOUND.format(query=escape(cleaned)))
    if parse_fixture_text(cleaned) is not None:
        return await _fixtures_by_text(flow, settings, cleaned) or not_found
    by_team = await _by_team_name(flow, settings, cleaned)
    if by_team is not None:
        return by_team
    return await _fixtures_by_text(flow, settings, cleaned) or not_found


async def _day_resolution(
    flow: FlowContext, settings: UserSettingsView, spec: DateSpec, title: str
) -> Resolution:
    request = FixtureListRequest(dates=spec, sports=settings.sports, page_size=MAX_LISTED_FIXTURES)
    page = await flow.services.catalog.list_fixtures(flow.user_id, request)
    if not page.items:
        return Resolution(notice=texts.CUSTOM_DAY_EMPTY.format(day=escape(title)))
    draft = _fixtures_draft(
        page.items, settings.zone, source=SOURCE_DAY, title=title, total=page.total
    )
    return Resolution(draft=draft)


# --------------------------------------------------------------------------- entry points
async def open_custom(message: Message, flow: FlowContext) -> None:
    """🔍 Maxsus tahlil: ask for a team or a fixture."""
    if await flow.state.get_state() == CustomFlow.running.state:
        await send_html(message, texts.CUSTOM_ALREADY_RUNNING)
        return
    await drop_draft(flow.state)
    text, markup = render_custom(CustomDraft())
    sent = await send_html(message, text, markup)
    await _save(flow.state, CustomDraft(message_id=sent.message_id), CustomFlow.query)


async def _previous_message_id(state: FSMContext) -> int | None:
    try:
        return (await load_custom(state)).message_id
    except StaleDraftError:
        return None


async def show_resolution(message: Message, flow: FlowContext, resolution: Resolution) -> bool:
    """Send the next screen as a new message and retire the previous keyboard.

    Returns whether a screen was shown (``False``: only the notice went out).
    """
    if resolution.draft is None:
        await send_html(message, resolution.notice or texts.UNKNOWN_TEXT)
        return False
    previous = await _previous_message_id(flow.state)
    if message.bot is not None:
        await drop_keyboard(message.bot, message.chat.id, previous)
    text, markup = render_custom(resolution.draft)
    sent = await send_html(message, text, markup)
    await _save(flow.state, replace(resolution.draft, message_id=sent.message_id))
    return True


async def search_text(message: Message, flow: FlowContext, text: str) -> None:
    """Resolve ``text`` and show the result; ``/tahlil A - B`` and typed names come here."""
    settings = await flow.settings()
    await show_resolution(message, flow, await resolve_text(flow, settings, text))


async def on_query_text(message: Message, flow: FlowContext) -> None:
    """Text typed while the flow waits for a team or a fixture."""
    await search_text(message, flow, message.text or "")


async def on_plain_text(message: Message, flow: FlowContext) -> None:
    """Free text outside any flow: a team or fixture name starts a custom analysis."""
    settings = await flow.settings()
    resolution = await resolve_text(flow, settings, message.text or "")
    if resolution.draft is None:
        await send_html(message, texts.UNKNOWN_TEXT)
        return
    await show_resolution(message, flow, resolution)


# --------------------------------------------------------------------------- buttons
async def _show_in_place(
    callback: CallbackQuery, flow: FlowContext, draft: CustomDraft, resolution: Resolution
) -> None:
    """Show a search result in the flow's own message, or just the notice."""
    message = _current(callback, draft)
    await answer_callback(callback)
    if resolution.draft is None:
        await send_html(message, resolution.notice or texts.UNKNOWN_TEXT)
        return
    shown = replace(resolution.draft, message_id=draft.message_id)
    text, markup = render_custom(shown)
    await edit_html(message, text, markup)
    await _save(flow.state, shown)


async def on_day(callback: CallbackQuery, callback_data: CustomCb, flow: FlowContext) -> None:
    """📅 Bugungi / 📆 Ertangi o'yinlar."""
    draft = await load_custom(flow.state)
    settings = await flow.settings()
    today = callback_data.action == "today"
    spec = DateSpec.today() if today else DateSpec.tomorrow()
    title = texts.BTN_TODAY if today else texts.BTN_TOMORROW
    await _show_in_place(callback, flow, draft, await _day_resolution(flow, settings, spec, title))


async def on_team(callback: CallbackQuery, callback_data: CustomCb, flow: FlowContext) -> None:
    """A team of the search result: show its next fixtures."""
    draft = await load_custom(flow.state)
    team = _option(draft, callback_data.index)
    settings = await flow.settings()
    await _show_in_place(callback, flow, draft, await _team_fixtures(flow, settings, team))


async def on_toggle(callback: CallbackQuery, callback_data: CustomCb, flow: FlowContext) -> None:
    """Check or uncheck a fixture (at most :data:`MAX_CUSTOM_FIXTURES`)."""
    draft = await load_custom(flow.state)
    message = _current(callback, draft)
    changed = draft.toggled(callback_data.index)
    if len(changed.selected) > MAX_CUSTOM_FIXTURES:
        await answer_callback(
            callback, texts.CUSTOM_LIMIT.format(max=MAX_CUSTOM_FIXTURES), alert=True
        )
        return
    await answer_callback(callback)
    text, markup = render_custom(changed)
    await edit_html(message, text, markup)
    await _save(flow.state, changed)


async def on_again(callback: CallbackQuery, flow: FlowContext) -> None:
    """🔎 Qayta qidirish: back to the prompt."""
    draft = await load_custom(flow.state)
    message = _current(callback, draft)
    await answer_callback(callback)
    fresh = CustomDraft(message_id=draft.message_id)
    text, markup = render_custom(fresh)
    await edit_html(message, text, markup)
    await _save(flow.state, fresh, CustomFlow.query)


async def on_cancel(callback: CallbackQuery, flow: FlowContext) -> None:
    """❌ Bekor qilish."""
    draft = await load_custom(flow.state)
    message = _current(callback, draft)
    await answer_callback(callback)
    await drop_custom(flow.state)
    await edit_html(message, texts.CANCELLED)


async def on_busy(callback: CallbackQuery) -> None:
    """Any button while the analysis runs."""
    await answer_callback(callback, texts.CUSTOM_ALREADY_RUNNING, alert=True)


# --------------------------------------------------------------------------- the run
@dataclass(frozen=True, slots=True)
class _Run:
    flow: FlowContext
    message: Message
    progress: Message
    query: AnalysisQuery


async def _build_query(
    flow: FlowContext, settings: UserSettingsView, draft: CustomDraft
) -> AnalysisQuery:
    """Every priced market of the chosen fixtures, without the EV filter."""
    sports = frozenset(Sport(value) for value in draft.sports)
    markets = supported_markets(await sport_options(flow.services), sports) or settings.markets
    return AnalysisQuery(
        dates=DateSpec.next_days(MAX_FUTURE_DAYS),
        sports=sports,
        min_probability=settings.min_probability,
        min_ev=settings.min_ev,
        scope=ScopeKind.FIXTURES,
        scope_ids=frozenset(draft.selected),
        markets=markets,
        max_signals=None,
        sort=SortKey.KICKOFF,
        include_no_value=True,
    )


async def on_run(callback: CallbackQuery, flow: FlowContext) -> None:
    """▶️ Tahlil qilish: analyse the checked fixtures in the background."""
    draft = await load_custom(flow.state)
    message = _current(callback, draft)
    if not draft.selected:
        await answer_callback(callback, texts.CUSTOM_SELECT_ONE, alert=True)
        return
    query = await _build_query(flow, await flow.settings(), draft)
    await answer_callback(callback)
    await flow.state.set_state(CustomFlow.running)
    progress = await send_html(message, texts.CUSTOM_RUNNING)
    run = _Run(flow, message, progress, query)
    flow.jobs.spawn(_execute(run), name=f"custom:{flow.user_id}")


async def _quietly(action: Awaitable[object]) -> None:
    """Best-effort Telegram call on the failure path (the user may have blocked the bot)."""
    try:
        await action
    except TelegramAPIError as error:
        logger.warning("custom_cleanup_failed", error=error.message)


async def _execute(run: _Run) -> None:
    """The analysis job: run, publish, then leave the flow; on failure explain and stay."""
    flow = run.flow
    reporter = ProgressReporter(run.progress)
    try:
        result = await flow.services.analysis.analyze(flow.user_id, run.query, progress=reporter)
        await _publish(run, result)
    except ServiceError as error:
        logger.info("custom_rejected", error_type=type(error).__name__, detail=str(error))
        await _fail(run, user_message_for(error))
    except Exception:
        logger.exception("custom_analysis_failed")
        await _fail(run, texts.UNEXPECTED_ERROR)
    else:
        async with flow.jobs.lock(flow.state.key):
            if await flow.state.get_state() == CustomFlow.running.state:
                await drop_custom(flow.state)


async def _fail(run: _Run, message: str) -> None:
    """Close the progress message, explain, and give the pick screen back for a retry."""
    flow = run.flow
    await _quietly(edit_html(run.progress, texts.CANCELLED))
    async with flow.jobs.lock(flow.state.key):
        if await flow.state.get_state() == CustomFlow.running.state:
            await flow.state.set_state(CustomFlow.pick)
    await _quietly(send_html(run.message, message))


async def _publish(run: _Run, result: AnalysisResult) -> None:
    """The header, one block per fixture and a full card for every recommended selection."""
    zone = await run.flow.zone()
    await edit_html(run.progress, texts.RECALC_DONE)
    notes = price_lines(result)
    if not result.signals:
        await send_html(run.message, "\n".join([texts.CUSTOM_EMPTY_RESULT, *notes]))
        return
    groups = by_fixture(result.signals)
    header = texts.CUSTOM_RESULT_HEADER.format(fixtures=len(groups), picks=len(result.signals))
    await send_html(run.message, "\n".join([header, *notes]))
    for group in groups:
        await send_html(run.message, format_custom_fixture(group, zone))
        for signal in (item for item in group if item.recommended):
            await send_html(run.message, format_signal_card(signal, zone), signal_keyboard(signal))


# --------------------------------------------------------------------------- router
def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="custom")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    open_states = StateFilter(CustomFlow.query, CustomFlow.pick)
    router.message.register(open_custom, F.text == texts.MENU_CUSTOM)
    router.message.register(on_query_text, TYPED_INPUT, open_states)
    router.callback_query.register(on_busy, CustomCb.filter(), StateFilter(CustomFlow.running))
    for action, handler in (
        (("today", "tomorrow"), on_day),
        (("team",), on_team),
        (("toggle",), on_toggle),
        (("again",), on_again),
        (("cancel",), on_cancel),
        (("run",), on_run),
    ):
        router.callback_query.register(handler, CustomCb.filter(F.action.in_(action)), open_states)
    router.message.register(
        on_plain_text, TYPED_INPUT, F.text.len() >= MIN_SEARCH_LENGTH, StateFilter(None)
    )
    return router
