"""⚙️ Sozlamalar (plan §6.5): overview plus an inline editor for every field.

Changes are saved immediately through ``SettingsService``; the DTO constructor validates
every value, and a rejected value is explained by the error middleware. Multi-select
fields (sports, markets, leagues) are toggled in place; single-value fields return to the
overview after saving. Only typed values need FSM state (``SettingsFlow.value_input``).
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Final

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import keyboards, texts
from app.bot.callbacks import SettingsCb
from app.bot.draft import StaleDraftError
from app.bot.formatters import clock, format_settings, percent, settings_values
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.bot.notices import HIGH_PROBABILITY, OnceNotices
from app.bot.parsing import (
    parse_count,
    parse_money,
    parse_odds_range,
    parse_percent,
    parse_time,
    parse_time_range,
    parse_timezone,
)
from app.bot.states import SETTINGS_FIELD_KEY, SettingsFlow
from app.bot.wizard import available_sports, high_probability
from app.domain import MarketType, Sport
from app.services.interfaces import (
    MIN_EV_LOWER,
    MIN_EV_UPPER,
    MIN_PROBABILITY_LOWER,
    MIN_PROBABILITY_UPPER,
    SETTINGS_MAX_SIGNALS_UPPER,
    QuietHours,
    Services,
    UserSettingsView,
)

PERCENT_SCALE: Final = 100


def _bounds(field: str) -> dict[str, str]:
    """``low``/``high`` placeholders of a field's prompts."""
    bounds = {
        "min_probability": (MIN_PROBABILITY_LOWER, MIN_PROBABILITY_UPPER),
        "min_ev": (MIN_EV_LOWER, MIN_EV_UPPER),
    }
    if field == "max_signals":
        return {"low": "1", "high": str(SETTINGS_MAX_SIGNALS_UPPER)}
    low, high = bounds.get(field, (0.0, 0.0))
    return {"low": f"{low * PERCENT_SCALE:g}", "high": f"{high * PERCENT_SCALE:g}"}


# --------------------------------------------------------------------------- values
@dataclass(frozen=True, slots=True)
class Choices:
    """Preset values of a single-choice field and their button labels."""

    values: tuple[object, ...]
    labels: tuple[str, ...]


def _quiet_label(value: object) -> str:
    if not isinstance(value, tuple):
        return texts.OFF
    start, end = value
    return f"{clock(start)}{texts.RANGE_DASH}{clock(end)}"


CHOICES: Final[dict[str, Choices]] = {
    "max_signals": Choices(
        keyboards.SETTINGS_MAX_SIGNALS_CHOICES,
        tuple(str(n) for n in keyboards.SETTINGS_MAX_SIGNALS_CHOICES),
    ),
    "min_probability": Choices(
        keyboards.SETTINGS_PROBABILITY_CHOICES,
        tuple(percent(p) for p in keyboards.SETTINGS_PROBABILITY_CHOICES),
    ),
    "min_ev": Choices(
        keyboards.SETTINGS_EV_CHOICES, tuple(percent(p) for p in keyboards.SETTINGS_EV_CHOICES)
    ),
    "odds": Choices(
        keyboards.SETTINGS_ODDS_CHOICES,
        tuple(
            f"{low:.2f}{texts.RANGE_DASH}{high:.2f}"
            for low, high in keyboards.SETTINGS_ODDS_CHOICES
        ),
    ),
    "send_time": Choices(
        keyboards.SETTINGS_SEND_TIME_CHOICES,
        tuple(clock(at) for at in keyboards.SETTINGS_SEND_TIME_CHOICES),
    ),
    "prematch": Choices(
        keyboards.SETTINGS_PREMATCH_CHOICES,
        tuple(
            texts.OFF if minutes is None else texts.PREMATCH_MINUTES.format(minutes=minutes)
            for minutes in keyboards.SETTINGS_PREMATCH_CHOICES
        ),
    ),
    "quiet": Choices(
        keyboards.SETTINGS_QUIET_CHOICES,
        tuple(_quiet_label(value) for value in keyboards.SETTINGS_QUIET_CHOICES),
    ),
    "timezone": Choices(keyboards.SETTINGS_TIMEZONE_CHOICES, keyboards.SETTINGS_TIMEZONE_CHOICES),
}


def _current_value(settings: UserSettingsView, field: str) -> object:
    """The settings value a field's presets are compared with."""
    match field:
        case "odds":
            return (settings.odds_min, settings.odds_max)
        case "quiet":
            hours = settings.quiet_hours
            return None if hours is None else (hours.start, hours.end)
        case "prematch":
            return settings.prematch_minutes
        case _:
            return getattr(settings, field)


_RENAMED: Final = {
    "prematch": "prematch_minutes",
    "daily_loss": "daily_loss_limit",
    "weekly_loss": "weekly_loss_limit",
    "leagues": "league_ids",
}


def _pair(value: object) -> tuple[Any, Any]:
    """Unpack a two-value preset/parse result (odds range, quiet hours)."""
    if not isinstance(value, tuple) or len(value) != 2:  # noqa: PLR2004 - a pair
        raise StaleDraftError
    return value[0], value[1]


def with_value(settings: UserSettingsView, field: str, value: object) -> UserSettingsView:
    """Copy of ``settings`` with ``field`` set (validated by the DTO constructor).

    Raises:
        ValidationFailedError: the value breaks a settings rule (e.g. unit above bank).
    """
    if field == "odds":
        low, high = _pair(value)
        return replace(settings, odds_min=low, odds_max=high)
    if field == "quiet":
        hours = None if value is None else QuietHours(*_pair(value))
        return replace(settings, quiet_hours=hours)
    changes: dict[str, Any] = {_RENAMED.get(field, field): value}
    return replace(settings, **changes)


TEXT_PARSERS: Final[dict[str, Callable[[str], object]]] = {
    "max_signals": lambda text: parse_count(text, 1, SETTINGS_MAX_SIGNALS_UPPER),
    "min_probability": lambda text: parse_percent(
        text, MIN_PROBABILITY_LOWER, MIN_PROBABILITY_UPPER
    ),
    "min_ev": lambda text: parse_percent(text, MIN_EV_LOWER, MIN_EV_UPPER),
    "odds": parse_odds_range,
    "send_time": parse_time,
    "quiet": parse_time_range,
    "timezone": parse_timezone,
    "bank": parse_money,
    "unit": parse_money,
    "daily_loss": parse_money,
    "weekly_loss": parse_money,
}


# --------------------------------------------------------------------------- editors
async def _toggle_options(
    field: str, settings: UserSettingsView, services: Services
) -> tuple[list[str], list[str], frozenset[str]]:
    """``(values, labels, selected values)`` of a multi-select field."""
    if field == "sports":
        sports = await available_sports(services)
        return (
            [sport.value for sport in sports],
            [texts.SPORT_LABELS[sport] for sport in sports],
            frozenset(sport.value for sport in settings.sports),
        )
    if field == "markets":
        return (
            [market.value for market in MarketType],
            [texts.MARKET_LABELS[market] for market in MarketType],
            frozenset(market.value for market in settings.markets),
        )
    values: list[str] = []
    labels: list[str] = []
    for sport in sorted(settings.sports):
        for league in await services.catalog.list_leagues(sport):
            values.append(league.league_id)
            labels.append(league.name)
    return values, labels, settings.league_ids


async def league_names(settings: UserSettingsView, services: Services) -> dict[str, str]:
    """Names of the leagues of the user's sports, for the overview."""
    values, labels, _ = await _toggle_options("leagues", settings, services)
    return dict(zip(values, labels, strict=True))


async def render_editor(
    field: str, settings: UserSettingsView, services: Services
) -> tuple[str, InlineKeyboardMarkup]:
    """Text and keyboard of one field's editor."""
    prompt = texts.SETTINGS_PROMPTS[field].format(**_bounds(field))
    if field in keyboards.MULTI_SELECT_FIELDS:
        values, labels, selected = await _toggle_options(field, settings, services)
        flags = [value in selected for value in values]
        return prompt, keyboards.settings_toggle_keyboard(field, labels, flags)
    if field == "risk":
        return prompt, keyboards.risk_keyboard(settings.risk_profile)
    shown = settings_values(settings, await league_names(settings, services))[field]
    text = f"{prompt}\n{texts.SETTINGS_CURRENT.format(value=shown)}"
    choices = CHOICES.get(field)
    if choices is None:
        return text, keyboards.settings_choice_keyboard(field, ())
    current = _current_value(settings, field)
    index = choices.values.index(current) if current in choices.values else None
    return text, keyboards.settings_choice_keyboard(field, choices.labels, current=index)


async def _overview(settings: UserSettingsView, services: Services) -> str:
    return format_settings(settings, await league_names(settings, services))


# --------------------------------------------------------------------------- handlers
async def show_settings(
    message: Message, state: FSMContext, services: Services, user_id: int
) -> None:
    """⚙️ Sozlamalar / ``/sozlamalar``: the overview."""
    await state.set_state(None)
    settings = await services.settings.get_settings(user_id)
    await send_html(message, await _overview(settings, services), keyboards.settings_keyboard())


def _field(callback_data: SettingsCb) -> str:
    if callback_data.field not in texts.SETTINGS_BUTTONS:
        raise StaleDraftError
    return callback_data.field


async def _save(services: Services, user_id: int, updated: UserSettingsView) -> UserSettingsView:
    return await services.settings.update_settings(user_id, updated)


async def _apply_callback(
    callback_data: SettingsCb, settings: UserSettingsView, services: Services, user_id: int
) -> UserSettingsView:
    """Apply a ``set``/``toggle``/``off`` press and return the saved settings."""
    field = _field(callback_data)
    if callback_data.action == "off":
        return await _save(services, user_id, with_value(settings, field, None))
    if field == "risk":
        profile = _pick(keyboards.SETTINGS_RISK_CHOICES, callback_data.index)
        return await services.settings.apply_risk_profile(user_id, profile)
    if callback_data.action == "toggle":
        values, _, selected = await _toggle_options(field, settings, services)
        toggled_value = _pick(tuple(values), callback_data.index)
        toggled = frozenset(selected ^ {toggled_value})
        typed = _typed_selection(field, toggled)
        return await _save(services, user_id, with_value(settings, field, typed))
    choices = CHOICES.get(field)
    if choices is None:
        raise StaleDraftError
    value = _pick(choices.values, callback_data.index)
    return await _save(services, user_id, with_value(settings, field, value))


def _typed_selection(field: str, values: frozenset[str]) -> object:
    if field == "sports":
        return frozenset(Sport(value) for value in values)
    if field == "markets":
        return frozenset(MarketType(value) for value in values)
    return values


def _pick[T](values: tuple[T, ...], index: int) -> T:
    if not 0 <= index < len(values):
        raise StaleDraftError
    return values[index]


async def on_settings(
    callback: CallbackQuery,
    callback_data: SettingsCb,
    state: FSMContext,
    services: Services,
    notices: OnceNotices,
) -> None:
    """Every settings button."""
    user_id = callback.from_user.id
    message = callback_message(callback)
    if message is None:
        raise StaleDraftError
    field = _field(callback_data)
    settings = await services.settings.get_settings(user_id)
    action = callback_data.action
    if action == "back":
        await state.set_state(None)
        await answer_callback(callback)
        await edit_html(message, await _overview(settings, services), keyboards.settings_keyboard())
        return
    if action == "input":
        await state.set_state(SettingsFlow.value_input)
        await state.update_data({SETTINGS_FIELD_KEY: field})
        await answer_callback(callback)
        prompt = texts.SETTINGS_INPUT_PROMPTS[field].format(**_bounds(field))
        await edit_html(message, prompt, keyboards.settings_input_keyboard(field))
        return
    if action != "open":
        settings = await _apply_callback(callback_data, settings, services, user_id)
        await answer_callback(callback, texts.SETTINGS_SAVED)
        if field not in keyboards.MULTI_SELECT_FIELDS:
            text = await _overview(settings, services)
            if field == "min_probability":
                text = _with_warning(text, settings, notices, user_id)
            await edit_html(message, text, keyboards.settings_keyboard())
            return
    await answer_callback(callback)
    text, markup = await render_editor(field, settings, services)
    await edit_html(message, text, markup)


async def on_settings_text(
    message: Message, state: FSMContext, services: Services, user_id: int, notices: OnceNotices
) -> None:
    """A typed settings value."""
    data = await state.get_data()
    field = data.get(SETTINGS_FIELD_KEY)
    parser = TEXT_PARSERS.get(field) if isinstance(field, str) else None
    if parser is None or not isinstance(field, str):
        await state.set_state(None)
        raise StaleDraftError
    value = parser(message.text or "")
    settings = await services.settings.get_settings(user_id)
    saved = await _save(services, user_id, with_value(settings, field, value))
    await state.set_state(None)
    text = await _overview(saved, services)
    if field == "min_probability":
        text = _with_warning(text, saved, notices, user_id)
    await send_html(message, f"{texts.SETTINGS_SAVED}\n\n{text}", keyboards.settings_keyboard())


def _with_warning(text: str, settings: UserSettingsView, notices: OnceNotices, user_id: int) -> str:
    """Prefix the one-time §6.6 warning when the saved minimum probability is ≥ 80%."""
    if high_probability(settings.min_probability) and notices.first_time(user_id, HIGH_PROBABILITY):
        return f"{texts.HIGH_PROBABILITY_WARNING}\n\n{text}"
    return text


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="settings")
    router.message.register(show_settings, F.text == texts.MENU_SETTINGS)
    router.message.register(show_settings, Command("sozlamalar"))
    router.message.register(
        on_settings_text,
        TYPED_INPUT,
        StateFilter(SettingsFlow.value_input),
    )
    router.callback_query.register(on_settings, SettingsCb.filter())
    return router
