"""Analytics wording: match buttons, the five sections (AI or facts) and the model block."""

from dataclasses import replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.bot import texts
from app.bot.analytics_ui import (
    PAGE_SIZE,
    analysis_keyboard,
    day_keyboard,
    form_text,
    format_analysis,
    format_day,
    format_league,
    game_label,
    league_keyboard,
    squads_fact,
    summary_fact,
    tactics_fact,
    venue_fact,
)
from app.bot.callbacks import GameCb
from app.services._fake_matches import SAMPLE_FACTS, SAMPLE_OUTLOOK, SAMPLE_SECTIONS
from app.services.interfaces import (
    AnalysisSections,
    LineupPlayer,
    MatchAnalysis,
    MatchItem,
    MatchSheet,
    MatchState,
    Meeting,
    SideLineup,
)
from tests.e2e.bot_client import telegram_html_problems

TASHKENT = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 31, 10, tzinfo=UTC)
ITEM = MatchItem(
    match_id=7,
    kickoff=datetime(2026, 10, 31, 16, 30, tzinfo=UTC),
    competition="Premier League",
    competition_code="PL",
    home="Arsenal <FC>",
    away="Chelsea FC",
    state=MatchState.SCHEDULED,
)
SHEET = MatchSheet(
    match=ITEM,
    home_ht_goals=None,
    away_ht_goals=None,
    home=SideLineup(team=ITEM.home),
    away=SideLineup(team=ITEM.away),
)
LINEUPS = replace(
    SHEET,
    home=SideLineup(
        team=ITEM.home,
        formation="4-3-3",
        coach="Mikel Arteta",
        starters=(LineupPlayer(name="David Raya"),),
    ),
    away=SideLineup(team=ITEM.away, starters=(LineupPlayer(name="Robert Sanchez"),)),
)


def analysis(**changes: object) -> MatchAnalysis:
    base = MatchAnalysis(
        sheet=SHEET,
        facts=SAMPLE_FACTS,
        outlook=SAMPLE_OUTLOOK,
        sections=SAMPLE_SECTIONS,
        generated_at=NOW,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_game_buttons_show_flag_teams_and_time_or_score() -> None:
    assert game_label(ITEM, TASHKENT) == "🏴󠁧󠁢󠁥󠁮󠁧󠁿 Arsenal <FC> vs Chelsea FC | 21:30"
    assert game_label(ITEM, TASHKENT, with_date=True).endswith("| 31.10 21:30")
    played = replace(ITEM, home_goals=2, away_goals=1, competition_code="XX", home=None)
    assert game_label(played, TASHKENT) == "⚽ ? vs Chelsea FC | 2:1"
    long = replace(ITEM, home="Wolverhampton Wanderers Football Club")
    assert "Wolverhampton Wandere… vs" in game_label(long, TASHKENT)


def test_lists_page_and_lead_to_the_analysis() -> None:
    items = [replace(ITEM, match_id=n) for n in range(PAGE_SIZE + 1)]
    day = date(2026, 10, 31)
    markup = day_keyboard(items, day, 1, TASHKENT)
    [only], pages = markup.inline_keyboard
    assert GameCb.unpack(only.callback_data or "") == GameCb(
        action="an", d=day.toordinal(), p=1, i=PAGE_SIZE
    )
    assert [b.text for b in pages] == [texts.BTN_PAGE_PREV, "2/2"]
    league = league_keyboard(items[:1], "PL", 0, TASHKENT)
    assert league.inline_keyboard[-1][0].text == texts.BTN_BACK_TO_LEAGUES
    assert texts.DAY_EMPTY in format_day(day, 0)
    assert "Premier League" in format_league("PL", 3)
    assert texts.LEAGUE_EMPTY.format(days=7) in format_league("PL", 0)
    assert "XX" in format_league("XX", 0)


def test_analysis_has_only_refresh_and_back() -> None:
    from_day = GameCb(action="an", d=10, p=2, i=7)
    refresh, back = analysis_keyboard(from_day).inline_keyboard
    assert [b.text for b in (*refresh, *back)] == [texts.BTN_REFRESH, texts.BTN_BACK_TO_LIST]
    assert GameCb.unpack(refresh[0].callback_data or "").action == "re"
    assert GameCb.unpack(back[0].callback_data or "") == GameCb(action="day", d=10, p=2)
    from_league = GameCb(action="re", c="PD", p=1, i=7)
    [[_], [back]] = analysis_keyboard(from_league).inline_keyboard
    assert GameCb.unpack(back.callback_data or "") == GameCb(action="league", c="PD", p=1)


def test_the_ai_analysis_layout() -> None:
    text = format_analysis(analysis(), TASHKENT)
    assert text.startswith("⚽ <b>Arsenal &lt;FC&gt; — Chelsea FC</b>\n🏆 Premier League | ⏰")
    for header in texts.ANALYSIS_SECTIONS.values():
        assert header in text
    assert SAMPLE_SECTIONS.summary is not None
    assert SAMPLE_SECTIONS.summary in text
    assert "Arsenal &lt;FC&gt;: 52% · Durang: 26% · Chelsea FC: 22%" in text
    assert "Kutilayotgan gollar: 1.7 — 1.1" in text
    assert texts.ANALYSIS_SOURCE_AI in text
    assert texts.ANALYSIS_DISCLAIMER in text
    for banned in ("EV", "Tavsiya:", "koeffitsient"):
        assert banned not in text
    assert telegram_html_problems(text) == []


def test_without_ai_every_section_is_worded_from_facts() -> None:
    text = format_analysis(analysis(sections=None), TASHKENT)
    assert texts.ANALYSIS_SOURCE_FACTS in text
    assert "• Arsenal &lt;FC&gt;: GGDGM — o'rtacha 2.1 gol uradi, 0.9 gol o'tkazadi" in text
    assert "uyda so'nggi 5 o'yinda: 4 g'alaba, 1 durang, 0 mag'lubiyat (gollar 11:4)" in text
    assert "2-o'rin (21 ochko)" in text
    assert texts.FACT_SQUADS_PENDING in text
    assert texts.FACT_TACTICS["balanced"] in text
    assert "Arsenal &lt;FC&gt; ustunroq ko'rinadi. Ko'p gollar kutilmoqda." in text
    assert telegram_html_problems(text) == []


def test_without_history_the_analysis_still_has_every_section() -> None:
    text = format_analysis(
        analysis(facts=None, outlook=None, sections=None, sheet=LINEUPS), TASHKENT
    )
    assert text.count(texts.FACT_NO_HISTORY) == 2
    assert "• Arsenal &lt;FC&gt; (4-3-3): David Raya" in text
    assert "Murabbiy: Mikel Arteta" in text
    assert texts.FACT_TACTICS["unknown"] in text
    assert texts.FACT_SUMMARY_UNKNOWN in text
    assert "Model ko'rsatkichlari" not in text


def test_ai_squads_text_is_followed_by_the_announced_line_ups() -> None:
    sections = AnalysisSections(squads="Ikkala jamoa asosiy tarkibda.")
    text = format_analysis(analysis(sheet=LINEUPS, sections=sections), TASHKENT)
    assert "Ikkala jamoa asosiy tarkibda.\n• Arsenal &lt;FC&gt; (4-3-3): David Raya" in text


def test_live_or_played_match_shows_its_state() -> None:
    live = replace(SHEET, match=replace(ITEM, state=MatchState.LIVE))
    assert "📍 " in format_analysis(analysis(sheet=live), TASHKENT)


@pytest.mark.parametrize(
    ("expected", "verdict"),
    [((2.0, 1.2), "open"), ((1.0, 0.8), "cagey"), ((1.4, 1.1), "balanced")],
)
def test_tactics_follow_the_expected_goals(expected: tuple[float, float], verdict: str) -> None:
    outlook = replace(
        SAMPLE_OUTLOOK, expected_home_goals=expected[0], expected_away_goals=expected[1]
    )
    assert tactics_fact(outlook, None) == texts.FACT_TACTICS[verdict]


def test_tactics_fall_back_to_goal_averages() -> None:
    # (2.1 + 1.7) / 2 + (1.1 + 0.9) / 2 = 2.9 goals together
    assert tactics_fact(None, SAMPLE_FACTS) == texts.FACT_TACTICS["balanced"]


@pytest.mark.parametrize(
    ("home", "away", "over", "expected"),
    [
        (0.2, 0.55, 0.40, "B ustunroq ko'rinadi. Kam gollar kutilmoqda."),
        (0.35, 0.33, 0.50, "Kuchlar teng ko'rinadi. O'rtacha miqdorda gol kutilmoqda."),
    ],
)
def test_summary_names_the_stronger_side_and_the_goals(
    home: float, away: float, over: float, expected: str
) -> None:
    outlook = replace(SAMPLE_OUTLOOK, home_win=home, away_win=away, over_2_5=over)
    assert summary_fact(outlook, "A", "B") == expected


def test_meetings_are_listed_and_escaped() -> None:
    facts = replace(
        SAMPLE_FACTS,
        meetings=(Meeting(day=date(2026, 3, 1), home="A&B", away="C", home_goals=2, away_goals=0),),
    )
    assert "O'zaro so'nggi o'yinlar: A&amp;B 2:0 C (01.03.2026)" in venue_fact(facts, "A", "C")
    no_table = replace(SAMPLE_FACTS, away=replace(SAMPLE_FACTS.away, position=None))
    assert "Jadvalda" not in venue_fact(no_table, "A", "C")


def test_squads_and_form_helpers() -> None:
    assert squads_fact(SHEET.home, LINEUPS.away) == texts.FACT_SQUADS_PENDING  # one side only
    assert form_text("WDL") == "GDM"
    assert form_text("") == "—"
