"""Analytics screens (v1.16): match lists as buttons, the league menu and a match analysis.

The bot is an analysis centre, not a tipster: no odds, EV, filters or picks. A match analysis has
five sections; each is the AI's text when it answered, otherwise worded here from the facts, so
the user always gets a full analysis. Every name from the provider is escaped.
"""

from collections.abc import Sequence
from datetime import date
from html import escape
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import GameCb, NoopCb
from app.bot.formatters import kickoff_stamp, weekday_name
from app.bot.keyboards import markup_of, single_button
from app.services.interfaces import (
    AnalysisSections,
    MatchAnalysis,
    MatchFacts,
    MatchItem,
    MatchOutlook,
    MatchState,
    SideLineup,
    TeamFacts,
)

PAGE_SIZE: Final = 8
LEAGUE_DAYS: Final = 7
BUTTON_NAME_LIMIT: Final = 22
STARTERS_SHOWN: Final = 11
MEETINGS_SHOWN: Final = 3
PERCENT: Final = 100
# Expected goals of both sides together: above OPEN is an open game, below CAGEY a cagey one.
OPEN_GAME_GOALS: Final = 3.0
CAGEY_GAME_GOALS: Final = 2.2
# A side is the favourite when its win chance beats the other's by this much.
FAVOURITE_MARGIN: Final = 0.15
MANY_GOALS_FROM: Final = 0.55
FEW_GOALS_UNDER: Final = 0.45

type Row = list[InlineKeyboardButton]


# ------------------------------------------------------------------------------- lists
def page_count(total: int) -> int:
    """Pages needed for ``total`` matches (at least one, so an empty list still renders)."""
    return max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)


def clamp_page(page: int, total: int) -> int:
    """``page`` kept inside the list (the provider's list may have shrunk since the button)."""
    return min(max(page, 0), page_count(total) - 1)


def _short(name: str | None) -> str:
    if not name:
        return texts.TEAM_UNKNOWN
    return name if len(name) <= BUTTON_NAME_LIMIT else name[: BUTTON_NAME_LIMIT - 1] + "…"


def game_label(item: MatchItem, zone: ZoneInfo, *, with_date: bool = False) -> str:
    """``🏴 Arsenal vs Chelsea | 21:30`` (a played match shows its score instead of the time).

    Button text is plain text in Telegram, so names are not HTML-escaped here.
    """
    if item.home_goals is not None and item.away_goals is not None:
        when = texts.GAME_BUTTON_SCORE.format(
            home_goals=item.home_goals, away_goals=item.away_goals
        )
    else:
        when = item.kickoff.astimezone(zone).strftime("%d.%m %H:%M" if with_date else "%H:%M")
    flag = texts.COMPETITION_FLAGS.get(item.competition_code or "", texts.DEFAULT_FLAG)
    return texts.GAME_BUTTON.format(
        flag=flag, home=_short(item.home), away=_short(item.away), when=when
    )


def _page_rows(
    items: Sequence[MatchItem], page: int, zone: ZoneInfo, base: GameCb, *, with_date: bool
) -> list[Row]:
    start = page * PAGE_SIZE
    rows = [
        [
            single_button(
                game_label(item, zone, with_date=with_date),
                base.model_copy(update={"action": "an", "p": page, "i": item.match_id}),
            )
        ]
        for item in items[start : start + PAGE_SIZE]
    ]
    pages = page_count(len(items))
    if pages > 1:
        nav: Row = []
        if page > 0:
            nav.append(single_button(texts.BTN_PAGE_PREV, base.model_copy(update={"p": page - 1})))
        nav.append(single_button(f"{page + 1}/{pages}", NoopCb()))
        if page < pages - 1:
            nav.append(single_button(texts.BTN_PAGE_NEXT, base.model_copy(update={"p": page + 1})))
        rows.append(nav)
    return rows


def format_day(day: date, total: int) -> str:
    """Header of a day's list."""
    title = texts.DAY_TITLE.format(
        day=day.strftime("%d.%m.%Y"), weekday=weekday_name(day), total=total
    )
    return f"{title}\n\n{texts.LIST_HINT if total else texts.DAY_EMPTY}"


def day_keyboard(
    items: Sequence[MatchItem], day: date, page: int, zone: ZoneInfo
) -> InlineKeyboardMarkup:
    """One button per match of the page, then the page row."""
    base = GameCb(action="day", d=day.toordinal(), p=page)
    return markup_of(_page_rows(items, page, zone, base, with_date=False))


def leagues_text() -> str:
    """Header of the league menu."""
    return texts.LEAGUES_TITLE.format(days=LEAGUE_DAYS)


def leagues_keyboard() -> InlineKeyboardMarkup:
    """One button per top league."""
    return markup_of(
        [
            [single_button(label, GameCb(action="league", c=code))]
            for code, label in texts.TOP_LEAGUES
        ]
    )


def league_name(code: str) -> str:
    """The menu label of a league code (the code itself when unknown)."""
    return dict(texts.TOP_LEAGUES).get(code, escape(code))


def format_league(code: str, total: int) -> str:
    """Header of a league's list."""
    title = texts.LEAGUE_TITLE.format(league=league_name(code), days=LEAGUE_DAYS, total=total)
    hint = texts.LIST_HINT if total else texts.LEAGUE_EMPTY.format(days=LEAGUE_DAYS)
    return f"<b>{title}</b>\n\n{hint}"


def league_keyboard(
    items: Sequence[MatchItem], code: str, page: int, zone: ZoneInfo
) -> InlineKeyboardMarkup:
    """One button per match (with its date), the page row and back to the league menu."""
    base = GameCb(action="league", c=code, p=page)
    rows = _page_rows(items, page, zone, base, with_date=True)
    rows.append([single_button(texts.BTN_BACK_TO_LEAGUES, GameCb(action="leagues"))])
    return markup_of(rows)


# ---------------------------------------------------------------------------- analysis
def analysis_keyboard(source: GameCb) -> InlineKeyboardMarkup:
    """🔄 Yangilash and back to the list the match was opened from (``source`` is its button)."""
    back = source.model_copy(update={"action": "league" if source.c else "day", "i": 0})
    return markup_of(
        [
            [single_button(texts.BTN_REFRESH, source.model_copy(update={"action": "re"}))],
            [single_button(texts.BTN_BACK_TO_LIST, back)],
        ]
    )


def _team(name: str | None) -> str:
    return escape(name) if name else texts.TEAM_UNKNOWN


def _pct(probability: float) -> str:
    return f"{round(probability * PERCENT)}%"


def format_analysis(analysis: MatchAnalysis, zone: ZoneInfo) -> str:
    """The full analysis message: header, five sections, model numbers and the source."""
    item = analysis.sheet.match
    home, away = _team(item.home), _team(item.away)
    lines = [
        texts.ANALYSIS_TITLE.format(home=home, away=away),
        texts.ANALYSIS_META.format(
            league=escape(item.competition), kickoff=kickoff_stamp(item.kickoff, zone)
        ),
    ]
    if item.state is not MatchState.SCHEDULED:
        state = texts.MATCHES_STATES[item.state.value]
        lines.append(texts.ANALYSIS_STATE.format(state=state))
    sections = analysis.sections or AnalysisSections()
    for key, text in section_texts(analysis, sections):
        lines.extend(["", texts.ANALYSIS_SECTIONS[key], text])
    if analysis.outlook is not None:
        lines.extend(["", model_block(analysis.outlook, home, away)])
    source = texts.ANALYSIS_SOURCE_AI if analysis.by_ai else texts.ANALYSIS_SOURCE_FACTS
    lines.extend(
        [
            "",
            source,
            texts.ANALYSIS_UPDATED.format(at=kickoff_stamp(analysis.generated_at, zone)),
            texts.ANALYSIS_DISCLAIMER,
        ]
    )
    return "\n".join(lines)


def section_texts(
    analysis: MatchAnalysis, sections: AnalysisSections
) -> tuple[tuple[str, str], ...]:
    """``(section key, HTML text)`` of the five sections, AI text first, facts otherwise."""
    item = analysis.sheet.match
    home, away = _team(item.home), _team(item.away)
    facts, outlook = analysis.facts, analysis.outlook
    squads = _ai(sections.squads) or squads_fact(analysis.sheet.home, analysis.sheet.away)
    if sections.squads and has_lineups(analysis.sheet.home, analysis.sheet.away):
        squads = f"{squads}\n{squads_fact(analysis.sheet.home, analysis.sheet.away)}"
    return (
        ("form", _ai(sections.form) or form_fact(facts, home, away)),
        ("venue", _ai(sections.venue) or venue_fact(facts, home, away)),
        ("squads", squads),
        ("tactics", _ai(sections.tactics) or tactics_fact(outlook, facts)),
        ("summary", _ai(sections.summary) or summary_fact(outlook, home, away)),
    )


def _ai(text: str | None) -> str | None:
    return None if not text else escape(text, quote=False)


def model_block(outlook: MatchOutlook, home: str, away: str) -> str:
    """The model's main numbers, shown as background."""
    return texts.ANALYSIS_MODEL.format(
        home=home,
        away=away,
        home_win=_pct(outlook.home_win),
        draw=_pct(outlook.draw),
        away_win=_pct(outlook.away_win),
        over=_pct(outlook.over_2_5),
        btts=_pct(outlook.btts),
        expected_home=f"{outlook.expected_home_goals:.1f}",
        expected_away=f"{outlook.expected_away_goals:.1f}",
    )


# ------------------------------------------------------------------ sections from facts
def form_text(form: str) -> str:
    """``WDL`` in Uzbek letters (``GDM``); ``—`` when there is no match yet."""
    return "".join(texts.FORM_WORD_LETTERS.get(letter, letter) for letter in form) or "—"


def form_fact(facts: MatchFacts | None, home: str, away: str) -> str:
    """Both sides' last results and goal averages."""
    if facts is None:
        return texts.FACT_NO_HISTORY
    lines = [
        texts.FACT_FORM_LINE.format(
            team=name,
            form=form_text(side.form),
            scored=f"{side.goals_for_avg:.1f}",
            conceded=f"{side.goals_against_avg:.1f}",
        )
        for name, side in ((home, facts.home), (away, facts.away))
    ]
    return "\n".join([*lines, texts.FACT_FORM_LEGEND])


def venue_fact(facts: MatchFacts | None, home: str, away: str) -> str:
    """The hosts at home, the visitors away, the table and the last meetings."""
    if facts is None:
        return texts.FACT_NO_HISTORY
    lines = [
        _venue_line(home, facts.home, at_home=True),
        _venue_line(away, facts.away, at_home=False),
    ]
    if facts.home.position is not None and facts.away.position is not None:
        lines.append(
            texts.FACT_TABLE.format(
                home=home,
                away=away,
                home_position=facts.home.position,
                away_position=facts.away.position,
                home_points=facts.home.points or 0,
                away_points=facts.away.points or 0,
            )
        )
    if facts.meetings:
        meetings = "; ".join(
            texts.FACT_MEETING.format(
                home=escape(m.home),
                away=escape(m.away),
                home_goals=m.home_goals,
                away_goals=m.away_goals,
                day=m.day.strftime("%d.%m.%Y"),
            )
            for m in facts.meetings[:MEETINGS_SHOWN]
        )
        lines.append(texts.FACT_H2H.format(meetings=meetings))
    return "\n".join(lines)


def _venue_line(name: str, side: TeamFacts, *, at_home: bool) -> str:
    venue = side.venue
    return texts.FACT_VENUE_LINE.format(
        team=name,
        where=texts.FACT_WHERE[at_home],
        played=venue.played,
        wins=venue.wins,
        draws=venue.draws,
        losses=venue.losses,
        goals_for=venue.goals_for,
        goals_against=venue.goals_against,
    )


def has_lineups(home: SideLineup, away: SideLineup) -> bool:
    """True once both starting line-ups are announced."""
    return bool(home.starters and away.starters)


def squads_fact(home: SideLineup, away: SideLineup) -> str:
    """The announced line-ups, or a note that they are not out yet."""
    if not has_lineups(home, away):
        return texts.FACT_SQUADS_PENDING
    lines: list[str] = []
    for side in (home, away):
        formation = (
            texts.FACT_SQUAD_FORMATION.format(formation=escape(side.formation))
            if side.formation
            else ""
        )
        players = ", ".join(escape(p.name) for p in side.starters[:STARTERS_SHOWN])
        lines.append(
            texts.FACT_SQUAD_LINE.format(
                team=_team(side.team), formation=formation, players=players
            )
        )
        if side.coach:
            lines.append(texts.FACT_SQUAD_COACH.format(coach=escape(side.coach)))
    return "\n".join(lines)


def expected_total(outlook: MatchOutlook | None, facts: MatchFacts | None) -> float | None:
    """Goals both sides are expected to score together: the model's, else the averages'."""
    if outlook is not None:
        return outlook.expected_home_goals + outlook.expected_away_goals
    if facts is None:
        return None
    home, away = facts.home, facts.away
    return (home.goals_for_avg + away.goals_against_avg) / 2 + (
        away.goals_for_avg + home.goals_against_avg
    ) / 2


def tactics_fact(outlook: MatchOutlook | None, facts: MatchFacts | None) -> str:
    """Open, cagey or balanced, from the expected goals."""
    total = expected_total(outlook, facts)
    if total is None:
        return texts.FACT_TACTICS["unknown"]
    if total >= OPEN_GAME_GOALS:
        return texts.FACT_TACTICS["open"]
    if total <= CAGEY_GAME_GOALS:
        return texts.FACT_TACTICS["cagey"]
    return texts.FACT_TACTICS["balanced"]


def summary_fact(outlook: MatchOutlook | None, home: str, away: str) -> str:
    """The most expected scenario: who is stronger and how many goals."""
    if outlook is None:
        return texts.FACT_SUMMARY_UNKNOWN
    if outlook.home_win - outlook.away_win >= FAVOURITE_MARGIN:
        side = texts.FACT_SUMMARY_FAVOURITE.format(team=home)
    elif outlook.away_win - outlook.home_win >= FAVOURITE_MARGIN:
        side = texts.FACT_SUMMARY_FAVOURITE.format(team=away)
    else:
        side = texts.FACT_SUMMARY_EVEN
    if outlook.over_2_5 >= MANY_GOALS_FROM:
        goals = texts.FACT_SUMMARY_GOALS["many"]
    elif outlook.over_2_5 < FEW_GOALS_UNDER:
        goals = texts.FACT_SUMMARY_GOALS["few"]
    else:
        goals = texts.FACT_SUMMARY_GOALS["middle"]
    return texts.FACT_SUMMARY.format(side=side, goals=goals)
