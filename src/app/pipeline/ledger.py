"""Durable record of what the pipeline already did: idempotency keys, run status, errors.

One small table, ``pipeline_ledger``, keyed by ``(job, day, user_id, subject)`` (the
idempotency key of the service contract). It answers four questions:

- *Was this notification/job already done?* :meth:`Ledger.claim` / :meth:`Ledger.complete`
  give at-most-once execution per key with a lease, so a crash between "claimed" and
  "done" lets another run retry after the lease expires, and a finished key is never run
  twice (re-running the daily job never duplicates a message). A claim is a compare-and-set,
  so two workers can never both win it.
- *May a failed key be retried yet?* :meth:`Ledger.fail` counts the attempt and sets an
  exponential ``retry_after``; after :data:`MAX_ATTEMPTS` the key is ``exhausted`` and never
  given out again (the caller alerts the administrators).
- *When did X last happen?* :meth:`Ledger.stamp` (cooldowns, data coverage).
- *How did the last run of job Y end?* :meth:`Ledger.record_run` (``/holat``).
- *What went wrong recently?* :meth:`Ledger.record_error`.

Every method commits its own short transaction and takes ``now`` explicitly, so claims can
be taken *before* any network call (ADR 0007) and tests control time. The table is
``app.db.schema.LedgerRow`` (Alembic ``0004``); :meth:`Ledger.ensure_schema` only creates it
for a database that was never migrated (``CREATE TABLE IF NOT EXISTS``).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final, cast

from sqlalchemy import ColumnElement, Table, and_, delete, insert, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.schema import LEDGER_SUBJECT_LENGTH, Base, LedgerRow
from app.pipeline.redaction import scrub_text

DEFAULT_LEASE: Final = timedelta(minutes=15)
"""A claim older than this is considered abandoned (the worker died) and may be retaken."""
STAMP_DAY: Final = date(1970, 1, 1)
"""Placeholder ``day`` of keys that are not per-day (stamps, run status)."""
SYSTEM_USER: Final = 0
RUN_PREFIX: Final = "run."
ERROR_JOB: Final = "error"
DETAIL_LIMIT: Final = 1000
"""Longest detail text kept per ledger entry."""
MAX_KEPT_ERRORS: Final = 100
STATUS_CLAIMED: Final = "claimed"
STATUS_DONE: Final = "done"
STATUS_FAILED: Final = "failed"
STATUS_EXHAUSTED: Final = "exhausted"
MAX_ATTEMPTS: Final = 5
"""A key that failed this many times is given up on (``exhausted``)."""
RETRY_BASE: Final = timedelta(minutes=1)
RETRY_CAP: Final = timedelta(minutes=30)
"""Backoff after failure ``n`` is ``RETRY_BASE * 2 ** (n - 1)``, at most ``RETRY_CAP``."""
DELIVERY_BACKOFFS: Final = tuple(
    timedelta(minutes=minutes) for minutes in (1, 2, 4, 8, 15, 30, 60, 120, 120)
)
"""Waits after the 1st, 2nd, ... failure of a *delivery* key (a message to a user, T6-F-7).

A Telegram outage of an hour or two must not cost the user the whole day's digest, so the
schedule spans ~6 hours (the digest's catch-up window, ``PipelineConfig.daily_catch_up``,
is 12 hours: a retry beyond it is never made anyway). The next failure after the last wait
exhausts the key."""
DELIVERY_MAX_ATTEMPTS: Final = len(DELIVERY_BACKOFFS) + 1
KEY_RETENTION: Final = timedelta(days=35)
"""Per-day keys and message references older than this are forgotten."""
PURGED_STAMP_JOBS: Final = ("query", "msg", "daily.outcome", "daily.progress", "notified")
"""Jobs whose entries are not per-day but expire by age (``done_at``)."""
_SUBJECT_LENGTH: Final = LEDGER_SUBJECT_LENGTH

ledger_table: Final = cast("Table", LedgerRow.__table__)
"""Core view of :class:`~app.db.schema.LedgerRow` (Alembic ``0004``)."""


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
    attempts: int = 0
    retry_after: datetime | None = None

    @property
    def ok(self) -> bool:
        """True unless the run was recorded as failed."""
        return self.status not in {STATUS_FAILED, STATUS_EXHAUSTED}


@dataclass(frozen=True, slots=True)
class Failure:
    """What :meth:`Ledger.fail` decided: when to retry, or that the key is given up."""

    attempts: int
    retry_after: datetime | None
    exhausted: bool


def backoff(attempts: int, *, delivery: bool = False) -> timedelta:
    """Wait before attempt ``attempts + 1``: exponential and capped, or the delivery schedule."""
    if delivery:
        return DELIVERY_BACKOFFS[min(max(attempts, 1), len(DELIVERY_BACKOFFS)) - 1]
    steps = 1 << max(attempts - 1, 0)
    return min(RETRY_BASE * steps, RETRY_CAP)


def _where(key: LedgerKey) -> tuple[ColumnElement[bool], ...]:
    table = ledger_table.c
    return (
        table.job == key.job,
        table.day == key.day,
        table.user_id == key.user_id,
        table.subject == key.subject,
    )


def _reusable(entry: LedgerEntry, now: datetime, lease: timedelta) -> bool:
    """Whether a stored key may be taken again (see :meth:`Ledger.claim`)."""
    if entry.status == STATUS_FAILED:
        return entry.retry_after is None or entry.retry_after <= now
    return entry.status == STATUS_CLAIMED and entry.claimed_at <= now - lease


def _entry(row: object) -> LedgerEntry:
    data = row._mapping  # type: ignore[attr-defined]  # SQLAlchemy Row
    return LedgerEntry(
        key=LedgerKey(data["job"], data["day"], data["user_id"], data["subject"]),
        status=data["status"],
        claimed_at=data["claimed_at"],
        done_at=data["done_at"],
        detail=data["detail"],
        attempts=data["attempts"],
        retry_after=data["retry_after"],
    )


def _clean(detail: str | None) -> str | None:
    """Error text reaches ``/holat``: credentials are masked (digests never pass through here)."""
    return None if detail is None else scrub_text(detail, limit=DETAIL_LIMIT)


class Ledger:
    """Idempotency keys, stamps, run status and errors over ``pipeline_ledger``."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Use ``sessions`` for one short transaction per call."""
        self._sessions = sessions

    @staticmethod
    async def ensure_schema(engine: AsyncEngine) -> None:
        """Create the table when it does not exist yet (idempotent)."""
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=[ledger_table])

    # ------------------------------------------------------------------ idempotency keys
    async def claim(
        self, key: LedgerKey, *, now: datetime, lease: timedelta = DEFAULT_LEASE
    ) -> bool:
        """Take ``key`` for this worker: ``True`` exactly once until it is done or abandoned.

        A key that is done (or exhausted) is never given out again; one that is claimed is
        given out again only after ``lease`` (the earlier worker is presumed dead); a failed
        one only once its ``retry_after`` has passed. The takeover is a compare-and-set on
        the state that was read, so of two racing workers exactly one wins.
        """
        async with self._sessions.begin() as session:
            row = (await session.execute(select(ledger_table).where(*_where(key)))).first()
            if row is None:
                return await self._insert_claim(session, key, now)
            entry = _entry(row)
            if not _reusable(entry, now, lease):
                return False
            result = await session.execute(
                update(ledger_table)
                .where(
                    *_where(key),
                    ledger_table.c.status == entry.status,
                    ledger_table.c.claimed_at == entry.claimed_at,
                )
                .values(
                    status=STATUS_CLAIMED,
                    claimed_at=now,
                    done_at=None,
                    detail=None,
                    retry_after=None,
                )
            )
            return getattr(result, "rowcount", 0) == 1

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

    async def complete_many(
        self, entries: Sequence[tuple[LedgerKey, str | None]], *, now: datetime
    ) -> None:
        """Mark every key done in ONE statement and transaction (all or nothing).

        For bookkeeping that must not be torn by a crash, e.g. the message references of all
        cards of a digest.
        """
        if not entries:
            return
        rows = [
            {
                "job": key.job,
                "day": key.day,
                "user_id": key.user_id,
                "subject": key.subject,
                "status": STATUS_DONE,
                "claimed_at": now,
                "done_at": now,
                "detail": detail,
            }
            for key, detail in entries
        ]
        statement = sqlite_insert(ledger_table).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["job", "day", "user_id", "subject"],
            set_={
                "status": STATUS_DONE,
                "done_at": now,
                "detail": statement.excluded.detail,
                "retry_after": None,
            },
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)

    async def fail(
        self, key: LedgerKey, *, now: datetime, detail: str | None = None, delivery: bool = False
    ) -> Failure:
        """Record a failed attempt of ``key``: retry after a backoff, or give up for good.

        The attempt count survives restarts; the ``n``-th failure waits ``backoff(n)``, and
        the :data:`MAX_ATTEMPTS`-th makes the key ``exhausted`` (never claimable again). A
        ``delivery`` key (a message to a user) follows the longer :data:`DELIVERY_BACKOFFS`.
        """
        async with self._sessions.begin() as session:
            row = (await session.execute(select(ledger_table).where(*_where(key)))).first()
            attempts = (0 if row is None else _entry(row).attempts) + 1
            exhausted = attempts >= (DELIVERY_MAX_ATTEMPTS if delivery else MAX_ATTEMPTS)
            retry_after = None if exhausted else now + backoff(attempts, delivery=delivery)
            values = {
                "status": STATUS_EXHAUSTED if exhausted else STATUS_FAILED,
                "done_at": now,
                "detail": _clean(detail),
                "attempts": attempts,
                "retry_after": retry_after,
            }
            if row is None:
                await session.execute(
                    insert(ledger_table).values(
                        job=key.job,
                        day=key.day,
                        user_id=key.user_id,
                        subject=key.subject,
                        claimed_at=now,
                        **values,
                    )
                )
            else:
                await session.execute(update(ledger_table).where(*_where(key)).values(**values))
        return Failure(attempts, retry_after, exhausted)

    async def due_retries(
        self, job: str, *, now: datetime, lease: timedelta = DEFAULT_LEASE, limit: int = 50
    ) -> list[LedgerKey]:
        """Keys of ``job`` that may be attempted again now (backoff over, or claim abandoned)."""
        table = ledger_table.c
        async with self._sessions() as session:
            rows = await session.execute(
                select(ledger_table)
                .where(
                    table.job == job,
                    or_(
                        and_(
                            table.status == STATUS_FAILED,
                            or_(table.retry_after.is_(None), table.retry_after <= now),
                        ),
                        and_(table.status == STATUS_CLAIMED, table.claimed_at <= now - lease),
                    ),
                )
                .order_by(table.claimed_at, table.id)
                .limit(limit)
            )
            return [_entry(row).key for row in rows]

    async def release(self, key: LedgerKey) -> None:
        """Forget a claim that was not used."""
        async with self._sessions.begin() as session:
            await session.execute(
                delete(ledger_table).where(*_where(key), ledger_table.c.status == STATUS_CLAIMED)
            )

    async def forget(self, key: LedgerKey) -> None:
        """Delete ``key`` in any state (a retry counter that is no longer needed)."""
        async with self._sessions.begin() as session:
            await session.execute(delete(ledger_table).where(*_where(key)))

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
                .values(status=status, done_at=now, detail=detail, retry_after=None)
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

    # ------------------------------------------------------------------ housekeeping
    async def purge_expired(self, *, now: datetime) -> int:
        """Forget old per-day keys, query ids, message references and snapshots.

        Keeps the table small: idempotency only matters around the day a key belongs to,
        and the reply references of messages are useless long after the match.
        """
        table = ledger_table.c
        cutoff = now - KEY_RETENTION
        async with self._sessions.begin() as session:
            old_days = await session.execute(
                delete(ledger_table).where(
                    table.day != STAMP_DAY,
                    table.day < cutoff.date(),
                    table.job != ERROR_JOB,
                    table.status != STATUS_CLAIMED,
                )
            )
            old_stamps = await session.execute(
                delete(ledger_table).where(table.job.in_(PURGED_STAMP_JOBS), table.done_at < cutoff)
            )
        return int(getattr(old_days, "rowcount", 0)) + int(getattr(old_stamps, "rowcount", 0))

    # ------------------------------------------------------------------ errors
    async def record_error(self, component: str, message: str, *, now: datetime) -> None:
        """Keep a recent error for ``/holat`` (the newest :data:`MAX_KEPT_ERRORS` are kept)."""
        subject = f"{component}:{int(now.timestamp() * 1_000_000)}"[:_SUBJECT_LENGTH]
        key = LedgerKey(ERROR_JOB, now.date(), SYSTEM_USER, subject)
        await self._finish(key, STATUS_DONE, now, _clean(f"{component}\n{message}"))
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
