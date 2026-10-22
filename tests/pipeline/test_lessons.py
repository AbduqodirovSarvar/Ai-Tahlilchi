"""Lessons (F8.4): confirmed by distinct fixtures, tagged, templated, expiring after 90 days."""

from dataclasses import replace
from datetime import timedelta

from hypothesis import given
from hypothesis import strategies as st

from app.db.enums import LessonStatus, PostmortemCategory
from app.db.repo.feedback import LessonRecord, PostmortemRecord, list_lessons
from app.llm.inputs import MAX_LESSON_CHARS
from app.llm.schemas import PostmortemCategory as LlmCategory
from app.pipeline.lessons import (
    LESSON_TTL,
    LessonDraft,
    derive_lessons,
    lesson_text,
    next_status,
    plan_changes,
    run_lessons,
    scopes_of,
)
from app.pipeline.postmortem import run_postmortems
from tests.pipeline.doubles import ScriptedAnalyst, verdict
from tests.pipeline.feedback_world import NOW, finished_fixture, open_world, place, settle

MISSING = PostmortemCategory.MISSING_INFO
MODEL = PostmortemCategory.MODEL_ERROR
TAGS = ("team:1", "team:2", "league:3", "market:totals")
LABELS = {"team:1": "Chelsea", "team:2": "Arsenal", "league:3": "Premier League"}


def fact(  # noqa: PLR0913 - the columns of one post-mortem
    row: int,
    fixture: int,
    *,
    category: PostmortemCategory = MISSING,
    reason: str = "Asosiy himoyachi yo'q edi.",
    tags: tuple[str, ...] = TAGS,
    age: timedelta = timedelta(days=1),
) -> PostmortemRecord:
    return PostmortemRecord(
        id=row,
        signal_id=row,
        fixture_id=fixture,
        category=category,
        reason=reason,
        tags=tags,
        created_at=NOW - age,
    )


def by_scope(drafts: list[LessonDraft]) -> dict[tuple[str, ...], LessonDraft]:
    return {draft.scope: draft for draft in drafts if draft.category is MISSING}


# ----------------------------------------------------------------------------- derivation
def test_one_post_mortem_supports_a_pending_lesson_per_scope() -> None:
    scopes = by_scope(derive_lessons([fact(1, 10)], now=NOW))

    assert set(scopes) == {("team:1",), ("team:2",), ("league:3", "market:totals")}
    assert all(draft.confirmations == 1 for draft in scopes.values())
    assert all(next_status(d.confirmations, None) is LessonStatus.PENDING for d in scopes.values())


def test_two_distinct_fixtures_confirm_a_lesson() -> None:
    drafts = derive_lessons([fact(1, 10), fact(2, 11)], now=NOW)

    assert all(draft.confirmations == 2 for draft in drafts)
    assert all(next_status(d.confirmations, None) is LessonStatus.ACTIVE for d in drafts)


def test_the_same_fixture_explained_twice_is_one_confirmation() -> None:
    drafts = derive_lessons([fact(1, 10), fact(2, 10), fact(3, 10)], now=NOW)

    assert {draft.confirmations for draft in drafts} == {1}


def test_only_reversible_causes_with_a_reason_count() -> None:
    facts = [
        fact(1, 10, category=PostmortemCategory.BAD_LUCK),  # luck is not a lesson
        fact(2, 11, reason=""),  # the guard rejected the reason
        fact(3, 12, reason="   "),
        fact(4, 13),
    ]

    drafts = derive_lessons(facts, now=NOW)

    assert {draft.confirmations for draft in drafts} == {1}
    assert {draft.category for draft in drafts} == {MISSING}


def test_categories_are_separate_lessons() -> None:
    drafts = derive_lessons([fact(1, 10), fact(2, 11, category=MODEL)], now=NOW)

    assert {(d.category, d.confirmations) for d in drafts} == {(MISSING, 1), (MODEL, 1)}
    assert len({draft.tags for draft in drafts}) == len(drafts)


def test_evidence_exactly_ninety_days_old_has_expired() -> None:
    boundary = LESSON_TTL
    older = derive_lessons([fact(1, 10, age=boundary)], now=NOW)
    inside = derive_lessons([fact(1, 10, age=boundary - timedelta(seconds=1))], now=NOW)
    future = derive_lessons([fact(1, 10, age=-timedelta(seconds=1))], now=NOW)

    assert older == []
    assert len(inside) == 3
    assert future == []


def test_the_latest_reason_and_the_sighting_dates_are_kept() -> None:
    old = fact(1, 10, reason="Birinchi sabab.", age=timedelta(days=10))
    new = fact(2, 11, reason="Oxirgi sabab.", age=timedelta(days=2))

    [draft] = [d for d in derive_lessons([old, new], now=NOW) if d.scope == ("team:1",)]

    assert draft.reason == "Oxirgi sabab."
    assert draft.first_seen == NOW - timedelta(days=10)
    assert draft.last_seen == NOW - timedelta(days=2)
    assert draft.tags == ("category:missing_info", "team:1")


def test_a_post_mortem_without_a_league_and_market_still_confirms_its_teams() -> None:
    assert scopes_of(("team:1", "team:2")) == [("team:1",), ("team:2",)]
    assert scopes_of(("league:3",)) == []  # a league alone is not a scope
    assert scopes_of(("market:totals",)) == []


def test_status_transitions() -> None:
    assert next_status(2, None) is LessonStatus.ACTIVE
    assert next_status(5, LessonStatus.EXPIRED) is LessonStatus.ACTIVE  # fresh evidence revives
    assert next_status(1, None) is LessonStatus.PENDING
    assert next_status(1, LessonStatus.PENDING) is LessonStatus.PENDING
    assert next_status(1, LessonStatus.ACTIVE) is LessonStatus.EXPIRED  # support fell away
    assert next_status(1, LessonStatus.EXPIRED) is LessonStatus.EXPIRED


# ----------------------------------------------------------------------------- the text
def draft_of(scope: tuple[str, ...], reason: str, count: int = 2, category=MISSING) -> LessonDraft:  # type: ignore[no-untyped-def]
    return LessonDraft(
        category=category,
        scope=scope,
        confirmations=count,
        first_seen=NOW - timedelta(days=3),
        last_seen=NOW,
        reason=reason,
    )


def test_a_team_lesson_is_templated_with_the_count_and_the_reason() -> None:
    text = lesson_text(draft_of(("team:1",), "Asosiy himoyachi yo'q edi."), LABELS)

    assert text == (
        "Chelsea: 2 ta o'yinda yutqazish sababi - o'yindan oldin e'tibor berilmagan omil. "
        "Sabab: Asosiy himoyachi yo'q edi."
    )


def test_a_league_market_lesson_names_both() -> None:
    text = lesson_text(
        draft_of(("league:3", "market:totals"), "Hakam qattiq edi.", category=MODEL), LABELS
    )

    assert text.startswith("Premier League · Total: 2 ta o'yinda model noto'g'ri baholagan.")
    assert text.endswith("Sabab: Hakam qattiq edi.")


def test_an_unknown_id_keeps_its_tag_and_an_unknown_market_its_key() -> None:
    assert lesson_text(draft_of(("team:99",), ""), {}).startswith("team:99:")
    text = lesson_text(draft_of(("league:3", "market:corners"), ""), LABELS)
    assert text.startswith("Premier League · corners:")


def test_a_long_reason_is_cut_at_a_word_with_an_ellipsis() -> None:
    reason = "so'z " * 80
    text = lesson_text(draft_of(("team:1",), reason), LABELS)

    assert len(text) <= MAX_LESSON_CHARS
    assert text.endswith("…")
    assert "Sabab: so'z so'z" in text


def test_no_reason_is_added_when_there_is_no_room() -> None:
    long_name = {"team:1": "N" * 130}
    text = lesson_text(draft_of(("team:1",), "Qisqa sabab."), long_name)

    assert "Sabab" not in text
    assert len(text) <= MAX_LESSON_CHARS


@given(
    label=st.text(max_size=300),
    reason=st.text(max_size=600),
    count=st.integers(min_value=1, max_value=999),
)
def test_a_lesson_text_never_exceeds_the_prompt_limit(label: str, reason: str, count: int) -> None:
    text = lesson_text(draft_of(("team:1",), reason, count=count), {"team:1": label})

    assert len(text) <= MAX_LESSON_CHARS


# ----------------------------------------------------------------------------- reconciling
def stored_from(changes: list[LessonRecord], stored: list[LessonRecord]) -> list[LessonRecord]:
    """The table after writing ``changes`` (ids are handed out like the database does)."""
    table = {record.tags: record for record in stored}
    next_id = max((r.id or 0 for r in stored), default=0) + 1
    for change in changes:
        numbered = change
        if change.id is None:
            numbered = replace(change, id=next_id)
            next_id += 1
        table[numbered.tags] = numbered
    return list(table.values())


def test_planning_inserts_then_converges() -> None:
    drafts = derive_lessons([fact(1, 10), fact(2, 11)], now=NOW)

    first = plan_changes(drafts, [], LABELS)
    table = stored_from(first, [])
    second = plan_changes(drafts, table, LABELS)

    assert len(first) == len(drafts) == 3
    assert {change.status for change in first} == {LessonStatus.ACTIVE}
    assert second == []


def test_a_lesson_whose_evidence_is_gone_expires_once() -> None:
    table = stored_from(
        plan_changes(derive_lessons([fact(1, 10), fact(2, 11)], now=NOW), [], LABELS), []
    )

    later = plan_changes([], table, LABELS)
    table = stored_from(later, table)
    again = plan_changes([], table, LABELS)

    assert {change.status for change in later} == {LessonStatus.EXPIRED}
    assert len(later) == 3
    assert again == []
    assert {record.confirmations for record in table} == {2}  # the history is kept


def test_an_active_lesson_that_loses_a_confirmation_expires() -> None:
    both = derive_lessons([fact(1, 10), fact(2, 11, age=timedelta(days=89))], now=NOW)
    table = stored_from(plan_changes(both, [], LABELS), [])
    assert {r.status for r in table} == {LessonStatus.ACTIVE}

    one_left = derive_lessons(
        [fact(1, 10), fact(2, 11, age=timedelta(days=89))], now=NOW + timedelta(days=2)
    )
    changes = plan_changes(one_left, table, LABELS)

    assert {change.status for change in changes} == {LessonStatus.EXPIRED}
    assert {change.confirmations for change in changes} == {1}


def test_an_expired_lesson_is_revived_by_fresh_evidence_and_keeps_its_first_sighting() -> None:
    old = derive_lessons(
        [fact(1, 10, age=timedelta(days=80)), fact(2, 11, age=timedelta(days=79))], now=NOW
    )
    table = stored_from(plan_changes(old, [], LABELS), [])
    expired = stored_from(plan_changes([], table, LABELS), table)
    fresh = derive_lessons([fact(3, 12, age=timedelta(days=1)), fact(4, 13)], now=NOW)

    revived = plan_changes(fresh, expired, LABELS)

    assert {c.status for c in revived} == {LessonStatus.ACTIVE}
    assert {c.first_seen for c in revived} == {NOW - timedelta(days=80)}


facts_strategy = st.lists(
    st.builds(
        PostmortemRecord,
        id=st.integers(min_value=1, max_value=10_000),
        signal_id=st.integers(min_value=1, max_value=10_000),
        fixture_id=st.integers(min_value=1, max_value=6),
        category=st.sampled_from(list(PostmortemCategory)),
        reason=st.sampled_from(["", "Sabab bor.", "Boshqa sabab."]),
        tags=st.sampled_from([TAGS, ("team:1",), ("team:2", "league:3", "market:btts"), ()]),
        created_at=st.integers(min_value=0, max_value=120).map(lambda d: NOW - timedelta(days=d)),
    ),
    max_size=14,
)


@given(facts=facts_strategy)
def test_confirmations_never_exceed_distinct_fixtures_and_planning_converges(
    facts: list[PostmortemRecord],
) -> None:
    drafts = derive_lessons(facts, now=NOW)

    for draft in drafts:
        supporting = {
            f.fixture_id
            for f in facts
            if f.category is draft.category
            and f.reason.strip()
            and NOW - LESSON_TTL < f.created_at <= NOW
            and all(tag in f.tags for tag in draft.scope)
        }
        assert draft.confirmations == len(supporting)
    table = stored_from(plan_changes(drafts, [], LABELS), [])
    assert plan_changes(drafts, table, LABELS) == []


# ----------------------------------------------------------------------------- the job
async def lost_pair(bare, names):  # type: ignore[no-untyped-def]
    for index, fixture in enumerate(names):
        await settle(bare, await place(bare, fixture), at=NOW - timedelta(hours=10 - index))


async def test_the_job_builds_activates_and_expires_lessons(tmp_path) -> None:  # type: ignore[no-untyped-def]
    one, two = finished_fixture("X1"), finished_fixture("X2")
    analyst = ScriptedAnalyst(default=verdict(LlmCategory.MISSING_INFO, "Hujumchi o'ynamadi."))
    async with open_world(
        tmp_path / "lessons.db", (one, 0, 2), (two, 1, 3), analyst=analyst
    ) as bare:
        await lost_pair(bare, [one, two])
        await run_postmortems(bare.ctx)

        built = await run_lessons(bare.ctx)
        async with bare.sessions() as session:
            lessons = await list_lessons(session)
        again = await run_lessons(bare.ctx)

        bare.clock.now = NOW + LESSON_TTL - timedelta(seconds=1)
        still = await run_lessons(bare.ctx)
        bare.clock.now = NOW + LESSON_TTL
        expired = await run_lessons(bare.ctx)

    assert (built.active, built.pending, built.written) == (1, 4, 5)
    [active] = [r for r in lessons if r.status is LessonStatus.ACTIVE]
    assert active.confirmations == 2
    assert active.tags[0] == "category:missing_info"
    assert active.text.startswith("Premier League · 1X2: 2 ta o'yinda yutqazish sababi")
    assert active.text.endswith("Sabab: Hujumchi o'ynamadi.")
    assert again.written == 0  # a rebuild of the same evidence changes nothing
    assert still.active == 1  # a second before the 90th day it is still valid
    assert (expired.active, expired.pending, expired.expired) == (0, 0, 5)


async def test_without_evidence_the_job_writes_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with open_world(tmp_path / "empty.db") as bare:
        report = await run_lessons(bare.ctx)
        runs = await bare.ctx.ledger.last_runs()

    assert (report.active, report.pending, report.expired, report.written) == (0, 0, 0, 0)
    assert runs["lessons"].ok
