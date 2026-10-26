"""Pure parts of 📡 So'rovlar: statistics, the call list, the call detail and the text file.

Request and response texts are provider data (prompts, JSON bodies): they are escaped, shown
in ``<pre>`` blocks that each fit one message, and anything longer than the inline limit is
shortened on screen with the full text offered as a ``.txt`` file. The log itself cuts texts at
``MAX_LOGGED_TEXT`` characters and appends a marker, which the detail surfaces as a note.
"""

import re
from collections.abc import Iterable, Sequence
from itertools import groupby
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.bot.browse_ui import size_label
from app.bot.callbacks import CallsCb
from app.bot.formatters import escape
from app.bot.keyboards import markup_of, single_button
from app.bot.paging import Page, pager_row
from app.domain import MAX_LOGGED_TEXT, CallOutcome, Provider
from app.services.interfaces import (
    CallLogDetail,
    CallLogEntry,
    CallLogPage,
    CallLogRequest,
    CallLogStat,
)

CALLS_PAGE_SIZE: Final = 10
FILTER_ALL: Final = "a"
FILTER_API: Final = "f"
FILTER_GEMINI: Final = "g"
FILTER_ERRORS: Final = "e"
FILTERS: Final = (FILTER_ALL, FILTER_API, FILTER_GEMINI, FILTER_ERRORS)
MESSAGE_BUDGET: Final = 3900
"""Characters of one detail message; below Telegram's 4096 to leave room for the escaping."""
MS_PER_SECOND: Final = 1000
_CLIP_MARK: Final = re.compile(r"…\[\d+ ta belgi qirqildi\]\s*$")
_CHOICE_WIDTH: Final = 5

type Row = list[InlineKeyboardButton]


def request_for(filter_code: str, page: int) -> CallLogRequest:
    """The service request behind a filter button (unknown codes mean "all")."""
    provider = {FILTER_API: Provider.API_FOOTBALL, FILTER_GEMINI: Provider.GEMINI}.get(filter_code)
    outcome = CallOutcome.ERROR if filter_code == FILTER_ERRORS else None
    return CallLogRequest(
        provider=provider, outcome=outcome, page=max(page, 0), page_size=CALLS_PAGE_SIZE
    )


def duration_label(duration_ms: int) -> str:
    """``230 ms`` below a second, ``1.4 s`` above."""
    if duration_ms < MS_PER_SECOND:
        return texts.CALLS_DURATION_MS.format(value=duration_ms)
    return texts.CALLS_DURATION_S.format(value=f"{duration_ms / MS_PER_SECOND:.1f}")


def _local(entry: CallLogEntry, zone: ZoneInfo, pattern: str) -> str:
    return entry.at.astimezone(zone).strftime(pattern)


# --------------------------------------------------------------------------- statistics
def _operation_line(stat: CallLogStat) -> str:
    average = (
        ""
        if stat.avg_duration_ms is None
        else texts.CALLS_STAT_AVERAGE.format(duration=duration_label(stat.avg_duration_ms))
    )
    errors = texts.CALLS_STAT_ERRORS.format(errors=stat.errors) if stat.errors else ""
    return texts.CALLS_STAT_OPERATION.format(
        operation=escape(stat.operation), calls=stat.calls, average=average, errors=errors
    )


def _provider_block(provider: Provider, stats: Sequence[CallLogStat]) -> list[str]:
    return [
        texts.CALLS_STAT_PROVIDER.format(
            provider=texts.CALLS_PROVIDERS[provider.value],
            calls=sum(stat.calls for stat in stats),
            errors=sum(stat.errors for stat in stats),
            cached=sum(stat.cached for stat in stats),
            blocked=sum(stat.blocked for stat in stats),
            quota=sum(stat.quota_cost for stat in stats),
        ),
        *(_operation_line(stat) for stat in sorted(stats, key=lambda s: -s.calls)),
    ]


def format_stats(stats: Iterable[CallLogStat]) -> str:
    """Per provider: totals, then one line per operation (or the empty state)."""
    ordered = sorted(stats, key=lambda stat: list(Provider).index(stat.provider))
    if not ordered:
        return "\n".join([texts.CALLS_TITLE, "", texts.CALLS_STATS_EMPTY])
    lines = [texts.CALLS_TITLE]
    for provider, group in groupby(ordered, key=lambda stat: stat.provider):
        lines.extend(["", *_provider_block(provider, list(group))])
    lines.extend(["", texts.CALLS_PICK_FILTER])
    return "\n".join(lines)


def filter_row(current: str | None = None) -> Row:
    """The four filter buttons; the active one carries a dot."""
    return [
        single_button(
            texts.CALLS_FILTER_MARK.format(label=texts.BTN_CALLS_FILTERS[code])
            if code == current
            else texts.BTN_CALLS_FILTERS[code],
            CallsCb(action="list", f=code),
        )
        for code in FILTERS
    ]


def stats_keyboard() -> InlineKeyboardMarkup:
    """Filters in two rows: provider choices and "errors only"."""
    row = filter_row()
    return markup_of([row[:2], row[2:]])


# --------------------------------------------------------------------------- list
def _preview(entry: CallLogEntry) -> str:
    cleaned = " ".join(entry.request_preview.split())
    if len(cleaned) > texts.CALLS_PREVIEW_CHARS:
        cleaned = cleaned[: texts.CALLS_PREVIEW_CHARS - 1] + "…"
    return cleaned


def _call_line(number: int, entry: CallLogEntry, zone: ZoneInfo) -> list[str]:
    outcome = texts.CALLS_OUTCOMES[entry.outcome.value]
    if entry.outcome is CallOutcome.ERROR and entry.http_status is not None:
        outcome += texts.CALLS_LINE_HTTP.format(status=entry.http_status)
    lines = [
        texts.CALLS_LINE.format(
            n=number,
            provider=texts.CALLS_PROVIDER_ICONS[entry.provider.value],
            time=_local(entry, zone, "%d.%m %H:%M"),
            operation=escape(entry.operation),
            outcome=outcome,
            duration=duration_label(entry.duration_ms),
        )
    ]
    preview = _preview(entry)
    if preview:
        lines.append(texts.CALLS_LINE_PREVIEW.format(preview=escape(preview)))
    return lines


def format_calls(page: CallLogPage, filter_code: str, zone: ZoneInfo) -> str:
    """The numbered calls of a page, newest first (or why there are none)."""
    title = texts.CALLS_LIST_TITLE.format(
        filter=texts.BTN_CALLS_FILTERS.get(filter_code, texts.BTN_CALLS_FILTERS[FILTER_ALL]),
        total=page.total,
    )
    if not page.items:
        empty = texts.CALLS_EMPTY if filter_code == FILTER_ALL else texts.CALLS_EMPTY_FILTER
        return f"{title}\n\n{empty}"
    lines = [title, ""]
    for number, entry in enumerate(page.items, start=1):
        lines.extend(_call_line(number, entry, zone))
    return "\n".join(lines)


def calls_keyboard(page: CallLogPage, filter_code: str) -> InlineKeyboardMarkup:
    """Filters, numbered buttons that open a call, the pager and the statistics."""
    choices = [
        single_button(
            str(number), CallsCb(action="item", i=entry.call_id, f=filter_code, p=page.page)
        )
        for number, entry in enumerate(page.items, start=1)
    ]
    rows: list[Row] = [
        filter_row(filter_code),
        *(choices[i : i + _CHOICE_WIDTH] for i in range(0, len(choices), _CHOICE_WIDTH)),
    ]
    pager = pager_row(
        Page((), page.page, max(page.page_count, 1)),
        lambda n: CallsCb(action="list", f=filter_code, p=n),
    )
    if pager:
        rows.append(pager)
    rows.append([single_button(texts.BTN_CALLS_STATS, CallsCb(action="stats"))])
    return markup_of(rows)


# --------------------------------------------------------------------------- detail
def is_clipped(detail: CallLogDetail) -> bool:
    """True when the log cut one of the texts (it ends with the clip marker)."""
    return any(
        text is not None and _CLIP_MARK.search(text)
        for text in (detail.request, detail.response, detail.error)
    )


def needs_file(detail: CallLogDetail) -> bool:
    """True when a text is too long to show in full on screen."""
    return any(
        text is not None and len(text) > texts.CALLS_INLINE_LIMIT
        for text in (detail.request, detail.response, detail.error)
    )


def _pre_blocks(text: str) -> list[str]:
    """``<pre>`` blocks of at most :data:`MESSAGE_BUDGET` escaped characters each.

    A ``<pre>`` spans lines, so it must never be cut by the generic line splitter: every block
    is a complete element that fits one message on its own.
    """
    blocks: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        piece = escape(char)
        if size + len(piece) > MESSAGE_BUDGET:
            blocks.append(f"<pre>{''.join(current)}</pre>")
            current, size = [], 0
        current.append(piece)
        size += len(piece)
    if current:
        blocks.append(f"<pre>{''.join(current)}</pre>")
    return blocks


def _section(title: str, body: str) -> list[str]:
    """A titled text: in full when short, else its beginning and a pointer to the file."""
    body = body or texts.CALLS_FILE_EMPTY
    if len(body) <= texts.CALLS_INLINE_LIMIT:
        return [title, *_pre_blocks(body)]
    shown = body[: texts.CALLS_INLINE_PREVIEW]
    note = texts.CALLS_DETAIL_CUT.format(shown=len(shown), total=len(body))
    return [title, *_pre_blocks(shown), note]


def _header(detail: CallLogDetail, zone: ZoneInfo) -> list[str]:
    http = (
        ""
        if detail.http_status is None
        else texts.CALLS_DETAIL_HTTP.format(status=detail.http_status)
    )
    size = (
        ""
        if detail.size_bytes is None
        else texts.CALLS_DETAIL_SIZE.format(size=size_label(detail.size_bytes))
    )
    lines = [
        texts.CALLS_DETAIL_TITLE.format(id=detail.call_id),
        texts.CALLS_DETAIL_WHO.format(
            provider=texts.CALLS_PROVIDERS[detail.provider.value],
            operation=escape(detail.operation),
        ),
        texts.CALLS_DETAIL_TIME.format(
            time=detail.at.astimezone(zone).strftime("%d.%m.%Y %H:%M:%S"), zone=escape(zone.key)
        ),
        texts.CALLS_DETAIL_STATE.format(
            outcome=texts.CALLS_OUTCOME_LONG[detail.outcome.value], http=http
        ),
        texts.CALLS_DETAIL_NUMBERS.format(
            duration=duration_label(detail.duration_ms), quota=detail.quota_cost, size=size
        ),
    ]
    if is_clipped(detail):
        lines.append(texts.CALLS_DETAIL_CLIPPED.format(limit=MAX_LOGGED_TEXT))
    return lines


def _pack(blocks: Sequence[str]) -> list[str]:
    """Join blocks into messages of at most :data:`MESSAGE_BUDGET` characters, never cutting one."""
    parts: list[str] = []
    current = ""
    for block in blocks:
        candidate = block if not current else f"{current}\n{block}"
        if current and len(candidate) > MESSAGE_BUDGET:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def format_detail(detail: CallLogDetail, zone: ZoneInfo) -> list[str]:
    """The detail as messages (each within Telegram's limit; ``<pre>`` blocks stay whole)."""
    blocks = [*_header(detail, zone), *_section(texts.CALLS_DETAIL_REQUEST, detail.request)]
    if detail.response is None:
        blocks.append(texts.CALLS_DETAIL_NO_RESPONSE)
    else:
        blocks.extend(_section(texts.CALLS_DETAIL_RESPONSE, detail.response))
    if detail.error:
        blocks.extend(_section(texts.CALLS_DETAIL_ERROR, detail.error))
    return _pack(blocks)


def detail_keyboard(detail: CallLogDetail, filter_code: str, page: int) -> InlineKeyboardMarkup:
    """The file button (only for long texts) and the way back to the list page."""
    rows: list[Row] = []
    if needs_file(detail):
        rows.append([single_button(texts.BTN_CALLS_FILE, CallsCb(action="file", i=detail.call_id))])
    rows.append(
        [single_button(texts.BTN_CALLS_BACK, CallsCb(action="list", f=filter_code, p=page))]
    )
    return markup_of(rows)


def format_call_file(detail: CallLogDetail, zone: ZoneInfo) -> str:
    """Plain text of the whole call (no HTML), for the ``.txt`` document."""
    http = (
        ""
        if detail.http_status is None
        else texts.CALLS_DETAIL_HTTP.format(status=detail.http_status)
    )
    header = texts.CALLS_FILE_HEADER.format(
        id=detail.call_id,
        provider=detail.provider.value,
        operation=detail.operation,
        time=detail.at.astimezone(zone).strftime("%d.%m.%Y %H:%M:%S"),
        zone=zone.key,
        outcome=detail.outcome.value,
        http=http,
        duration=duration_label(detail.duration_ms),
        quota=detail.quota_cost,
    )
    sections = [
        (texts.CALLS_FILE_REQUEST, detail.request),
        (texts.CALLS_FILE_RESPONSE, detail.response),
        (texts.CALLS_FILE_ERROR, detail.error),
    ]
    body = [f"{title}\n{text or texts.CALLS_FILE_EMPTY}" for title, text in sections]
    return "\n\n".join([header, *body]) + "\n"
