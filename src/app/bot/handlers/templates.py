"""💾 Shablonlarim (plan §6.2, F7.4): list, run in the wizard, schedule editor, delete.

Running a template opens the wizard's confirmation screen with the template's query, so the
cost estimate is shown and every parameter can still be changed before ▶️ Boshlash. The
schedule editor keeps its state in FSM data (``ScheduleDraft``) tied to its message; the
buttons of the list and of a template screen are stateless and always re-read the template,
so a template deleted meanwhile is reported instead of acted on.
"""

from dataclasses import replace
from typing import Final

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.callbacks import TplCb
from app.bot.draft import StaleDraftError, WizardDraft
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.formatters import escape
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.keyboards import SETTINGS_SEND_TIME_CHOICES, markup_of, single_button
from app.bot.messaging import (
    answer_callback,
    callback_message,
    drop_keyboard,
    edit_html,
    send_html,
)
from app.bot.parsing import parse_time
from app.bot.session import WizardSession
from app.bot.states import TemplateFlow, drop_draft
from app.bot.template_ui import (
    ScheduleDraft,
    delete_keyboard,
    format_schedule,
    format_template,
    format_templates,
    schedule_keyboard,
    schedule_label,
    template_keyboard,
    templates_keyboard,
)
from app.bot.wizard import WizardContext
from app.logging import get_logger
from app.services.errors import NotFoundError
from app.services.interfaces import TemplateDraft, TemplateView

logger = get_logger(__name__)

SCHEDULE_KEY: Final = "template_schedule"


def _message_of(callback: CallbackQuery) -> Message:
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        raise StaleDraftError
    return message


async def _template(flow: FlowContext, template_id: int) -> TemplateView:
    """The user's template.

    Raises:
        NotFoundError: it was deleted meanwhile (or never was the user's).
    """
    for view in await flow.services.templates.list_templates(flow.user_id):
        if view.template_id == template_id:
            return view
    raise NotFoundError("template", template_id)


# --------------------------------------------------------------------------- list and screen
async def _show_list(message: Message, flow: FlowContext, notice: str | None = None) -> None:
    views = await flow.services.templates.list_templates(flow.user_id)
    text = format_templates(views)
    await edit_html(message, f"{notice}\n\n{text}" if notice else text, templates_keyboard(views))


async def open_templates(message: Message, flow: FlowContext) -> None:
    """💾 Shablonlarim: the saved templates."""
    await _drop_schedule(flow)
    views = await flow.services.templates.list_templates(flow.user_id)
    await send_html(message, format_templates(views), templates_keyboard(views))


async def on_list(callback: CallbackQuery, flow: FlowContext) -> None:
    """⬅️ Shablonlarga."""
    await answer_callback(callback)
    await _drop_schedule(flow)
    await _show_list(_message_of(callback), flow)


async def on_open(callback: CallbackQuery, callback_data: TplCb, flow: FlowContext) -> None:
    """A template of the list: its parameters and actions."""
    view = await _template(flow, callback_data.id)
    await answer_callback(callback)
    await _drop_schedule(flow)
    await edit_html(_message_of(callback), format_template(view), template_keyboard(view))


# --------------------------------------------------------------------------- run
async def _scope_labels(flow: FlowContext, view: TemplateView) -> dict[str, str]:
    """Names for the league ids of the query (teams and fixtures have no id lookup)."""
    labels: dict[str, str] = {}
    for sport in sorted(view.query.sports):
        for league in await flow.services.catalog.list_leagues(sport):
            labels[league.league_id] = league.name
    return labels


async def on_run(callback: CallbackQuery, callback_data: TplCb, flow: FlowContext) -> None:
    """▶️ Ishga tushirish: the wizard's confirmation screen with the template's query."""
    view = await _template(flow, callback_data.id)
    settings = await flow.settings()
    labels = await _scope_labels(flow, view)
    draft = WizardDraft.from_settings(settings).with_query(view.query, labels)
    ctx = WizardContext(
        services=flow.services,
        user_id=flow.user_id,
        zone=settings.zone,
        now=flow.clock(),
        notices=flow.notices,
    )
    await _drop_schedule(flow)
    await drop_draft(flow.state)
    session = WizardSession(ctx, flow.state, callback, None)
    await session.go("confirm", draft, texts.TPL_RUNNING.format(name=escape(view.name)))


# --------------------------------------------------------------------------- delete
async def on_delete(callback: CallbackQuery, callback_data: TplCb, flow: FlowContext) -> None:
    """🗑 O'chirish: ask first."""
    view = await _template(flow, callback_data.id)
    await answer_callback(callback)
    text = texts.TPL_DELETE_ASK.format(name=escape(view.name))
    await edit_html(_message_of(callback), text, delete_keyboard(view.template_id))


async def on_delete_yes(callback: CallbackQuery, callback_data: TplCb, flow: FlowContext) -> None:
    """🗑 Ha, o'chirish."""
    view = await _template(flow, callback_data.id)
    await flow.services.templates.delete_template(flow.user_id, view.template_id)
    await answer_callback(callback)
    notice = texts.TPL_DELETED.format(name=escape(view.name))
    await _show_list(_message_of(callback), flow, notice)


# --------------------------------------------------------------------------- schedule
async def _save_schedule_draft(flow: FlowContext, draft: ScheduleDraft) -> None:
    await flow.state.update_data({SCHEDULE_KEY: draft.to_data()})


async def _drop_schedule(flow: FlowContext) -> None:
    data = await flow.state.get_data()
    if SCHEDULE_KEY in data or await flow.state.get_state() == TemplateFlow.time_input.state:
        data.pop(SCHEDULE_KEY, None)
        await flow.state.set_state(None)
        await flow.state.set_data(data)


async def _load_schedule(
    flow: FlowContext, callback: CallbackQuery, template_id: int
) -> ScheduleDraft:
    """The editor's draft for the message a button belongs to (anything else is stale)."""
    data = await flow.state.get_data()
    draft = ScheduleDraft.from_data(data.get(SCHEDULE_KEY))
    message = _message_of(callback)
    if draft.message_id != message.message_id or draft.template_id != template_id:
        raise StaleDraftError
    return draft


async def _show_schedule(message: Message, flow: FlowContext, draft: ScheduleDraft) -> None:
    view = await _template(flow, draft.template_id)
    zone = (await flow.settings()).timezone
    await edit_html(message, format_schedule(view, draft, zone), schedule_keyboard(draft))


async def on_schedule(callback: CallbackQuery, callback_data: TplCb, flow: FlowContext) -> None:
    """🗓 Jadval: open the editor."""
    view = await _template(flow, callback_data.id)
    settings = await flow.settings()
    message = _message_of(callback)
    await answer_callback(callback)
    draft = ScheduleDraft.starting(view, message.message_id, settings.send_time)
    await flow.state.set_state(None)
    await _save_schedule_draft(flow, draft)
    await _show_schedule(message, flow, draft)


async def on_day(callback: CallbackQuery, callback_data: TplCb, flow: FlowContext) -> None:
    """Switch a weekday of the schedule."""
    draft = (await _load_schedule(flow, callback, callback_data.id)).toggled(callback_data.value)
    await answer_callback(callback)
    await _save_schedule_draft(flow, draft)
    await _show_schedule(_message_of(callback), flow, draft)


async def on_time(callback: CallbackQuery, callback_data: TplCb, flow: FlowContext) -> None:
    """Pick one of the time presets."""
    draft = await _load_schedule(flow, callback, callback_data.id)
    if not 0 <= callback_data.value < len(SETTINGS_SEND_TIME_CHOICES):
        raise StaleDraftError
    draft = draft.at_time(SETTINGS_SEND_TIME_CHOICES[callback_data.value])
    await answer_callback(callback)
    await _save_schedule_draft(flow, draft)
    await _show_schedule(_message_of(callback), flow, draft)


async def on_typed(callback: CallbackQuery, callback_data: TplCb, flow: FlowContext) -> None:
    """✏️ Vaqt kiritish: wait for a typed time."""
    await _load_schedule(flow, callback, callback_data.id)
    await answer_callback(callback)
    await flow.state.set_state(TemplateFlow.time_input)
    back = markup_of(
        [[single_button(texts.BTN_TPL_BACK, TplCb(action="open", id=callback_data.id))]]
    )
    await edit_html(_message_of(callback), texts.TPL_TIME_PROMPT, back)


async def on_time_text(message: Message, flow: FlowContext) -> None:
    """The typed time: back to the editor in a fresh message (the old keyboard is retired)."""
    data = await flow.state.get_data()
    draft = ScheduleDraft.from_data(data.get(SCHEDULE_KEY))
    draft = draft.at_time(parse_time(message.text or ""))
    if message.bot is not None:
        await drop_keyboard(message.bot, message.chat.id, draft.message_id)
    view = await _template(flow, draft.template_id)
    zone = (await flow.settings()).timezone
    sent = await send_html(message, format_schedule(view, draft, zone), schedule_keyboard(draft))
    await flow.state.set_state(None)
    await _save_schedule_draft(flow, replace(draft, message_id=sent.message_id))


async def on_save(callback: CallbackQuery, callback_data: TplCb, flow: FlowContext) -> None:
    """💾 Saqlash: store the schedule with the template."""
    draft = await _load_schedule(flow, callback, callback_data.id)
    schedule = draft.schedule()
    if schedule is None:
        await answer_callback(callback, texts.TPL_PICK_A_DAY, alert=True)
        return
    view = await _template(flow, draft.template_id)
    saved = await flow.services.templates.save_template(
        flow.user_id, TemplateDraft(name=view.name, query=view.query, schedule=schedule)
    )
    await answer_callback(callback)
    await _drop_schedule(flow)
    text = f"{texts.TPL_SCHEDULE_SAVED.format(schedule=schedule_label(schedule))}\n\n"
    await edit_html(_message_of(callback), text + format_template(saved), template_keyboard(saved))


async def on_clear(callback: CallbackQuery, callback_data: TplCb, flow: FlowContext) -> None:
    """🚫 Jadvalni o'chirish."""
    view = await _template(flow, callback_data.id)
    saved = await flow.services.templates.save_template(
        flow.user_id, TemplateDraft(name=view.name, query=view.query, schedule=None)
    )
    await answer_callback(callback)
    await _drop_schedule(flow)
    text = f"{texts.TPL_SCHEDULE_CLEARED.format(name=escape(saved.name))}\n\n"
    await edit_html(_message_of(callback), text + format_template(saved), template_keyboard(saved))


# --------------------------------------------------------------------------- router
def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="templates")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(open_templates, F.text == texts.MENU_TEMPLATES)
    router.message.register(on_time_text, TYPED_INPUT, StateFilter(TemplateFlow.time_input))
    for action, handler in (
        ("list", on_list),
        ("open", on_open),
        ("run", on_run),
        ("delete", on_delete),
        ("delete_yes", on_delete_yes),
        ("schedule", on_schedule),
        ("day", on_day),
        ("time", on_time),
        ("typed", on_typed),
        ("save", on_save),
        ("clear", on_clear),
    ):
        router.callback_query.register(handler, TplCb.filter(F.action == action))
    return router
