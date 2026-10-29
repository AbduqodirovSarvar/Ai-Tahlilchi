"""🎯 Eng ehtimolli tanlovlar: the most likely picks of some days, ranked without any odds.

The service ranks them (:func:`app.services.interfaces.rank_picks`); this module only words
them. Provider names are escaped; kick-offs are in the user's zone.
"""

from collections.abc import Sequence
from datetime import date
from html import escape
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import MatchCb
from app.bot.formatters import kickoff_stamp, odds, percent
from app.bot.keyboards import markup_of, single_button
from app.domain import MarketType
from app.services.interfaces import CardChance, CardScope, MatchItem, OutcomeChance, TopPick

_NO_VALUE = "—"


def _team(name: str | None) -> str:
    return escape(name) if name else texts.MATCHES_TBD


def _outcome_label(outcome: OutcomeChance, match: MatchItem) -> str:
    names = {"home": _team(match.home), "away": _team(match.away)}
    selection = outcome.selection.value
    match outcome.market.type:
        case MarketType.MATCH_RESULT:
            return texts.PICK_RESULT_LABELS[selection].format(**names)
        case MarketType.DOUBLE_CHANCE:
            return texts.PICK_DOUBLE_LABELS[selection].format(**names)
        case MarketType.TOTALS:
            return texts.PICK_TOTALS_LABELS[selection].format(line=outcome.market.line)
        case MarketType.BTTS:
            return texts.PICK_BTTS_LABELS[selection]
        case _:
            return escape(f"{outcome.market.type.value} {selection}")


def card_label(card: CardChance, match: MatchItem) -> str:
    """``Sariq kartochkalar jami 4.5 dan ko'p`` / ``Chelsea 1.5 dan ko'p sariq kartochka oladi``."""
    if card.scope is CardScope.TOTAL:
        return texts.PICK_CARD_TOTAL[card.over].format(line=card.line)
    team = match.home if card.scope is CardScope.HOME else match.away
    return texts.PICK_CARD_TEAM[card.over].format(team=_team(team), line=card.line)


def pick_label(pick: TopPick) -> str:
    """``1X — Arsenal yutqazmaydi`` / ``Total 2.5 dan kam`` / ``Chelsea gol uradi``."""
    if pick.outcome is not None:
        return _outcome_label(pick.outcome, pick.match)
    if pick.card is not None:
        return card_label(pick.card, pick.match)
    if pick.team is None:  # pragma: no cover - TopPick guarantees exactly one
        return _NO_VALUE
    names = {"home": _team(pick.match.home), "away": _team(pick.match.away)}
    return texts.PICK_TEAM_LABELS[pick.team.pick.value].format(**names)


def _fair(pick: TopPick) -> str:
    fair = pick.fair_odds
    return _NO_VALUE if fair is None else odds(fair)


def _pick_lines(number: int, pick: TopPick, zone: ZoneInfo) -> list[str]:
    confidence = (
        ""
        if pick.confidence is None
        else texts.FORECAST_DAY_CONFIDENCE.format(level=texts.CONFIDENCE_LABELS[pick.confidence])
    )
    match = pick.match
    return [
        texts.TOP_PICK_LINE.format(
            n=number,
            probability=percent(pick.probability),
            label=pick_label(pick),
            fair=_fair(pick),
            confidence=confidence,
        ),
        texts.TOP_PICK_MATCH.format(
            kickoff=kickoff_stamp(match.kickoff, zone),
            home=_team(match.home),
            away=_team(match.away),
            competition=escape(match.competition),
        ),
    ]


def window_text(day_from: date, day_to: date) -> str:
    """``30.10.2026`` or ``30.10.2026 — 01.11.2026``."""
    start = day_from.strftime("%d.%m.%Y")
    return start if day_from == day_to else f"{start} — {day_to:%d.%m.%Y}"


def format_top_picks(
    picks: Sequence[TopPick],
    *,
    day_from: date,
    day_to: date,
    min_probability: float,
    zone: ZoneInfo,
) -> str:
    """The ranked picks, most likely first, with fair odds and the trust level."""
    floor = percent(min_probability)
    lines = [
        texts.TOP_PICKS_TITLE.format(window=window_text(day_from, day_to), count=len(picks)),
        texts.TOP_PICKS_NOTE.format(min_probability=floor),
    ]
    if not picks:
        return "\n".join([*lines, "", texts.TOP_PICKS_EMPTY.format(min_probability=floor)])
    for number, pick in enumerate(picks, start=1):
        lines.extend(["", *_pick_lines(number, pick, zone)])
    return "\n".join(lines)


def top_picks_keyboard(day_from: date) -> InlineKeyboardMarkup:
    """Back to the matches of the first day."""
    back = MatchCb(action="day", d=day_from.toordinal())
    return markup_of([[single_button(texts.BTN_FORECAST_BACK, back)]])
