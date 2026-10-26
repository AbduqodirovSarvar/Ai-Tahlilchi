"""Pure texts of the administrator's "🔄 Bazani yangilash" and of the stale-database notice.

Analysis reads only the database (decision #25), so every analysis path that hits
``DataStaleError`` tells the user when the data was last refreshed and offers the refresh
button right there. The button is shown to everyone on the whitelist: a user who is not an
administrator gets a clear refusal from the service instead of a missing feature.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.account_formatters import stamp
from app.bot.browse_ui import home_button, refresh_button
from app.bot.formatters import cooldown_minutes, escape
from app.bot.keyboards import markup_of
from app.services.errors import (
    BudgetExceededError,
    CooldownActiveError,
    NotAuthorizedError,
    ServiceError,
    ServiceUnavailableError,
)
from app.services.interfaces import RefreshReport


def format_refresh_report(report: RefreshReport) -> str:
    """What the refresh stored and cost, plus the steps that could not run."""
    lines = [
        texts.DB_REFRESH_DONE.format(
            fixtures=report.fixtures,
            results=report.results,
            odds=report.odds,
            requests=report.api_requests,
            seconds=round((report.finished_at - report.started_at).total_seconds()),
        )
    ]
    if report.skipped:
        steps = ", ".join(texts.DB_REFRESH_STEPS.get(step, escape(step)) for step in report.skipped)
        lines.append(texts.DB_REFRESH_PARTIAL.format(steps=steps))
    return "\n".join(lines)


def refresh_error_text(error: ServiceError) -> str:
    """The user message for a refresh the service refused or could not finish."""
    match error:
        case CooldownActiveError():
            return texts.DB_REFRESH_COOLDOWN.format(minutes=cooldown_minutes(error))
        case BudgetExceededError():
            return texts.DB_REFRESH_BUDGET.format(needed=error.needed, available=error.available)
        case ServiceUnavailableError():
            return texts.DB_REFRESH_UNAVAILABLE.format(component=escape(error.component))
        case NotAuthorizedError():
            return texts.NOT_AUTHORIZED
        case _:
            return texts.UNEXPECTED_ERROR


def format_data_stale(last_refresh_at: datetime | None, zone: ZoneInfo) -> str:
    """``Baza yangilanmagan (oxirgi: ...)`` with the last refresh in the user's zone."""
    if last_refresh_at is None:
        return texts.DATA_STALE_NEVER
    return texts.DATA_STALE.format(when=stamp(last_refresh_at, zone))


def stale_keyboard() -> InlineKeyboardMarkup:
    """The refresh button under a stale-database notice."""
    return markup_of([[refresh_button()]])


def refresh_done_keyboard() -> InlineKeyboardMarkup:
    """After a refresh (or a refusal): the way to the overview."""
    return markup_of([[home_button()]])
