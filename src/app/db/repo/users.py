"""Users, their settings, watchlist and saved queries.

Every function runs in the caller's transaction (flush, no commit). Ownership is part of
every lookup (``user_id`` in the WHERE clause): another user's row behaves as missing.
"""

from dataclasses import asdict
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import WatchKind
from app.db.repo.records import (
    SavedQueryDraft,
    SavedQueryRecord,
    UserRecord,
    UserSettingsRecord,
    WatchRecord,
)
from app.db.schema import SavedQueryRow, UserRow, UserSettingsRow, WatchlistRow
from app.domain import Sport

# --------------------------------------------------------------------------- users


async def ensure_user(
    session: AsyncSession, user_id: int, *, timezone: str, is_admin: bool = False
) -> UserRecord:
    """The user row, created on first contact (existing rows are not modified)."""
    row = await session.get(UserRow, user_id)
    if row is None:
        row = UserRow(id=user_id, timezone=timezone, is_admin=is_admin)
        session.add(row)
        await session.flush()
    return _user_record(row)


async def get_user(session: AsyncSession, user_id: int) -> UserRecord | None:
    """A user, or ``None`` if unknown."""
    row = await session.get(UserRow, user_id)
    return None if row is None else _user_record(row)


async def update_user(
    session: AsyncSession,
    user_id: int,
    *,
    timezone: str,
    bank: Decimal | None,
    unit: Decimal | None,
) -> UserRecord | None:
    """Replace a user's timezone, bank and unit; ``None`` if the user is unknown."""
    row = await session.get(UserRow, user_id)
    if row is None:
        return None
    row.timezone, row.bank, row.unit = timezone, bank, unit
    await session.flush()
    return _user_record(row)


def _user_record(row: UserRow) -> UserRecord:
    return UserRecord(
        id=row.id,
        is_admin=row.is_admin,
        timezone=row.timezone,
        bank=row.bank,
        unit=row.unit,
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------- settings


async def get_user_settings(session: AsyncSession, user_id: int) -> UserSettingsRecord | None:
    """Stored settings, or ``None`` (the service then applies its defaults)."""
    row = await session.get(UserSettingsRow, user_id)
    return None if row is None else _settings_record(row)


async def upsert_user_settings(
    session: AsyncSession, settings: UserSettingsRecord
) -> UserSettingsRecord:
    """Replace a user's settings entirely (the user row must exist: :func:`ensure_user`)."""
    row = await session.get(UserSettingsRow, settings.user_id)
    if row is None:
        row = UserSettingsRow(user_id=settings.user_id)
        session.add(row)
    values = asdict(settings)
    for name in ("sports", "leagues", "markets"):
        values[name] = list(values[name])
    for name, value in values.items():
        setattr(row, name, value)
    await session.flush()
    return _settings_record(row)


def _settings_record(row: UserSettingsRow) -> UserSettingsRecord:
    return UserSettingsRecord(
        user_id=row.user_id,
        sports=tuple(row.sports),
        leagues=tuple(row.leagues),
        markets=tuple(row.markets),
        max_signals=row.max_signals,
        min_prob=row.min_prob,
        min_ev=row.min_ev,
        odds_min=row.odds_min,
        odds_max=row.odds_max,
        risk_profile=row.risk_profile,
        send_time=row.send_time,
        prematch_minutes=row.prematch_minutes,
        quiet_hours_start=row.quiet_hours_start,
        quiet_hours_end=row.quiet_hours_end,
        daily_loss_limit=row.daily_loss_limit,
        weekly_loss_limit=row.weekly_loss_limit,
    )


# --------------------------------------------------------------------------- watchlist


async def list_watchlist(session: AsyncSession, user_id: int) -> list[WatchRecord]:
    """A user's followed teams and leagues, oldest first."""
    rows = await session.scalars(
        select(WatchlistRow).where(WatchlistRow.user_id == user_id).order_by(WatchlistRow.id)
    )
    return [_watch_record(row) for row in rows]


async def add_watch(  # noqa: PLR0913 - one column per argument
    session: AsyncSession,
    user_id: int,
    *,
    kind: WatchKind,
    sport: Sport,
    entity_id: int,
    notify_flags: tuple[str, ...] = (),
) -> WatchRecord:
    """Follow a team/league; following it again returns the existing entry unchanged."""
    row = await _watch_row(session, user_id, kind, entity_id)
    if row is None:
        row = WatchlistRow(
            user_id=user_id,
            kind=kind,
            sport=sport,
            entity_id=entity_id,
            notify_flags=list(notify_flags),
        )
        session.add(row)
        await session.flush()
    return _watch_record(row)


async def remove_watch(
    session: AsyncSession, user_id: int, *, kind: WatchKind, entity_id: int
) -> bool:
    """Stop following; ``False`` if it was not followed."""
    result = await session.execute(
        delete(WatchlistRow).where(
            WatchlistRow.user_id == user_id,
            WatchlistRow.kind == kind,
            WatchlistRow.entity_id == entity_id,
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def set_watch_notifications(
    session: AsyncSession,
    user_id: int,
    *,
    kind: WatchKind,
    entity_id: int,
    notify_flags: tuple[str, ...],
) -> WatchRecord | None:
    """Replace the notification flags of a followed entity; ``None`` if not followed."""
    row = await _watch_row(session, user_id, kind, entity_id)
    if row is None:
        return None
    row.notify_flags = list(notify_flags)
    await session.flush()
    return _watch_record(row)


async def _watch_row(
    session: AsyncSession, user_id: int, kind: WatchKind, entity_id: int
) -> WatchlistRow | None:
    return await session.scalar(
        select(WatchlistRow).where(
            WatchlistRow.user_id == user_id,
            WatchlistRow.kind == kind,
            WatchlistRow.entity_id == entity_id,
        )
    )


def _watch_record(row: WatchlistRow) -> WatchRecord:
    return WatchRecord(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        sport=row.sport,
        entity_id=row.entity_id,
        notify_flags=tuple(row.notify_flags),
    )


# --------------------------------------------------------------------------- saved queries

_LIST_FIELDS = ("sports", "scope_ids", "markets", "schedule_weekdays")


async def save_query(session: AsyncSession, draft: SavedQueryDraft) -> SavedQueryRecord:
    """Create or replace (same user and name) a saved query."""
    row = await session.scalar(
        select(SavedQueryRow).where(
            SavedQueryRow.user_id == draft.user_id, SavedQueryRow.name == draft.name
        )
    )
    if row is None:
        row = SavedQueryRow(user_id=draft.user_id, name=draft.name)
        session.add(row)
    for name, value in asdict(draft).items():
        setattr(row, name, list(value) if name in _LIST_FIELDS else value)
    await session.flush()
    return _query_record(row)


async def list_saved_queries(session: AsyncSession, user_id: int) -> list[SavedQueryRecord]:
    """A user's saved queries by name."""
    rows = await session.scalars(
        select(SavedQueryRow).where(SavedQueryRow.user_id == user_id).order_by(SavedQueryRow.name)
    )
    return [_query_record(row) for row in rows]


async def get_saved_query(
    session: AsyncSession, user_id: int, query_id: int
) -> SavedQueryRecord | None:
    """One of the user's saved queries."""
    row = await session.scalar(
        select(SavedQueryRow).where(SavedQueryRow.id == query_id, SavedQueryRow.user_id == user_id)
    )
    return None if row is None else _query_record(row)


async def delete_saved_query(session: AsyncSession, user_id: int, query_id: int) -> bool:
    """Delete one of the user's saved queries; ``False`` if there was none."""
    result = await session.execute(
        delete(SavedQueryRow).where(SavedQueryRow.id == query_id, SavedQueryRow.user_id == user_id)
    )
    return bool(getattr(result, "rowcount", 0))


def _query_record(row: SavedQueryRow) -> SavedQueryRecord:
    draft = SavedQueryDraft(
        user_id=row.user_id,
        name=row.name,
        date_mode=row.date_mode,
        days=row.days,
        date_from=row.date_from,
        date_to=row.date_to,
        sports=tuple(row.sports),
        min_prob=row.min_prob,
        min_ev=row.min_ev,
        scope=row.scope,
        scope_ids=tuple(row.scope_ids),
        markets=tuple(row.markets),
        max_signals=row.max_signals,
        sort=row.sort,
        per_day=row.per_day,
        include_no_value=row.include_no_value,
        schedule_weekdays=tuple(row.schedule_weekdays),
        schedule_at=row.schedule_at,
    )
    return SavedQueryRecord(
        id=row.id, query=draft, created_at=row.created_at, updated_at=row.updated_at
    )
