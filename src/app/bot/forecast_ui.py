"""📊 Prognoz screens: model-only forecasts of a day's matches and of one match (no odds).

Probabilities come from the service; the fair odds are ``1 / probability``. Every provider
name is escaped; kick-offs are shown in the user's zone (``30.10.2026 21:00``).
"""

from collections.abc import Sequence
from datetime import date
from html import escape
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import MatchCb
from app.bot.formatters import kickoff_stamp, odds, percent, weekday_name
from app.bot.keyboards import markup_of, single_button
from app.bot.matches_ui import MATCHES_PAGE_SIZE, page_count, page_row
from app.domain import Market, Selection
from app.services.interfaces import ForecastLine, ForecastStatus, MatchForecast

GOAL_LINES: Final = (1.5, 2.5, 3.5)
_RESULT: Final = Market.match_result()
_BTTS: Final = Market.btts()
_NO_VALUE: Final = "—"


def _team(name: str | None) -> str:
    return escape(name) if name else texts.MATCHES_TBD


def _chance(forecast: MatchForecast, market: Market, selection: Selection) -> str:
    chance = forecast.chance(market, selection)
    return _NO_VALUE if chance is None else percent(chance.probability)


def _fair(forecast: MatchForecast, market: Market, selection: Selection) -> str:
    chance = forecast.chance(market, selection)
    fair = None if chance is None else chance.fair_odds
    return _NO_VALUE if fair is None else odds(fair)


def _confidence_suffix(forecast: MatchForecast) -> str:
    if forecast.confidence is None:
        return ""
    return texts.FORECAST_DAY_CONFIDENCE.format(level=texts.CONFIDENCE_LABELS[forecast.confidence])


def _day_lines(number: int, line: ForecastLine, zone: ZoneInfo) -> list[str]:
    match, forecast = line.match, line.forecast
    lines = [
        texts.FORECAST_DAY_LINE.format(
            n=number,
            kickoff=kickoff_stamp(match.kickoff, zone),
            home=_team(match.home),
            away=_team(match.away),
        )
    ]
    favourite = forecast.favourite
    if forecast.status is not ForecastStatus.READY or favourite is None:
        reason = texts.FORECAST_REASONS[forecast.status.value]
        return [*lines, texts.FORECAST_SKIPPED.format(reason=reason)]
    lines.append(
        texts.FORECAST_DAY_RESULT.format(
            home=_chance(forecast, _RESULT, Selection.HOME),
            draw=_chance(forecast, _RESULT, Selection.DRAW),
            away=_chance(forecast, _RESULT, Selection.AWAY),
            favourite=texts.FORECAST_FAVOURITES[favourite.selection.value],
            confidence=_confidence_suffix(forecast),
        )
    )
    lines.append(
        texts.FORECAST_DAY_EXTRA.format(
            over=_chance(forecast, Market.totals(2.5), Selection.OVER),
            btts=_chance(forecast, _BTTS, Selection.YES),
        )
    )
    return lines


def format_day_forecast(day: date, lines: Sequence[ForecastLine], page: int, zone: ZoneInfo) -> str:
    """One page of the day's forecasts, numbered like the match list."""
    text = [
        texts.FORECAST_DAY_TITLE.format(
            day=day.strftime("%d.%m.%Y"), weekday=weekday_name(day), total=len(lines)
        ),
        texts.FORECAST_NOTE,
    ]
    if not lines:
        return "\n".join([*text, "", texts.MATCHES_EMPTY])
    start = page * MATCHES_PAGE_SIZE
    for number, line in enumerate(lines[start : start + MATCHES_PAGE_SIZE], start=start + 1):
        text.extend(["", *_day_lines(number, line, zone)])
    pages = page_count(len(lines))
    if pages > 1:
        text.extend(["", texts.MATCHES_PAGE.format(page=page + 1, pages=pages)])
    return "\n".join(text)


def day_forecast_keyboard(day: date, page: int, total: int) -> InlineKeyboardMarkup:
    """Pages of the forecast and the way back to the day's matches."""
    paging = page_row(day, page, total, action="fday")
    back = [single_button(texts.BTN_FORECAST_BACK, MatchCb(action="day", d=day.toordinal()))]
    return markup_of([row for row in (paging, back) if row])


def _result_lines(forecast: MatchForecast) -> list[str]:
    lines = [texts.FORECAST_SECTION_RESULT]
    for selection in _RESULT.selections:
        chance = forecast.chance(_RESULT, selection)
        if chance is None:
            continue
        fair = chance.fair_odds
        lines.append(
            texts.FORECAST_RESULT_LINE.format(
                label=texts.FORECAST_RESULT_LABELS[selection.value],
                probability=percent(chance.probability),
                fair=_NO_VALUE if fair is None else odds(fair),
            )
        )
    return lines


def _goal_lines(forecast: MatchForecast) -> list[str]:
    lines = [texts.FORECAST_SECTION_GOALS]
    for line in GOAL_LINES:
        market = Market.totals(line)
        if forecast.chance(market, Selection.OVER) is None:
            continue
        lines.append(
            texts.FORECAST_GOALS_LINE.format(
                line=line,
                over=_chance(forecast, market, Selection.OVER),
                over_fair=_fair(forecast, market, Selection.OVER),
                under=_chance(forecast, market, Selection.UNDER),
                under_fair=_fair(forecast, market, Selection.UNDER),
            )
        )
    return lines if len(lines) > 1 else []


def _other_lines(forecast: MatchForecast) -> list[str]:
    lines: list[str] = []
    double = Market.double_chance()
    if forecast.chance(double, Selection.HOME_OR_DRAW) is not None:
        lines.append(
            texts.FORECAST_SECTION_DOUBLE.format(
                home_draw=_chance(forecast, double, Selection.HOME_OR_DRAW),
                draw_away=_chance(forecast, double, Selection.DRAW_OR_AWAY),
                home_away=_chance(forecast, double, Selection.HOME_OR_AWAY),
            )
        )
    if forecast.chance(_BTTS, Selection.YES) is not None:
        lines.append(
            texts.FORECAST_SECTION_BTTS.format(
                yes=_chance(forecast, _BTTS, Selection.YES),
                yes_fair=_fair(forecast, _BTTS, Selection.YES),
                no=_chance(forecast, _BTTS, Selection.NO),
                no_fair=_fair(forecast, _BTTS, Selection.NO),
            )
        )
    if forecast.scores:
        scores = " · ".join(
            texts.FORECAST_SCORE.format(
                home=s.home_goals, away=s.away_goals, probability=percent(s.probability)
            )
            for s in forecast.scores
        )
        lines.append(texts.FORECAST_SECTION_SCORES.format(scores=scores))
    return lines


def format_match_forecast(line: ForecastLine, zone: ZoneInfo) -> str:
    """Every forecast market of one match with fair odds, likely scores and the trust level."""
    match, forecast = line.match, line.forecast
    text = [
        texts.FORECAST_MATCH_TITLE.format(home=_team(match.home), away=_team(match.away)),
        texts.FORECAST_MATCH_META.format(
            kickoff=kickoff_stamp(match.kickoff, zone), competition=escape(match.competition)
        ),
    ]
    if forecast.status is not ForecastStatus.READY:
        reason = texts.FORECAST_REASONS[forecast.status.value]
        return "\n".join([*text, "", texts.FORECAST_SKIPPED.format(reason=reason).strip()])
    sections = [_result_lines(forecast), _goal_lines(forecast), _other_lines(forecast)]
    for section in sections:
        if section:
            text.extend(["", *section])
    if forecast.confidence is not None:
        text.extend(
            [
                "",
                texts.FORECAST_CONFIDENCE.format(
                    level=texts.CONFIDENCE_LABELS[forecast.confidence],
                    matches=forecast.team_matches,
                ),
            ]
        )
    return "\n".join([*text, "", texts.FORECAST_NOTE])


def match_forecast_keyboard(day_ordinal: int, page: int, match_id: int) -> InlineKeyboardMarkup:
    """Back to the match sheet and to the list."""
    return markup_of(
        [
            [
                single_button(texts.BTN_MATCHES_BACK, MatchCb(action="day", d=day_ordinal, p=page)),
                single_button(
                    texts.BTN_FORECAST_TO_SHEET,
                    MatchCb(action="item", d=day_ordinal, p=page, i=match_id),
                ),
            ]
        ]
    )
