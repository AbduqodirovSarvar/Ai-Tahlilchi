"""Durable record of what the pipeline already did: idempotency keys, run status, errors.

One small table, ``pipeline_ledger``, keyed by ``(job, day, user_id, subject)`` (the
idempotency key of the service contract). It answers four questions:

- *Was this notification/job already done?* :meth:`Ledger.claim` / :meth:`Ledger.complete`
  give at-most-once execution per key with a lease, so a crash between "claimed" and
  "done" lets another run retry after the lease expires, and a finished key is never run
  twice (re-running the daily job never duplicates a message).
- *When did X last happen?* :meth:`Ledger.stamp` (cooldowns, data coverage).
- *How did the last run of job Y end?* :meth:`Ledger.record_run` (``/holat``).
- *What went wrong recently?* :meth:`Ledger.record_error`.

Every method commits its own short transaction and takes ``now`` explicitly, so claims can
be taken *before* any network call (ADR 0007) and tests control time. The table is created
by :meth:`Ledger.ensure_schema` (``CREATE TABLE IF NOT EXISTS``); it is not an Alembic table
yet - the data-engineer is asked to adopt it (ADR 0008), after which ``ensure_schema`` is a
no-op.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from sqlalchemy import (
    BigInteger,
    Column,
    ColumnElement,
    Date,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.types import UTCDateTime

DEFAULT_LEASE: Final = timedelta(minutes=15)
"""A claim older than this is considered abandoned (the worker died) and may be retaken."""
STAMP_DAY: Final = date(1970, 1, 1)
"""Placeholder ``day`` of keys that are not per-day (stamps, run status)."""
SYSTEM_USER: Final = 0
RUN_PREFIX: Final = "run."
ERROR_JOB: Final = "error"
MAX_KEPT_ERRORS: Final = 100
STATUS_CLAIMED: Final = "claimed"
STATUS_DONE: Final = "done"
STATUS_FAILED: Final = "failed"
_JOB_LENGTH: Final = 48
_SUBJECT_LENGTH: Final = 96

metadata = MetaData()
ledger_table = Table(
    "pipeline_ledger",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("job", String(_JOB_LENGTH), nullable=False),
    Column("day", Date, nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("subject", String(_SUBJECT_LENGTH), nullable=False),
    Column("status", String(12), nullable=False),
    Column("claimed_at", UTCDateTime, nullable=False),
    Column("done_at", UTCDateTime, nullable=True),
    Column("detail", Text, nullable=True),
    UniqueConstraint("job", "day", "user_id", "subject", name="uq_pipeline_ledger_key"),
    Index("ix_pipeline_ledger_job_done", "job", "done_at"),
)


@dataclass(frozen=True, slots=True)
class LedgerKey:
    """``(job, day, user_id, subject)``: what was done, for whom and about what."""

    job: str
    day: date
    user_id: int
    subject: str

    def __str__(self) -> str:
        """``job/day/user/subject``, for logs."""
        return f"{self.job}/{self.day.isoformat()}/{self.user_id}/{self.subject}"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A stored key with its state."""

    key: LedgerKey
    status: str
    claimed_at: datetime
    done_at: datetime | None
    detail: str | None

    @property
    def ok(self) -> bool:
        """True unless the run was recorded as failed."""
        return self.status != STATUS_FAILED


def _where(key: LedgerKey) -> tuple[ColumnElement[bool], ...]:
    table = ledger_table.c
    return (
        table.job == key.job,
        table.day == key.day,
        table.user_id == key.user_id,
        table.subject == key.subject,
    )


def _entry(row: object) -> LedgerEntry:
    data = row._mapping  # type: ignore[attr-defined]  # SQLAlchemy Row
    return LedgerEntry(
        key=LedgerKey(data["job"], data["day"], data["user_id"], data["subject"]),
        status=data["status"],
        claimed_at=data["claimed_at"],
        done_at=data["done_at"],
        detail=data["detail"],
    )


class Ledger:
    """Idempotency keys, stamps, run status and errors over ``pipeline_ledger``."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Use ``sessions`` for one short transaction per call."""
        self._sessions = sessions

    @staticmethod
    async def ensure_schema(engine: AsyncEngine) -> None:
        """Create the table when it does not exist yet (idempotent)."""
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

    # ------------------------------------------------------------------ idempotency keys
    async def claim(
        self, key: LedgerKey, *, now: datetime, lease: timedelta = DEFAULT_LEASE
    ) -> bool:
        """Take ``key`` for this worker: ``True`` exactly once until it is done or abandoned.

        A key that is done is never given out again; one that is claimed is given out again
        only after ``lease`` (the earlier worker is presumed dead) or after :meth:`fail`.
        """
        async with self._sessions.begin() as session:
            row = (await session.execute(select(ledger_table).where(*_where(key)))).first()
            if row is None:
                return await self._insert_claim(session, key, now)
            entry = _entry(row)
            reusable = entry.status == STATUS_FAILED or (
                entry.status == STATUS_CLAIMED and entry.claimed_at <= now - lease
            )
            if not reusable:
                return False
            await session.execute(
                update(ledger_table)
                .where(*_where(key))
                .values(status=STATUS_CLAIMED, claimed_at=now, done_at=None, detail=None)
            )
            return True

    @staticmethod
    async def _insert_claim(session: AsyncSession, key: LedgerKey, now: datetime) -> bool:
        try:
            async with session.begin_nested():
                await session.execute(
                    insert(ledger_table).values(
                        job=key.job,
                        day=key.day,
                        user_id=key.user_id,
                        subject=key.subject,
                        status=STATUS_CLAIMED,
                        claimed_at=now,
                    )
                )
        except IntegrityError:  # another worker inserted the same key first
            return False
        return True

    async def complete(self, key: LedgerKey, *, now: datetime, detail: str | None = None) -> None:
        """Mark ``key`` done for good (inserting it when it was never claimed)."""
        await self._finish(key, STATUS_DONE, now, detail)

    async def fail(self, key: LedgerKey, *, now: datetime, detail: str | None = None) -> None:
        """Mark ``key`` failed: the next :meth:`claim` retries it immediately."""
        await self._finish(key, STATUS_FAILED, now, detail)

    async def release(self, key: LedgerKey) -> None:
        """Forget a claim that was not used."""
        async with self._sessions.begin() as session:
            await session.execute(
                delete(ledger_table).where(*_where(key), ledger_table.c.status == STATUS_CLAIMED)
            )

    async def get(self, key: LedgerKey) -> LedgerEntry | None:
        """The stored state of ``key``."""
        async with self._sessions() as session:
            row = (await session.execute(select(ledger_table).where(*_where(key)))).first()
        return None if row is None else _entry(row)

    async def is_done(self, key: LedgerKey) -> bool:
        """True when ``key`` was completed."""
        entry = await self.get(key)
        return entry is not None and entry.status == STATUS_DONE

    async def _finish(self, key: LedgerKey, status: str, now: datetime, detail: str | None) -> None:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(ledger_table)
                .where(*_where(key))
                .values(status=status, done_at=now, detail=detail)
            )
            if getattr(result, "rowcount", 0) == 0:
                await session.execute(
                    insert(ledger_table).values(
                        job=key.job,
                        day=key.day,
                        user_id=key.user_id,
                        subject=key.subject,
                        status=status,
                        claimed_at=now,
                        done_at=now,
                        detail=detail,
                    )
                )

    # ------------------------------------------------------------------ stamps
    async def stamp(self, job: str, subject: str, *, now: datetime, user_id: int = 0) -> None:
        """Record that ``job`` happened for ``subject`` at ``now`` (latest wins)."""
        await self.complete(LedgerKey(job, STAMP_DAY, user_id, subject), now=now)

    async def stamped_at(self, job: str, subject: str, *, user_id: int = 0) -> datetime | None:
        """When :meth:`stamp` last ran for ``subject``."""
        entry = await self.get(LedgerKey(job, STAMP_DAY, user_id, subject))
        return None if entry is None else entry.done_at

    # ------------------------------------------------------------------ run status
    async def record_run(self, name: str, *, ok: bool, detail: str | None, now: datetime) -> None:
        """Remember how the last run of job ``name`` ended (for ``/holat``)."""
        key = LedgerKey(f"{RUN_PREFIX}{name}", STAMP_DAY, SYSTEM_USER, name)
        await self._finish(key, STATUS_DONE if ok else STATUS_FAILED, now, detail)

    async def last_runs(self) -> dict[str, LedgerEntry]:
        """The last recorded run of every job, by job name."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(ledger_table).where(ledger_table.c.job.like(f"{RUN_PREFIX}%"))
            )
            entries = [_entry(row) for row in rows]
        return {entry.key.subject: entry for entry in entries}

    # ------------------------------------------------------------------ errors
    async def record_error(self, component: str, message: str, *, now: datetime) -> None:
        """Keep a recent error for ``/holat`` (the newest :data:`MAX_KEPT_ERRORS` are kept)."""
        subject = f"{component}:{int(now.timestamp() * 1_000_000)}"[:_SUBJECT_LENGTH]
        key = LedgerKey(ERROR_JOB, now.date(), SYSTEM_USER, subject)
        await self._finish(key, STATUS_DONE, now, f"{component}\n{message}")
        async with self._sessions.begin() as session:
            newest = (
                select(ledger_table.c.id)
                .where(ledger_table.c.job == ERROR_JOB)
                .order_by(ledger_table.c.claimed_at.desc(), ledger_table.c.id.desc())
                .limit(MAX_KEPT_ERRORS)
            )
            await session.execute(
                delete(ledger_table).where(
                    ledger_table.c.job == ERROR_JOB, ledger_table.c.id.not_in(newest)
                )
            )

    async def recent_errors(self, limit: int = 10) -> list[tuple[datetime, str, str]]:
        """``(when, component, message)`` of the newest errors, newest first."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(ledger_table)
                .where(ledger_table.c.job == ERROR_JOB)
                .order_by(ledger_table.c.claimed_at.desc(), ledger_table.c.id.desc())
                .limit(limit)
            )
            entries = [_entry(row) for row in rows]
        result = []
        for entry in entries:
            component, _, message = (entry.detail or "").partition("\n")
            result.append((entry.claimed_at, component, message))
        return result
