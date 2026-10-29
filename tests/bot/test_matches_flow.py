"""⚽ O'yinlar: pure formatters and the flow through the real dispatcher (stub match service)."""

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot

from app.bot import texts
from app.bot.formatters import kickoff_stamp
from app.bot.matches_ui import (
    MATCHES_PAGE_SIZE,
    clamp_page,
    format_match_sheet,
    format_matches,
    ordered_for_display,
    page_count,
)
from app.services.errors import NotFoundError, ServiceUnavailableError
from app.services.fake import create_fake_services
from app.services.interfaces import LineupPlayer, MatchItem, MatchSheet, MatchState, SideLineup
from tests.bot.fakes import BotHarness, RecordingSession, fixed_clock
from tests.e2e.bot_client import telegram_html_problems

TASHKENT = ZoneInfo("Asia/Tashkent")
TODAY = fixed_clock().astimezone(TASHKENT).date()  # 21.10.2026


def item(match_id: int = 1, **overrides: object) -> MatchItem:
    base = MatchItem(
        match_id=match_id,
        kickoff=datetime(2026, 10, 30, 16, 0, tzinfo=UTC),
        competition="Premier League",
        competition_code="PL",
        home="Chelsea <FC>",
        away="Liverpool FC",
        state=MatchState.SCHEDULED,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def sheet(**overrides: object) -> MatchSheet:
    base = MatchSheet(
        match=item(state=MatchState.FINISHED, home_goals=1, away_goals=2),
        home_ht_goals=0,
        away_ht_goals=1,
        home=SideLineup(
            team="Chelsea <FC>",
            formation="4-2-3-1",
            coach="Enzo Maresca",
            starters=(LineupPlayer(name="Robert Sanchez", position="Goalkeeper", shirt_number=1),),
            bench=(LineupPlayer(name="Filip Jorgensen"),),
        ),
        away=SideLineup(
            team="Liverpool FC", starters=(LineupPlayer(name="Alisson", shirt_number=1),)
        ),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- pure
def test_kickoff_stamp_is_full_local_date_and_time() -> None:
    assert kickoff_stamp(datetime(2026, 10, 30, 16, 0, tzinfo=UTC), TASHKENT) == "30.10.2026 21:00"


def test_list_shows_full_date_groups_and_escapes() -> None:
    items = ordered_for_display(
        [
            item(1),
            item(2, competition="Serie A", kickoff=datetime(2026, 10, 30, 15, 0, tzinfo=UTC)),
            item(3, state=MatchState.LIVE, home_goals=1, away_goals=0),
            item(4, home=None),
        ]
    )
    text = format_matches(date(2026, 10, 30), items, 0, TASHKENT)
    assert telegram_html_problems(text) == []
    assert "30.10.2026" in text
    assert text.index("Serie A") < text.index("Premier League")  # earliest competition first
    assert "<b>30.10.2026 21:00</b> · Chelsea &lt;FC&gt; — Liverpool FC · ⏳ kutilmoqda" in text
    assert "🔴 jonli 1:0" in text
    assert texts.MATCHES_TBD in text
    assert "Football-Data.org" in text


def test_empty_day_and_paging() -> None:
    assert texts.MATCHES_EMPTY in format_matches(TODAY, (), 0, TASHKENT)
    many = tuple(item(i) for i in range(MATCHES_PAGE_SIZE * 2 + 1))
    assert page_count(len(many)) == 3
    assert page_count(0) == 1
    assert clamp_page(9, len(many)) == 2
    assert clamp_page(-1, len(many)) == 0
    text = format_matches(TODAY, many, 2, TASHKENT)
    assert "Sahifa 3/3" in text
    assert f"{MATCHES_PAGE_SIZE * 2 + 1}. " in text


def test_sheet_shows_score_half_time_coach_and_lineups() -> None:
    text = format_match_sheet(sheet(), TASHKENT)
    assert telegram_html_problems(text) == []
    assert "30.10.2026 21:00" in text
    assert "✅ tugagan 1:2" in text
    assert "Birinchi bo'lim: 0:1" in text
    assert "<b>Chelsea &lt;FC&gt;</b> · 4-2-3-1" in text
    assert "Murabbiy: Enzo Maresca" in text
    assert "1 Robert Sanchez <i>(Goalkeeper)</i>" in text
    assert "— Filip Jorgensen" in text
    assert texts.MATCH_LINEUPS_MISSING not in text


def test_sheet_without_lineups_says_so() -> None:
    bare = sheet(home=SideLineup(team="A"), away=SideLineup(team="B"), home_ht_goals=None)
    text = format_match_sheet(bare, TASHKENT)
    assert texts.MATCH_LINEUPS_MISSING in text
    assert "Birinchi bo'lim" not in text


# --------------------------------------------------------------------------- flow
@dataclass
class StubMatches:
    """A MatchesService recording the days it was asked for."""

    items: tuple[MatchItem, ...] = (item(1), item(2))
    error: Exception | None = None
    days: list[tuple[date, date]] = field(default_factory=list)

    async def list_matches(
        self,
        user_id: int,  # noqa: ARG002 - protocol signature
        day_from: date,
        day_to: date,
    ) -> tuple[MatchItem, ...]:
        self.days.append((day_from, day_to))
        if self.error is not None:
            raise self.error
        return self.items

    async def get_match(self, user_id: int, match_id: int) -> MatchSheet:  # noqa: ARG002 - protocol signature
        if match_id != 1:
            raise NotFoundError("match", match_id)
        return sheet()


@pytest.fixture
def stub() -> StubMatches:
    return StubMatches()


@pytest.fixture
def harness(bot: Bot, session: RecordingSession, stub: StubMatches) -> BotHarness:
    services = replace(create_fake_services(clock=fixed_clock), matches=stub)
    return BotHarness(bot, session, services)


async def test_menu_opens_today_and_buttons_move_between_days(
    harness: BotHarness, stub: StubMatches
) -> None:
    await harness.send(texts.MENU_MATCHES)
    assert stub.days == [(TODAY, TODAY)]
    assert "21.10.2026" in harness.last_text
    await harness.press(texts.BTN_MATCHES_TOMORROW)
    await harness.press(texts.BTN_MATCHES_NEXT_DAY)
    await harness.press(texts.BTN_MATCHES_PREV_DAY)
    assert stub.days[1:] == [
        (TODAY + timedelta(days=1),) * 2,
        (TODAY + timedelta(days=2),) * 2,
        (TODAY + timedelta(days=1),) * 2,
    ]


async def test_typed_date_and_bad_date(harness: BotHarness, stub: StubMatches) -> None:
    await harness.send("/oyinlar")
    await harness.press(texts.BTN_MATCHES_PICK_DATE)
    assert texts.MATCHES_ASK_DATE in harness.last_text
    await harness.send("bugun emas")
    assert "bugun emas" in harness.last_text  # the parse error quotes the input
    await harness.send("30.10.2026")
    assert stub.days[-1] == (date(2026, 10, 30), date(2026, 10, 30))
    assert "30.10.2026" in harness.last_text


async def test_number_opens_sheet_and_back_returns(harness: BotHarness, stub: StubMatches) -> None:
    await harness.send(texts.MENU_MATCHES)
    await harness.press("1")
    assert "Murabbiy: Enzo Maresca" in harness.last_text
    await harness.press(texts.BTN_MATCHES_BACK)
    assert "O'yinlar" in harness.last_text
    assert len(stub.days) == 2


async def test_provider_down_shows_a_message(harness: BotHarness, stub: StubMatches) -> None:
    stub.error = ServiceUnavailableError("football-data", "HTTP 503")
    await harness.send(texts.MENU_MATCHES)
    assert harness.last_text  # the error middleware answered instead of crashing
    assert "Chelsea" not in harness.last_text


async def test_forged_day_falls_back_to_today(harness: BotHarness, stub: StubMatches) -> None:
    await harness.send(texts.MENU_MATCHES)
    message_id, _ = harness.find_button(texts.BTN_MATCHES_TODAY)
    await harness.press_data("mt:day:0:0:0", message_id)
    assert stub.days[-1] == (TODAY, TODAY)
