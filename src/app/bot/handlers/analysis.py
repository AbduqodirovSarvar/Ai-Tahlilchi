"""📊 Tahlil wizard (plan §6.2): one handler per button kind or typed input.

Each handler reads its input, updates the draft and calls ``session.go(next step)``;
rendering lives in :mod:`app.bot.wizard`, the run itself in :mod:`.analysis_run`.
Callback handlers are bound to their step's FSM state, so a button from another step or
from a lost session falls through to the stale-button handler.
"""

from dataclasses import replace
from datetime import date
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.callbacks import (
    CalendarCb,
    ConfirmCb,
    CountCb,
    DateCb,
    EditCb,
    EvCb,
    MarketCb,
    NavCb,
    PickCb,
    ProbabilityCb,
    ScopeCb,
    SortCb,
    SportCb,
)
from app.bot.draft import StaleDraftError, WizardDraft
from app.bot.formatters import escape
from app.bot.handlers.analysis_run import run_analysis
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.notices import HIGH_PROBABILITY
from app.bot.parsing import (
    PERCENT,
    InputError,
    check_window,
    parse_count,
    parse_date_range,
    parse_percent,
)
from app.bot.session import WizardSession, WizardSessionMiddleware
from app.bot.states import AnalysisFlow, drop_draft
from app.bot.wizard import (
    BACK,
    MAX_TEAM_NAMES_PER_MESSAGE,
    add_teams,
    distinct_names,
    fit_markets,
    high_probability,
    lookup_teams,
    next_step,
    sport_options,
    step_of,
    supported_markets,
)
from app.domain import MarketType, Sport
from app.services.interfaces import (
    MAX_SCOPE_IDS,
    MIN_EV_LOWER,
    MIN_EV_UPPER,
    MIN_PROBABILITY_LOWER,
    MIN_PROBABILITY_UPPER,
    QUERY_MAX_SIGNALS_UPPER,
    DateSpec,
    ScopeKind,
    SortKey,
    TemplateDraft,
)

_WIZARD_STATES = tuple(
    state for state in AnalysisFlow.__all_states__ if state != AnalysisFlow.running
)


# --------------------------------------------------------------------------- entry and nav
async def start_wizard(message: Message, session: WizardSession) -> None:
    """📊 Tahlil: a fresh draft from the user's settings, step 1."""
    del message  # the session already knows where to answer
    await drop_draft(session.state)
    settings = await session.ctx.services.settings.get_settings(session.ctx.user_id)
    await session.go("dates", WizardDraft.from_settings(settings))


async def on_nav(callback: CallbackQuery, callback_data: NavCb, session: WizardSession) -> None:
    """⬅️ Orqaga / ⏭ Standart / ❌ Bekor qilish on any step."""
    del callback
    step = step_of(await session.state.get_state()) or "dates"
    draft = session.draft
    if callback_data.action == "default":
        await session.go(next_step(step, draft), _with_default(step, draft))
        return
    previous = BACK.get(step) if callback_data.action == "back" else None
    if previous is None:
        await session.cancel()
        return
    await session.go(previous, draft)


def _with_default(step: str, draft: WizardDraft) -> WizardDraft:
    """The draft with the settings default of ``step`` applied ("⏭ Standart")."""
    defaults = draft.defaults
    if step == "scope":
        return draft.with_scope(ScopeKind.ALL)
    changes: dict[str, dict[str, Any]] = {
        "probability": {"min_probability": float(defaults["min_probability"])},
        "ev": {"min_ev": float(defaults["min_ev"])},
        "count": {"max_signals": int(defaults["max_signals"]), "per_day": False},
        "markets": {"markets": tuple(defaults["markets"])},
        "sort": {"sort": SortKey.EV.value},
    }
    return replace(draft, **changes.get(step, {}))


# --------------------------------------------------------------------------- dates
async def on_date(callback: CallbackQuery, callback_data: DateCb, session: WizardSession) -> None:
    """Quick date buttons and "📅 Oraliq tanlash"."""
    del callback
    draft = session.draft
    choices = {
        "today": DateSpec.today,
        "tomorrow": DateSpec.tomorrow,
        "weekend": DateSpec.weekend,
    }
    if callback_data.choice == "calendar":
        calendar_draft = replace(draft, calendar_start=None, calendar_month=0)
        await session.go("calendar", calendar_draft)
        return
    if callback_data.choice == "days":
        spec = DateSpec.next_days(callback_data.days)
    else:
        spec = choices.get(callback_data.choice, DateSpec.today)()
    await session.go(next_step("dates", draft), draft.with_dates(spec))


async def on_calendar(
    callback: CallbackQuery, callback_data: CalendarCb, session: WizardSession
) -> None:
    """Calendar widget: month navigation, then start and end day."""
    del callback
    draft = session.draft
    if callback_data.action == "noop":
        return
    if callback_data.action == "month":
        await session.go("calendar", replace(draft, calendar_month=callback_data.value))
        return
    day = date.fromordinal(callback_data.value)
    if draft.calendar_start is None:
        await session.go("calendar", replace(draft, calendar_start=day.isoformat()))
        return
    start = date.fromisoformat(draft.calendar_start)
    spec = check_window(start, day, session.ctx.today)
    await session.go(next_step("calendar", draft), draft.with_dates(spec))


async def on_dates_text(message: Message, session: WizardSession) -> None:
    """Typed range on the date step: ``25.10 - 31.10`` or ``25.10.2026-31.10.2026``."""
    start, end = parse_date_range(message.text or "", session.ctx.today)
    spec = check_window(start, end, session.ctx.today)
    draft = session.draft
    await session.go(next_step("dates", draft), draft.with_dates(spec))


# --------------------------------------------------------------------------- sport
async def on_sport(callback: CallbackQuery, callback_data: SportCb, session: WizardSession) -> None:
    """One sport or 🌐 Hammasi."""
    del callback
    options = await sport_options(session.ctx.services)
    offered = [option.sport.value for option in options]
    if callback_data.sport == "all":
        sports = tuple(offered)
    elif callback_data.sport in offered:
        sports = (callback_data.sport,)
    else:
        raise StaleDraftError
    draft = session.draft
    changed = set(sports) != set(draft.sports)
    chosen = frozenset(Sport(value) for value in sports)
    # Leagues/teams/fixtures of another sport make no sense any more; markets must be
    # ones the chosen sports are priced in.
    draft = replace(draft, sports=sports, markets=fit_markets(draft.markets, options, chosen))
    if changed:
        draft = draft.with_scope(ScopeKind.ALL)
    await session.go(next_step("sport", draft), draft)


# --------------------------------------------------------------------------- probability / EV
async def _set_probability(session: WizardSession, step: str, value: float) -> None:
    draft = replace(session.draft, min_probability=value)
    notice = None
    if high_probability(value) and session.ctx.notices.first_time(
        session.ctx.user_id, HIGH_PROBABILITY
    ):
        notice = texts.HIGH_PROBABILITY_WARNING
    await session.go(next_step(step, draft), draft, notice)


async def on_probability(
    callback: CallbackQuery, callback_data: ProbabilityCb, session: WizardSession
) -> None:
    """Probability preset or "✏️ O'zim kiritaman"."""
    del callback
    if callback_data.percent == keyboards.CUSTOM:
        await session.go("probability_input", session.draft)
        return
    await _set_probability(session, "probability", callback_data.percent / PERCENT)


async def on_probability_text(message: Message, session: WizardSession) -> None:
    """Typed minimal probability."""
    value = parse_percent(message.text or "", MIN_PROBABILITY_LOWER, MIN_PROBABILITY_UPPER)
    await _set_probability(session, "probability_input", value)


async def on_ev(callback: CallbackQuery, callback_data: EvCb, session: WizardSession) -> None:
    """EV preset or "✏️ O'zim kiritaman"."""
    del callback
    draft = session.draft
    if callback_data.percent == keyboards.CUSTOM:
        await session.go("ev_input", draft)
        return
    draft = replace(draft, min_ev=callback_data.percent / PERCENT)
    await session.go(next_step("ev", draft), draft)


async def on_ev_text(message: Message, session: WizardSession) -> None:
    """Typed minimal EV."""
    value = parse_percent(message.text or "", MIN_EV_LOWER, MIN_EV_UPPER)
    draft = replace(session.draft, min_ev=value)
    await session.go(next_step("ev_input", draft), draft)


# --------------------------------------------------------------------------- scope
async def on_scope(callback: CallbackQuery, callback_data: ScopeCb, session: WizardSession) -> None:
    """Scope kind; the pickers open for leagues, teams and fixtures."""
    del callback
    kind = ScopeKind(callback_data.kind)
    draft = replace(session.draft.with_scope(kind), page=0)
    if kind is ScopeKind.ALL:
        await session.go(next_step("scope", draft), draft)
        return
    await session.go(kind.value, draft)


async def on_pick(callback: CallbackQuery, callback_data: PickCb, session: WizardSession) -> None:
    """Checkbox toggles, pages and "✅ Tayyor" of the league/team/fixture pickers."""
    del callback
    step = step_of(await session.state.get_state()) or "leagues"
    draft = session.draft
    if callback_data.action == "done":
        if not draft.scope_ids:
            await session.answer(texts.SELECT_AT_LEAST_ONE, alert=True)
            return
        await session.go(next_step(step, draft), draft)
        return
    if callback_data.action == "page":
        await session.go(step, replace(draft, page=max(callback_data.index, 0)))
        return
    if not 0 <= callback_data.index < len(draft.options):
        raise StaleDraftError
    entity_id, label = draft.options[callback_data.index]
    adding = entity_id not in draft.scope_ids
    if adding and len(draft.scope_ids) >= MAX_SCOPE_IDS:
        await session.answer(texts.SCOPE_LIMIT.format(max_scope=MAX_SCOPE_IDS), alert=True)
        return
    await session.go(step, draft.toggle_scope_item(entity_id, label))


async def on_team_text(message: Message, session: WizardSession) -> None:
    """Typed team names (comma-separated) on the teams picker."""
    names = distinct_names((message.text or "").split(","))
    if len(names) > MAX_TEAM_NAMES_PER_MESSAGE:
        raise InputError(
            texts.WIZARD_TEAMS_TOO_MANY_NAMES.format(max_names=MAX_TEAM_NAMES_PER_MESSAGE)
        )
    draft = session.draft
    lookup = await lookup_teams(session.ctx.services, names, draft.sport_set)
    notice = None
    if lookup.missing:
        notice = texts.WIZARD_TEAMS_NOT_FOUND.format(
            names=", ".join(f"«{escape(name)}»" for name in lookup.missing)
        )
    await session.go("teams", add_teams(draft, lookup), notice)


# --------------------------------------------------------------------------- count / markets / sort
async def on_count(callback: CallbackQuery, callback_data: CountCb, session: WizardSession) -> None:
    """Signal count, "Hammasi", typed value or the per-day switch."""
    del callback
    draft = session.draft
    value = callback_data.value
    if value == keyboards.CUSTOM:
        await session.go("count_input", draft)
    elif value == keyboards.COUNT_TOGGLE_PER_DAY:
        await session.go("count", replace(draft, per_day=not draft.per_day))
    elif value == keyboards.COUNT_ALL:
        draft = replace(draft, max_signals=None, per_day=False)
        await session.go(next_step("count", draft), draft)
    else:
        draft = replace(draft, max_signals=value)
        await session.go(next_step("count", draft), draft)


async def on_count_text(message: Message, session: WizardSession) -> None:
    """Typed signal count."""
    value = parse_count(message.text or "", 1, QUERY_MAX_SIGNALS_UPPER)
    draft = replace(session.draft, max_signals=value)
    await session.go(next_step("count_input", draft), draft)


async def on_market(
    callback: CallbackQuery, callback_data: MarketCb, session: WizardSession
) -> None:
    """Toggle a market; "✅ Tayyor" needs at least one."""
    del callback
    draft = session.draft
    if callback_data.market == "done":
        if not draft.markets:
            await session.answer(texts.SELECT_AT_LEAST_ONE, alert=True)
            return
        await session.go(next_step("markets", draft), draft)
        return
    offered = supported_markets(await sport_options(session.ctx.services), draft.sport_set)
    if callback_data.market not in {market.value for market in offered}:
        raise StaleDraftError
    market = MarketType(callback_data.market).value
    chosen = set(draft.markets) ^ {market}
    ordered = tuple(item.value for item in MarketType if item.value in chosen)
    await session.go("markets", replace(draft, markets=ordered))


async def on_sort(callback: CallbackQuery, callback_data: SortCb, session: WizardSession) -> None:
    """Sort order, then the confirmation screen."""
    del callback
    draft = replace(session.draft, sort=SortKey(callback_data.key).value)
    await session.go(next_step("sort", draft), draft)


# --------------------------------------------------------------------------- confirmation
async def on_confirm(
    callback: CallbackQuery, callback_data: ConfirmCb, session: WizardSession
) -> None:
    """▶️ Boshlash / ✏️ O'zgartirish / 💾 Shablon sifatida saqlash."""
    del callback
    match callback_data.action:
        case "start":
            await run_analysis(session)
        case "edit":
            await session.go("edit", session.draft)
        case _:
            await session.go("template_name", session.draft)


async def on_edit(callback: CallbackQuery, callback_data: EditCb, session: WizardSession) -> None:
    """Jump to one step; after it the wizard returns to the confirmation."""
    del callback
    step = callback_data.step if callback_data.step in dict(keyboards.EDIT_STEPS) else "dates"
    await session.go(step, replace(session.draft, editing=True))


async def on_template_name(message: Message, session: WizardSession) -> None:
    """Save the current query as a template, then back to the confirmation."""
    name = (message.text or "").strip()
    draft = session.draft
    template = TemplateDraft(name=name, query=draft.to_query())
    saved = await session.ctx.services.templates.save_template(session.ctx.user_id, template)
    notice = texts.TEMPLATE_SAVED.format(name=escape(saved.name))
    await session.go("confirm", draft, notice)


async def on_running(callback: CallbackQuery, session: WizardSession) -> None:
    """A second tap while the analysis runs."""
    del callback
    await session.answer(texts.ALREADY_RUNNING, alert=True)


# --------------------------------------------------------------------------- router
def _register_callbacks(router: Router) -> None:
    flow = AnalysisFlow
    router.callback_query.register(on_running, StateFilter(flow.running))
    router.callback_query.register(on_nav, NavCb.filter(), StateFilter(*_WIZARD_STATES))
    router.callback_query.register(on_date, DateCb.filter(), StateFilter(flow.dates))
    router.callback_query.register(on_calendar, CalendarCb.filter(), StateFilter(flow.calendar))
    router.callback_query.register(on_sport, SportCb.filter(), StateFilter(flow.sport))
    router.callback_query.register(
        on_probability, ProbabilityCb.filter(), StateFilter(flow.probability)
    )
    router.callback_query.register(on_ev, EvCb.filter(), StateFilter(flow.ev))
    router.callback_query.register(on_scope, ScopeCb.filter(), StateFilter(flow.scope))
    router.callback_query.register(
        on_pick, PickCb.filter(), StateFilter(flow.leagues, flow.teams, flow.fixtures)
    )
    router.callback_query.register(on_count, CountCb.filter(), StateFilter(flow.count))
    router.callback_query.register(on_market, MarketCb.filter(), StateFilter(flow.markets))
    router.callback_query.register(on_sort, SortCb.filter(), StateFilter(flow.sort))
    router.callback_query.register(on_confirm, ConfirmCb.filter(), StateFilter(flow.confirm))
    router.callback_query.register(on_edit, EditCb.filter(), StateFilter(flow.edit))


def _register_messages(router: Router) -> None:
    flow = AnalysisFlow
    router.message.register(start_wizard, F.text == texts.MENU_ANALYSIS)
    text = TYPED_INPUT
    router.message.register(on_dates_text, text, StateFilter(flow.dates, flow.calendar))
    router.message.register(on_probability_text, text, StateFilter(flow.probability_input))
    router.message.register(on_ev_text, text, StateFilter(flow.ev_input))
    router.message.register(on_team_text, text, StateFilter(flow.teams))
    router.message.register(on_count_text, text, StateFilter(flow.count_input))
    router.message.register(on_template_name, text, StateFilter(flow.template_name))


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="analysis")
    router.message.middleware(WizardSessionMiddleware())
    router.callback_query.middleware(WizardSessionMiddleware())
    _register_callbacks(router)
    _register_messages(router)
    return router


__all__ = ["create_router", "start_wizard"]
