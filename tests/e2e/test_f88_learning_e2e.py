"""F8.8 through the real dispatcher: ``/ozgarishlar``, undo, the weekly summary and ``/holat``.

The fake services carry a hand-built journal (shadow vs applied, stale entries, paging); the
real services run over a seeded database for the end-to-end rollback.
"""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Dispatcher

from app.bot import texts
from app.bot.callbacks import LearnCb
from app.bot.learning_ui import format_summary
from app.db.repo import (
    CalibrationRecord,
    add_calibration_version,
    add_learning_change,
    set_active_calibration,
)
from app.pipeline.learning import KIND_CALIBRATION_ACTIVATED, KIND_SEGMENT_DISABLED
from app.services.interfaces import (
    LearningChangeKind,
    LearningChangeView,
    LearningMode,
    LearningSummary,
    Services,
)
from tests.bot.fakes import FIXED_NOW
from tests.e2e.bot_client import Client, answers, buttons_of, shown, telegram_html_problems
from tests.e2e.real_world import ADMIN, SECOND, RealBot

HOSTILE = '<b>Liga</b> & "Seriya"'


def make_change(change_id: int, **overrides: object) -> LearningChangeView:
    values: dict[str, object] = {
        "change_id": change_id,
        "at": FIXED_NOW - timedelta(days=change_id),
        "kind": LearningChangeKind.SEGMENT_DISABLED,
        "subject": f"39|totals|1.5-2.0|{change_id}",
        "reason": "roi_negative",
        "applied": True,
        "rolled_back": False,
        "ev_floor": 0.05,
        "samples": 80,
    }
    values.update(overrides)
    return LearningChangeView(**values)  # type: ignore[arg-type]


def journal(services: Services):  # type: ignore[no-untyped-def]  # the fake's seedable state
    return services.learning.state  # type: ignore[attr-defined]


def undo_data(client: Client, change_id: int, message_id: int) -> str:
    wanted = f"#{change_id}"
    for button in buttons_of(client.markups.get(message_id)):
        if button.text.endswith(wanted) and button.callback_data:
            return str(button.callback_data)
    msg = f"no undo button for {wanted}"
    raise AssertionError(msg)


async def open_journal(client: Client) -> int:
    await client.send("/ozgarishlar")
    return client.last_message_id


# --------------------------------------------------------------------------- the list
async def test_an_empty_journal_says_so_and_names_the_mode(admin: Client) -> None:
    text = "\n".join(shown(await admin.send("/ozgarishlar")))

    assert texts.LEARN_EMPTY in text
    assert texts.LEARN_MODES["shadow"] in text


async def test_shadow_entries_say_they_would_have_acted_and_have_no_undo_button(
    admin: Client, services: Services
) -> None:
    journal(services).add(make_change(1, applied=False))
    journal(services).add(make_change(2, applied=True))
    message_id = await open_journal(admin)

    text = admin.texts[message_id]
    buttons = [b.text for b in buttons_of(admin.markups[message_id])]

    assert texts.LEARN_STATE_SHADOW.strip() in text
    assert texts.LEARN_STATE_APPLIED.strip() in text
    assert texts.BTN_LEARN_UNDO.format(id=2) in buttons
    assert texts.BTN_LEARN_UNDO.format(id=1) not in buttons  # nothing was changed to undo


async def test_the_newest_entry_comes_first(admin: Client, services: Services) -> None:
    for change_id in (3, 1, 2):
        journal(services).add(make_change(change_id))

    text = admin.texts[await open_journal(admin)]

    assert text.index("#1") < text.index("#2") < text.index("#3")


async def test_the_journal_is_paged_and_every_entry_is_reachable(
    admin: Client, services: Services
) -> None:
    for change_id in range(1, 26):
        journal(services).add(make_change(change_id))
    message_id = await open_journal(admin)

    seen: list[int] = []
    for _ in range(10):
        undo = [
            b.callback_data for b in buttons_of(admin.markups[message_id]) if b.text.startswith("↩️")
        ]
        seen += [int(str(d).split(":")[2]) for d in undo]
        forward = [b.callback_data for b in buttons_of(admin.markups[message_id]) if b.text == "▶️"]
        if not forward:
            break
        await admin.press_data(str(forward[0]), message_id)

    assert sorted(seen) == list(range(1, 26))


async def test_hostile_text_in_the_journal_is_escaped(admin: Client, services: Services) -> None:
    journal(services).add(make_change(1, subject=HOSTILE, reason="<i>x</i>"))

    text = admin.texts[await open_journal(admin)]

    assert HOSTILE not in text
    assert "&lt;b&gt;Liga&lt;/b&gt;" in text
    assert telegram_html_problems(text) == []


async def test_off_mode_is_named(admin: Client, services: Services) -> None:
    journal(services).mode = LearningMode.OFF

    text = admin.texts[await open_journal(admin)]

    assert texts.LEARN_MODES["off"] in text


# --------------------------------------------------------------------------- undo
async def test_undo_asks_first_then_rolls_back_and_states_the_hold(
    admin: Client, services: Services
) -> None:
    journal(services).add(make_change(7))
    message_id = await open_journal(admin)

    asked = shown(await admin.press_data(undo_data(admin, 7, message_id), message_id))
    assert "#7" in asked[0]
    assert "30 kun" in asked[0]
    assert not journal(services).journal[0].rolled_back  # asking changed nothing

    yes = next(b.callback_data for b in buttons_of(admin.markups[message_id]) if "Ha" in b.text)
    done = shown(await admin.press_data(str(yes), message_id))

    assert done == [texts.LEARN_UNDO_OUTCOMES["rolled_back"].format(days=30)]
    assert journal(services).journal[0].rolled_back
    back = [b.callback_data for b in buttons_of(admin.markups[message_id])]
    listing = shown(await admin.press_data(str(back[0]), message_id))
    assert texts.LEARN_STATE_ROLLED_BACK.strip() in listing[0]


async def test_a_stale_entry_is_refused_with_the_reason_and_changes_nothing(
    admin: Client, services: Services
) -> None:
    journal(services).add(make_change(7))
    journal(services).stale.add(7)
    message_id = await open_journal(admin)
    await admin.press_data(undo_data(admin, 7, message_id), message_id)
    yes = next(b.callback_data for b in buttons_of(admin.markups[message_id]) if "Ha" in b.text)

    done = shown(await admin.press_data(str(yes), message_id))

    assert done == [texts.LEARN_UNDO_OUTCOMES["stale"].format(days=30)]
    assert not journal(services).journal[0].rolled_back


async def test_a_double_tap_on_yes_says_already_undone(admin: Client, services: Services) -> None:
    journal(services).add(make_change(7))
    message_id = await open_journal(admin)
    await admin.press_data(undo_data(admin, 7, message_id), message_id)
    yes = str(
        next(b.callback_data for b in buttons_of(admin.markups[message_id]) if "Ha" in b.text)
    )

    await admin.press_data(yes, message_id)
    second = shown(await admin.press_data(yes, message_id))

    assert second == [texts.LEARN_UNDO_OUTCOMES["already_rolled_back"].format(days=30)]


async def test_buttons_of_entries_that_moved_or_vanished_are_stale(
    admin: Client, services: Services
) -> None:
    journal(services).add(make_change(7))
    message_id = await open_journal(admin)
    journal(services).journal.clear()  # gone meanwhile

    methods = await admin.press_data(undo_data(admin, 7, message_id), message_id)

    (reply,) = answers(methods)
    assert "eskirgan" in (reply.text or "")


async def test_undoing_an_unknown_entry_is_reported_not_crashed(admin: Client) -> None:
    methods = await admin.press_data(LearnCb(action="undo", id=999, page=0).pack(), 5)

    assert shown(methods) == [texts.LEARN_UNDO_OUTCOMES["not_found"].format(days=30)]


async def test_the_second_user_sees_the_same_system_wide_journal(
    dispatcher: Dispatcher, services: Services, admin: Client
) -> None:
    from tests.e2e.conftest import SECOND_ADMIN_ID  # noqa: PLC0415

    journal(services).add(make_change(1))
    other = Client(dispatcher, admin.bot, admin.session, SECOND_ADMIN_ID)

    assert "#1" in other.texts.get(await open_journal(other), "")


# --------------------------------------------------------------------------- weekly summary
def _summary(*changes: LearningChangeView, mode: LearningMode = LearningMode.SHADOW) -> str:
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    summary = LearningSummary(week=FIXED_NOW.date(), mode=mode, changes=changes)
    return format_summary(summary, ZoneInfo("Asia/Tashkent"))


def test_a_quiet_week_says_nothing_changed() -> None:
    text = _summary()

    assert texts.LEARN_SUMMARY_NONE in text
    assert texts.LEARN_MODES["shadow"] in text


def test_the_weekly_summary_counts_by_kind_and_marks_shadow_entries() -> None:
    text = _summary(
        make_change(1, kind=LearningChangeKind.SEGMENT_DISABLED, applied=False),
        make_change(2, kind=LearningChangeKind.SEGMENT_RAISED),
        make_change(3, kind=LearningChangeKind.CALIBRATION_ACTIVATED, version=4, samples=300),
    )

    assert "kalibratsiya: 1" in text.lower()
    assert "o'chirilgan segment: 1" in text
    assert texts.LEARN_STATE_SHADOW.strip() in text
    assert "versiya 4" in text
    assert "/ozgarishlar" in text
    assert telegram_html_problems(text) == []


def test_a_long_week_is_cut_with_a_remainder_line() -> None:
    text = _summary(*[make_change(n) for n in range(1, 30)])

    assert "…va yana" in text
    assert len(text) < 4096


def test_hostile_subject_is_escaped_in_the_summary() -> None:
    text = _summary(make_change(1, subject=HOSTILE))

    assert HOSTILE not in text
    assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- /holat
async def test_status_shows_the_learning_mode_and_the_missed_week_warning(
    admin: Client, services: Services
) -> None:
    journal(services).missed = True

    text = "\n".join(shown(await admin.send("/holat")))

    assert texts.STATUS_LEARNING_HEADER in text
    assert texts.STATUS_LEARNING_MODE["shadow"] in text
    assert texts.STATUS_LEARNING_MISSED in text


async def test_status_without_a_missed_week_has_no_warning(admin: Client) -> None:
    text = "\n".join(shown(await admin.send("/holat")))

    assert texts.STATUS_LEARNING_HEADER in text
    assert texts.STATUS_LEARNING_MISSED not in text


# --------------------------------------------------------------------------- real services


@pytest.mark.slow
async def test_real_rollback_of_a_calibration_activation_and_the_stale_refusal(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    now = datetime(2026, 10, 20, tzinfo=UTC)
    async with rb.env.sessions.begin() as session:
        for version in (1, 2):
            await add_calibration_version(
                session,
                CalibrationRecord(
                    version=version,
                    method="isotonic",
                    params={"x": [0.0, 1.0], "y": [0.0, 1.0]},
                    n_samples=300,
                    brier_before=0.25,
                    brier_after=0.24,
                    active=False,
                    passed_gate=True,
                    created_at=now,
                ),
            )
        await set_active_calibration(session, 2)
        old = await add_learning_change(
            session,
            kind=KIND_CALIBRATION_ACTIVATED,
            subject="calibration:1",
            before={"version": None},
            after={"version": 1, "n_samples": 300},
            reason="gate_passed",
            now=now,
        )
        newest = await add_learning_change(
            session,
            kind=KIND_CALIBRATION_ACTIVATED,
            subject="calibration:2",
            before={"version": 1},
            after={"version": 2, "n_samples": 400},
            reason="gate_passed",
            now=now + timedelta(days=7),
        )
        shadow = await add_learning_change(
            session,
            kind=KIND_SEGMENT_DISABLED,
            subject="39|totals|1.5-2.0",
            before={},
            after={"ev_threshold": 0.0, "evidence": {"n": 80}},
            reason="roi_negative",
            now=now + timedelta(days=1),
            applied=False,
        )
    admin = rb.client()
    message_id = await open_journal(admin)
    assert f"#{shadow.id}" in admin.texts[message_id]
    buttons = [b.text for b in buttons_of(admin.markups[message_id])]
    assert texts.BTN_LEARN_UNDO.format(id=shadow.id) not in buttons

    # the older activation is no longer the state: refused as stale, nothing overwritten
    await admin.press_data(undo_data(admin, old.id, message_id), message_id)
    yes = str(
        next(b.callback_data for b in buttons_of(admin.markups[message_id]) if "Ha" in b.text)
    )
    stale = shown(await admin.press_data(yes, message_id))
    assert stale == [texts.LEARN_UNDO_OUTCOMES["stale"].format(days=30)]

    # the newest one can be undone, by the other user as well (the model is shared)
    other = rb.client(SECOND)
    other_id = await open_journal(other)
    await other.press_data(undo_data(other, newest.id, other_id), other_id)
    yes2 = str(next(b.callback_data for b in buttons_of(other.markups[other_id]) if "Ha" in b.text))
    done = shown(await other.press_data(yes2, other_id))
    assert done == [texts.LEARN_UNDO_OUTCOMES["rolled_back"].format(days=30)]
    page = await rb.services.learning.list_changes(ADMIN)
    assert {c.change_id: c.rolled_back for c in page.items}[newest.id]
    assert page.mode is LearningMode.SHADOW
    assert page.hold_days == 30


@pytest.mark.slow
async def test_real_status_reports_the_mode_and_flags_an_overdue_weekly_run(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    ctx = rb.env.ctx
    now = rb.env.clock()
    await ctx.ledger.record_run(
        "learning", ok=True, detail="signals=1", now=now - timedelta(days=9)
    )
    admin = rb.client()

    text = "\n".join(shown(await admin.send("/holat")))

    assert texts.STATUS_LEARNING_MODE["shadow"] in text
    assert texts.STATUS_LEARNING_MISSED in text
    await ctx.ledger.record_run(
        "learning", ok=True, detail="signals=1", now=now - timedelta(days=2)
    )
    fresh = "\n".join(shown(await admin.send("/holat")))
    assert texts.STATUS_LEARNING_MISSED not in fresh
