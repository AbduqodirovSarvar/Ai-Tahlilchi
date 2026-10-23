"""📈 Hisobot (plan §6.10, F7.6): periods, slices, calibration, chart PNGs and CSV export.

The report message edits itself when a period or slice button is pressed; a custom period is
typed once and kept in FSM data for the following slice buttons. Charts and exports are sent as
new messages (photos and documents cannot replace a text). Chart rendering blocks, so it runs in
a worker thread.
"""

import asyncio
from dataclasses import replace
from datetime import date
from typing import Final

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot import texts
from app.bot.callbacks import ReportCb
from app.bot.charts import calibration_chart, segments_chart
from app.bot.draft import StaleDraftError
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.keyboards import markup_of, single_button
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.bot.parsing import InputError, parse_date_range
from app.bot.report_ui import (
    GROUP_CODES,
    PERIOD_CUSTOM,
    build_request,
    format_report,
    report_keyboard,
    segment_label,
)
from app.bot.states import ReportFlow, leave_flow
from app.services.interfaces import ExportFile, ReportGroup, ReportView

COMMAND_REPORT: Final = "hisobot"
COMMAND_EXPORT: Final = "eksport"
DEFAULT_PERIOD: Final = "7"
RANGE_KEY: Final = "report_range"


def _message_of(callback: CallbackQuery) -> Message:
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        raise StaleDraftError
    return message


def _group(code: str) -> ReportGroup | None:
    if code not in GROUP_CODES:
        raise StaleDraftError
    return GROUP_CODES[code]


async def _custom_range(flow: FlowContext) -> tuple[date, date] | None:
    stored = (await flow.state.get_data()).get(RANGE_KEY)
    if not isinstance(stored, list) or len(stored) != 2:  # noqa: PLR2004 - (from, to)
        return None
    try:
        return date.fromisoformat(stored[0]), date.fromisoformat(stored[1])
    except (TypeError, ValueError):
        return None


async def _report(
    flow: FlowContext, period: str, group: ReportGroup | None
) -> tuple[ReportView, str]:
    request = build_request(period, group, await _custom_range(flow))
    view = await flow.services.reports.report(flow.user_id, request)
    return view, format_report(view, group)


async def _leave_input(flow: FlowContext) -> None:
    if await flow.state.get_state() == ReportFlow.range_input.state:
        await leave_flow(flow.state)


# --------------------------------------------------------------------------- show
async def open_report(message: Message, flow: FlowContext) -> None:
    """📈 Hisobot / ``/hisobot``: the last 7 days."""
    await _leave_input(flow)
    _, text = await _report(flow, DEFAULT_PERIOD, None)
    await send_html(message, text, report_keyboard(DEFAULT_PERIOD, None))


async def on_show(callback: CallbackQuery, callback_data: ReportCb, flow: FlowContext) -> None:
    """A period or slice button: the report again, with the choice highlighted."""
    group = _group(callback_data.group)
    _, text = await _report(flow, callback_data.period, group)
    await answer_callback(callback)
    await _leave_input(flow)
    await edit_html(_message_of(callback), text, report_keyboard(callback_data.period, group))


async def on_custom(callback: CallbackQuery, callback_data: ReportCb, flow: FlowContext) -> None:
    """📅 O'zim: ask for a date range."""
    _group(callback_data.group)
    await answer_callback(callback)
    await flow.state.set_state(ReportFlow.range_input)
    back = markup_of(
        [
            [
                single_button(
                    texts.BTN_BACK,
                    ReportCb(action="show", period=DEFAULT_PERIOD, group=callback_data.group),
                )
            ]
        ]
    )
    await edit_html(_message_of(callback), texts.REPORT_RANGE_PROMPT, back)


async def on_range_text(message: Message, flow: FlowContext) -> None:
    """The typed range: store it and show the report for it."""
    today = await flow.today()
    start, end = parse_date_range(message.text or "", today)
    if end > today:
        raise InputError(texts.REPORT_RANGE_FUTURE)
    await flow.state.update_data({RANGE_KEY: [start.isoformat(), end.isoformat()]})
    await leave_flow(flow.state)
    _, text = await _report(flow, PERIOD_CUSTOM, None)
    await send_html(message, text, report_keyboard(PERIOD_CUSTOM, None))


# --------------------------------------------------------------------------- charts
async def on_chart(callback: CallbackQuery, callback_data: ReportCb, flow: FlowContext) -> None:
    """📊 Grafik: calibration and (with a slice) yield PNGs of the shown report."""
    group = _group(callback_data.group)
    view, _ = await _report(flow, callback_data.period, group)
    message = _message_of(callback)
    drawings: list[tuple[str, bytes]] = []
    if view.calibration:
        png = await asyncio.to_thread(calibration_chart, view.calibration)
        drawings.append((texts.REPORT_CHART_CALIBRATION, png))
    if group is not None and view.segments:
        label = texts.REPORT_GROUP_LABELS[group.value]
        title = texts.CHART_SEGMENTS_TITLE.format(group=label)
        named = [
            replace(segment, key=segment_label(group, segment.key)) for segment in view.segments
        ]
        png = await asyncio.to_thread(segments_chart, named, title)
        drawings.append((texts.REPORT_CHART_SEGMENTS.format(group=label), png))
    if not drawings:
        await answer_callback(callback, texts.REPORT_NO_CHART, alert=True)
        return
    await answer_callback(callback)
    for caption, png in drawings:
        await message.answer_photo(
            BufferedInputFile(png, filename="report.png"),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )


# --------------------------------------------------------------------------- export
async def _send_file(message: Message, file: ExportFile, caption: str) -> None:
    await message.answer_document(
        BufferedInputFile(file.content, filename=file.filename), caption=caption
    )


async def export_files(message: Message, flow: FlowContext) -> None:
    """``/eksport``: the signals and the bets as CSV documents."""
    signals = await flow.services.reports.export(flow.user_id)
    bets = await flow.services.reports.export_bets(flow.user_id)
    await _send_file(message, signals, texts.EXPORT_CAPTION_SIGNALS)
    await _send_file(message, bets, texts.EXPORT_CAPTION_BETS)


async def on_export(callback: CallbackQuery, flow: FlowContext) -> None:
    """📤 CSV button."""
    await answer_callback(callback)
    await export_files(_message_of(callback), flow)


# --------------------------------------------------------------------------- router
def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="report")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(open_report, F.text == texts.MENU_REPORT)
    router.message.register(open_report, Command(COMMAND_REPORT))
    router.message.register(export_files, Command(COMMAND_EXPORT))
    router.message.register(on_range_text, TYPED_INPUT, StateFilter(ReportFlow.range_input))
    router.callback_query.register(on_show, ReportCb.filter(F.action == "show"))
    router.callback_query.register(on_custom, ReportCb.filter(F.action == "custom"))
    router.callback_query.register(on_chart, ReportCb.filter(F.action == "chart"))
    router.callback_query.register(on_export, ReportCb.filter(F.action == "export"))
    return router
