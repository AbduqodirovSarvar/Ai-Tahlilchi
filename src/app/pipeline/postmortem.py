"""Post-mortems of lost signals (F8.3, plan 6.13 layer 4).

A daily batch, after the results are in: every recommended, non-retro signal that lost and has
no post-mortem yet is explained once by the LLM (``omadsizlik`` / ``ma'lumot yetishmadi`` /
``model xatosi`` plus a short sanitized reason) and stored with team/league/market tags.

Safety rules:

- **Never in the way of settlement.** A separate job; a missing or exhausted LLM only means
  "not yet": nothing is stored, the signal stays unexplained and the next run tries again
  (until it is older than ``postmortem_lookback``).
- **Idempotent per signal.** A signal that has a post-mortem is never explained again (a crash
  between the LLM answer and the write costs one repeated call, never a duplicate row).
- **One explanation per pick.** The same pick delivered to several users is one event: it is
  explained once and the answer is copied to each user's signal.
- **Budget aware.** At most ``postmortems_per_run`` LLM calls per run, and the run stops early
  when the LLM keeps answering nothing (disabled, over its daily budget, down).
- **All inputs are data.** The case is built from stored facts only (probabilities, score, xG,
  form, the original review); the model's answer is sanitized by the LLM layer before it is
  stored, and an empty ``reason`` (rejected by its guard) is stored empty, never invented.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError

from app.db.enums import PostmortemCategory
from app.db.repo import SignalRecord, get_signal
from app.db.repo.feedback import add_postmortem, lost_signal_ids, pick_postmortem
from app.llm.inputs import MAX_USER_NOTE_CHARS, CandidateInput, PostmortemInput
from app.llm.schemas import PostmortemCategory as LlmCategory
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.form import forms_for
from app.pipeline.review import wire_verdict

logger = get_logger(__name__)

JOB: Final = "postmortem"
MAX_STORED_REASON_CHARS: Final = 300
MAX_SILENT_CALLS: Final = 3
"""Consecutive "no answer" results after which the LLM is presumed unavailable for this run."""
CANDIDATES_PER_CALL: Final = 3
"""Signals read per allowed LLM call: the same pick of several users shares one call."""
_CATEGORIES: Final = {
    LlmCategory.BAD_LUCK: PostmortemCategory.BAD_LUCK,
    LlmCategory.MISSING_INFO: PostmortemCategory.MISSING_INFO,
    LlmCategory.MODEL_ERROR: PostmortemCategory.MODEL_ERROR,
}

type PickKey = tuple[int, str, float, str]
"""``(fixture id, market type, line, selection)``: the identity of a pick across users."""


@dataclass(frozen=True, slots=True)
class PostmortemReport:
    """What one run did."""

    explained: int = 0
    """Signals that got a post-mortem from an LLM call."""
    copied: int = 0
    """Signals that got the stored post-mortem of the same pick."""
    skipped: int = 0
    """Picks the LLM could not answer now (retried by a later run)."""
    invalid: int = 0
    """Picks whose case could not be built (kept for the log; never retried successfully)."""


def tags_of(record: SignalRecord) -> tuple[str, ...]:
    """The team, league and market tags of a signal (the keys lessons are matched on)."""
    fixture = record.fixture
    tags = [
        f"team:{fixture.home_team_id}",
        f"team:{fixture.away_team_id}",
        f"league:{fixture.league_id}",
        f"market:{record.market.type.value}",
    ]
    return tuple(tag for tag in tags if "None" not in tag)


def pick_key(record: SignalRecord) -> PickKey:
    """The identity of the pick behind ``record``."""
    line = 0.0 if record.market.line is None else record.market.line
    return (record.fixture.id, record.market.type.value, line, record.selection.value)


async def build_case(ctx: PipelineContext, record: SignalRecord) -> PostmortemInput:
    """The LLM input of a lost signal: what was known before the match, and what happened.

    Raises:
        ValidationError: a stored value does not fit the LLM input (e.g. a 100+ goal score).
        ValueError: the signal has no final score.
    """
    fixture = record.fixture
    result = fixture.result
    if result is None or result.home_score is None or result.away_score is None:
        msg = f"signal {record.id} has no final score"
        raise ValueError(msg)
    forms = (await forms_for(ctx, [fixture])).get(fixture.id, ())
    line = record.market.line
    candidate = CandidateInput(
        candidate_id=":".join(map(str, (fixture.id, *pick_key(record)[1:]))),
        league=fixture.fixture.league.name,
        home_team=fixture.fixture.home.name,
        away_team=fixture.fixture.away.name,
        kickoff=fixture.fixture.kickoff,
        market=record.market.type.value,
        selection=record.selection.value,
        line=line,
        odds=record.first_odds,
        p_model=record.p_model,
        ev=record.ev,
        confidence_level=record.confidence.value,
        forms=forms,
    )
    xg = (
        None
        if fixture.home_xg is None or fixture.away_xg is None
        else f"xG {fixture.home_xg:.2f}-{fixture.away_xg:.2f}"
    )
    note = (record.llm_note or "").strip()[:MAX_USER_NOTE_CHARS] or None
    return PostmortemInput(
        candidate=candidate,
        final_score=f"{result.home_score}-{result.away_score}",
        xg_summary=xg,
        original_verdict=None if record.llm_verdict is None else wire_verdict(record.llm_verdict),
        original_note=note,
    )


async def run_postmortems(ctx: PipelineContext) -> PostmortemReport:
    """Explain the recent unexplained losses (see the module docstring)."""
    now = ctx.clock()
    config = ctx.config
    async with ctx.sessions() as session:
        ids = await lost_signal_ids(
            session,
            settled_since=now - config.postmortem_lookback,
            limit=config.postmortems_per_run * CANDIDATES_PER_CALL,
        )
        records = [r for i in ids if (r := await get_signal(session, i)) is not None]
    groups: defaultdict[PickKey, list[SignalRecord]] = defaultdict(list)
    for record in records:
        groups[pick_key(record)].append(record)
    counts = {"explained": 0, "copied": 0, "skipped": 0, "invalid": 0}
    calls = silent = 0
    for group in groups.values():
        if await _copy_known(ctx, group, counts):
            continue
        if calls >= config.postmortems_per_run or silent >= MAX_SILENT_CALLS:
            counts["skipped"] += 1
            continue
        calls += 1
        answered = await _explain(ctx, group, counts)
        silent = 0 if answered else silent + 1
    report = PostmortemReport(**counts)
    await ctx.ledger.record_run(JOB, ok=True, detail=_describe(report), now=ctx.clock())
    logger.info("postmortems_done", **counts)
    return report


def _describe(report: PostmortemReport) -> str:
    return (
        f"explained={report.explained} copied={report.copied} "
        f"skipped={report.skipped} invalid={report.invalid}"
    )


async def _copy_known(
    ctx: PipelineContext, group: list[SignalRecord], counts: dict[str, int]
) -> bool:
    """Give the group the post-mortem its pick already has (another user's), if any."""
    async with ctx.sessions() as session:
        known = await pick_postmortem(session, group[0].id)
    if known is None:
        return False
    async with ctx.sessions.begin() as session:
        for record in group:
            await add_postmortem(
                session,
                signal_id=record.id,
                category=known.category,
                reason=known.reason,
                tags=tags_of(record),
                created_at=ctx.clock(),
            )
    counts["copied"] += len(group)
    return True


async def _explain(ctx: PipelineContext, group: list[SignalRecord], counts: dict[str, int]) -> bool:
    """Ask the LLM about the group's pick and store the answer; ``False`` = no answer."""
    head = group[0]
    try:
        case = await build_case(ctx, head)
    except (ValidationError, ValueError) as error:
        logger.warning("postmortem_case_invalid", signal_id=head.id, error=repr(error))
        counts["invalid"] += 1
        return True  # not the LLM's fault: do not count it as silence
    answer = await ctx.postmortem.classify(case)
    if answer is None:
        counts["skipped"] += 1
        return False
    reason = answer.reason.strip()[:MAX_STORED_REASON_CHARS]
    async with ctx.sessions.begin() as session:
        for record in group:
            await add_postmortem(
                session,
                signal_id=record.id,
                category=_CATEGORIES[answer.category],
                reason=reason,
                tags=tags_of(record),
                created_at=ctx.clock(),
            )
    counts["explained"] += len(group)
    return True
