"""Lessons (F8.4, plan 6.13): repeated, reversible mistakes the prompt may learn from.

A post-mortem of a lost signal says why it lost. One occurrence is noise ("a single case is not
a lesson"), so lessons are *derived* from the post-mortems of the last 90 days:

- only the reversible causes count (``ma'lumot yetishmadi``, ``model xatosi``); bad luck is never
  a lesson, and neither is a post-mortem whose reason the LLM guard rejected (empty);
- a lesson is scoped by a tag set: one team (``team:<id>``) or a league and market together
  (``league:<id>`` + ``market:<type>``), always with its ``category:<name>``;
- its **confirmations** are the *distinct fixtures* behind it: the same match lost by three
  users, or explained twice, is one confirmation (independence);
- it is ``active`` from 2 confirmations, ``pending`` with 1, and ``expired`` when its evidence
  has aged out (older than 90 days) or has fallen below the bar after it had been active.

Because lessons are a pure function of the post-mortems and the clock, :func:`run_lessons` is a
rebuild: idempotent, safe to run twice or after a crash, and the weekly run is simply the same
call that also lets old evidence expire. The text is templated Uzbek built from the scope and
count, plus the latest *sanitized* reason when it fits; no other model output ever reaches a
lesson.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from app.db.enums import LessonStatus, PostmortemCategory
from app.db.repo.catalog import get_league, get_team
from app.db.repo.feedback import (
    LessonRecord,
    PostmortemRecord,
    list_lessons,
    list_postmortems,
    save_lesson,
)
from app.llm.inputs import MAX_LESSON_CHARS
from app.logging import get_logger
from app.pipeline.context import PipelineContext

logger = get_logger(__name__)

JOB: Final = "lessons"
MIN_CONFIRMATIONS: Final = 2
LESSON_TTL: Final = timedelta(days=90)
LESSON_CATEGORIES: Final = frozenset(
    {PostmortemCategory.MISSING_INFO, PostmortemCategory.MODEL_ERROR}
)
CATEGORY_TAG: Final = "category:"
_TEAM: Final = "team:"
_LEAGUE: Final = "league:"
_MARKET: Final = "market:"
MIN_REASON_ROOM: Final = 20
"""Characters that must remain for a reason to be worth appending to a lesson."""
_MARKET_LABELS: Final = {
    "1x2": "1X2",
    "double_chance": "Qo'sh imkoniyat",
    "totals": "Total",
    "btts": "Ikkalasi gol uradi",
    "asian_handicap": "Osiyo fora",
}
_TEMPLATES: Final = {
    PostmortemCategory.MISSING_INFO: (
        "{label}: {count} ta o'yinda yutqazish sababi - o'yindan oldin e'tibor berilmagan omil."
    ),
    PostmortemCategory.MODEL_ERROR: "{label}: {count} ta o'yinda model noto'g'ri baholagan.",
}

type Scope = tuple[str, ...]
"""The tags a lesson applies to, without its category: ``("team:7",)`` or league + market."""


@dataclass(frozen=True, slots=True)
class LessonDraft:
    """A lesson as the evidence supports it now (not yet stored, no text)."""

    category: PostmortemCategory
    scope: Scope
    confirmations: int
    first_seen: datetime
    last_seen: datetime
    reason: str

    @property
    def tags(self) -> tuple[str, ...]:
        """The identity of the lesson: its category tag and scope, sorted."""
        return tuple(sorted((f"{CATEGORY_TAG}{self.category.value}", *self.scope)))


def scopes_of(tags: Sequence[str]) -> list[Scope]:
    """The scopes one post-mortem can confirm: each team, and the league with its market."""
    scopes: list[Scope] = [(tag,) for tag in tags if tag.startswith(_TEAM)]
    league = next((tag for tag in tags if tag.startswith(_LEAGUE)), None)
    market = next((tag for tag in tags if tag.startswith(_MARKET)), None)
    if league is not None and market is not None:
        scopes.append((league, market))
    return scopes


def derive_lessons(facts: Sequence[PostmortemRecord], *, now: datetime) -> list[LessonDraft]:
    """The lessons the post-mortems of the last :data:`LESSON_TTL` support (pure).

    Evidence exactly :data:`LESSON_TTL` old is already out of the window.
    """
    oldest = now - LESSON_TTL
    evidence: defaultdict[tuple[PostmortemCategory, Scope], dict[int, PostmortemRecord]]
    evidence = defaultdict(dict)
    for fact in facts:
        if fact.category not in LESSON_CATEGORIES or not fact.reason.strip():
            continue
        if fact.created_at <= oldest or fact.created_at > now:
            continue
        for scope in scopes_of(fact.tags):
            seen = evidence[(fact.category, scope)]
            known = seen.get(fact.fixture_id)
            if known is None or (fact.created_at, fact.id) > (known.created_at, known.id):
                seen[fact.fixture_id] = fact
    return [_draft(category, scope, seen) for (category, scope), seen in evidence.items()]


def _draft(
    category: PostmortemCategory, scope: Scope, seen: Mapping[int, PostmortemRecord]
) -> LessonDraft:
    latest = max(seen.values(), key=lambda fact: (fact.created_at, fact.id))
    return LessonDraft(
        category=category,
        scope=scope,
        confirmations=len(seen),
        first_seen=min(fact.created_at for fact in seen.values()),
        last_seen=latest.created_at,
        reason=latest.reason.strip(),
    )


def next_status(confirmations: int, previous: LessonStatus | None) -> LessonStatus:
    """Active from :data:`MIN_CONFIRMATIONS`; below it, pending only if it was never active."""
    if confirmations >= MIN_CONFIRMATIONS:
        return LessonStatus.ACTIVE
    if previous in {None, LessonStatus.PENDING}:
        return LessonStatus.PENDING
    return LessonStatus.EXPIRED


def lesson_text(draft: LessonDraft, labels: Mapping[str, str]) -> str:
    """The templated lesson: scope, count and (if it fits) the latest sanitized reason."""
    label = " · ".join(_label(tag, labels) for tag in draft.scope)
    base = _TEMPLATES[draft.category].format(label=label, count=draft.confirmations)
    base = base[:MAX_LESSON_CHARS]
    room = MAX_LESSON_CHARS - len(base) - len(" Sabab: ")
    if room < MIN_REASON_ROOM:
        return base
    reason = draft.reason if len(draft.reason) <= room else _cut(draft.reason, room)
    return f"{base} Sabab: {reason}"


def _cut(text: str, room: int) -> str:
    """``text`` shortened to ``room`` characters at a word boundary, with an ellipsis."""
    clipped = text[: room - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{clipped}…"


def _label(tag: str, labels: Mapping[str, str]) -> str:
    if tag.startswith(_MARKET):
        key = tag.removeprefix(_MARKET)
        return _MARKET_LABELS.get(key, key)
    return labels.get(tag, tag)


def plan_changes(
    drafts: Sequence[LessonDraft], stored: Sequence[LessonRecord], labels: Mapping[str, str]
) -> list[LessonRecord]:
    """The lesson rows to write so the table matches the evidence (unchanged rows excluded).

    Stored lessons are matched on their exact tag set. A lesson without evidence any more is
    expired; one with evidence gets its count, last sighting, text and status refreshed.
    """
    by_tags = {record.tags: record for record in stored}
    changes: list[LessonRecord] = []
    covered: set[tuple[str, ...]] = set()
    for draft in drafts:
        previous = by_tags.get(draft.tags)
        covered.add(draft.tags)
        wanted = LessonRecord(
            id=None if previous is None else previous.id,
            text=lesson_text(draft, labels),
            tags=draft.tags,
            confirmations=draft.confirmations,
            status=next_status(draft.confirmations, None if previous is None else previous.status),
            first_seen=draft.first_seen if previous is None else previous.first_seen,
            last_seen=draft.last_seen,
        )
        if wanted != previous:
            changes.append(wanted)
    lapsed = [
        record
        for record in stored
        if record.tags not in covered and record.status is not LessonStatus.EXPIRED
    ]
    return [*changes, *(replace(record, status=LessonStatus.EXPIRED) for record in lapsed)]


@dataclass(frozen=True, slots=True)
class LessonReport:
    """The lessons after a run, and how many rows it wrote."""

    active: int = 0
    pending: int = 0
    expired: int = 0
    written: int = 0


async def _labels(ctx: PipelineContext, drafts: Sequence[LessonDraft]) -> dict[str, str]:
    """Names for the team and league tags of ``drafts`` (an unknown id keeps its tag)."""
    tags = {tag for draft in drafts for tag in draft.scope}
    labels: dict[str, str] = {}
    async with ctx.sessions() as session:
        for tag in sorted(tags):
            if tag.startswith(_TEAM) and (team := await get_team(session, _id(tag, _TEAM))):
                labels[tag] = team[1].name
            elif tag.startswith(_LEAGUE) and (
                league := await get_league(session, _id(tag, _LEAGUE))
            ):
                labels[tag] = league[1].name
    return labels


def _id(tag: str, prefix: str) -> int:
    return int(tag.removeprefix(prefix))


async def run_lessons(ctx: PipelineContext) -> LessonReport:
    """Rebuild the lessons from the post-mortems of the last 90 days (see the module docstring)."""
    now = ctx.clock()
    async with ctx.sessions() as session:
        facts = await list_postmortems(session, since=now - LESSON_TTL)
    drafts = derive_lessons(facts, now=now)
    labels = await _labels(ctx, drafts)
    async with ctx.sessions.begin() as session:
        stored = await list_lessons(session)
        changes = plan_changes(drafts, stored, labels)
        for change in changes:
            await save_lesson(session, change)
        final = await list_lessons(session)
    report = LessonReport(
        active=sum(r.status is LessonStatus.ACTIVE for r in final),
        pending=sum(r.status is LessonStatus.PENDING for r in final),
        expired=sum(r.status is LessonStatus.EXPIRED for r in final),
        written=len(changes),
    )
    await ctx.ledger.record_run(
        JOB,
        ok=True,
        detail=f"active={report.active} pending={report.pending} written={report.written}",
        now=ctx.clock(),
    )
    logger.info("lessons_rebuilt", active=report.active, written=report.written)
    return report
