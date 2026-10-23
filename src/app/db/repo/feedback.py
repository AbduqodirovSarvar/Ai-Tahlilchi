"""Reads and writes of the feedback loop: post-mortems, lessons and the experience context.

Post-mortems (plan 6.13 layer 4) classify why a recommended signal lost; lessons are repeated,
tagged observations built from them; the experience context reads both back, together with the
users' own notes and the record of a team, for the LLM prompt. Everything runs in the caller's
transaction (flush, never commit), like the rest of the repository.

Import from this module (``app.db.repo.feedback``), the package ``__init__`` does not list it.
"""

from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.enums import LessonStatus, Outcome, PostmortemCategory, SignalSource
from app.db.schema import (
    FixtureRow,
    LessonRow,
    PostmortemRow,
    SignalNoteRow,
    SignalResultRow,
    SignalRow,
)

POSTMORTEM_OUTCOMES: Final = (Outcome.LOSS,)
"""Results that earn a post-mortem: a full loss (a half loss is too noisy to explain)."""
_WON: Final = (Outcome.WIN, Outcome.HALF_WIN)
_LOST: Final = (Outcome.LOSS, Outcome.HALF_LOSS)


@dataclass(frozen=True, slots=True, kw_only=True)
class PostmortemRecord:
    """A stored post-mortem with the fixture of its signal (lessons count distinct fixtures)."""

    id: int
    signal_id: int
    fixture_id: int
    category: PostmortemCategory
    reason: str
    tags: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class LessonRecord:
    """A stored lesson; ``id`` is ``None`` for one that is not stored yet."""

    id: int | None
    text: str
    tags: tuple[str, ...]
    confirmations: int
    status: LessonStatus
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class UserNote:
    """A note the user wrote on a signal, with the teams of its fixture."""

    text: str
    created_at: datetime


# --------------------------------------------------------------------------- post-mortems


async def lost_signal_ids(
    session: AsyncSession, *, settled_since: datetime, limit: int
) -> list[int]:
    """Recommended, non-retro signals that lost since ``settled_since`` and have no post-mortem.

    Newest result first: fresh losses make the most useful lessons and an old backlog (the LLM
    was down for days) simply ages out of ``settled_since``. A manual result counts like an
    automatic one (the user said it lost).
    """
    has_postmortem = exists().where(PostmortemRow.signal_id == SignalRow.id)
    rows = await session.scalars(
        select(SignalRow.id)
        .join(SignalResultRow, SignalResultRow.signal_id == SignalRow.id)
        .where(
            SignalRow.recommended_at.is_not(None),
            SignalRow.source != SignalSource.RETRO,
            SignalResultRow.outcome.in_(POSTMORTEM_OUTCOMES),
            SignalResultRow.settled_at >= settled_since,
            ~has_postmortem,
        )
        .order_by(SignalResultRow.settled_at.desc(), SignalRow.id.desc())
        .limit(limit)
    )
    return list(rows)


async def pick_postmortem(session: AsyncSession, signal_id: int) -> PostmortemRecord | None:
    """The post-mortem of the same pick (fixture, market, line, selection) of *any* user.

    The same pick delivered to several users is one event; its cause is explained once.
    """
    other = aliased(SignalRow)
    row = (
        await session.execute(
            select(PostmortemRow, other.fixture_id)
            .join(other, other.id == PostmortemRow.signal_id)
            .join(
                SignalRow,
                and_(
                    SignalRow.fixture_id == other.fixture_id,
                    SignalRow.market == other.market,
                    SignalRow.line == other.line,
                    SignalRow.selection == other.selection,
                ),
            )
            .where(SignalRow.id == signal_id)
            .order_by(PostmortemRow.id)
            .limit(1)
        )
    ).first()
    return None if row is None else _postmortem(row[0], row[1])


async def add_postmortem(  # noqa: PLR0913 - one column per argument
    session: AsyncSession,
    *,
    signal_id: int,
    category: PostmortemCategory,
    reason: str,
    tags: Sequence[str],
    created_at: datetime,
) -> PostmortemRecord:
    """Store the post-mortem of ``signal_id``; a signal that has one keeps it (idempotent)."""
    fixture_id = await session.scalar(select(SignalRow.fixture_id).where(SignalRow.id == signal_id))
    if fixture_id is None:
        msg = f"signal {signal_id} does not exist"
        raise LookupError(msg)
    existing = await session.scalar(
        select(PostmortemRow).where(PostmortemRow.signal_id == signal_id).order_by(PostmortemRow.id)
    )
    if existing is not None:
        return _postmortem(existing, fixture_id)
    row = PostmortemRow(
        signal_id=signal_id,
        category=category,
        reason=reason,
        tags=list(tags),
        created_at=created_at,
    )
    session.add(row)
    await session.flush()
    return _postmortem(row, fixture_id)


async def list_postmortems(session: AsyncSession, *, since: datetime) -> list[PostmortemRecord]:
    """Post-mortems made since ``since``, oldest first."""
    rows = await session.execute(
        select(PostmortemRow, SignalRow.fixture_id)
        .join(SignalRow, SignalRow.id == PostmortemRow.signal_id)
        .where(PostmortemRow.created_at >= since)
        .order_by(PostmortemRow.created_at, PostmortemRow.id)
    )
    return [_postmortem(row, fixture_id) for row, fixture_id in rows.all()]


def _postmortem(row: PostmortemRow, fixture_id: int) -> PostmortemRecord:
    return PostmortemRecord(
        id=row.id,
        signal_id=row.signal_id,
        fixture_id=fixture_id,
        category=row.category,
        reason=row.reason,
        tags=tuple(row.tags),
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------- lessons


async def list_lessons(
    session: AsyncSession, *, statuses: Collection[LessonStatus] | None = None
) -> list[LessonRecord]:
    """Stored lessons (all, or only those in ``statuses``), strongest first."""
    statement = select(LessonRow).order_by(
        LessonRow.confirmations.desc(), LessonRow.last_seen.desc(), LessonRow.id
    )
    if statuses is not None:
        statement = statement.where(LessonRow.status.in_(list(statuses)))
    return [_lesson(row) for row in await session.scalars(statement)]


async def save_lesson(session: AsyncSession, record: LessonRecord) -> LessonRecord:
    """Insert ``record`` (``id`` is ``None``) or update the stored lesson with its id."""
    row = LessonRow() if record.id is None else await session.get(LessonRow, record.id)
    if row is None:
        msg = f"lesson {record.id} does not exist"
        raise LookupError(msg)
    row.text = record.text
    row.tags = list(record.tags)
    row.confirmations = record.confirmations
    row.status = record.status
    row.first_seen = record.first_seen
    row.last_seen = record.last_seen
    if record.id is None:
        session.add(row)
    await session.flush()
    return _lesson(row)


def _lesson(row: LessonRow) -> LessonRecord:
    return LessonRecord(
        id=row.id,
        text=row.text,
        tags=tuple(row.tags),
        confirmations=row.confirmations,
        status=row.status,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
    )


# --------------------------------------------------------------------------- experience


async def team_records(
    session: AsyncSession, team_ids: Collection[int], *, since: datetime
) -> dict[int, tuple[int, int]]:
    """``team id -> (won, lost)`` of the recommended signals settled since ``since``.

    One count per pick (the same pick of several users is one event), half results count as
    won/lost, void and cash-out not at all.
    """
    if not team_ids:
        return {}
    wanted = list(team_ids)
    rows = await session.execute(
        select(
            SignalRow.fixture_id,
            SignalRow.market,
            SignalRow.line,
            SignalRow.selection,
            SignalResultRow.outcome,
            FixtureRow.home_team_id,
            FixtureRow.away_team_id,
        )
        .join(SignalResultRow, SignalResultRow.signal_id == SignalRow.id)
        .join(FixtureRow, FixtureRow.id == SignalRow.fixture_id)
        .where(
            SignalRow.recommended_at.is_not(None),
            SignalRow.source != SignalSource.RETRO,
            SignalResultRow.outcome.in_((*_WON, *_LOST)),
            SignalResultRow.settled_at >= since,
            or_(FixtureRow.home_team_id.in_(wanted), FixtureRow.away_team_id.in_(wanted)),
        )
        .order_by(SignalRow.id)
    )
    seen: set[tuple[object, ...]] = set()
    counts: defaultdict[int, list[int]] = defaultdict(lambda: [0, 0])
    for fixture_id, market, line, selection, outcome, home_id, away_id in rows.all():
        pick = (fixture_id, market, line, selection)
        if pick in seen:
            continue
        seen.add(pick)
        for team_id in (home_id, away_id):
            if team_id in team_ids:
                counts[team_id][0 if outcome in _WON else 1] += 1
    return {team_id: (won, lost) for team_id, (won, lost) in counts.items()}


async def user_notes_for_teams(
    session: AsyncSession,
    user_id: int,
    team_ids: Collection[int],
    *,
    since: datetime,
    limit: int,
) -> list[UserNote]:
    """The user's newest notes since ``since`` on signals of fixtures involving ``team_ids``."""
    if not team_ids:
        return []
    wanted = list(team_ids)
    rows = await session.scalars(
        select(SignalNoteRow)
        .join(SignalRow, SignalRow.id == SignalNoteRow.signal_id)
        .join(FixtureRow, FixtureRow.id == SignalRow.fixture_id)
        .where(
            SignalNoteRow.user_id == user_id,
            SignalNoteRow.created_at >= since,
            or_(FixtureRow.home_team_id.in_(wanted), FixtureRow.away_team_id.in_(wanted)),
        )
        .order_by(SignalNoteRow.created_at.desc(), SignalNoteRow.id.desc())
        .limit(limit)
    )
    return [UserNote(text=row.text, created_at=row.created_at) for row in rows]
