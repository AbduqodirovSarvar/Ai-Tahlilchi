"""Real signal, result, bet and report services (everything that reads stored signals)."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final

from app.domain import FixtureStatus
from app.pipeline import data
from app.pipeline.profile import utc_bounds
from app.pipeline.settle import settle_manually
from app.services.errors import (
    NotFoundError,
    ValidationFailedError,
)
from app.services.interfaces import (
    MAX_NOTE_LENGTH,
    BankPoint,
    BankView,
    BetDraft,
    BetId,
    BetView,
    DateSpec,
    DaySummary,
    ExportFile,
    ReportRequest,
    ReportView,
    ResultMark,
    ResultMarkRequest,
    RiskProfile,
    SignalId,
    SignalView,
    infer_risk_profile,
)
from app.services.real.analysis import window_of
from app.services.real.core import Core, money, require_signal, to_view
from app.services.report_math import SignalOutcome, build_report, day_summary, export_file

MAX_BET_LIST: Final = 100
ALL_BETS: Final = 100_000
_MANUAL_OUTCOMES: Final = {
    ResultMark.WON: data.Outcome.WIN,
    ResultMark.LOST: data.Outcome.LOSS,
    ResultMark.VOID: data.Outcome.VOID,
    ResultMark.CASHOUT: data.Outcome.CASHOUT,
}


class RealSignalService:
    """:class:`~app.services.interfaces.SignalService` over stored signals."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def get_signal(self, user_id: int, signal_id: SignalId) -> SignalView:
        """See :meth:`SignalService.get_signal`."""
        return to_view(self._core, await require_signal(self._core, user_id, signal_id))

    async def get_signals(
        self, user_id: int, signal_ids: Sequence[SignalId]
    ) -> tuple[SignalView, ...]:
        """See :meth:`SignalService.get_signals`."""
        views = []
        async with self._core.ctx.sessions() as session:
            for signal_id in signal_ids:
                record = await data.get_signal(session, signal_id, user_id=user_id)
                if record is not None:
                    views.append(to_view(self._core, record))
        return tuple(views)

    async def list_signals(
        self, user_id: int, dates: DateSpec, *, include_hidden: bool = False
    ) -> tuple[SignalView, ...]:
        """See :meth:`SignalService.list_signals`."""
        profile = await self._core.profile(user_id)
        window = dates.resolve(self._core.local_today(profile.zone))
        start, end = window.utc_bounds(profile.zone)
        async with self._core.ctx.sessions() as session:
            records = await data.list_user_signals(
                session, user_id, start=start, end=end, include_hidden=include_hidden
            )
        return tuple(to_view(self._core, record) for record in records)

    async def hide_signal(self, user_id: int, signal_id: SignalId) -> SignalView:
        """See :meth:`SignalService.hide_signal`."""
        await require_signal(self._core, user_id, signal_id)
        async with self._core.ctx.sessions.begin() as session:
            await data.set_signal_status(
                session, signal_id, data.SignalStatus.HIDDEN, user_id=user_id
            )
        return await self.get_signal(user_id, signal_id)


def outcome_row(core: Core, record: data.SignalRecord) -> SignalOutcome:
    """A recommended signal with its stored profit and closing-line value."""
    result = record.result
    thresholds = record.thresholds
    profile = (
        RiskProfile.CUSTOM
        if thresholds is None
        else infer_risk_profile(
            thresholds.min_prob, thresholds.min_ev, (thresholds.odds_min, thresholds.odds_max)
        )
    )
    return SignalOutcome(
        view=to_view(core, record),
        profit_units=None if result is None else result.profit_units,
        clv=None if result is None else result.clv,
        risk_profile=profile,
    )


async def _counted(
    core: Core, user_id: int, start: datetime | None, end: datetime | None
) -> list[data.SignalRecord]:
    """The signals that count in reports (recommended, not retro, hidden included)."""
    async with core.ctx.sessions() as session:
        return await data.report_signals(session, user_id, start=start, end=end)


class RealResultService:
    """:class:`~app.services.interfaces.ResultService`."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def pending_results(self, user_id: int) -> tuple[SignalView, ...]:
        """See :meth:`ResultService.pending_results`."""
        over = self._core.now() - self._core.ctx.config.match_duration
        records = await _counted(self._core, user_id, None, None)
        return tuple(
            to_view(self._core, r)
            for r in records
            if r.result is None
            and r.fixture.fixture.kickoff <= over
            and r.fixture.fixture.status is not FixtureStatus.POSTPONED
        )

    async def mark_result(self, user_id: int, request: ResultMarkRequest) -> SignalView:
        """See :meth:`ResultService.mark_result`."""
        record = await require_signal(self._core, user_id, request.signal_id)
        if record.retro:
            raise ValidationFailedError("signal_id", "a retro signal has no result to mark")
        if record.fixture.fixture.kickoff > self._core.now():
            raise ValidationFailedError("signal_id", "the match has not started yet")
        await settle_manually(
            self._core.ctx,
            record,
            _MANUAL_OUTCOMES[request.mark],
            cashout_units=request.cashout_profit_units,
        )
        return await RealSignalService(self._core).get_signal(user_id, request.signal_id)

    async def add_note(self, user_id: int, signal_id: SignalId, text: str) -> None:
        """See :meth:`ResultService.add_note`.

        Notes are rows of ``signal_notes`` (ADR 0011), so a note also fits a future signal.
        """
        await require_signal(self._core, user_id, signal_id)
        note = text.strip()
        if not note or len(note) > MAX_NOTE_LENGTH:
            raise ValidationFailedError("text", f"must be 1..{MAX_NOTE_LENGTH} characters")
        async with self._core.ctx.sessions.begin() as session:
            await data.add_signal_note(
                session, user_id, signal_id, note, created_at=self._core.now()
            )

    async def day_summary(self, user_id: int, day: date) -> DaySummary:
        """See :meth:`ResultService.day_summary`."""
        profile = await self._core.profile(user_id)
        start, end = utc_bounds(profile.zone, day, day)
        records = await _counted(self._core, user_id, start, end)
        return day_summary([outcome_row(self._core, r) for r in records], day)


@dataclass(frozen=True, slots=True)
class _Settled:
    day: date
    profit: Decimal


class RealBetService:
    """:class:`~app.services.interfaces.BetService`."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def place_bet(self, user_id: int, draft: BetDraft) -> BetView:
        """See :meth:`BetService.place_bet`."""
        core = self._core
        record = await require_signal(core, user_id, draft.signal_id)
        repeated = await self._repeated(user_id, draft)
        if repeated is not None:  # the same tap again: answer with the bet stored first
            return self._view(repeated, record)
        self._ensure_open(record)
        await core.ensure_user(user_id)
        stake = draft.stake or await self._default_stake(user_id, record)
        async with core.ctx.sessions.begin() as session:
            bet = await data.create_bet(
                session,
                user_id=user_id,
                signal_id=record.id,
                odds_taken=draft.odds_taken or record.odds,
                stake=stake,
                placed_at=core.now(),
                client_token=draft.client_token,
            )
        return self._view(bet, record)

    async def _repeated(self, user_id: int, draft: BetDraft) -> data.UserBetRecord | None:
        if draft.client_token is None:
            return None
        async with self._core.ctx.sessions() as session:
            return await data.get_bet_by_token(session, user_id, draft.client_token)

    def _ensure_open(self, record: data.SignalRecord) -> None:
        """Only an open signal takes a bet: a settled one would leave the bet open for good."""
        if record.retro:
            raise ValidationFailedError("signal_id", "a retro signal cannot be bet on")
        if record.result is not None:
            raise ValidationFailedError("signal_id", "the signal is already settled")
        if record.status is data.SignalStatus.CANCELLED:
            raise ValidationFailedError("signal_id", "the signal was cancelled")
        if record.fixture.fixture.kickoff <= self._core.now():
            raise ValidationFailedError("signal_id", "the match has already started")

    async def _default_stake(self, user_id: int, record: data.SignalRecord) -> Decimal:
        unit = (await self._core.profile(user_id)).unit
        if unit is None or record.stake_units == 0:
            raise ValidationFailedError("stake", "enter the stake: no unit set or no stake advised")
        return unit * record.stake_units

    def _view(self, bet: data.UserBetRecord, record: data.SignalRecord | None) -> BetView:
        fixture = None if record is None else record.fixture.fixture
        if record is None or fixture is None:  # pragma: no cover - bets always have a signal here
            raise NotFoundError("signal", bet.signal_id)
        return BetView(
            bet_id=bet.id,
            signal_id=bet.signal_id,
            fixture_label=f"{fixture.home.name} — {fixture.away.name}",
            market=record.market,
            selection=record.selection,
            odds_taken=bet.odds_taken,
            stake=bet.stake,
            placed_at=bet.placed_at,
            outcome=None if bet.outcome is None else ResultMark(bet.outcome.value),
            profit=bet.profit,
        )

    async def _bets(
        self, user_id: int, limit: int, *, open_only: bool = False
    ) -> tuple[BetView, ...]:
        async with self._core.ctx.sessions() as session:
            bets = await data.list_bets(session, user_id, open_only=open_only, limit=limit)
            views = []
            for bet in bets:
                if bet.signal_id is None:
                    continue
                record = await data.get_signal(session, bet.signal_id)
                views.append(self._view(bet, record))
        return tuple(views)

    async def list_bets(
        self, user_id: int, *, open_only: bool = False, limit: int = 20
    ) -> tuple[BetView, ...]:
        """See :meth:`BetService.list_bets`."""
        if not 1 <= limit <= MAX_BET_LIST:
            raise ValidationFailedError("limit", f"must be within [1, {MAX_BET_LIST}]")
        return await self._bets(user_id, limit, open_only=open_only)

    async def bank_summary(self, user_id: int) -> BankView:
        """See :meth:`BetService.bank_summary`."""
        profile = await self._core.profile(user_id)
        today = self._core.local_today(profile.zone)
        async with self._core.ctx.sessions() as session:
            bets = await data.list_bets(session, user_id, limit=ALL_BETS)
        settled = [
            _Settled(bet.settled_at.astimezone(profile.zone).date(), bet.profit)
            for bet in bets
            if bet.profit is not None and bet.settled_at is not None
        ]
        by_day: dict[date, Decimal] = {}
        for item in settled:
            by_day[item.day] = by_day.get(item.day, Decimal(0)) + item.profit
        week_start = today - timedelta(days=today.weekday())
        loss_today = _loss(p for d, p in by_day.items() if d == today)
        loss_week = _loss(p for d, p in by_day.items() if d >= week_start)
        s = profile.settings
        return BankView(
            balance=None
            if profile.bank is None
            else profile.bank + sum(by_day.values(), Decimal(0)),
            unit=profile.unit,
            month_profit=sum(
                (p for d, p in by_day.items() if (d.year, d.month) == (today.year, today.month)),
                Decimal(0),
            ),
            open_bets=sum(1 for bet in bets if bet.outcome is None),
            history=_history(profile.bank, by_day),
            loss_today=loss_today,
            loss_this_week=loss_week,
            loss_limit_reached=_limit_hit(s.daily_loss_limit, loss_today)
            or _limit_hit(s.weekly_loss_limit, loss_week),
        )


def _loss(profits: Iterable[Decimal]) -> Decimal:
    return max(-sum(profits, Decimal(0)), Decimal(0))


def _limit_hit(limit: Decimal | None, loss: Decimal) -> bool:
    return limit is not None and loss >= limit


def _history(bank: Decimal | None, by_day: dict[date, Decimal]) -> tuple[BankPoint, ...]:
    if bank is None:
        return ()
    points, balance = [], bank
    for day in sorted(by_day):
        balance += by_day[day]
        points.append(BankPoint(day, balance))
    return tuple(points)


class RealReportService:
    """:class:`~app.services.interfaces.ReportService`."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def report(self, user_id: int, request: ReportRequest) -> ReportView:
        """See :meth:`ReportService.report`."""
        profile = await self._core.profile(user_id)
        date_from, date_to = request.resolve(self._core.local_today(profile.zone))
        start = None if date_from is None else utc_bounds(profile.zone, date_from, date_from)[0]
        end = utc_bounds(profile.zone, date_to, date_to)[1]
        records = await _counted(self._core, user_id, start, end)
        rows = [outcome_row(self._core, r) for r in records]
        period = {r.id for r in records}
        async with self._core.ctx.sessions() as session:
            bets = await data.list_bets(session, user_id, limit=ALL_BETS)
        settled = [b for b in bets if b.profit is not None and b.signal_id in period]
        totals = (
            sum((money(b.profit) for b in settled), Decimal(0)),
            sum((b.stake for b in settled), Decimal(0)),
        )
        return build_report((date_from, date_to), request.group_by, rows, totals)

    async def export(self, user_id: int) -> ExportFile:
        """See :meth:`ReportService.export`."""
        records = await _counted(self._core, user_id, None, None)
        return export_file([outcome_row(self._core, r) for r in records])


__all__ = ["BetId", "window_of"]
