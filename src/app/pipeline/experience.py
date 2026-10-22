"""The "Tajriba konteksti" block of the LLM prompt (F8.7, plan 6.13 layer 3).

For one review batch the block carries only what is relevant to *its* candidates, never the
whole history (tokens are scarce and an unrelated lesson distracts the model):

- ``segment_stats``: the league x market segments of the candidates (only with enough settled
  signals, never a noisy small sample) and the recent record of their teams;
- ``lessons``: *active* lessons whose whole scope the batch touches (a lesson on a team applies
  when that team plays; one on a league and market when both match), strongest first, at most
  :data:`~app.llm.inputs.MAX_LESSONS`;
- ``user_notes``: the user's own newest notes (at most :data:`MAX_USER_NOTES`) on signals of the
  teams involved - their observations, given to the model as data, never as instructions.

Everything is read-only and bounded in size; the prompt builder still trims it to the token
budget. A failure here costs the block only: the reviewer goes on without it.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.enums import LessonStatus
from app.db.repo import SegmentRecord, list_segment_rows
from app.db.repo.feedback import LessonRecord, list_lessons, team_records, user_notes_for_teams
from app.llm.inputs import (
    MAX_LESSONS,
    MAX_STAT_CHARS,
    MAX_USER_NOTE_CHARS,
    ExperienceContext,
)
from app.models.segments import MIN_SEGMENT_SAMPLE
from app.pipeline.clock import Clock
from app.pipeline.review import ReviewCandidate

EXPERIENCE_WINDOW: Final = timedelta(days=90)
"""The history the block summarises (plan: "so'nggi 90 kun")."""
MAX_USER_NOTES: Final = 3
MAX_STAT_LINES: Final = 4
MIN_TEAM_SIGNALS: Final = 3
"""A team's record is mentioned from this many settled signals on."""
MIN_BIAS_POINTS: Final = 1.0
"""Calibration bias below this many percentage points is not worth a word."""
_MARKET_NAMES: Final = {
    "1x2": "1X2",
    "double_chance": "Qo'sh imkoniyat",
    "totals": "Total",
    "btts": "Ikkalasi gol uradi",
    "asian_handicap": "Osiyo fora",
}


@dataclass(frozen=True, slots=True)
class _Segment:
    """Rows of one league x market, aggregated over the odds buckets."""

    n: int
    roi: float
    bias: float | None


def tags_of_candidate(candidate: ReviewCandidate) -> frozenset[str]:
    """The team, league and market tags of a candidate (lessons are matched on them)."""
    tags = [
        f"team:{candidate.home_team_id}",
        f"team:{candidate.away_team_id}",
        f"league:{candidate.league_id}",
        f"market:{candidate.market.type.value}",
    ]
    return frozenset(tag for tag in tags if "None" not in tag)


def lesson_applies(lesson: LessonRecord, candidate_tags: frozenset[str]) -> bool:
    """True when every scope tag of ``lesson`` (all but its category) is in ``candidate_tags``."""
    scope = [tag for tag in lesson.tags if not tag.startswith("category:")]
    return bool(scope) and all(tag in candidate_tags for tag in scope)


def segment_line(label: str, segment: _Segment) -> str:
    """``Premier League · Total: 48 signal, ROI +6.2%, model 3 p.p. ortiqcha baholaydi``."""
    parts = [f"{label}: {segment.n} signal", f"ROI {segment.roi:+.1%}"]
    if segment.bias is not None and abs(segment.bias) * 100 >= MIN_BIAS_POINTS:
        points = abs(segment.bias) * 100
        verb = "ortiqcha" if segment.bias > 0 else "kam"
        parts.append(f"model {points:.0f} p.p. {verb} baholaydi")
    return ", ".join(parts)[:MAX_STAT_CHARS]


def _aggregate(rows: Sequence[SegmentRecord]) -> _Segment | None:
    """One league x market from its bucket rows, weighted by their sample sizes."""
    total = sum(row.n for row in rows)
    if total < MIN_SEGMENT_SAMPLE:
        return None
    roi = sum(row.roi * row.n for row in rows) / total
    biased = [row for row in rows if row.calib_bias is not None]
    weight = sum(row.n for row in biased)
    bias = sum((row.calib_bias or 0.0) * row.n for row in biased) / weight if weight else None
    return _Segment(total, roi, bias)


class DbExperience:
    """Builds the experience block from the database (see the module docstring)."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        """Read through ``sessions`` (short read-only transactions); ``clock`` dates the window."""
        self._sessions = sessions
        self._clock = clock

    async def build(self, candidates: Sequence[ReviewCandidate]) -> ExperienceContext | None:
        """The block for ``candidates``, ``None`` when nothing relevant is known."""
        if not candidates:
            return None
        since = self._clock() - EXPERIENCE_WINDOW
        tags = [tags_of_candidate(candidate) for candidate in candidates]
        async with self._sessions() as session:
            stats = await self._segment_lines(session, candidates)
            stats += await self._team_lines(session, candidates, since)
            lessons = await self._lessons(session, tags)
            notes = await self._notes(session, candidates, since)
        context = ExperienceContext(
            segment_stats=tuple(stats[:MAX_STAT_LINES]), lessons=lessons, user_notes=notes
        )
        return context if stats or lessons or notes else None

    async def _segment_lines(
        self, session: AsyncSession, candidates: Sequence[ReviewCandidate]
    ) -> list[str]:
        wanted = {(c.league_id, c.market.type): c for c in candidates if c.league_id is not None}
        if not wanted:
            return []
        rows: defaultdict[tuple[int, str], list[SegmentRecord]] = defaultdict(list)
        for row in await list_segment_rows(session):
            rows[(row.league_id, row.market.value)].append(row)
        found = []
        for (league_id, market), candidate in wanted.items():
            segment = _aggregate(rows.get((league_id, market.value), []))
            if segment is not None:
                label = f"{candidate.league} · {_MARKET_NAMES.get(market.value, market.value)}"
                found.append((segment.n, segment_line(label, segment)))
        return [line for _, line in sorted(found, key=lambda item: -item[0])]

    async def _team_lines(
        self, session: AsyncSession, candidates: Sequence[ReviewCandidate], since: datetime
    ) -> list[str]:
        names = {
            team_id: name
            for candidate in candidates
            for team_id, name in (
                (candidate.home_team_id, candidate.home_team),
                (candidate.away_team_id, candidate.away_team),
            )
            if team_id is not None
        }
        records = await team_records(session, names, since=since)
        ranked = sorted(
            ((won + lost, won, lost, team_id) for team_id, (won, lost) in records.items()),
            reverse=True,
        )
        return [
            f"{names[team_id]} ishtirokidagi signallar: {total} ta, {won} ✅ / {lost} ❌"[
                :MAX_STAT_CHARS
            ]
            for total, won, lost, team_id in ranked
            if total >= MIN_TEAM_SIGNALS
        ]

    async def _lessons(
        self, session: AsyncSession, tags: Sequence[frozenset[str]]
    ) -> tuple[str, ...]:
        active = await list_lessons(session, statuses=(LessonStatus.ACTIVE,))
        relevant = [
            lesson for lesson in active if any(lesson_applies(lesson, found) for found in tags)
        ]
        return tuple(lesson.text for lesson in relevant[:MAX_LESSONS])

    async def _notes(
        self, session: AsyncSession, candidates: Sequence[ReviewCandidate], since: datetime
    ) -> tuple[str, ...]:
        user_id = next((c.user_id for c in candidates if c.user_id is not None), None)
        team_ids = {
            team_id
            for c in candidates
            for team_id in (c.home_team_id, c.away_team_id)
            if team_id is not None
        }
        if user_id is None:
            return ()
        notes = await user_notes_for_teams(
            session,
            user_id,
            team_ids,
            since=since,
            limit=MAX_USER_NOTES,
        )
        return tuple(
            f"{note.created_at:%d.%m}: {note.text}"[:MAX_USER_NOTE_CHARS] for note in notes
        )
