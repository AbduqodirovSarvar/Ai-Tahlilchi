"""⚽ O'yinlar: pure formatters and the flow through the real dispatcher (stub match service)."""

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot

from app.bot import texts
from app.bot.forecast_ui import format_day_forecast, format_match_forecast
from app.bot.formatters import kickoff_stamp
from app.bot.matches_ui import (
    MATCHES_PAGE_SIZE,
    clamp_page,
    format_match_sheet,
    format_matches,
    ordered_for_display,
    page_count,
)
from app.bot.picks_ui import format_top_picks, pick_label, window_text
from app.domain import Market, Selection
from app.services._fake_matches import sample_forecast
from app.services.errors import NotFoundError, ServiceUnavailableError
from app.services.fake import create_fake_services
from app.services.interfaces import (
    CardChance,
    CardOutlook,
    CardScope,
    ConfidenceLevel,
    ExpertMatch,
    ExpertPick,
    ExpertReport,
    ForecastLine,
    ForecastStatus,
    LineupPlayer,
    MatchForecast,
    MatchItem,
    MatchSheet,
    MatchState,
    OutcomeChance,
    SideLineup,
    TeamChance,
    TeamPick,
    TopPick,
)
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

    async def forecast_day(self, user_id: int, day: date) -> tuple[ForecastLine, ...]:  # noqa: ARG002 - protocol signature
        self.days.append((day, day))
        return tuple(
            ForecastLine(match=i, forecast=sample_forecast(i.match_id)) for i in self.items
        )

    async def top_picks(self, user_id: int, day_from: date, day_to: date) -> tuple[TopPick, ...]:  # noqa: ARG002 - protocol signature
        self.days.append((day_from, day_to))
        return (
            TopPick(
                match=item(1),
                team=TeamChance(pick=TeamPick.HOME_TO_SCORE, probability=0.81),
                confidence=ConfidenceLevel.MEDIUM,
            ),
        )

    async def expert_picks(self, user_id: int, day_from: date, day_to: date) -> ExpertReport:  # noqa: ARG002 - protocol signature
        self.days.append((day_from, day_to))
        pick = ExpertPick(
            pick=TopPick(
                match=item(1), team=TeamChance(pick=TeamPick.HOME_TO_SCORE, probability=0.81)
            ),
            reason="Uyda kuchli <b>.",
        )
        return ExpertReport(
            matches=(ExpertMatch(match=item(1), picks=(pick,)),), without_forecast=1
        )

    async def forecast_match(self, user_id: int, match_id: int) -> ForecastLine:  # noqa: ARG002 - protocol signature
        return ForecastLine(match=sheet().match, forecast=sample_forecast(match_id))


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


# --------------------------------------------------------------------------- forecasts
def test_day_forecast_shows_result_chances_favourite_and_skips() -> None:
    lines = (
        ForecastLine(match=item(1), forecast=sample_forecast(1)),
        ForecastLine(
            match=item(2, home=None),
            forecast=MatchForecast(match_id=2, status=ForecastStatus.THIN_HISTORY),
        ),
    )
    text = format_day_forecast(date(2026, 10, 30), lines, 0, TASHKENT)
    assert telegram_html_problems(text) == []
    assert "30.10.2026 21:00" in text
    assert "1 52% · X 26% · 2 22% → <b>uy g'alabasi</b> · 🛡 O'rta" in text
    assert "Total 2.5+: 55% · Ikkala jamoa gol uradi: 51%" in text
    assert texts.FORECAST_REASONS["thin_history"] in text
    assert "Chelsea &lt;FC&gt;" in text
    assert texts.MATCHES_EMPTY in format_day_forecast(date(2026, 10, 30), (), 0, TASHKENT)


def test_match_forecast_card_lists_fair_odds_and_scores() -> None:
    text = format_match_forecast(ForecastLine(match=item(1), forecast=sample_forecast(1)), TASHKENT)
    assert telegram_html_problems(text) == []
    assert "Uy g'alabasi (1): <b>52%</b> · adolatli koeff. 1.92" in text
    assert "Total 2.5: ko'p 55% (1.82) · kam 45% (2.22)" in text
    assert "ha 51% (1.96)" in text
    assert "1:1 12% · 1:0 10%" in text
    assert "Ishonch: <b>O'rta</b>" in text
    assert texts.FORECAST_NOTE in text
    unavailable = MatchForecast(match_id=1, status=ForecastStatus.UNAVAILABLE)
    bare = format_match_forecast(ForecastLine(match=item(1), forecast=unavailable), TASHKENT)
    assert texts.FORECAST_REASONS["unavailable"] in bare
    assert "Kim yutadi" not in bare


def test_forecast_card_without_optional_markets() -> None:
    result = Market.match_result()
    only_result = MatchForecast(
        match_id=1,
        status=ForecastStatus.READY,
        chances=tuple(c for c in sample_forecast(1).chances if c.market == result),
    )
    text = format_match_forecast(ForecastLine(match=item(1), forecast=only_result), TASHKENT)
    assert "Kim yutadi" in text
    assert "Gollar soni" not in text
    assert "Ishonch" not in text
    assert only_result.chance(result, Selection.HOME) is not None


async def test_forecast_buttons_from_the_list_and_the_sheet(harness: BotHarness) -> None:
    await harness.send(texts.MENU_MATCHES)
    await harness.press(texts.BTN_FORECAST_DAY)
    assert "📊 <b>Prognoz · 21.10.2026" in harness.last_text
    await harness.press(texts.BTN_FORECAST_BACK)
    assert "⚽ <b>O'yinlar" in harness.last_text
    await harness.press("1")
    await harness.press(texts.BTN_FORECAST_MATCH)
    assert "📊 <b>Prognoz: Chelsea" in harness.last_text
    await harness.press(texts.BTN_FORECAST_TO_SHEET)
    assert "Murabbiy: Enzo Maresca" in harness.last_text


# --------------------------------------------------------------------------- top picks
def _outcome(market: Market, selection: Selection, probability: float = 0.6) -> TopPick:
    chance = OutcomeChance(market=market, selection=selection, probability=probability)
    return TopPick(match=item(1), outcome=chance)


@pytest.mark.parametrize(
    ("pick", "label"),
    [
        (_outcome(Market.match_result(), Selection.HOME), "1 — Chelsea &lt;FC&gt; yutadi"),
        (_outcome(Market.match_result(), Selection.DRAW), "X — durang"),
        (_outcome(Market.match_result(), Selection.AWAY), "2 — Liverpool FC yutadi"),
        (
            _outcome(Market.double_chance(), Selection.HOME_OR_DRAW),
            "1X — Chelsea &lt;FC&gt; yutqazmaydi",
        ),
        (_outcome(Market.double_chance(), Selection.DRAW_OR_AWAY), "X2 — Liverpool FC yutqazmaydi"),
        (_outcome(Market.double_chance(), Selection.HOME_OR_AWAY), "12 — durang bo'lmaydi"),
        (_outcome(Market.totals(2.5), Selection.UNDER), "Total 2.5 dan kam"),
        (_outcome(Market.totals(1.5), Selection.OVER), "Total 1.5 dan ko'p"),
        (_outcome(Market.btts(), Selection.YES), "Ikkala jamoa gol uradi"),
        (_outcome(Market.btts(), Selection.NO), "Ikkala jamoadan kamida biri gol urmaydi"),
        (
            TopPick(match=item(1), team=TeamChance(pick=TeamPick.AWAY_TO_SCORE, probability=0.7)),
            "Liverpool FC gol uradi",
        ),
    ],
)
def test_pick_labels(pick: TopPick, label: str) -> None:
    assert pick_label(pick) == label


def test_top_picks_text_and_empty_state() -> None:
    picks = (
        TopPick(
            match=item(1),
            team=TeamChance(pick=TeamPick.HOME_TO_SCORE, probability=0.8),
            confidence=ConfidenceLevel.HIGH,
        ),
        _outcome(Market.totals(3.5), Selection.UNDER, 0.75),
    )
    day = date(2026, 10, 30)
    text = format_top_picks(picks, day_from=day, day_to=day, min_probability=0.55, zone=TASHKENT)
    assert telegram_html_problems(text) == []
    assert "1. <b>80%</b> · Chelsea &lt;FC&gt; gol uradi · adolatli koeff. 1.25 · 🛡 Yuqori" in text
    assert "2. <b>75%</b> · Total 3.5 dan kam · adolatli koeff. 1.33" in text
    assert "30.10.2026 21:00 · Chelsea &lt;FC&gt; — Liverpool FC · Premier League" in text
    assert "55%" in text
    empty = format_top_picks((), day_from=day, day_to=day, min_probability=0.9, zone=TASHKENT)
    assert texts.TOP_PICKS_EMPTY.format(min_probability="90%") in empty
    assert window_text(day, date(2026, 11, 1)) == "30.10.2026 — 01.11.2026"


async def test_top_picks_button_from_the_day_list(harness: BotHarness, stub: StubMatches) -> None:
    await harness.send(texts.MENU_MATCHES)
    await harness.press(texts.BTN_TOP_PICKS)
    assert "🎯 <b>Eng ehtimolli tanlovlar · 21.10.2026" in harness.last_text
    assert "81%" in harness.last_text
    assert stub.days[-1] == (TODAY, TODAY)
    await harness.press(texts.BTN_FORECAST_BACK)
    assert "⚽ <b>O'yinlar" in harness.last_text


def test_match_forecast_shows_cards_and_the_referee() -> None:
    text = format_match_forecast(ForecastLine(match=item(1), forecast=sample_forecast(1)), TASHKENT)
    assert "🟨 <b>Sariq kartochkalar</b>: kutilgan ~4.1 (uy 1.8 · mehmon 2.3)" in text
    assert "Hakam: Sample Referee — qattiq" in text
    assert "• Jami 3.5: ko'p 58% (1.72) · kam 42% (2.38)" in text
    assert "• Liverpool FC 1.5: ko'p 67% · kam 33%" in text
    assert telegram_html_problems(text) == []


@pytest.mark.parametrize(
    ("cards", "expected"),
    [
        (CardOutlook(expected_home=1.0, expected_away=1.0), "Hakam hali e'lon qilinmagan"),
        (
            CardOutlook(expected_home=1.0, expected_away=1.0, referee="X <Y>"),
            "X &lt;Y&gt; (tarixda yo'q",
        ),
        (
            CardOutlook(
                expected_home=1.0,
                expected_away=1.0,
                referee="R",
                referee_known=True,
                strictness=0.8,
            ),
            "yumshoq",
        ),
        (
            CardOutlook(expected_home=1.0, expected_away=1.0, referee="R", referee_known=True),
            "o'rtacha",
        ),
    ],
)
def test_referee_wording(cards: CardOutlook, expected: str) -> None:
    forecast = replace(sample_forecast(1), cards=cards)
    assert expected in format_match_forecast(
        ForecastLine(match=item(1), forecast=forecast), TASHKENT
    )


def test_no_card_history_is_said() -> None:
    forecast = replace(sample_forecast(1), cards=None)
    text = format_match_forecast(ForecastLine(match=item(1), forecast=forecast), TASHKENT)
    assert texts.CARDS_NONE in text


def test_day_forecast_mentions_cards() -> None:
    text = format_day_forecast(
        date(2026, 10, 30), (ForecastLine(match=item(1), forecast=sample_forecast(1)),), 0, TASHKENT
    )
    assert "🟨 ~4.1 sariq kartochka · hakam Sample Referee" in text


@pytest.mark.parametrize(
    ("card", "label"),
    [
        (
            CardChance(scope=CardScope.TOTAL, line=4.5, over=True, probability=0.6),
            "Sariq kartochkalar jami 4.5 dan ko'p",
        ),
        (
            CardChance(scope=CardScope.TOTAL, line=3.5, over=False, probability=0.6),
            "Sariq kartochkalar jami 3.5 dan kam",
        ),
        (
            CardChance(scope=CardScope.HOME, line=1.5, over=True, probability=0.6),
            "Chelsea &lt;FC&gt; 1.5 dan ko'p sariq kartochka oladi",
        ),
        (
            CardChance(scope=CardScope.AWAY, line=2.5, over=False, probability=0.6),
            "Liverpool FC 2.5 dan kam sariq kartochka oladi",
        ),
    ],
)
def test_card_pick_labels(card: CardChance, label: str) -> None:
    assert pick_label(TopPick(match=item(1), card=card)) == label


async def test_expert_button_sends_the_report(harness: BotHarness, stub: StubMatches) -> None:
    await harness.send(texts.MENU_MATCHES)
    await harness.press(texts.BTN_EXPERT)
    text = harness.last_text
    assert "🧠 <b>Ekspert tahlili · 21.10.2026</b> — 1 ta o'yin" in text
    assert "📊 <b>O'tish ehtimoli:</b> 81% (🟢 Ishonchli)" in text
    assert "Uyda kuchli &lt;b&gt;." in text
    assert texts.EXPERT_WITHOUT_FORECAST.format(count=1) in text
    assert stub.days[-1] == (TODAY, TODAY)
    await harness.press(texts.BTN_FORECAST_BACK)
    assert "⚽ <b>O'yinlar" in harness.last_text
