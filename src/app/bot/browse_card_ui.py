"""Pure parts of the 🗄 Baza fixture card and the price history page.

The card shows what the database holds about one fixture: league, kick-off in the user's zone
and in UTC, status and result, the latest price of every bookmaker market with the time it was
taken, and the asking user's own signals. Provider text (team, league, bookmaker) is escaped.
"""

from collections.abc import Iterable
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.account_formatters import stamp
from app.bot.browse_nav import BrowseNav
from app.bot.browse_ui import home_button
from app.bot.formatters import clock, describe_selection, escape, odds, percent, selection_label
from app.bot.keyboards import markup_of, single_button
from app.bot.paging import Page, pager_row
from app.domain import DomainValidationError, Market, MarketType, Selection
from app.services.interfaces import (
    BookmakerOdds,
    StoredFixture,
    StoredFixtureDetail,
    StoredOdds,
    StoredOddsPage,
)

MAX_CARD_ODDS_PER_BOOKMAKER: Final = 10
"""Latest prices listed per bookmaker on the card; the history page has the rest."""
ODDS_PAGE_SIZE: Final = 15

_MARKET_ORDER: Final = {market: index for index, market in enumerate(MarketType)}
_SELECTION_ORDER: Final = {selection: index for index, selection in enumerate(Selection)}


def quote_label(quote: StoredOdds, fixture: StoredFixture) -> str:
    """The backed selection in words; unknown shapes fall back to the raw codes."""
    market_line = quote.line if quote.line else None
    try:
        return describe_selection(
            Market(quote.market, market_line),
            quote.selection,
            escape(fixture.home_team),
            escape(fixture.away_team),
        )
    except (DomainValidationError, KeyError):
        return escape(f"{quote.market.value} {quote.selection.value} {quote.line:g}")


def _kind(quote: StoredOdds) -> str:
    return texts.DB_ODDS_KINDS.get(quote.kind.value, escape(quote.kind.value))


def latest_quotes(quotes: Iterable[StoredOdds]) -> list[StoredOdds]:
    """The newest snapshot of every market/line/selection, in market order."""
    newest: dict[tuple[MarketType, float, Selection], StoredOdds] = {}
    for quote in quotes:
        key = (quote.market, quote.line, quote.selection)
        current = newest.get(key)
        if current is None or quote.taken_at >= current.taken_at:
            newest[key] = quote
    return sorted(
        newest.values(),
        key=lambda q: (_MARKET_ORDER[q.market], q.line, _SELECTION_ORDER[q.selection]),
    )


def _bookmaker_lines(book: BookmakerOdds, fixture: StoredFixture, zone: ZoneInfo) -> list[str]:
    quotes = latest_quotes(book.quotes)
    lines = [texts.DB_CARD_BOOKMAKER.format(bookmaker=escape(book.bookmaker))]
    lines.extend(
        texts.DB_CARD_ODDS_LINE.format(
            selection=quote_label(quote, fixture),
            odds=odds(quote.odds),
            time=stamp(quote.taken_at, zone),
            kind=_kind(quote),
        )
        for quote in quotes[:MAX_CARD_ODDS_PER_BOOKMAKER]
    )
    if len(quotes) > MAX_CARD_ODDS_PER_BOOKMAKER:
        lines.append(
            texts.DB_CARD_ODDS_MORE.format(count=len(quotes) - MAX_CARD_ODDS_PER_BOOKMAKER)
        )
    return lines


def _header_lines(detail: StoredFixtureDetail, zone: ZoneInfo) -> list[str]:
    fixture = detail.fixture
    local = fixture.kickoff.astimezone(zone)
    lines = [
        texts.DB_CARD_TITLE.format(label=escape(fixture.label)),
        texts.DB_CARD_LEAGUE.format(
            league=escape(fixture.league),
            country=texts.DB_COUNTRY.format(country=escape(fixture.country))
            if fixture.country
            else "",
        ),
        texts.DB_CARD_TIME.format(
            local=f"{local:%d.%m.%Y} {clock(local.time())}",
            zone=escape(zone.key),
            utc=f"{fixture.kickoff:%d.%m %H:%M}",
        ),
        texts.DB_CARD_STATE.format(state=texts.DB_STATES[fixture.status.value]),
    ]
    if fixture.home_score is not None and fixture.away_score is not None:
        half = (
            ""
            if detail.home_ht_score is None or detail.away_ht_score is None
            else texts.DB_CARD_HALF.format(home=detail.home_ht_score, away=detail.away_ht_score)
        )
        lines.append(
            texts.DB_CARD_SCORE.format(home=fixture.home_score, away=fixture.away_score, half=half)
        )
    if detail.home_xg is not None and detail.away_xg is not None:
        lines.append(
            texts.DB_CARD_XG.format(home=f"{detail.home_xg:.2f}", away=f"{detail.away_xg:.2f}")
        )
    lines.append(texts.DB_CARD_SOURCE.format(source=escape(fixture.source), season=detail.season))
    return lines


def _odds_lines(detail: StoredFixtureDetail, zone: ZoneInfo) -> list[str]:
    if not detail.odds:
        return [texts.DB_CARD_ODDS_NONE]
    lines = [texts.DB_CARD_ODDS_HEADER.format(total=detail.odds_total)]
    for book in detail.odds:
        lines.extend(_bookmaker_lines(book, detail.fixture, zone))
    if detail.truncated:
        lines.append(texts.DB_CARD_TRUNCATED.format(shown=sum(len(b.quotes) for b in detail.odds)))
    return lines


def _signal_lines(detail: StoredFixtureDetail) -> list[str]:
    if not detail.signals:
        return [texts.DB_CARD_SIGNALS_NONE]
    return [
        texts.DB_CARD_SIGNALS_HEADER,
        *(
            texts.DB_CARD_SIGNAL.format(
                selection=selection_label(signal),
                odds=odds(signal.odds),
                probability=percent(signal.probability),
                ev=percent(signal.ev, signed=True, decimals=1),
            )
            for signal in detail.signals
        ),
    ]


def format_fixture_card(detail: StoredFixtureDetail, zone: ZoneInfo) -> str:
    """Everything stored about one fixture (may exceed one message; the sender splits it)."""
    return "\n".join(
        [
            *_header_lines(detail, zone),
            "",
            *_odds_lines(detail, zone),
            "",
            *_signal_lines(detail),
        ]
    )


def card_keyboard(nav: BrowseNav) -> InlineKeyboardMarkup:
    """Price history, back to the list, back to the overview."""
    return markup_of(
        [
            [single_button(texts.BTN_DB_ODDS, nav.button("odds"))],
            [
                single_button(texts.BTN_DB_BACK_FIXTURES, nav.button("back")),
                home_button(),
            ],
        ]
    )


def format_odds_history(page: StoredOddsPage, fixture: StoredFixture, zone: ZoneInfo) -> str:
    """One page of price snapshots, newest first."""
    title = texts.DB_ODDS_TITLE.format(label=escape(fixture.label), total=page.total)
    if page.total == 0:
        return f"{title}\n\n{texts.DB_ODDS_EMPTY}"
    lines = [
        texts.DB_ODDS_LINE.format(
            time=stamp(quote.taken_at, zone),
            bookmaker=escape(quote.bookmaker),
            selection=quote_label(quote, fixture),
            odds=odds(quote.odds),
            kind=_kind(quote),
        )
        for quote in page.items
    ]
    return "\n".join([title, "", *lines])


def odds_keyboard(page: StoredOddsPage, nav: BrowseNav) -> InlineKeyboardMarkup:
    """The pager and the way back to the card."""
    rows = []
    pager = pager_row(
        Page((), page.page, max(page.page_count, 1)),
        lambda n: nav.button("opage", n),
    )
    if pager:
        rows.append(pager)
    rows.append([single_button(texts.BTN_DB_BACK_CARD, nav.button("back"))])
    return markup_of(rows)
