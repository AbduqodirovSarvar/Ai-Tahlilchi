"""📈 Hisobot and /eksport through the real dispatcher over the fake services (F7.6)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from aiogram import Bot
from aiogram.methods import SendDocument, SendPhoto

from app.bot import texts
from app.bot.callbacks import ReportCb
from app.bot.dispatcher import build_dispatcher
from app.bot.middleware import RateLimitMiddleware
from app.bot.report_ui import LABEL_WIDTH, segment_label
from app.bot.states import ReportFlow
from app.services.fake import create_fake_services
from app.services.interfaces import (
    AnalysisQuery,
    BetDraft,
    DateSpec,
    ReportGroup,
    ReportRequest,
    Services,
)
from tests.bot.fakes import ADMIN_ID, RecordingSession
from tests.e2e.bot_client import Client, answers, shown, telegram_html_problems
from tests.e2e.conftest import SECOND_ADMIN_ID
from tests.services.contract_suite import START, MutableClock


class World:
    """A client over fake services whose clock the test moves."""

    def __init__(self, bot: Bot, session: RecordingSession) -> None:
        self.clock = MutableClock(now=START)
        self.services: Services = create_fake_services(clock=self.clock)
        dispatcher = build_dispatcher(
            frozenset({ADMIN_ID, SECOND_ADMIN_ID}),
            self.services,
            clock=self.clock,
            rate_limit=RateLimitMiddleware(capacity=10_000),
        )
        self.admin = Client(dispatcher, bot, session, ADMIN_ID)

    async def settle_a_week(self) -> None:
        """Recommend many signals, bet on a few, then let every match finish."""
        result = await self.services.analysis.analyze(
            ADMIN_ID,
            AnalysisQuery(
                dates=DateSpec.next_days(7),
                min_probability=0.3,
                min_ev=0.0,
                markets=frozenset(texts.MARKET_LABELS),
            ),
        )
        for signal in result.signals[:4]:
            await self.services.bets.place_bet(
                ADMIN_ID,
                BetDraft(signal_id=signal.signal_id, odds_taken=2.0, stake=Decimal(1000)),
            )
        self.clock.now = self.clock.now + timedelta(days=9)


@pytest.fixture
def world(bot: Bot, session: RecordingSession) -> World:
    return World(bot, session)


# --------------------------------------------------------------------------- the report
async def test_report_without_signals_is_an_explained_empty_state(world: World) -> None:
    methods = await world.admin.send(texts.MENU_REPORT)

    (text,) = shown(methods)
    assert texts.REPORT_EMPTY in text
    assert world.admin.has_button("✅ 7 kun")
    assert world.admin.has_button(texts.BTN_REPORT_EXPORT)


async def test_report_numbers_match_the_service(world: World) -> None:
    await world.settle_a_week()
    view = await world.services.reports.report(ADMIN_ID, ReportRequest.recent(7))

    (text,) = shown(await world.admin.send(texts.MENU_REPORT))

    assert f"Signallar: <b>{view.signals}</b>" in text
    assert f"yopilgan {view.settled}: ✅ {view.won} · ❌ {view.lost} · ↩️ {view.void}" in text
    assert f"eng uzun mag'lubiyat seriyasi: {view.longest_losing_streak}" in text
    assert texts.REPORT_CALIBRATION_HEADER in text
    assert "<pre>" in text
    assert telegram_html_problems(text) == []
    assert "Kesim bo'yicha" not in text


async def test_hisobot_command_is_the_menu_screen(world: World) -> None:
    await world.settle_a_week()

    assert shown(await world.admin.send("/hisobot")) == shown(
        await world.admin.send(texts.MENU_REPORT)
    )


async def test_changing_the_period_re_reads_the_report(world: World) -> None:
    await world.settle_a_week()
    await world.admin.send(texts.MENU_REPORT)

    methods = await world.admin.press("Hammasi")

    assert "hammasi (" in shown(methods)[0]
    assert world.admin.has_button("✅ Hammasi")
    assert world.admin.has_button("▫️ 7 kun")


@pytest.mark.parametrize("group", list(ReportGroup))
async def test_every_slice_shows_its_table_with_readable_labels(
    world: World, group: ReportGroup
) -> None:
    await world.settle_a_week()
    await world.admin.send(texts.MENU_REPORT)
    await world.admin.press("30 kun")
    view = await world.services.reports.report(ADMIN_ID, ReportRequest.recent(30, group_by=group))

    methods = await world.admin.press(texts.REPORT_GROUP_LABELS[group.value])

    (text,) = shown(methods)
    assert "Kesim bo'yicha" in text
    assert view.segments
    for segment in view.segments[:3]:
        label = segment_label(group, segment.key)[: LABEL_WIDTH - 1]
        assert label.replace("&", "&amp;") in text
    assert telegram_html_problems(text) == []
    assert world.admin.has_button(f"✅ {texts.REPORT_GROUP_LABELS[group.value]}")


async def test_removing_the_slice_returns_to_the_plain_report(world: World) -> None:
    await world.settle_a_week()
    await world.admin.send(texts.MENU_REPORT)
    await world.admin.press("liga")

    methods = await world.admin.press("kesimsiz")

    assert "Kesim bo'yicha" not in shown(methods)[0]


# --------------------------------------------------------------------------- custom period
async def test_custom_range_is_asked_validated_and_kept_for_slices(world: World) -> None:
    await world.settle_a_week()
    await world.admin.send(texts.MENU_REPORT)

    prompt = shown(await world.admin.press("O'zim"))
    assert prompt == [texts.REPORT_RANGE_PROMPT]
    assert await world.admin.state() == ReportFlow.range_input.state
    assert "bunday sana yo'q" in shown(await world.admin.send("31.02 - 03.03"))[0]
    assert shown(await world.admin.send("01.01.2099 - 02.01.2099")) == [texts.REPORT_RANGE_FUTURE]
    assert await world.admin.state() == ReportFlow.range_input.state

    methods = await world.admin.send("20.10 - 30.10")
    assert f"20.10{texts.RANGE_DASH}30.10" in shown(methods)[0]
    assert await world.admin.state() is None
    sliced = await world.admin.press("liga")

    assert f"20.10{texts.RANGE_DASH}30.10" in shown(sliced)[0]
    assert "Kesim bo'yicha" in shown(sliced)[0]


async def test_a_custom_period_button_without_a_stored_range_is_stale(world: World) -> None:
    await world.admin.send(texts.MENU_REPORT)
    message_id = world.admin.last_message_id

    methods = await world.admin.press_data(
        ReportCb(action="show", period="c", group="").pack(), message_id
    )

    assert "Bu tugma eskirgan" in (answers(methods)[0].text or "")


async def test_forged_period_and_group_are_stale(world: World) -> None:
    await world.admin.send(texts.MENU_REPORT)
    message_id = world.admin.last_message_id

    for forged in (ReportCb(action="show", period="9"), ReportCb(action="show", group="zzz")):
        methods = await world.admin.press_data(forged.pack(), message_id)
        assert "Bu tugma eskirgan" in (answers(methods)[0].text or "")


async def test_leaving_the_range_input_by_a_period_button_clears_the_state(world: World) -> None:
    await world.admin.send(texts.MENU_REPORT)
    await world.admin.press("O'zim")

    await world.admin.press(texts.BTN_BACK)

    assert await world.admin.state() is None


# --------------------------------------------------------------------------- charts
async def test_chart_button_sends_the_calibration_png(world: World) -> None:
    await world.settle_a_week()
    await world.admin.send(texts.MENU_REPORT)

    methods = await world.admin.press(texts.BTN_REPORT_CHART)

    photos = [m for m in methods if isinstance(m, SendPhoto)]
    assert len(photos) == 1
    assert photos[0].photo.data.startswith(b"\x89PNG")  # type: ignore[union-attr]
    assert photos[0].caption == texts.REPORT_CHART_CALIBRATION


async def test_chart_with_a_slice_adds_the_yield_chart(world: World) -> None:
    await world.settle_a_week()
    await world.admin.send(texts.MENU_REPORT)
    await world.admin.press("liga")

    methods = await world.admin.press(texts.BTN_REPORT_CHART)

    assert len([m for m in methods if isinstance(m, SendPhoto)]) == 2


async def test_chart_without_data_explains(world: World) -> None:
    await world.admin.send(texts.MENU_REPORT)

    methods = await world.admin.press(texts.BTN_REPORT_CHART)

    assert answers(methods)[0].text == texts.REPORT_NO_CHART
    assert not [m for m in methods if isinstance(m, SendPhoto)]


# --------------------------------------------------------------------------- export
async def test_eksport_sends_signals_and_bets_as_csv(world: World) -> None:
    await world.settle_a_week()

    methods = await world.admin.send("/eksport")

    documents = [m for m in methods if isinstance(m, SendDocument)]
    assert [d.document.filename for d in documents] == ["signals.csv", "bets.csv"]  # type: ignore[union-attr]
    signals, bets = (d.document.data.decode() for d in documents)  # type: ignore[union-attr]
    assert signals.startswith("signal_id,")
    assert bets.startswith("bet_id,")
    assert len(bets.strip().splitlines()) == 1 + 4
    assert [d.caption for d in documents] == [
        texts.EXPORT_CAPTION_SIGNALS,
        texts.EXPORT_CAPTION_BETS,
    ]


async def test_export_button_and_empty_exports(world: World) -> None:
    await world.admin.send(texts.MENU_REPORT)

    methods = await world.admin.press(texts.BTN_REPORT_EXPORT)

    documents = [m for m in methods if isinstance(m, SendDocument)]
    assert len(documents) == 2
    assert len(documents[1].document.data.decode().strip().splitlines()) == 1  # type: ignore[union-attr]


async def test_exports_are_per_user(world: World) -> None:
    await world.settle_a_week()
    other = Client(world.admin.dispatcher, world.admin.bot, world.admin.session, SECOND_ADMIN_ID)

    methods = await other.send("/eksport")

    documents = [m for m in methods if isinstance(m, SendDocument)]
    assert all(len(d.document.data.decode().strip().splitlines()) == 1 for d in documents)  # type: ignore[union-attr]
