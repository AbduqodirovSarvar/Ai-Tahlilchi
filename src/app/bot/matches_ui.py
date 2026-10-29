"""⚽ O'yinlar screens: a day's live matches (paged, grouped by competition) and a match sheet.

Every name comes from the provider and is escaped. Kick-offs are shown in the user's zone as
``30.10.2026 21:00`` (:func:`~app.bot.formatters.kickoff_stamp`).
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from html import escape
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import MatchCb, NoopCb
from app.bot.formatters import kickoff_stamp, weekday_name
from app.bot.keyboards import markup_of, single_button
from app.services.interfaces import LineupPlayer, MatchItem, MatchSheet, SideLineup

MATCHES_PAGE_SIZE: Final = 10
_NUMBERS_PER_ROW: Final = 5

type Row = list[InlineKeyboardButton]


def page_count(total: int) -> int:
    """Pages needed for ``total`` matches (at least one, so an empty day still renders)."""
    return max((total + MATCHES_PAGE_SIZE - 1) // MATCHES_PAGE_SIZE, 1)


def clamp_page(page: int, total: int) -> int:
    """``page`` kept inside the list (the provider's list may have shrunk since the button)."""
    return min(max(page, 0), page_count(total) - 1)


def _page_items(items: Sequence[MatchItem], page: int) -> Sequence[MatchItem]:
    start = page * MATCHES_PAGE_SIZE
    return items[start : start + MATCHES_PAGE_SIZE]


def _team(name: str | None) -> str:
    return escape(name) if name else texts.MATCHES_TBD


def match_state(item: MatchItem) -> str:
    """``⏳ kutilmoqda`` / ``✅ tugagan 2:1`` / ``🔴 jonli 1:0``."""
    state = texts.MATCHES_STATES[item.state.value]
    if item.home_goals is None or item.away_goals is None:
        return state
    return texts.MATCHES_STATE_SCORE.format(state=state, home=item.home_goals, away=item.away_goals)


def format_matches(day: date, items: Sequence[MatchItem], page: int, zone: ZoneInfo) -> str:
    """One page of the day's matches, numbered, under competition headers."""
    lines = [
        texts.MATCHES_TITLE.format(
            day=day.strftime("%d.%m.%Y"), weekday=weekday_name(day), total=len(items)
        ),
        texts.MATCHES_SOURCE.format(zone=escape(zone.key)),
    ]
    if not items:
        lines.extend(["", texts.MATCHES_EMPTY])
        return "\n".join(lines)
    competition: str | None = None
    first = page * MATCHES_PAGE_SIZE + 1
    for number, item in enumerate(_page_items(items, page), start=first):
        if item.competition != competition:
            competition = item.competition
            lines.extend(["", texts.MATCHES_COMPETITION.format(competition=escape(competition))])
        lines.append(
            texts.MATCHES_LINE.format(
                n=number,
                kickoff=kickoff_stamp(item.kickoff, zone),
                home=_team(item.home),
                away=_team(item.away),
                state=match_state(item),
            )
        )
    pages = page_count(len(items))
    if pages > 1:
        lines.extend(["", texts.MATCHES_PAGE.format(page=page + 1, pages=pages)])
    return "\n".join(lines)


def ordered_for_display(items: Sequence[MatchItem]) -> tuple[MatchItem, ...]:
    """Competitions in order of their first kick-off, matches by kick-off inside each."""
    first_kickoff: dict[str, datetime] = {}
    for item in items:
        earliest = first_kickoff.get(item.competition)
        if earliest is None or item.kickoff < earliest:
            first_kickoff[item.competition] = item.kickoff
    return tuple(
        sorted(
            items,
            key=lambda item: (first_kickoff[item.competition], item.competition, item.kickoff),
        )
    )


def _number_rows(items: Sequence[MatchItem], day: date, page: int) -> list[Row]:
    first = page * MATCHES_PAGE_SIZE + 1
    buttons = [
        single_button(
            str(number), MatchCb(action="item", d=day.toordinal(), p=page, i=item.match_id)
        )
        for number, item in enumerate(_page_items(items, page), start=first)
    ]
    return [buttons[i : i + _NUMBERS_PER_ROW] for i in range(0, len(buttons), _NUMBERS_PER_ROW)]


def page_row(day: date, page: int, total: int, *, action: str = "day") -> Row:
    """Previous / page indicator / next for a paged list of ``day`` (``action`` = which list)."""
    pages = page_count(total)
    if pages == 1:
        return []
    ordinal = day.toordinal()
    row: Row = []
    if page > 0:
        previous = MatchCb(action=action, d=ordinal, p=page - 1)
        row.append(single_button(texts.BTN_MATCHES_PREV_PAGE, previous))
    row.append(single_button(f"{page + 1}/{pages}", NoopCb()))
    if page < pages - 1:
        following = MatchCb(action=action, d=ordinal, p=page + 1)
        row.append(single_button(texts.BTN_MATCHES_NEXT_PAGE, following))
    return row


def day_choice_rows(today: date) -> list[Row]:
    """Bugun / Ertaga / Sana kiritish."""
    return [
        [
            single_button(texts.BTN_MATCHES_TODAY, MatchCb(action="day", d=today.toordinal())),
            single_button(
                texts.BTN_MATCHES_TOMORROW,
                MatchCb(action="day", d=(today + timedelta(days=1)).toordinal()),
            ),
        ],
        [single_button(texts.BTN_MATCHES_PICK_DATE, MatchCb(action="ask"))],
    ]


def matches_keyboard(
    items: Sequence[MatchItem], day: date, page: int, today: date
) -> InlineKeyboardMarkup:
    """Number buttons, pages, neighbouring days and the day choices."""
    neighbours = [
        single_button(texts.BTN_MATCHES_PREV_DAY, MatchCb(action="day", d=day.toordinal() - 1)),
        single_button(texts.BTN_MATCHES_NEXT_DAY, MatchCb(action="day", d=day.toordinal() + 1)),
    ]
    ordinal = day.toordinal()
    forecast = (
        [
            single_button(texts.BTN_FORECAST_DAY, MatchCb(action="fday", d=ordinal)),
            single_button(texts.BTN_TOP_PICKS, MatchCb(action="picks", d=ordinal)),
        ]
        if items
        else []
    )
    rows = [
        *_number_rows(items, day, page),
        page_row(day, page, len(items)),
        forecast,
        neighbours,
    ]
    return markup_of([row for row in [*rows, *day_choice_rows(today)] if row])


def _player_line(player: LineupPlayer) -> str:
    number = texts.MATCH_NO_NUMBER if player.shirt_number is None else str(player.shirt_number)
    position = (
        texts.MATCH_PLAYER_POSITION.format(position=escape(player.position))
        if player.position
        else ""
    )
    return texts.MATCH_PLAYER.format(number=number, name=escape(player.name), position=position)


def _side_lines(side: SideLineup) -> list[str]:
    formation = (
        texts.MATCH_SIDE_FORMATION.format(formation=escape(side.formation))
        if side.formation
        else ""
    )
    lines = [texts.MATCH_SIDE_HEADER.format(team=_team(side.team), formation=formation)]
    if side.coach:
        lines.append(texts.MATCH_SIDE_COACH.format(coach=escape(side.coach)))
    if side.starters:
        lines.extend([texts.MATCH_SIDE_STARTERS, *map(_player_line, side.starters)])
    if side.bench:
        lines.extend([texts.MATCH_SIDE_BENCH, *map(_player_line, side.bench)])
    return lines


def format_match_sheet(sheet: MatchSheet, zone: ZoneInfo) -> str:
    """Teams, kick-off, state and score, then each side's coach and line-up."""
    item = sheet.match
    lines = [
        texts.MATCH_SHEET_HEADER.format(home=_team(item.home), away=_team(item.away)),
        texts.MATCH_SHEET_META.format(
            competition=escape(item.competition),
            kickoff=kickoff_stamp(item.kickoff, zone),
            zone=escape(zone.key),
            state=match_state(item),
        ),
    ]
    if sheet.home_ht_goals is not None and sheet.away_ht_goals is not None:
        lines.append(
            texts.MATCH_SHEET_HALF_TIME.format(home=sheet.home_ht_goals, away=sheet.away_ht_goals)
        )
    lines.extend(_side_lines(sheet.home))
    lines.extend(_side_lines(sheet.away))
    if not sheet.lineups_announced:
        lines.extend(["", texts.MATCH_LINEUPS_MISSING])
    return "\n".join(lines)


def sheet_keyboard(day_ordinal: int, page: int, match_id: int) -> InlineKeyboardMarkup:
    """The match's forecast, and back to the list the match was opened from."""
    forecast = MatchCb(action="fmatch", d=day_ordinal, p=page, i=match_id)
    return markup_of(
        [
            [single_button(texts.BTN_FORECAST_MATCH, forecast)],
            [single_button(texts.BTN_MATCHES_BACK, MatchCb(action="day", d=day_ordinal, p=page))],
        ]
    )
