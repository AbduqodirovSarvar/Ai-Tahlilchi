"""Pure HTML for ``/holat`` and the minimal bets view (F6.9, bets wiring of the cards)."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from app.bot import texts
from app.bot.formatters import (
    clock,
    describe_selection,
    escape,
    money,
    odds,
    selection_label,
    short_date,
    sport_label,
    units,
)
from app.services.interfaces import (
    ApiQuota,
    BankView,
    BetView,
    ErrorEntry,
    JobStatus,
    SignalView,
    SystemStatus,
    UserSettingsView,
)

MAX_STATUS_ERRORS: Final = 5
MAX_STATUS_JOBS: Final = 12
MAX_DETAIL_CHARS: Final = 80
MAX_ERROR_CHARS: Final = 160
MAX_LISTED_BETS: Final = 15
SECONDS_PER_MINUTE: Final = 60
MINUTES_PER_HOUR: Final = 60
HOURS_PER_DAY: Final = 24


# --------------------------------------------------------------------------- /holat
def age_label(moment: datetime, now: datetime) -> str:
    """Age as ``12 daq. oldin`` / ``3 soat oldin`` / ``2 kun oldin`` (the future counts as now)."""
    minutes = int(max(now - moment, timedelta(0)).total_seconds() // SECONDS_PER_MINUTE)
    if minutes < 1:
        return texts.STATUS_AGE_NOW
    if minutes < MINUTES_PER_HOUR:
        return texts.STATUS_AGE_MINUTES.format(minutes=minutes)
    hours = minutes // MINUTES_PER_HOUR
    if hours < HOURS_PER_DAY:
        return texts.STATUS_AGE_HOURS.format(hours=hours)
    return texts.STATUS_AGE_DAYS.format(days=hours // HOURS_PER_DAY)


def _stamp(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{short_date(local.date())} {clock(local.time())}"


def _cut(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _quota_line(quota: ApiQuota) -> str:
    sport = "" if quota.sport is None else f" ({sport_label(quota.sport)})"
    return texts.STATUS_QUOTA_LINE.format(
        provider=escape(quota.provider),
        sport=sport,
        used=quota.used,
        limit=quota.limit,
        remaining=quota.remaining,
        manual_used=quota.manual_used,
        manual_reserve=quota.manual_reserve,
        manual_left=quota.manual_left,
    )


def _job_line(job: JobStatus, zone: ZoneInfo) -> str:
    when = texts.STATUS_JOB_NEVER if job.last_run_at is None else _stamp(job.last_run_at, zone)
    detail = f" ({escape(_cut(job.detail, MAX_DETAIL_CHARS))})" if job.detail else ""
    return texts.STATUS_JOB_LINE.format(
        mark="✅" if job.ok else "❌", name=escape(job.name), time=when, detail=detail
    )


def _error_line(entry: ErrorEntry, zone: ZoneInfo) -> str:
    return texts.STATUS_ERROR_LINE.format(
        time=_stamp(entry.at, zone),
        component=escape(entry.component),
        message=escape(_cut(entry.message, MAX_ERROR_CHARS)),
    )


def _quota_block(status: SystemStatus) -> list[str]:
    if not status.quotas:
        return [texts.STATUS_QUOTA_NONE]
    return [texts.STATUS_QUOTA_HEADER, *(_quota_line(quota) for quota in status.quotas)]


def _llm_block(status: SystemStatus) -> list[str]:
    llm = status.llm
    if llm is None:
        return [texts.STATUS_LLM_NONE]
    return [
        texts.STATUS_LLM.format(
            requests_used=llm.requests_used,
            requests_limit=llm.requests_limit,
            tokens_used=llm.tokens_used,
            tokens_limit=llm.tokens_limit,
        )
    ]


def _refresh_block(status: SystemStatus, zone: ZoneInfo, now: datetime) -> list[str]:
    if status.last_refresh_at is None:
        return [texts.STATUS_REFRESH_NEVER]
    return [
        texts.STATUS_REFRESH.format(
            time=_stamp(status.last_refresh_at, zone), age=age_label(status.last_refresh_at, now)
        )
    ]


def _jobs_block(status: SystemStatus, zone: ZoneInfo) -> list[str]:
    if not status.jobs:
        return [texts.STATUS_JOBS_NONE]
    shown = status.jobs[:MAX_STATUS_JOBS]
    lines = [texts.STATUS_JOBS_HEADER, *(_job_line(job, zone) for job in shown)]
    if len(status.jobs) > len(shown):
        lines.append(texts.STATUS_MORE.format(count=len(status.jobs) - len(shown)))
    return lines


def _learning_block(status: SystemStatus, zone: ZoneInfo) -> list[str]:
    """Learning mode, the last runs of its jobs and the "missed week" warning (F8.8)."""
    if status.learning_mode is None:
        return []
    lines = [texts.STATUS_LEARNING_HEADER, texts.STATUS_LEARNING_MODE[status.learning_mode.value]]
    runs = {job.name: job for job in status.jobs}
    for name, label in texts.STATUS_LEARNING_JOBS.items():
        job = runs.get(name)
        when = (
            texts.STATUS_JOB_NEVER
            if job is None or job.last_run_at is None
            else _stamp(job.last_run_at, zone)
        )
        mark = "" if job is None else ("✅ " if job.ok else "❌ ")
        lines.append(texts.STATUS_LEARNING_JOB_LINE.format(mark=mark, name=label, time=when))
    if status.learning_missed:
        lines.append(texts.STATUS_LEARNING_MISSED)
    return lines


def _errors_block(status: SystemStatus, zone: ZoneInfo) -> list[str]:
    if not status.recent_errors:
        return [texts.STATUS_ERRORS_NONE]
    shown = status.recent_errors[:MAX_STATUS_ERRORS]
    lines = [texts.STATUS_ERRORS_HEADER, *(_error_line(entry, zone) for entry in shown)]
    if len(status.recent_errors) > len(shown):
        lines.append(texts.STATUS_MORE.format(count=len(status.recent_errors) - len(shown)))
    return lines


def format_status(status: SystemStatus, zone: ZoneInfo, now: datetime, backend: str) -> str:
    """The ``/holat`` message: mode, API quota, data freshness, jobs and recent errors."""
    blocks = [
        [
            texts.STATUS_TITLE,
            texts.STATUS_MODE.get(backend, escape(backend)),
            *([texts.STATUS_BUILD.format(build=escape(status.build))] if status.build else []),
        ],
        _quota_block(status),
        _llm_block(status),
        _refresh_block(status, zone, now),
        _jobs_block(status, zone),
        _learning_block(status, zone),
        _errors_block(status, zone),
        [texts.STATUS_FOOTER],
    ]
    return "\n\n".join("\n".join(block) for block in blocks if block)


# --------------------------------------------------------------------------- bets
def default_stake(settings: UserSettingsView, signal: SignalView) -> Decimal | None:
    """The recommended stake in money, or ``None`` when no unit is set / none is advised."""
    if settings.unit is None or signal.stake_units <= 0:
        return None
    return settings.unit * signal.stake_units


def stake_label(settings: UserSettingsView, signal: SignalView) -> str:
    """Recommended stake as money plus units, or units alone without a configured unit."""
    unit_text = texts.BET_STAKE_UNITS.format(units=units(signal.stake_units))
    amount = default_stake(settings, signal)
    if amount is None:
        return unit_text
    return texts.BET_STAKE_MONEY.format(amount=money(amount), units=units(signal.stake_units))


def format_bet_prompt(signal: SignalView, settings: UserSettingsView) -> str:
    """What the "✅ Stavka qildim" prompt shows before anything is recorded."""
    return texts.BET_PROMPT.format(
        fixture=escape(signal.fixture_label),
        selection=selection_label(signal),
        odds=odds(signal.odds),
        stake=stake_label(settings, signal),
    )


def format_bet_placed(bet: BetView) -> str:
    """Confirmation of a recorded bet."""
    return texts.BET_PLACED.format(
        fixture=escape(bet.fixture_label),
        selection=_bet_selection(bet),
        odds=odds(bet.odds_taken),
        stake=money(bet.stake),
    )


def _bet_selection(bet: BetView) -> str:
    return describe_selection(bet.market, bet.selection, texts.BET_SIDE_HOME, texts.BET_SIDE_AWAY)


def _bank_lines(bank: BankView) -> list[str]:
    lines = [
        texts.BETS_BALANCE.format(balance=money(bank.balance))
        if bank.balance is not None
        else texts.BETS_BANK_UNSET
    ]
    if bank.balance is not None:
        lines.append(texts.BETS_MONTH_PROFIT.format(profit=money(bank.month_profit)))
    lines.append(texts.BETS_OPEN_COUNT.format(count=bank.open_bets))
    if bank.loss_limit_reached:
        lines.append(
            texts.BETS_LOSS_LIMIT.format(
                today=money(bank.loss_today), week=money(bank.loss_this_week)
            )
        )
    return lines


def bet_lines(bets: tuple[BetView, ...] | list[BetView], zone: ZoneInfo) -> list[str]:
    """One line per bet (newest first), capped at :data:`MAX_LISTED_BETS` with a tail."""
    lines = [
        texts.BETS_LINE.format(
            fixture=escape(bet.fixture_label),
            selection=_bet_selection(bet),
            odds=odds(bet.odds_taken),
            stake=money(bet.stake),
            date=short_date(bet.placed_at.astimezone(zone).date()),
        )
        for bet in bets[:MAX_LISTED_BETS]
    ]
    if len(bets) > MAX_LISTED_BETS:
        lines.append(texts.BETS_MORE.format(count=len(bets) - MAX_LISTED_BETS))
    return lines


def format_bets(bank: BankView, bets: tuple[BetView, ...], zone: ZoneInfo) -> str:
    """The bets view: the bank snapshot and the open bets (newest first)."""
    lines = [texts.BETS_TITLE, *_bank_lines(bank), ""]
    if not bets:
        lines.append(texts.BETS_EMPTY)
        return "\n".join(lines)
    lines.extend(bet_lines(bets, zone))
    return "\n".join(lines)
