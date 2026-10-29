"""Pure HTML for the detail card "📋 Batafsil" (plan §6.8, F7.2).

Every team, player and league name is escaped here. The card is edited into the signal's
message, so it must fit one Telegram message: each list is capped and, if the whole card is
still too long, the least important sections (odds movement, the markets table) shrink first.
"""

from typing import Final
from zoneinfo import ZoneInfo

from app.bot import texts
from app.bot.formatters import (
    TELEGRAM_TEXT_LIMIT,
    clock,
    escape,
    kickoff_stamp,
    odds,
    percent,
    short_date,
)
from app.domain import Market, MarketType, Selection
from app.services.interfaces import (
    Absence,
    FixtureDetail,
    FormResult,
    HeadToHead,
    MarketQuote,
    OddsMove,
    TeamSide,
    TeamStats,
)

MAX_H2H: Final = 5
MAX_ABSENCES: Final = 8
MAX_MOVES: Final = 8
MAX_MARKET_ROWS: Final = 24
LABEL_WIDTH: Final = 20
SAFETY_MARGIN: Final = 200
"""Room kept below the Telegram limit for the keyboard-less edit and the header."""
MIN_MARKET_ROWS: Final = 4
DROP_ORDER: Final = (7, 3, 4, 8, 5, 2)
"""Block indexes (see :func:`_blocks`) dropped one by one when the card is still too long:
odds movement, head to head, absences, footer, top scores, statistics."""


def form_line(results: tuple[FormResult, ...]) -> str:
    """Win/draw/loss symbols, oldest first, or "ma'lumot yo'q" for a team without history."""
    return "".join(texts.FORM_MARKS[result] for result in results) or texts.DETAIL_NO_DATA


def short_selection(market: Market, selection: Selection) -> str:
    """Compact selection name for the monospace table (roles, not team names)."""
    match market.type:
        case MarketType.MATCH_RESULT:
            return {
                Selection.HOME: texts.SHORT_HOME,
                Selection.DRAW: texts.SHORT_DRAW,
                Selection.AWAY: texts.SHORT_AWAY,
            }[selection]
        case MarketType.DOUBLE_CHANCE:
            return texts.SHORT_DOUBLE[selection]
        case MarketType.TOTALS:
            template = texts.SHORT_OVER if selection is Selection.OVER else texts.SHORT_UNDER
            return template.format(line=f"{market.line:g}")
        case MarketType.BTTS:
            return texts.SHORT_BTTS_YES if selection is Selection.YES else texts.SHORT_BTTS_NO
        case MarketType.ASIAN_HANDICAP:
            line = market.line or 0.0
            home = selection is Selection.HOME
            return texts.SHORT_HANDICAP.format(
                side=texts.SHORT_HOME if home else texts.SHORT_AWAY,
                line=f"{line if home else -line:+g}",
            )


def _market_label(quote: MarketQuote | OddsMove) -> str:
    """``1X2 · 1`` — the market name and the compact selection."""
    name = texts.MARKET_LABELS[quote.market.type]
    return f"{name} · {short_selection(quote.market, quote.selection)}"


def market_row(quote: MarketQuote) -> str:
    """One line of the all-markets table (already escaped: no user text can enter it)."""
    price = texts.DETAIL_NO_PRICE if quote.odds is None else odds(quote.odds)
    ev = texts.DETAIL_NO_PRICE if quote.ev is None else percent(quote.ev, signed=True, decimals=1)
    label = _market_label(quote)[:LABEL_WIDTH]
    return (
        f"{escape(label):<{LABEL_WIDTH}} {percent(quote.probability):>5} "
        f"{odds(quote.fair_odds):>8} {price:>6} {ev:>7}"
    )


def _markets_block(quotes: tuple[MarketQuote, ...], limit: int) -> list[str]:
    if not quotes:
        return [texts.DETAIL_MARKETS_EMPTY]
    shown = quotes[:limit]
    rows = [texts.DETAIL_MARKETS_COLUMNS, *(market_row(quote) for quote in shown)]
    lines = [texts.DETAIL_MARKETS_HEADER, "<pre>" + "\n".join(rows) + "</pre>"]
    if len(quotes) > len(shown):
        lines.append(texts.DETAIL_MORE.format(count=len(quotes) - len(shown)))
    return lines


def _stats_line(team: str, role: str, stats: TeamStats) -> str:
    xg = ""
    if stats.xg_for_avg is not None and stats.xg_against_avg is not None:
        xg = texts.DETAIL_STATS_XG.format(
            xg_for=f"{stats.xg_for_avg:.2f}", xg_against=f"{stats.xg_against_avg:.2f}"
        )
    return texts.DETAIL_STATS_LINE.format(
        team=escape(team),
        role=role,
        matches=stats.matches,
        gf=f"{stats.goals_for_avg:.2f}",
        ga=f"{stats.goals_against_avg:.2f}",
        xg=xg,
    )


def _h2h_line(meeting: HeadToHead, zone: ZoneInfo) -> str:
    return texts.DETAIL_H2H_LINE.format(
        date=short_date(meeting.kickoff.astimezone(zone).date())
        + f".{meeting.kickoff.astimezone(zone).year}",
        home=escape(meeting.home_team),
        home_score=meeting.home_score,
        away_score=meeting.away_score,
        away=escape(meeting.away_team),
    )


def _absence_line(absence: Absence, detail: FixtureDetail) -> str:
    team = detail.home_team if absence.side is TeamSide.HOME else detail.away_team
    return texts.DETAIL_ABSENCE_LINE.format(
        team=escape(team), player=escape(absence.player), reason=escape(absence.reason)
    )


def _move_line(move: OddsMove) -> str:
    return texts.DETAIL_MOVE_LINE.format(
        selection=escape(_market_label(move)),
        opening=odds(move.opening),
        current=odds(move.current),
        change=percent(move.change, signed=True, decimals=1),
    )


def _capped(lines: list[str], limit: int) -> list[str]:
    """``lines`` cut to ``limit`` with a "…va yana N ta" tail."""
    if len(lines) <= limit:
        return lines
    return [*lines[:limit], texts.DETAIL_MORE.format(count=len(lines) - limit)]


def _header(detail: FixtureDetail, zone: ZoneInfo) -> list[str]:
    lines = [
        texts.DETAIL_TITLE.format(
            fixture=escape(f"{detail.home_team} — {detail.away_team}"),
            league=escape(detail.league),
            time=kickoff_stamp(detail.kickoff, zone),
        )
    ]
    if detail.home_score is not None and detail.away_score is not None:
        lines.append(texts.DETAIL_SCORE.format(home=detail.home_score, away=detail.away_score))
    else:
        lines.append(
            texts.DETAIL_LINEUPS_CONFIRMED
            if detail.lineups_confirmed
            else texts.DETAIL_LINEUPS_UNKNOWN
        )
    return lines


def _form_block(detail: FixtureDetail) -> list[str]:
    return [
        texts.DETAIL_FORM_HEADER,
        texts.DETAIL_FORM_LINE.format(
            team=escape(detail.home_team), form=form_line(detail.home_form)
        ),
        texts.DETAIL_FORM_LINE.format(
            team=escape(detail.away_team), form=form_line(detail.away_form)
        ),
    ]


def _stats_block(detail: FixtureDetail) -> list[str]:
    return [
        texts.DETAIL_STATS_HEADER,
        _stats_line(detail.home_team, texts.ROLE_HOME, detail.home_stats),
        _stats_line(detail.away_team, texts.ROLE_AWAY, detail.away_stats),
    ]


def _h2h_block(detail: FixtureDetail, zone: ZoneInfo) -> list[str]:
    if not detail.head_to_head:
        return [texts.DETAIL_H2H_EMPTY]
    meetings = [_h2h_line(meeting, zone) for meeting in detail.head_to_head]
    return [texts.DETAIL_H2H_HEADER, *_capped(meetings, MAX_H2H)]


def _absences_block(detail: FixtureDetail) -> list[str]:
    if not detail.absences:
        return [texts.DETAIL_ABSENCES_NONE]
    lines = [_absence_line(absence, detail) for absence in detail.absences]
    return [texts.DETAIL_ABSENCES_HEADER, *_capped(lines, MAX_ABSENCES)]


def _scores_block(detail: FixtureDetail) -> list[str]:
    if not detail.top_scores:
        return []
    items = [
        texts.DETAIL_SCORE_ITEM.format(
            home=score.home_goals, away=score.away_goals, probability=percent(score.probability)
        )
        for score in detail.top_scores
    ]
    return [texts.DETAIL_SCORES_HEADER, "  ·  ".join(items)]


def _moves_block(detail: FixtureDetail) -> list[str]:
    if not detail.odds_moves:
        return []
    moves = [_move_line(move) for move in detail.odds_moves]
    return [texts.DETAIL_MOVES_HEADER, *_capped(moves, MAX_MOVES)]


def _blocks(detail: FixtureDetail, zone: ZoneInfo, market_rows: int) -> list[list[str]]:
    footer = (
        []
        if detail.data_as_of is None
        else [
            texts.DETAIL_AS_OF.format(
                time=f"{short_date(detail.data_as_of.astimezone(zone).date())} "
                f"{clock(detail.data_as_of.astimezone(zone).time())}"
            )
        ]
    )
    return [
        _header(detail, zone),
        _form_block(detail),
        _stats_block(detail),
        _h2h_block(detail, zone),
        _absences_block(detail),
        _scores_block(detail),
        _markets_block(detail.markets, market_rows),
        _moves_block(detail),
        footer,
    ]


def _render(blocks: list[list[str]]) -> str:
    return "\n\n".join("\n".join(block) for block in blocks if block)


def format_fixture_detail(detail: FixtureDetail, zone: ZoneInfo) -> str:
    """The detail card; it always fits one message.

    Too long? The markets table loses rows first, then whole optional sections go in the
    order of :data:`DROP_ORDER`; the header and the form always stay.
    """
    budget = TELEGRAM_TEXT_LIMIT - SAFETY_MARGIN
    for rows in range(MAX_MARKET_ROWS, MIN_MARKET_ROWS - 1, -1):
        text = _render(_blocks(detail, zone, rows))
        if len(text) <= budget:
            return text
    blocks = _blocks(detail, zone, MIN_MARKET_ROWS)
    for index in DROP_ORDER:
        blocks[index] = []
        text = _render(blocks)
        if len(text) <= budget:
            return text
    # Only names of absurd length get here; a plain cut is the lesser evil.
    return text[:TELEGRAM_TEXT_LIMIT]
