"""Pure tests of the 🗄 Baza texts and keyboards: local time, escaping, empty states, limits."""

from dataclasses import replace
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import InlineKeyboardMarkup
from hypothesis import given
from hypothesis import strategies as st

from app.bot import texts
from app.bot.browse_card_ui import (
    MAX_CARD_ODDS_PER_BOOKMAKER,
    card_keyboard,
    format_fixture_card,
    format_odds_history,
    latest_quotes,
    odds_keyboard,
)
from app.bot.browse_nav import (
    SCOPE_ALL,
    SCOPE_LEAGUE,
    SCREEN_CODES,
    SCREEN_FIXTURE,
    SCREEN_FIXTURES,
    SCREEN_LEAGUES,
    BrowseNav,
)
from app.bot.browse_ui import (
    calendar_bounds,
    calendar_keyboard,
    fixtures_keyboard,
    format_fixtures,
    format_leagues,
    format_overview,
    format_teams,
    has_data,
    initial_month,
    leagues_keyboard,
    overview_keyboard,
    shift_month,
    size_label,
    teams_keyboard,
)
from app.bot.handlers.browse import apply_action
from app.bot.paging import paginate
from app.domain import FixtureStatus, MarketType, Selection
from app.services.interfaces import (
    BookmakerOdds,
    StoredFixturePage,
    StoredOddsPage,
    StoredTeam,
    StoredTeamPage,
)
from tests.bot.browse_samples import (
    LATE_KICKOFF,
    NOW,
    make_detail,
    make_overview,
    make_quote,
    make_stored_fixture,
    make_stored_league,
)
from tests.e2e.bot_client import telegram_html_problems

TASHKENT = ZoneInfo("Asia/Tashkent")
CALLBACK_LIMIT = 64


def callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def fixtures_page(*items: object, total: int | None = None, page: int = 0, pages: int = 1):
    return StoredFixturePage(
        items=tuple(items),  # type: ignore[arg-type]
        page=page,
        page_count=pages,
        total=len(items) if total is None else total,
    )


# --------------------------------------------------------------------------- overview
def test_size_label_uses_binary_units() -> None:
    assert size_label(0) == "0 B"
    assert size_label(1023) == "1023 B"
    assert size_label(1024) == "1.0 KB"
    assert size_label(13_000_000) == "12.4 MB"
    assert size_label(5 * 1024**4) == "5120.0 GB"
    assert size_label(-5) == "0 B"


def test_overview_shows_freshness_size_span_and_local_times() -> None:
    text = format_overview(make_overview(), TASHKENT, NOW)

    assert "21.10 06:00" not in text  # tables are written at NOW = 11:00 local
    assert "21.10 11:00" in text
    assert "3 soat oldin" in text
    assert "12.4 MB" in text
    assert "20.10.2026 — 31.10.2026" in text or "20.10.2026 — 01.11.2026" in text
    assert "Asia/Tashkent" in text
    assert "(faqat sizniki)" in text
    assert telegram_html_problems(text) == []


def test_overview_warns_when_stale_and_when_never_refreshed() -> None:
    stale = format_overview(make_overview(data_stale=True), TASHKENT, NOW)
    never = format_overview(make_overview(last_refresh_at=None), TASHKENT, NOW)

    assert texts.DB_STALE_WARNING in stale
    assert texts.DB_NEVER_REFRESHED in never


def test_empty_database_says_what_to_do_and_hides_the_sections() -> None:
    overview = make_overview(
        tables=(),
        earliest_kickoff=None,
        latest_kickoff=None,
        last_refresh_at=None,
        upcoming_fixtures=0,
    )

    text = format_overview(overview, TASHKENT, NOW)
    markup = overview_keyboard(with_data=has_data(overview))

    assert "Baza bo'sh" in text
    assert "Bazani yangilash" in text
    assert labels(markup) == [texts.BTN_DB_REFRESH]


def test_a_database_with_only_own_rows_counts_as_empty() -> None:
    from app.services.interfaces import StoredTable  # noqa: PLC0415

    overview = make_overview(tables=(StoredTable(name="signals", rows=9, own=True),))

    assert not has_data(overview)


def test_unknown_table_names_are_escaped() -> None:
    from app.services.interfaces import StoredTable  # noqa: PLC0415

    overview = make_overview(tables=(StoredTable(name="a<b>&c", rows=1),))

    text = format_overview(overview, TASHKENT, NOW)

    assert "a&lt;b&gt;&amp;c" in text
    assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- leagues
def test_leagues_page_lists_numbered_entries_with_counts_and_span() -> None:
    leagues = [make_stored_league(f"lg{i}", name=f"Liga {i}") for i in range(12)]
    page = paginate(leagues, 0, 10)
    nav = BrowseNav(token=3, screen=SCREEN_LEAGUES)

    text = format_leagues(page, len(leagues), TASHKENT)
    markup = leagues_keyboard(page, nav)

    assert "bazada 12 ta" in text
    assert "10. <b>Liga 9</b> (England)" in text
    assert "12 ta o'yin · kelgusi 7 · 20.10 — 25.10" in text
    assert "Liga 10" not in text
    assert [b for b in labels(markup) if b.isdigit()] == [str(i) for i in range(1, 11)]
    assert "1/2" in labels(markup)
    assert all(len(data.encode()) <= CALLBACK_LIMIT for data in callbacks(markup))


def test_leagues_empty_state_and_html_escape() -> None:
    assert format_leagues(paginate([], 0, 10), 0, TASHKENT) == texts.DB_LEAGUES_EMPTY

    page = paginate([make_stored_league(name="<script>&", country="A&B")], 0, 10)
    text = format_leagues(page, 1, TASHKENT)

    assert "&lt;script&gt;&amp;" in text
    assert "(A&amp;B)" in text
    assert telegram_html_problems(text) == []


def test_league_without_fixtures_has_no_span() -> None:
    page = paginate([make_stored_league(first_kickoff=None, last_kickoff=None, fixtures=0)], 0, 10)

    assert " — " not in format_leagues(page, 1, TASHKENT).split("\n", 2)[2]


# --------------------------------------------------------------------------- teams
def test_teams_page_query_empty_and_no_match() -> None:
    team = StoredTeam(
        team_id="tm1",
        sport=make_stored_league().sport,
        name="Man <U>",
        country=None,
        source="x",
        fixtures=4,
    )
    page = StoredTeamPage(items=(team,), page=0, page_count=1, total=1)

    text = format_teams(page, "man")
    empty = StoredTeamPage(items=(), page=0, page_count=0, total=0)

    assert "Qidiruv: <b>man</b>" in text
    assert "<b>Man &lt;U&gt;</b> · ⚽ 4 ta o'yin" in text
    assert format_teams(empty, "") == texts.DB_TEAMS_EMPTY
    assert "'&lt;x&gt;' bo'yicha jamoa topilmadi" in format_teams(empty, "<x>")


def test_teams_keyboard_has_search_and_home() -> None:
    page = StoredTeamPage(items=(), page=0, page_count=0, total=0)

    markup = teams_keyboard(page, BrowseNav(token=1, screen="teams"))

    assert labels(markup) == [texts.BTN_DB_SEARCH, texts.BTN_DB_HOME]


# --------------------------------------------------------------------------- fixtures
def test_utc_evening_kickoff_is_the_next_local_day() -> None:
    fixture = make_stored_fixture()  # 20:00 UTC on 21.10
    nav = BrowseNav(token=1, screen=SCREEN_FIXTURES, scope=SCOPE_ALL)

    text = format_fixtures(fixtures_page(fixture), nav, TASHKENT)

    assert "22.10, Payshanba" in text
    assert "<b>01:00</b> Arsenal — Chelsea" in text
    assert "21.10" not in text.split("Barcha kunlar")[1]


def test_fixtures_group_under_day_headers_and_show_state_score_and_league() -> None:
    finished = make_stored_fixture(
        "fx2",
        kickoff=LATE_KICKOFF - timedelta(days=1),
        status=FixtureStatus.FINISHED,
        home_score=2,
        away_score=1,
        has_odds=False,
        league="La <Liga>",
    )
    live = make_stored_fixture("fx3", status=FixtureStatus.LIVE, home_score=0, away_score=0)
    nav = BrowseNav(token=1, screen=SCREEN_FIXTURES, scope=SCOPE_ALL)

    text = format_fixtures(fixtures_page(finished, make_stored_fixture(), live), nav, TASHKENT)

    assert text.count("📅 <b>") == 2
    assert "La &lt;Liga&gt; · ✅ tugagan 2:1" in text
    assert "🔴 jonli 0:0 · 💱" in text
    assert "⏳ kutilmoqda · 💱" in text
    assert telegram_html_problems(text) == []


def test_fixtures_empty_state_depends_on_the_day_filter() -> None:
    nav = BrowseNav(token=1, screen=SCREEN_FIXTURES, scope=SCOPE_ALL)
    empty = StoredFixturePage(items=(), page=0, page_count=0, total=0)

    assert texts.DB_FIXTURES_EMPTY in format_fixtures(empty, nav, TASHKENT)
    filtered = replace(nav, day=date(2026, 10, 22).toordinal())
    assert texts.DB_FIXTURES_EMPTY_FILTER in format_fixtures(empty, filtered, TASHKENT)
    assert "Kun: <b>22.10.2026</b>" in format_fixtures(empty, filtered, TASHKENT)


def test_league_scope_title_is_escaped() -> None:
    nav = BrowseNav(
        token=1, screen=SCREEN_FIXTURES, scope=SCOPE_LEAGUE, scope_id="lg1", scope_name="A&B"
    )

    text = format_fixtures(fixtures_page(make_stored_fixture()), nav, TASHKENT)

    assert "<b>A&amp;B</b>" in text


def test_fixtures_keyboard_marks_the_active_day_and_pages() -> None:
    today = date(2026, 10, 21)
    nav = BrowseNav(token=7, screen=SCREEN_FIXTURES, scope=SCOPE_ALL, day=today.toordinal())
    page = fixtures_page(*(make_stored_fixture(f"fx{i}") for i in range(10)), total=25, pages=3)

    markup = fixtures_keyboard(page, nav, today)

    assert f"● {texts.BTN_TODAY}" in labels(markup)
    assert texts.BTN_TOMORROW in labels(markup)
    assert "1/3" in labels(markup)
    assert texts.BTN_DB_HOME in labels(markup)  # scope "all" goes back to the overview
    assert all(len(data.encode()) <= CALLBACK_LIMIT for data in callbacks(markup))
    assert all(data.startswith("db:") for data in callbacks(markup))


def test_last_page_has_no_next_button_and_first_no_previous() -> None:
    nav = BrowseNav(token=1, screen=SCREEN_FIXTURES, scope=SCOPE_ALL)
    last = fixtures_page(make_stored_fixture(), total=21, page=2, pages=3)
    first = fixtures_page(make_stored_fixture(), total=21, page=0, pages=3)

    assert texts.BTN_NEXT_PAGE not in labels(fixtures_keyboard(last, nav, date(2026, 10, 21)))
    assert texts.BTN_PREV_PAGE not in labels(fixtures_keyboard(first, nav, date(2026, 10, 21)))


# --------------------------------------------------------------------------- calendar
def test_calendar_is_limited_to_the_stored_span() -> None:
    overview = make_overview()  # 20.10 .. 31.10 (UTC evening: 01.11 local)
    today = date(2026, 10, 21)
    bounds = calendar_bounds(overview, TASHKENT, today)
    nav = BrowseNav(token=2, screen="calendar")

    markup = calendar_keyboard(nav, 202610, bounds, today)
    day_buttons = [d for d in callbacks(markup) if d.startswith("db:day:") and ":0:" not in d]

    assert bounds == (date(2026, 10, 20), date(2026, 11, 1))
    days = sorted(int(d.split(":")[2]) for d in day_buttons)
    assert days[0] == date(2026, 10, 20).toordinal()
    assert days[-1] == date(2026, 10, 31).toordinal()
    assert all(len(d.encode()) <= CALLBACK_LIMIT for d in callbacks(markup))


def test_calendar_month_navigation_stops_at_the_span() -> None:
    today = date(2026, 10, 21)
    bounds = (date(2026, 10, 20), date(2026, 11, 1))
    nav = BrowseNav(token=2, screen="calendar")

    october = calendar_keyboard(nav, 202610, bounds, today)
    november = calendar_keyboard(nav, 202611, bounds, today)

    assert texts.BTN_PREV_PAGE not in labels(october)
    assert texts.BTN_NEXT_PAGE in labels(october)
    assert texts.BTN_NEXT_PAGE not in labels(november)
    assert texts.BTN_PREV_PAGE in labels(november)


@pytest.mark.parametrize(
    ("key", "delta", "expected"),
    [(202610, 1, 202611), (202612, 1, 202701), (202601, -1, 202512), (202610, 0, 202610)],
)
def test_shift_month(key: int, delta: int, expected: int) -> None:
    assert shift_month(key, delta) == expected


def test_initial_month_follows_the_chosen_day_clamped_into_the_span() -> None:
    bounds = (date(2026, 10, 20), date(2026, 11, 1))
    today = date(2026, 12, 5)

    assert initial_month(BrowseNav(), bounds, today) == 202611
    assert initial_month(BrowseNav(day=date(2026, 10, 25).toordinal()), bounds, today) == 202610


def test_calendar_bounds_for_an_empty_span_is_today() -> None:
    overview = make_overview(earliest_kickoff=None, latest_kickoff=None)

    assert calendar_bounds(overview, TASHKENT, date(2026, 10, 21)) == (
        date(2026, 10, 21),
        date(2026, 10, 21),
    )


# --------------------------------------------------------------------------- card
def test_card_shows_times_state_prices_with_taken_time_and_no_signals() -> None:
    text = format_fixture_card(make_detail(), TASHKENT)

    assert "<b>22.10.2026 01:00</b> (Asia/Tashkent) · UTC 21.10 20:00" in text
    assert "Premier League (England)" in text
    assert "⏳ kutilmoqda" in text
    assert "Arsenal g'alabasi (1): <b>1.85</b> · 21.10 11:00 · ertalabki" in text
    assert "1.80" not in text  # only the newest price of a market is on the card
    assert texts.DB_CARD_SIGNALS_NONE in text
    assert telegram_html_problems(text) == []


def test_card_with_result_half_time_and_xg() -> None:
    detail = make_detail(
        fixture=make_stored_fixture(status=FixtureStatus.FINISHED, home_score=3, away_score=1),
        home_ht_score=1,
        away_ht_score=0,
        home_xg=2.345,
        away_xg=0.5,
    )

    text = format_fixture_card(detail, TASHKENT)

    assert "Hisob: <b>3:1</b> (birinchi bo'lim 1:0)" in text
    assert "xG: 2.35 — 0.50" in text


def test_card_without_prices_and_with_truncated_history() -> None:
    bare = format_fixture_card(make_detail(odds=(), odds_total=0), TASHKENT)
    cut = format_fixture_card(make_detail(truncated=True, odds_total=900), TASHKENT)

    assert texts.DB_CARD_ODDS_NONE in bare
    assert "Faqat eng yangi 2 ta yozuv" in cut


def test_card_escapes_provider_text() -> None:
    detail = make_detail(
        fixture=make_stored_fixture(home_team="A<b>", away_team="C&D", league="L<i>", source="s&"),
        odds=(BookmakerOdds(bookmaker="Bet<365>", quotes=(make_quote(Selection.DRAW, 3.4),)),),
    )

    text = format_fixture_card(detail, TASHKENT)

    assert "A&lt;b&gt; — C&amp;D" in text
    assert "Bet&lt;365&gt;" in text
    assert telegram_html_problems(text) == []


def test_card_limits_the_prices_per_bookmaker_and_says_how_many_more() -> None:
    quotes = tuple(
        make_quote(Selection.OVER, 1.5 + i / 100, market=MarketType.TOTALS, line=0.5 + i)
        for i in range(MAX_CARD_ODDS_PER_BOOKMAKER + 4)
    )
    detail = make_detail(odds=(BookmakerOdds(bookmaker="1xbet", quotes=quotes),))

    text = format_fixture_card(detail, TASHKENT)

    assert text.count("• ") == MAX_CARD_ODDS_PER_BOOKMAKER
    assert "…yana 4 ta" in text


def test_odd_stored_shapes_fall_back_to_raw_codes_instead_of_failing() -> None:
    odd = make_quote(Selection.OVER, 2.0, market=MarketType.TOTALS, line=0.0)  # totals need a line
    detail = make_detail(odds=(BookmakerOdds(bookmaker="x", quotes=(odd,)),))

    assert "totals over 0" in format_fixture_card(detail, TASHKENT)


def test_latest_quotes_keeps_the_newest_per_market_in_market_order() -> None:
    old = make_quote(Selection.HOME, 1.9, NOW - timedelta(hours=1))
    new = make_quote(Selection.HOME, 2.0, NOW)
    draw = make_quote(Selection.DRAW, 3.3)
    over = make_quote(Selection.OVER, 1.7, market=MarketType.TOTALS, line=2.5)

    result = latest_quotes([over, old, draw, new])

    assert [(q.market, q.selection, q.odds) for q in result] == [
        (MarketType.MATCH_RESULT, Selection.HOME, 2.0),
        (MarketType.MATCH_RESULT, Selection.DRAW, 3.3),
        (MarketType.TOTALS, Selection.OVER, 1.7),
    ]


def test_card_and_odds_keyboards() -> None:
    nav = BrowseNav(token=5, screen=SCREEN_FIXTURE, fixture_id="fx1")
    page = StoredOddsPage(items=(make_quote(),), page=0, page_count=3, total=30)

    assert labels(card_keyboard(nav)) == [
        texts.BTN_DB_ODDS,
        texts.BTN_DB_BACK_FIXTURES,
        texts.BTN_DB_HOME,
    ]
    odds_nav = replace(nav, screen="odds")
    assert labels(odds_keyboard(page, odds_nav)) == [
        "1/3",
        texts.BTN_NEXT_PAGE,
        texts.BTN_DB_BACK_CARD,
    ]


def test_odds_history_lists_local_times_and_has_an_empty_state() -> None:
    fixture = make_stored_fixture()
    page = StoredOddsPage(
        items=(make_quote(), make_quote(Selection.AWAY, 4.2)), page=0, page_count=1, total=2
    )
    empty = StoredOddsPage(items=(), page=0, page_count=0, total=0)

    text = format_odds_history(page, fixture, TASHKENT)

    assert "Arsenal — Chelsea" in text
    assert "21.10 11:00 · 1xbet · Chelsea g'alabasi (2) · <b>4.20</b> · ertalabki" in text
    assert texts.DB_ODDS_EMPTY in format_odds_history(empty, fixture, TASHKENT)
    assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- navigation record
def test_nav_roundtrips_through_fsm_data_and_rejects_damage() -> None:
    nav = BrowseNav(token=9, screen=SCREEN_FIXTURES, ids=("a:1", "b"), labels=("A", "B"), day=5)

    assert BrowseNav.from_data(nav.to_data()) == nav
    assert BrowseNav.from_data(None) is None
    assert BrowseNav.from_data({"nonsense": 1}) is None
    assert BrowseNav.from_data("text") is None


def test_pick_and_owns() -> None:
    nav = BrowseNav(token=1, screen=SCREEN_LEAGUES, ids=("lg1",), labels=("PL",))

    assert nav.pick(0) == ("lg1", "PL")
    assert nav.pick(1) is None
    assert nav.pick(-1) is None
    assert nav.owns(nav.button("pick", 0))
    assert not nav.owns(replace(nav, screen=SCREEN_FIXTURES).button("pick", 0))


def test_every_screen_has_a_code_and_the_next_token_cycles() -> None:
    assert len(set(SCREEN_CODES.values())) == len(SCREEN_CODES)
    assert BrowseNav(token=1000).next_token == 1
    assert BrowseNav(token=999).next_token == 1000
    assert BrowseNav(token=0).next_token == 1


def test_apply_action_walks_leagues_to_odds_and_back() -> None:
    leagues = BrowseNav(token=1, screen=SCREEN_LEAGUES, page=2, ids=("lg1",), labels=("PL",))

    fixtures = apply_action(leagues, "pick", 0)
    assert (fixtures.screen, fixtures.scope, fixtures.scope_id, fixtures.parent_page) == (
        SCREEN_FIXTURES,
        SCOPE_LEAGUE,
        "lg1",
        2,
    )
    card = apply_action(replace(fixtures, ids=("fx9",), labels=("X",)), "pick", 0)
    assert (card.screen, card.fixture_id) == (SCREEN_FIXTURE, "fx9")
    assert apply_action(card, "odds", 0).screen == "odds"
    assert apply_action(apply_action(card, "odds", 0), "back", 0).screen == SCREEN_FIXTURE
    assert apply_action(card, "back", 0).screen == SCREEN_FIXTURES
    assert apply_action(fixtures, "back", 0) == replace(fixtures, screen=SCREEN_LEAGUES, page=2)


def test_apply_action_day_calendar_and_unknown_actions() -> None:
    nav = BrowseNav(token=1, screen=SCREEN_FIXTURES, scope=SCOPE_ALL, page=4)

    assert apply_action(nav, "day", 7).page == 0
    assert apply_action(nav, "cal", 0).screen == "calendar"
    assert apply_action(nav, "month", 202611).cal_month == 202611
    assert apply_action(nav, "page", 3).page == 3
    assert apply_action(nav, "opage", 3).odds_page == 3
    assert apply_action(nav, "pick", 5) == nav  # no such item
    assert apply_action(nav, "whatever", 1) == nav
    assert apply_action(replace(nav, scope="team"), "back", 0).screen == "teams"
    assert apply_action(nav, "back", 0) == nav  # "all" has nowhere to go inside nav


@given(
    entity_id=st.text(min_size=1, max_size=40).filter(
        lambda s: s.strip() and len(s.encode()) <= 40
    ),
    page=st.integers(min_value=0, max_value=10_000),
    day=st.integers(min_value=0, max_value=800_000),
)
def test_callback_data_never_depends_on_entity_ids_and_fits_the_limit(
    entity_id: str, page: int, day: int
) -> None:
    nav = BrowseNav(token=999, screen=SCREEN_FIXTURES, ids=(entity_id,), scope_id=entity_id)

    for action, value in (
        ("pick", 0),
        ("page", page),
        ("day", day),
        ("back", 0),
        ("month", 202610),
    ):
        data = nav.button(action, value).pack()
        assert len(data.encode()) <= CALLBACK_LIMIT
