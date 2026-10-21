"""Analysis wizard screens: what each step shows and where "back"/"next" lead.

Handlers stay thin: they update the :class:`~app.bot.draft.WizardDraft` from the button or
text, then ask :func:`render` for the next :class:`Screen` and display it. Rendering may
call read-only services (leagues, fixture pages, cost estimate), never mutating ones.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.fsm.state import State
from aiogram.types import InlineKeyboardMarkup

from app.bot import keyboards, texts
from app.bot.draft import Option, WizardDraft
from app.bot.formatters import (
    clock,
    escape,
    markets_label,
    percent,
    short_date,
    sports_label,
    weekday_name,
    window_label,
)
from app.bot.keyboards import PickerRow
from app.bot.notices import OnceNotices
from app.bot.parsing import InputError
from app.bot.states import AnalysisFlow
from app.domain import MarketType, Sport
from app.services.interfaces import (
    HIGH_PROBABILITY_WARNING,
    MAX_FUTURE_DAYS,
    MAX_SCOPE_IDS,
    MAX_SEARCH_RESULTS,
    MAX_TEMPLATE_NAME_LENGTH,
    MIN_EV_LOWER,
    MIN_EV_UPPER,
    MIN_PROBABILITY_LOWER,
    MIN_PROBABILITY_UPPER,
    MIN_SEARCH_LENGTH,
    QUERY_MAX_SIGNALS_UPPER,
    FixtureListRequest,
    ScopeKind,
    Services,
    SportOption,
    TeamOption,
)

FIXTURE_PAGE_SIZE: Final = 8
TEAM_CANDIDATES: Final = 5
"""Search hits offered per ambiguous team name."""

STEP_STATES: Final[dict[str, State]] = {
    "dates": AnalysisFlow.dates,
    "calendar": AnalysisFlow.calendar,
    "sport": AnalysisFlow.sport,
    "probability": AnalysisFlow.probability,
    "probability_input": AnalysisFlow.probability_input,
    "ev": AnalysisFlow.ev,
    "ev_input": AnalysisFlow.ev_input,
    "scope": AnalysisFlow.scope,
    "leagues": AnalysisFlow.leagues,
    "teams": AnalysisFlow.teams,
    "fixtures": AnalysisFlow.fixtures,
    "count": AnalysisFlow.count,
    "count_input": AnalysisFlow.count_input,
    "markets": AnalysisFlow.markets,
    "sort": AnalysisFlow.sort,
    "confirm": AnalysisFlow.confirm,
    "edit": AnalysisFlow.edit,
    "template_name": AnalysisFlow.template_name,
}
BACK: Final[dict[str, str | None]] = {
    "dates": None,
    "calendar": "dates",
    "sport": "dates",
    "probability": "sport",
    "probability_input": "probability",
    "ev": "probability",
    "ev_input": "ev",
    "scope": "ev",
    "leagues": "scope",
    "teams": "scope",
    "fixtures": "scope",
    "count": "scope",
    "count_input": "count",
    "markets": "count",
    "sort": "markets",
    "confirm": "sort",
    "edit": "confirm",
    "template_name": "confirm",
}
"""Previous step; ``None`` means "back" leaves the wizard."""
NEXT: Final[dict[str, str]] = {
    "dates": "sport",
    "calendar": "sport",
    "sport": "probability",
    "probability": "ev",
    "probability_input": "ev",
    "ev": "scope",
    "ev_input": "scope",
    "scope": "count",
    "leagues": "count",
    "teams": "count",
    "fixtures": "count",
    "count": "markets",
    "count_input": "markets",
    "markets": "sort",
    "sort": "confirm",
}
_CONTINUES_WHEN_EDITING: Final = frozenset({"probability", "probability_input"})
"""Steps that keep going while editing ("Ehtimollik va EV" edits both)."""


def step_of(state_name: str | None) -> str | None:
    """Step name of an FSM state string such as ``AnalysisFlow:ev``."""
    for step, state in STEP_STATES.items():
        if state.state == state_name:
            return step
    return None


def next_step(step: str, draft: WizardDraft) -> str:
    """Where a completed step leads; while editing, back to the confirmation."""
    if draft.editing and step not in _CONTINUES_WHEN_EDITING:
        return "confirm"
    return NEXT[step]


@dataclass(frozen=True, slots=True)
class WizardContext:
    """Everything rendering needs about the user and the system."""

    services: Services
    user_id: int
    zone: ZoneInfo
    now: datetime
    notices: OnceNotices

    @property
    def today(self) -> date:
        """The user's local date."""
        return self.now.astimezone(self.zone).date()


@dataclass(frozen=True, slots=True)
class Screen:
    """A rendered step: message text, keyboard, FSM state and the (possibly updated) draft."""

    step: str
    text: str
    markup: InlineKeyboardMarkup
    draft: WizardDraft

    @property
    def state(self) -> State:
        """FSM state of the step."""
        return STEP_STATES[self.step]


async def sport_options(services: Services) -> tuple[SportOption, ...]:
    """Sports the system can analyse now (unavailable ones are not offered at all)."""
    return tuple(option for option in await services.catalog.list_sports() if option.available)


def supported_markets(
    options: Sequence[SportOption], sports: frozenset[Sport]
) -> frozenset[MarketType]:
    """Markets priced for at least one of ``sports``."""
    return frozenset(
        market
        for option in options
        if option.sport in sports
        for market in option.supported_markets
    )


def fit_markets(
    markets: Sequence[str], options: Sequence[SportOption], sports: frozenset[Sport]
) -> tuple[str, ...]:
    """Keep the chosen markets the sports support; if none remain, offer all supported."""
    supported = supported_markets(options, sports)
    kept = [market for market in MarketType if market.value in markets and market in supported]
    return tuple(market.value for market in (kept or sorted(supported)))


def high_probability(value: float) -> bool:
    """True when the §6.6 warning applies."""
    return value >= HIGH_PROBABILITY_WARNING


# --------------------------------------------------------------------------- rendering
async def render(
    step: str, draft: WizardDraft, ctx: WizardContext, notice: str | None = None
) -> Screen:
    """Screen of ``step``; ``notice`` (already HTML) is shown above the step text."""
    renderer = _RENDERERS[step]
    screen = await renderer(draft, ctx)
    if notice:
        screen = replace(screen, text=f"{notice}\n\n{screen.text}")
    return screen


async def _dates(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    text = texts.WIZARD_DATES.format(max_future=MAX_FUTURE_DAYS)
    return Screen("dates", text, keyboards.dates_keyboard(), draft)


async def _calendar(draft: WizardDraft, ctx: WizardContext) -> Screen:
    start = date.fromisoformat(draft.calendar_start) if draft.calendar_start else None
    month = draft.calendar_month or keyboards.month_key(ctx.today)
    text = (
        texts.CALENDAR_PICK_START
        if start is None
        else texts.CALENDAR_PICK_END.format(start=short_date(start))
    )
    markup = keyboards.calendar_keyboard(month, ctx.today, start)
    return Screen("calendar", text, markup, replace(draft, calendar_month=month))


async def _sport(draft: WizardDraft, ctx: WizardContext) -> Screen:
    options = await sport_options(ctx.services)
    return Screen("sport", texts.WIZARD_SPORT, keyboards.sports_keyboard(options), draft)


def _default(draft: WizardDraft, key: str, fallback: float) -> float:
    value = draft.defaults.get(key, fallback)
    return float(value) if isinstance(value, int | float) else fallback


async def _probability(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    default = _default(draft, "min_probability", draft.min_probability)
    text = texts.WIZARD_PROBABILITY.format(default=percent(default))
    return Screen("probability", text, keyboards.probability_keyboard(default), draft)


def _input_screen(step: str, text: str, draft: WizardDraft) -> Screen:
    return Screen(step, text, keyboards.nav_keyboard(), draft)


async def _probability_input(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    text = texts.WIZARD_PROBABILITY_INPUT.format(
        low=f"{MIN_PROBABILITY_LOWER * 100:g}", high=f"{MIN_PROBABILITY_UPPER * 100:g}"
    )
    return _input_screen("probability_input", text, draft)


async def _ev(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    default = _default(draft, "min_ev", draft.min_ev)
    text = texts.WIZARD_EV.format(
        probability=percent(draft.min_probability), default=percent(default)
    )
    return Screen("ev", text, keyboards.ev_keyboard(default), draft)


async def _ev_input(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    text = texts.WIZARD_EV_INPUT.format(
        low=f"{MIN_EV_LOWER * 100:g}", high=f"{MIN_EV_UPPER * 100:g}"
    )
    return _input_screen("ev_input", text, draft)


async def _scope(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    return Screen("scope", texts.WIZARD_SCOPE, keyboards.scope_keyboard(draft.scope_kind), draft)


def _rows(options: Sequence[Option], selected: Sequence[str]) -> list[PickerRow]:
    chosen = set(selected)
    return [
        PickerRow(label=label, index=index, selected=entity_id in chosen)
        for index, (entity_id, label) in enumerate(options)
    ]


async def _leagues(draft: WizardDraft, ctx: WizardContext) -> Screen:
    options: list[Option] = []
    for sport in sorted(draft.sport_set):
        leagues = await ctx.services.catalog.list_leagues(sport)
        options.extend((league.league_id, league.name) for league in leagues)
    draft = replace(draft, options=tuple(options))
    if not options:
        return Screen("leagues", texts.WIZARD_LEAGUES_EMPTY, keyboards.nav_keyboard(), draft)
    text = texts.WIZARD_LEAGUES.format(count=len(draft.scope_ids))
    markup = keyboards.picker_keyboard(_rows(options, draft.scope_ids))
    return Screen("leagues", text, markup, draft)


async def _teams(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    selected = list(zip(draft.scope_ids, draft.scope_labels, strict=True))
    extra = [option for option in draft.options if option[0] not in draft.scope_ids]
    options = (*selected, *extra)
    draft = replace(draft, options=options)
    text = texts.WIZARD_TEAMS.format(count=len(draft.scope_ids))
    if extra:
        text = f"{text}\n{texts.WIZARD_TEAMS_CHOOSE}"
    markup = keyboards.picker_keyboard(_rows(options, draft.scope_ids))
    return Screen("teams", text, markup, draft)


async def _fixtures(draft: WizardDraft, ctx: WizardContext) -> Screen:
    request = FixtureListRequest(
        dates=draft.date_spec(),
        sports=draft.sport_set,
        page=draft.page,
        page_size=FIXTURE_PAGE_SIZE,
    )
    page = await ctx.services.catalog.list_fixtures(ctx.user_id, request)
    if page.total == 0:
        empty = replace(draft, options=())
        return Screen("fixtures", texts.WIZARD_FIXTURES_EMPTY, keyboards.nav_keyboard(), empty)
    options: list[Option] = []
    rows: list[PickerRow] = []
    current_day: date | None = None
    chosen = set(draft.scope_ids)
    for fixture in page.items:
        local = fixture.kickoff.astimezone(ctx.zone)
        if local.date() != current_day:
            current_day = local.date()
            rows.append(
                PickerRow(label=f"📅 {weekday_name(current_day)}, {short_date(current_day)}")
            )
        label = f"{clock(local.time())} {fixture.label}"
        rows.append(
            PickerRow(label=label, index=len(options), selected=fixture.fixture_id in chosen)
        )
        options.append((fixture.fixture_id, f"{short_date(local.date())} {fixture.label}"))
    page_count = max(page.page_count, 1)
    draft = replace(draft, options=tuple(options), page_count=page_count)
    text = texts.WIZARD_FIXTURES.format(
        count=len(draft.scope_ids), page=draft.page + 1, pages=page_count
    )
    return Screen("fixtures", text, keyboards.picker_keyboard(rows, draft.page, page_count), draft)


async def _count(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    default = int(_default(draft, "max_signals", draft.max_signals or 0))
    text = texts.WIZARD_COUNT.format(
        default=default, per_day=texts.CHECKED if draft.per_day else texts.UNCHECKED
    )
    return Screen("count", text, keyboards.count_keyboard(default, per_day=draft.per_day), draft)


async def _count_input(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    text = texts.WIZARD_COUNT_INPUT.format(high=QUERY_MAX_SIGNALS_UPPER)
    return _input_screen("count_input", text, draft)


async def _markets(draft: WizardDraft, ctx: WizardContext) -> Screen:
    offered = supported_markets(await sport_options(ctx.services), draft.sport_set)
    markup = keyboards.markets_keyboard(draft.market_set, offered)
    return Screen("markets", texts.WIZARD_MARKETS, markup, draft)


async def _sort(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    return Screen("sort", texts.WIZARD_SORT, keyboards.sort_keyboard(draft.sort_key), draft)


def summary_fields(draft: WizardDraft, window: str) -> dict[str, str]:
    """Values of the "📋 So'rov" summary shared by the confirmation and result screens."""
    if draft.scope_kind is ScopeKind.ALL:
        scope = texts.SCOPE_ALL_LABEL
    else:
        scope = ", ".join(escape(label) for label in draft.scope_labels)
    if draft.max_signals is None:
        count = texts.COUNT_ALL_LABEL
    elif draft.per_day:
        count = texts.COUNT_PER_DAY_LABEL.format(count=draft.max_signals)
    else:
        count = str(draft.max_signals)
    return {
        "window": window,
        "sports": sports_label(draft.sport_set),
        "probability": percent(draft.min_probability),
        "ev": percent(draft.min_ev),
        "scope": scope,
        "markets": markets_label(draft.market_set),
        "count": count,
        "sort": texts.SORT_LABELS[draft.sort_key],
    }


async def _confirm(draft: WizardDraft, ctx: WizardContext) -> Screen:
    query = draft.to_query()
    estimate = await ctx.services.analysis.estimate_cost(ctx.user_id, query)
    text = texts.WIZARD_CONFIRM.format(
        **summary_fields(draft, window_label(estimate.window)),
        fixtures=estimate.fixtures_found,
        requests=estimate.api_requests,
        remaining=estimate.budget_remaining,
    )
    notes = []
    if estimate.window.is_retro(ctx.today):
        notes.append(texts.WIZARD_CONFIRM_RETRO)
    if estimate.fixtures_found == 0:
        notes.append(texts.WIZARD_NO_FIXTURES)
    if not estimate.within_budget:
        notes.append(
            texts.WIZARD_OVER_BUDGET.format(
                requests=estimate.api_requests, remaining=estimate.budget_remaining
            )
        )
    if notes:
        text = "\n\n".join([text, *notes])
    can_start = estimate.within_budget and estimate.fixtures_found > 0
    draft = replace(draft, editing=False)
    return Screen("confirm", text, keyboards.confirm_keyboard(can_start=can_start), draft)


async def _edit(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    return Screen(
        "edit", texts.WIZARD_EDIT, keyboards.edit_keyboard(), replace(draft, editing=True)
    )


async def _template_name(draft: WizardDraft, ctx: WizardContext) -> Screen:
    del ctx
    text = texts.TEMPLATE_NAME_PROMPT.format(max_name=MAX_TEMPLATE_NAME_LENGTH)
    return _input_screen("template_name", text, draft)


_RENDERERS: Final = {
    "dates": _dates,
    "calendar": _calendar,
    "sport": _sport,
    "probability": _probability,
    "probability_input": _probability_input,
    "ev": _ev,
    "ev_input": _ev_input,
    "scope": _scope,
    "leagues": _leagues,
    "teams": _teams,
    "fixtures": _fixtures,
    "count": _count,
    "count_input": _count_input,
    "markets": _markets,
    "sort": _sort,
    "confirm": _confirm,
    "edit": _edit,
    "template_name": _template_name,
}


# --------------------------------------------------------------------------- team search
@dataclass(frozen=True, slots=True)
class TeamLookup:
    """Outcome of resolving typed team names against the catalog."""

    found: tuple[TeamOption, ...]
    ambiguous: dict[str, tuple[TeamOption, ...]]
    missing: tuple[str, ...]


def _exact(name: str, options: Sequence[TeamOption]) -> TeamOption | None:
    wanted = name.casefold()
    exact = [option for option in options if option.name.casefold() == wanted]
    if len(exact) == 1:
        return exact[0]
    return options[0] if len(options) == 1 else None


async def lookup_teams(
    services: Services, names: Sequence[str], sports: frozenset[Sport]
) -> TeamLookup:
    """Resolve each name: a unique/exact hit is taken, several hits are ambiguous.

    Raises:
        InputError: a name is shorter than the catalog's minimum search length.
    """
    sport = next(iter(sports)) if len(sports) == 1 else None
    found: list[TeamOption] = []
    ambiguous: dict[str, tuple[TeamOption, ...]] = {}
    missing: list[str] = []
    for name in names:
        if len(name.strip()) < MIN_SEARCH_LENGTH:
            raise InputError(texts.VALIDATION_BY_FIELD["text"].format(min_search=MIN_SEARCH_LENGTH))
        options = await services.catalog.search_teams(
            name, sport=sport, limit=min(TEAM_CANDIDATES, MAX_SEARCH_RESULTS)
        )
        match = _exact(name, options)
        if match is not None:
            found.append(match)
        elif options:
            ambiguous[name] = tuple(options)
        else:
            missing.append(name)
    return TeamLookup(tuple(found), ambiguous, tuple(missing))


def add_teams(draft: WizardDraft, lookup: TeamLookup) -> WizardDraft:
    """Select the found teams and offer the ambiguous candidates as options."""
    for team in lookup.found:
        if team.team_id not in draft.scope_ids and len(draft.scope_ids) < MAX_SCOPE_IDS:
            draft = draft.toggle_scope_item(team.team_id, team.name)
    candidates = [
        (team.team_id, f"{team.name} ({team.league})")
        for options in lookup.ambiguous.values()
        for team in options
    ]
    return replace(draft, options=tuple(dict.fromkeys(candidates)))
