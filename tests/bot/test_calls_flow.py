"""📡 So'rovlar: pure formatters and the flow through the real dispatcher (fake call log)."""

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.methods import SendDocument
from aiogram.types import BufferedInputFile

from app.bot import texts
from app.bot.callbacks import CallsCb
from app.bot.calls_ui import (
    MESSAGE_BUDGET,
    calls_keyboard,
    duration_label,
    format_call_file,
    format_calls,
    format_detail,
    format_stats,
    is_clipped,
    needs_file,
    request_for,
)
from app.domain import CallOutcome, Provider
from app.services.fake import create_fake_services
from app.services.interfaces import CallLogDetail, CallLogEntry, CallLogPage, CallLogStat
from tests.bot.browse_samples import WithOverrides
from tests.bot.fakes import BotHarness, RecordingSession, answers, fixed_clock, sent_texts
from tests.e2e.bot_client import telegram_html_problems

TASHKENT = ZoneInfo("Asia/Tashkent")
AT = datetime(2026, 10, 21, 20, 0, 5, tzinfo=UTC)  # 01:00:05 on 22.10 in Tashkent


def entry(call_id: int = 1, **overrides: object) -> CallLogEntry:
    base = CallLogEntry(
        call_id=call_id,
        at=AT,
        provider=Provider.API_FOOTBALL,
        operation="fixtures",
        outcome=CallOutcome.OK,
        duration_ms=230,
        http_status=200,
        quota_cost=1,
        request_preview='fixtures {"date": "2026-10-21"}',
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def detail(**overrides: object) -> CallLogDetail:
    base = CallLogDetail(
        call_id=7,
        at=AT,
        provider=Provider.GEMINI,
        operation="llm_review",
        outcome=CallOutcome.OK,
        duration_ms=5200,
        http_status=None,
        quota_cost=1,
        request_preview="p",
        request="prompt <b>&",
        response='{"ok": true}',
        error=None,
        size_bytes=2048,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def page(*items: CallLogEntry, total: int | None = None, pages: int = 1) -> CallLogPage:
    return CallLogPage(
        items=tuple(items), page=0, page_count=pages, total=len(items) if total is None else total
    )


# --------------------------------------------------------------------------- pure
def test_stats_group_by_provider_and_escape_operations() -> None:
    stats = [
        CallLogStat(
            provider=Provider.GEMINI,
            operation="llm<x>",
            calls=3,
            errors=0,
            cached=0,
            blocked=0,
            avg_duration_ms=None,
            quota_cost=3,
        ),
        CallLogStat(
            provider=Provider.API_FOOTBALL,
            operation="odds",
            calls=10,
            errors=2,
            cached=3,
            blocked=1,
            avg_duration_ms=1500,
            quota_cost=6,
        ),
    ]

    text = format_stats(stats)

    assert text.index("API-Football") < text.index("Gemini")
    assert "<b>10</b> ta · ❌ 2 · 💾 keshdan 3 · 🚫 bloklangan 1 · kvota <b>6</b>" in text
    assert "~1.5 s · ❌ 2" in text
    assert "llm&lt;x&gt;" in text
    assert telegram_html_problems(text) == []


def test_empty_stats_have_a_state_text() -> None:
    assert texts.CALLS_STATS_EMPTY in format_stats([])


def test_list_shows_local_time_outcome_duration_and_escaped_preview() -> None:
    items = (
        entry(1, request_preview="<script>&" + "x" * 100),
        entry(2, outcome=CallOutcome.ERROR, http_status=500, duration_ms=15000),
        entry(3, outcome=CallOutcome.CACHED, duration_ms=0, http_status=None),
        entry(4, provider=Provider.GEMINI, outcome=CallOutcome.QUOTA_BLOCKED),
    )

    text = format_calls(page(*items), "a", TASHKENT)

    assert "<b>22.10 01:00</b>" in text
    assert "✅ · 230 ms" in text
    assert "❌ xato · HTTP 500 · 15.0 s" in text
    assert "💾 kesh · 0 ms" in text
    assert "🤖" in text
    assert "&lt;script&gt;&amp;" in text
    assert "…" in text  # long preview is cut
    assert telegram_html_problems(text) == []


def test_list_empty_states() -> None:
    assert texts.CALLS_EMPTY in format_calls(page(), "a", TASHKENT)
    assert texts.CALLS_EMPTY_FILTER in format_calls(page(), "e", TASHKENT)


def test_keyboard_has_filters_numbers_pager_and_short_callbacks() -> None:
    markup = calls_keyboard(page(entry(123456789), entry(2), total=30, pages=3), "g")

    labels = [b.text for row in markup.inline_keyboard for b in row]
    data = [b.callback_data or "" for row in markup.inline_keyboard for b in row]
    assert "● Gemini" in labels
    assert {"1", "2", "1/3", texts.BTN_CALLS_STATS} <= set(labels)
    assert all(len(d.encode()) <= 64 for d in data)


def test_filter_codes_map_to_service_requests() -> None:
    assert request_for("f", 2).provider is Provider.API_FOOTBALL
    assert request_for("g", 0).provider is Provider.GEMINI
    assert request_for("e", 0).outcome is CallOutcome.ERROR
    assert request_for("a", 0).provider is None
    assert request_for("zz", -3).page == 0


def test_short_detail_is_one_message_with_everything() -> None:
    parts = format_detail(detail(), TASHKENT)

    assert len(parts) == 1
    assert "So'rov #7" in parts[0]
    assert "22.10.2026 01:00:05" in parts[0]
    assert "<pre>prompt &lt;b&gt;&amp;</pre>" in parts[0]
    assert "5.2 s" in parts[0]
    assert not needs_file(detail())
    assert telegram_html_problems(parts[0]) == []


def test_long_texts_are_cut_on_screen_but_every_part_fits_telegram() -> None:
    big = detail(request="<" * 3000 + "\n" + "&" * 3000, response="r" * 9000, error="e" * 4000)

    parts = format_detail(big, TASHKENT)

    assert needs_file(big)
    assert all(len(part) <= 4096 for part in parts)
    assert all(telegram_html_problems(part) == [] for part in parts)
    joined = "\n".join(parts)
    assert "To'liq matnni fayl" in joined
    assert joined.count("<pre>") == joined.count("</pre>")


def test_detail_without_response_and_with_clip_marker() -> None:
    clipped = detail(response=None, request="a\n…[1500 ta belgi qirqildi]")

    text = format_detail(clipped, TASHKENT)[0]

    assert is_clipped(clipped)
    assert "qirqilgan" in text
    assert texts.CALLS_DETAIL_NO_RESPONSE in text
    assert not is_clipped(detail())


def test_a_message_budget_below_telegram_limit() -> None:
    assert MESSAGE_BUDGET < 4096
    assert duration_label(999) == "999 ms"
    assert duration_label(1000) == "1.0 s"


def test_file_text_is_plain_and_complete() -> None:
    text = format_call_file(detail(error="boom <x>"), TASHKENT)

    assert "prompt <b>&" in text
    assert "boom <x>" in text
    assert '=== JAVOB ===\n{"ok": true}' in text
    assert "&lt;" not in text
    assert "Asia/Tashkent" in text


# --------------------------------------------------------------------------- flow
async def test_menu_shows_stats_and_filter_buttons(harness: BotHarness) -> None:
    methods = await harness.send(texts.MENU_CALLS)

    text = sent_texts(methods)[0]
    assert "API-Football" in text
    assert "Gemini" in text
    harness.find_button("Faqat xatolar")


async def test_filters_list_open_detail_and_go_back(harness: BotHarness) -> None:
    await harness.send("/sorovlar")
    await harness.press("Faqat xatolar")
    assert "Faqat xatolar — 2 ta" in harness.last_text
    methods = await harness.press("1")
    assert any("So'rov #" in text for text in sent_texts(methods))
    assert "Holat: ❌ xato · HTTP 500" in harness.last_text or "Holat: ❌ xato" in harness.last_text
    await harness.press(texts.BTN_CALLS_BACK)
    assert "Faqat xatolar — 2 ta" in harness.last_text
    await harness.press(texts.BTN_CALLS_STATS)
    assert texts.CALLS_TITLE in harness.last_text


async def test_provider_filters_count(harness: BotHarness) -> None:
    await harness.send("/sorovlar")
    await harness.press("Gemini")
    assert "Gemini — 4 ta" in harness.last_text
    await harness.press("API-Football")
    assert "API-Football — 10 ta" in harness.last_text


async def test_non_admin_is_refused_on_every_entry(bot: Bot, session: RecordingSession) -> None:
    services = create_fake_services(clock=fixed_clock, admin_ids=frozenset({42}))
    harness = BotHarness(bot, session, services)

    methods = await harness.send("/sorovlar")

    assert sent_texts(methods) == [texts.NOT_AUTHORIZED]


async def test_unknown_call_is_reported_as_not_found(harness: BotHarness) -> None:
    await harness.send("/sorovlar")
    await harness.press("Hammasi")
    message_id, _ = harness.find_button("1")

    methods = await harness.press_data(CallsCb(action="item", i=9999).pack(), message_id)

    assert texts.NOT_FOUND in sent_texts(methods)
    assert len(answers(methods)) == 1


async def test_long_response_offers_the_file_and_sends_it(
    bot: Bot, session: RecordingSession
) -> None:
    services = create_fake_services(clock=fixed_clock)
    original = services.calls

    async def get_call(user_id: int, call_id: int) -> CallLogDetail:
        stored = await original.get_call(user_id, call_id)
        return replace(stored, response="R" * 9000)

    harness = BotHarness(
        bot,
        session,
        replace(services, calls=WithOverrides(original, get_call=get_call)),  # type: ignore[arg-type]
    )
    await harness.send("/sorovlar")
    await harness.press("Hammasi")
    methods = await harness.press("1")

    assert all(len(text) <= 4096 for text in sent_texts(methods))
    sent = await harness.press(texts.BTN_CALLS_FILE)
    documents = [m for m in sent if isinstance(m, SendDocument)]
    assert len(documents) == 1
    document = documents[0].document
    assert isinstance(document, BufferedInputFile)
    assert b"R" * 9000 in document.data
    assert document.filename.endswith(".txt")
