"""Pure HTML for recalculation diffs and the messages nobody asked for (notifier, F6.7).

Everything that can come from a user or a model (team names, notes, alert texts) is escaped
here; templates in :mod:`app.bot.texts` are trusted HTML.
"""

from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from app.bot import texts
from app.bot.formatters import (
    clock,
    escape,
    odds,
    percent,
    selection_label,
    short_date,
    signed_units,
    units,
    weekday_name,
)
from app.services.interfaces import (
    RECALC_COOLDOWN,
    AdminAlert,
    DailyDigest,
    DaySummary,
    RecalcBatch,
    RecalcDiff,
    SettledNotice,
    SignalCancelled,
    SignalUpdate,
    UpdateKind,
    WatchlistEvent,
)

MAX_ALERT_CHARS: Final = 1500
"""Admin alerts are cut so a runaway traceback cannot flood the chat."""
MAX_BATCH_DIFFS: Final = 10
"""Changed signals listed after a bulk recalculation; the rest is summarised."""
SECONDS_PER_MINUTE: Final = 60


def _arrow(before: str, after: str) -> str:
    return f"{before} → {after}"


def diff_lines(diff: RecalcDiff, zone: ZoneInfo, *, title: str = texts.RECALC_TITLE) -> list[str]:
    """Title, changed numbers and status of one recalculation (plan §6.4)."""
    before, after = diff.before, diff.after
    lines = [
        title.format(
            fixture=escape(diff.signal.fixture_label),
            time=clock(diff.recalculated_at.astimezone(zone).time()),
        ),
        texts.DIFF_ODDS_EV.format(
            odds_before=odds(before.odds),
            odds_after=odds(after.odds),
            ev_before=percent(before.ev, signed=True, decimals=1),
            ev_after=percent(after.ev, signed=True, decimals=1),
        ),
    ]
    extra = (
        (texts.DIFF_PROBABILITY, percent(before.probability), percent(after.probability)),
        (texts.DIFF_STAKE, units(before.stake_units), units(after.stake_units)),
        (
            texts.DIFF_CONFIDENCE,
            texts.CONFIDENCE_LABELS[before.confidence],
            texts.CONFIDENCE_LABELS[after.confidence],
        ),
    )
    lines.extend(
        template.format(before=old, after=new) for template, old, new in extra if old != new
    )
    if not diff.changed:
        lines.append(texts.DIFF_UNCHANGED)
    lines.append(texts.DIFF_STATUS.format(status=texts.VERDICT_LABELS[diff.verdict.value]))
    if diff.signal.data_as_of is not None:
        as_of = clock(diff.signal.data_as_of.astimezone(zone).time())
        lines.append(texts.DIFF_DATA_AS_OF.format(time=as_of))
    return lines


def format_recalc_diff(diff: RecalcDiff, zone: ZoneInfo) -> str:
    """The message a manual "🔄 Qayta hisoblash" produces."""
    return "\n".join(diff_lines(diff, zone))


def format_update(update: SignalUpdate, zone: ZoneInfo) -> str:
    """A scheduled 24h / pre-match recalculation."""
    title = (
        texts.UPDATE_TITLE_24H
        if update.kind is UpdateKind.BEFORE_24H
        else texts.UPDATE_TITLE_PREMATCH
    )
    lines = diff_lines(update.diff, zone, title=title)
    if update.lineups_confirmed:
        lines.append(texts.DIFF_LINEUPS)
    return "\n".join(lines)


def format_cancelled(cancelled: SignalCancelled) -> str:
    """A signal that cannot be played any more (postponed, cancelled, abandoned)."""
    signal = cancelled.signal
    return "\n".join(
        [
            texts.CANCELLED_TITLE.format(fixture=escape(signal.fixture_label)),
            texts.CANCELLED_BODY.format(
                reason=texts.CANCEL_REASONS[cancelled.reason.value],
                selection=selection_label(signal),
            ),
        ]
    )


def format_watch_event(event: WatchlistEvent, zone: ZoneInfo) -> str:
    """A watchlist notification: line-ups, odds move or result (plan §6.7)."""
    fixture = event.fixture
    lines = [
        texts.WATCH_HEADER.format(
            title=texts.WATCH_TITLES[event.kind.value],
            icon=texts.SPORT_ICONS[fixture.sport],
            fixture=escape(fixture.label),
            time=clock(fixture.kickoff.astimezone(zone).time()),
            league=escape(fixture.league),
        )
    ]
    if event.detail:
        lines.append(escape(event.detail))
    return "\n".join(lines)


def format_settled(notice: SettledNotice) -> str:
    """The settled signal with its result mark and the profit in units."""
    signal = notice.signal
    result = texts.RESULT_LABELS[signal.result] if signal.result is not None else "🏁"
    lines = [
        texts.SETTLED_HEADER.format(result=result, fixture=escape(signal.fixture_label)),
        texts.SETTLED_PICK.format(selection=selection_label(signal), odds=odds(signal.odds)),
    ]
    if notice.profit_units is not None:
        lines.append(texts.SETTLED_PROFIT.format(profit=signed_units(notice.profit_units)))
    return "\n".join(lines)


def format_admin_alert(alert: AdminAlert, zone: ZoneInfo) -> str:
    """A system error for the administrators; long messages are cut."""
    message = " ".join(alert.message.split())
    if len(message) > MAX_ALERT_CHARS:
        message = message[: MAX_ALERT_CHARS - 1] + "…"
    return texts.ADMIN_ALERT.format(
        component=escape(alert.component),
        time=_stamp(alert.at, zone),
        message=escape(message),
    )


def _stamp(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{short_date(local.date())} {clock(local.time())}"


def format_yesterday(summary: DaySummary) -> str:
    """Yesterday's tally, e.g. 6 signals, 4 won, 2 lost, +2.3 units (plan §6.13 A)."""
    extra = ""
    if summary.void:
        extra += texts.DIGEST_YESTERDAY_VOID.format(count=summary.void)
    if summary.pending:
        extra += texts.DIGEST_YESTERDAY_PENDING.format(count=summary.pending)
    clv = (
        ""
        if summary.clv is None
        else texts.DIGEST_YESTERDAY_CLV.format(clv=percent(summary.clv, signed=True, decimals=1))
    )
    return texts.DIGEST_YESTERDAY.format(
        signals=summary.signals,
        won=summary.won,
        lost=summary.lost,
        extra=extra,
        profit=signed_units(summary.profit_units),
        clv=clv,
    )


def format_digest_title(digest: DailyDigest) -> str:
    """Title and yesterday's tally of the morning message."""
    lines = [
        texts.DIGEST_TITLE.format(weekday=weekday_name(digest.day), day=short_date(digest.day))
    ]
    if digest.yesterday is not None:
        lines.append(format_yesterday(digest.yesterday))
    return "\n".join(lines)


def format_recalc_batch(batch: RecalcBatch, zone: ZoneInfo) -> str:
    """Summary of a bulk recalculation: counts, skipped reasons and the changed signals."""
    if not (batch.diffs or batch.skipped_cooldown or batch.skipped_started or batch.failed):
        return texts.RECALC_BATCH_EMPTY
    changed = [diff for diff in batch.diffs if diff.changed]
    lines = [texts.RECALC_BATCH_HEADER.format(updated=len(batch.diffs), changed=len(changed))]
    if batch.diffs and not changed:
        lines.append(texts.RECALC_BATCH_UNCHANGED)
    for diff in changed[:MAX_BATCH_DIFFS]:
        lines.append("")
        lines.extend(diff_lines(diff, zone))
    if len(changed) > MAX_BATCH_DIFFS:
        lines.append("")
        lines.append(texts.RECALC_BATCH_MORE.format(count=len(changed) - MAX_BATCH_DIFFS))
    lines.extend(_skipped_lines(batch))
    return "\n".join(lines)


def _skipped_lines(batch: RecalcBatch) -> list[str]:
    """Why some signals were not recalculated (each reason on its own line)."""
    minutes = int(RECALC_COOLDOWN.total_seconds() // SECONDS_PER_MINUTE)
    notes = (
        (batch.skipped_cooldown, texts.RECALC_BATCH_COOLDOWN),
        (batch.skipped_started, texts.RECALC_BATCH_STARTED),
        (batch.failed, texts.RECALC_BATCH_FAILED),
    )
    return [template.format(count=len(ids), minutes=minutes) for ids, template in notes if ids]
