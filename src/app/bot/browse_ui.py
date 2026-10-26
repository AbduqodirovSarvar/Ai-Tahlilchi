"""Pure parts of 🗄 Baza: overview, league / team / fixture lists and the day calendar.

Lists show numbered entries in the text and a numbered button per entry (a league or team
name does not fit a button, and ids never travel in ``callback_data``: the nav record in FSM
data holds them, see :mod:`app.bot.browse_nav`). Every stored instant is UTC; everything shown
is converted to the user's zone here. Names come from the provider and are escaped.
"""

import calendar
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from itertools import groupby
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.bot.account_formatters import age_label, stamp
from app.bot.browse_nav import (
    SCOPE_ALL,
    SCOPE_LEAGUE,
    BrowseNav,
)
from app.bot.callbacks import DbCb
from app.bot.formatters import clock, escape, short_date, weekday_name
from app.bot.keyboards import markup_of, single_button
from app.bot.paging import Page, pager_row
from app.services.interfaces import (
    DatabaseOverview,
    StoredFixture,
    StoredFixturePage,
    StoredLeague,
    StoredTable,
    StoredTeam,
    StoredTeamPage,
)

BYTES_PER_UNIT: Final = 1024
BROWSE_PAGE_SIZE: Final = 10
"""Entries per page of the lists: ten numbered buttons fit two rows of five."""

type Row = list[InlineKeyboardButton]


# --------------------------------------------------------------------------- small helpers
def size_label(size_bytes: int) -> str:
    """``12.4 MB`` (binary units, one decimal above bytes)."""
    value = float(max(size_bytes, 0))
    unit = 0
    while value >= BYTES_PER_UNIT and unit < len(texts.DB_SIZE_UNITS) - 1:
        value /= BYTES_PER_UNIT
        unit += 1
    if unit == 0:
        return f"{int(value)} {texts.DB_SIZE_UNITS[0]}"
    return f"{value:.1f} {texts.DB_SIZE_UNITS[unit]}"


def local_date_label(moment: datetime, zone: ZoneInfo) -> str:
    """``26.10.2026`` in the user's zone."""
    return moment.astimezone(zone).strftime("%d.%m.%Y")


def _country(country: str | None) -> str:
    return "" if not country else texts.DB_COUNTRY.format(country=escape(country))


def _choice_rows(count: int, nav: BrowseNav) -> list[Row]:
    """Numbered buttons ``1..count`` (the index is 0-based in the callback)."""
    buttons = [single_button(str(i + 1), nav.button("pick", i)) for i in range(count)]
    width = texts.DB_CHOICE_ROW_WIDTH
    return [buttons[i : i + width] for i in range(0, len(buttons), width)]


def _pager(number: int, count: int, nav: BrowseNav) -> Row:
    """``◀️ n/m ▶️`` over the ``page`` action of the current screen."""
    return pager_row(Page((), number, max(count, 1)), lambda n: nav.button("page", n))


def home_button() -> InlineKeyboardButton:
    """``🗄 Baza``: back to the overview (works on any screen, needs no token)."""
    return single_button(texts.BTN_DB_HOME, DbCb(action="menu"))


def refresh_button() -> InlineKeyboardButton:
    """``🔄 Bazani yangilash``."""
    return single_button(texts.BTN_DB_REFRESH, DbCb(action="refresh"))


# --------------------------------------------------------------------------- overview
def has_data(overview: DatabaseOverview) -> bool:
    """True when the shared provider tables hold anything (the user's own rows do not count)."""
    return any(table.rows for table in overview.tables if not table.own)


def _table_line(table: StoredTable, zone: ZoneInfo) -> str:
    label = texts.DB_TABLE_LABELS.get(table.name, escape(table.name))
    when = (
        ""
        if table.last_updated_at is None
        else texts.DB_TABLE_WHEN.format(time=stamp(table.last_updated_at, zone))
    )
    return texts.DB_TABLE_LINE.format(
        label=label, rows=table.rows, own=texts.DB_TABLE_OWN if table.own else "", when=when
    )


def _span_line(overview: DatabaseOverview, zone: ZoneInfo) -> str:
    if overview.earliest_kickoff is None or overview.latest_kickoff is None:
        return texts.DB_SPAN_NONE
    return texts.DB_SPAN.format(
        first=local_date_label(overview.earliest_kickoff, zone),
        last=local_date_label(overview.latest_kickoff, zone),
        upcoming=overview.upcoming_fixtures,
    )


def format_overview(overview: DatabaseOverview, zone: ZoneInfo, now: datetime) -> str:
    """``/baza``: freshness first (it decides whether analysis works), then size and tables."""
    lines = [texts.DB_TITLE]
    if not has_data(overview):
        lines.extend(["", texts.DB_EMPTY])
        return "\n".join(lines)
    refreshed = overview.last_refresh_at
    lines.append(
        texts.DB_NEVER_REFRESHED
        if refreshed is None
        else texts.DB_REFRESHED.format(time=stamp(refreshed, zone), age=age_label(refreshed, now))
    )
    if overview.data_stale:
        lines.append(texts.DB_STALE_WARNING)
    lines.extend(
        [texts.DB_SIZE.format(size=size_label(overview.size_bytes)), _span_line(overview, zone)]
    )
    lines.extend(["", texts.DB_TABLES_HEADER])
    lines.extend(_table_line(table, zone) for table in overview.tables)
    lines.extend(["", texts.DB_ZONE_NOTE.format(zone=escape(zone.key))])
    return "\n".join(lines)


def overview_keyboard(*, with_data: bool) -> InlineKeyboardMarkup:
    """Section buttons (only when there is something to browse) and the refresh button."""
    rows: list[Row] = []
    if with_data:
        rows.append(
            [
                single_button(texts.BTN_DB_LEAGUES, DbCb(action="leagues")),
                single_button(texts.BTN_DB_TEAMS, DbCb(action="teams")),
            ]
        )
        rows.append([single_button(texts.BTN_DB_ALL, DbCb(action="all"))])
    rows.append([refresh_button()])
    return markup_of(rows)


# --------------------------------------------------------------------------- leagues
def _league_line(number: int, league: StoredLeague, zone: ZoneInfo) -> str:
    span = ""
    if league.first_kickoff is not None and league.last_kickoff is not None:
        span = texts.DB_LEAGUE_SPAN.format(
            first=short_date(league.first_kickoff.astimezone(zone).date()),
            last=short_date(league.last_kickoff.astimezone(zone).date()),
        )
    return texts.DB_LEAGUE_LINE.format(
        n=number,
        name=escape(league.name),
        country=_country(league.country),
        fixtures=league.fixtures,
        upcoming=league.upcoming,
        span=span,
    )


def format_leagues(page: Page[StoredLeague], total: int, zone: ZoneInfo) -> str:
    """The numbered leagues of one page (or the empty state)."""
    if total == 0:
        return texts.DB_LEAGUES_EMPTY
    lines = [texts.DB_LEAGUES_TITLE.format(total=total), ""]
    lines.extend(_league_line(i, league, zone) for i, league in enumerate(page.items, start=1))
    return "\n".join(lines)


def leagues_keyboard(page: Page[StoredLeague], nav: BrowseNav) -> InlineKeyboardMarkup:
    """Numbered buttons, the pager and the way back."""
    rows = _choice_rows(len(page.items), nav)
    pager = _pager(page.number, page.count, nav)
    if pager:
        rows.append(pager)
    rows.append([home_button()])
    return markup_of(rows)


# --------------------------------------------------------------------------- teams
def format_teams(page: StoredTeamPage, query: str) -> str:
    """The numbered teams of one page (or why the list is empty)."""
    if page.total == 0:
        if query:
            return texts.DB_TEAMS_NO_MATCH.format(text=escape(query))
        return texts.DB_TEAMS_EMPTY
    lines = [texts.DB_TEAMS_TITLE.format(total=page.total)]
    if query:
        lines.append(texts.DB_TEAMS_QUERY.format(text=escape(query)))
    lines.append("")
    lines.extend(_team_line(i, team) for i, team in enumerate(page.items, start=1))
    return "\n".join(lines)


def _team_line(number: int, team: StoredTeam) -> str:
    return texts.DB_TEAM_LINE.format(
        n=number, name=escape(team.name), country=_country(team.country), fixtures=team.fixtures
    )


def teams_keyboard(page: StoredTeamPage, nav: BrowseNav) -> InlineKeyboardMarkup:
    """Numbered buttons, the pager, search and the way back."""
    rows = _choice_rows(len(page.items), nav)
    pager = _pager(page.page, page.page_count, nav)
    if pager:
        rows.append(pager)
    rows.append(
        [single_button(texts.BTN_DB_SEARCH, DbCb(action="search")), home_button()],
    )
    return markup_of(rows)


# --------------------------------------------------------------------------- fixtures
def fixture_state(fixture: StoredFixture) -> str:
    """``✅ tugagan 2:1`` / ``⏳ kutilmoqda`` / ``🔴 jonli 1:0``."""
    state = texts.DB_STATES[fixture.status.value]
    if fixture.home_score is None or fixture.away_score is None:
        return state
    return texts.DB_STATE_SCORE.format(
        state=state, home=fixture.home_score, away=fixture.away_score
    )


def _fixture_lines(number: int, fixture: StoredFixture, zone: ZoneInfo) -> list[str]:
    local = fixture.kickoff.astimezone(zone)
    return [
        texts.DB_FIXTURE_LINE.format(
            n=number, time=clock(local.time()), label=escape(fixture.label)
        ),
        texts.DB_FIXTURE_META.format(
            league=escape(fixture.league),
            state=fixture_state(fixture),
            odds=texts.DB_FIXTURE_ODDS_MARK if fixture.has_odds else "",
        ),
    ]


def day_label(ordinal: int) -> str:
    """The day of a date ordinal as ``26.10.2026``."""
    return date.fromordinal(ordinal).strftime("%d.%m.%Y")


def _empty_fixtures_text(nav: BrowseNav) -> str:
    return texts.DB_FIXTURES_EMPTY_FILTER if nav.day else texts.DB_FIXTURES_EMPTY


def format_fixtures(page: StoredFixturePage, nav: BrowseNav, zone: ZoneInfo) -> str:
    """Numbered fixtures grouped under local-day headers, with the active filter above."""
    scope = texts.DB_SCOPE_ALL if nav.scope == SCOPE_ALL else escape(nav.scope_name)
    lines = [
        texts.DB_FIXTURES_TITLE.format(scope=scope, total=page.total),
        texts.DB_DAY_ONE.format(day=day_label(nav.day)) if nav.day else texts.DB_DAY_ALL,
        texts.DB_ZONE_NOTE.format(zone=escape(zone.key)),
    ]
    if page.total == 0:
        return "\n".join([*lines, "", _empty_fixtures_text(nav)])
    number = 0
    for day, group in groupby(page.items, key=lambda item: item.kickoff.astimezone(zone).date()):
        lines.extend(
            ["", texts.DB_DAY_HEADER.format(day=short_date(day), weekday=weekday_name(day))]
        )
        for fixture in group:
            number += 1
            lines.extend(_fixture_lines(number, fixture, zone))
    return "\n".join(lines)


def _day_row(nav: BrowseNav, today: date) -> Row:
    """``📅 Hammasi | Bugun | Ertaga | 🗓 Sana`` with a dot on the active filter."""
    choices = (
        (texts.BTN_DB_DAY_ALL, 0),
        (texts.BTN_TODAY, today.toordinal()),
        (texts.BTN_TOMORROW, (today + timedelta(days=1)).toordinal()),
    )
    row = [
        single_button(
            texts.CALLS_FILTER_MARK.format(label=label) if nav.day == ordinal else label,
            nav.button("day", ordinal),
        )
        for label, ordinal in choices
    ]
    row.append(single_button(texts.BTN_DB_DAY_PICK, nav.button("cal")))
    return row


def back_label(nav: BrowseNav) -> str:
    """Where ``⬅️`` of a fixture list leads."""
    if nav.scope == SCOPE_LEAGUE:
        return texts.BTN_DB_BACK_LEAGUES
    return texts.BTN_DB_BACK_TEAMS if nav.scope != SCOPE_ALL else texts.BTN_DB_HOME


def fixtures_keyboard(page: StoredFixturePage, nav: BrowseNav, today: date) -> InlineKeyboardMarkup:
    """Day filter, numbered buttons, the pager and the way back."""
    rows: list[Row] = [_day_row(nav, today), *_choice_rows(len(page.items), nav)]
    pager = _pager(page.page, page.page_count, nav)
    if pager:
        rows.append(pager)
    rows.append([single_button(back_label(nav), nav.button("back"))])
    return markup_of(rows)


# --------------------------------------------------------------------------- calendar
def calendar_bounds(overview: DatabaseOverview, zone: ZoneInfo, today: date) -> tuple[date, date]:
    """First and last stored local day (today for an empty span)."""
    first = overview.earliest_kickoff
    last = overview.latest_kickoff
    return (
        today if first is None else first.astimezone(zone).date(),
        today if last is None else last.astimezone(zone).date(),
    )


def shift_month(key: int, delta: int) -> int:
    """``202610`` shifted by ``delta`` months."""
    index = key // 100 * 12 + key % 100 - 1 + delta
    return index // 12 * 100 + index % 12 + 1


def _noop(text: str = " ") -> InlineKeyboardButton:
    return single_button(text, DbCb(action="noop"))


def _calendar_day(day: date, nav: BrowseNav, first: date, last: date, today: date) -> Row:
    if not first <= day <= last:
        return [_noop("·")]
    label = str(day.day)
    if day.toordinal() == nav.day:
        label = f"[{label}]"
    elif day == today:
        label = f"•{label}"
    return [single_button(label, nav.button("day", day.toordinal()))]


def _week_row(week: Sequence[date], month_number: int, make_day: Callable[[date], Row]) -> Row:
    """Seven cells: the days of the month, blanks for the neighbouring months."""
    return [
        button
        for day in week
        for button in (make_day(day) if day.month == month_number else [_noop()])
    ]


def calendar_keyboard(
    nav: BrowseNav, month: int, bounds: tuple[date, date], today: date
) -> InlineKeyboardMarkup:
    """Month grid over the stored span; days outside it are inert."""
    first, last = bounds
    year, month_number = divmod(month, 100)
    previous, following = shift_month(month, -1), shift_month(month, 1)
    header = [
        single_button(texts.BTN_PREV_PAGE, nav.button("month", previous))
        if previous >= first.year * 100 + first.month
        else _noop(),
        _noop(f"{texts.MONTHS[month_number - 1]} {year}"),
        single_button(texts.BTN_NEXT_PAGE, nav.button("month", following))
        if following <= last.year * 100 + last.month
        else _noop(),
    ]
    rows: list[Row] = [header, [_noop(name) for name in texts.WEEKDAYS_SHORT]]
    rows.extend(
        _week_row(week, month_number, lambda day: _calendar_day(day, nav, first, last, today))
        for week in calendar.Calendar().monthdatescalendar(year, month_number)
    )
    rows.append(
        [
            single_button(texts.BTN_DB_DAY_ALL, nav.button("day", 0)),
            single_button(texts.BTN_BACK, nav.button("back")),
        ]
    )
    return markup_of(rows)


def initial_month(nav: BrowseNav, bounds: tuple[date, date], today: date) -> int:
    """The month the calendar opens on: the chosen day's, else today's, clamped into the span."""
    first, last = bounds
    anchor = date.fromordinal(nav.day) if nav.day else today
    anchor = min(max(anchor, first), last)
    return anchor.year * 100 + anchor.month
