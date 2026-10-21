"""Tests for the wizard draft (JSON round trip, stale detection) and FSM data helpers."""

import json
from dataclasses import replace
from datetime import date

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.draft import StaleDraftError, WizardDraft
from app.bot.states import (
    RESULTS_TOKEN_MODULUS,
    StoredResults,
    drop_draft,
    load_draft,
    load_results,
    next_results,
    save_draft,
    save_results,
)
from app.domain import MarketType, Sport
from app.services.errors import ValidationFailedError
from app.services.interfaces import DateSpec, ScopeKind, UserSettingsView


def make_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=2, user_id=2))


def test_draft_from_settings_and_query() -> None:
    draft = WizardDraft.from_settings(UserSettingsView(min_probability=0.6))
    query = draft.with_dates(DateSpec.next_days(3)).to_query()

    assert query.min_probability == 0.6
    assert query.sports == {Sport.FOOTBALL}
    assert query.markets == {MarketType.MATCH_RESULT, MarketType.TOTALS}
    assert query.dates == DateSpec.next_days(3)
    assert query.scope is ScopeKind.ALL


def test_draft_round_trips_through_json() -> None:
    draft = (
        WizardDraft.from_settings(UserSettingsView())
        .with_dates(DateSpec.between(date(2026, 10, 25), date(2026, 10, 31)))
        .with_scope(ScopeKind.TEAMS)
        .toggle_scope_item("t1", "Arsenal")
    )
    data = json.loads(json.dumps(draft.to_data()))

    assert WizardDraft.from_data(data) == draft


@pytest.mark.parametrize(
    "data",
    [None, "x", {}, {"sports": ["curling"]}, {"unexpected": 1}],
)
def test_corrupt_draft_is_stale(data: object) -> None:
    base = WizardDraft().to_data()
    if isinstance(data, dict):
        base.update(data)
        data = base if data else {}
    with pytest.raises(StaleDraftError):
        WizardDraft.from_data(data)


def test_toggle_scope_item_adds_and_removes() -> None:
    draft = WizardDraft().with_scope(ScopeKind.LEAGUES).toggle_scope_item("l1", "APL")
    assert draft.scope_ids == ("l1",)
    assert draft.toggle_scope_item("l1", "APL").scope_ids == ()
    assert draft.with_scope(ScopeKind.LEAGUES) is draft
    assert draft.with_scope(ScopeKind.ALL).scope_ids == ()


def test_empty_scope_is_rejected_by_the_contract() -> None:
    with pytest.raises(ValidationFailedError):
        WizardDraft().with_scope(ScopeKind.TEAMS).to_query()


def test_per_day_only_with_a_count() -> None:
    draft = replace(WizardDraft(), per_day=True, max_signals=None)
    assert draft.to_query().per_day is False


async def test_fsm_helpers_keep_results_when_draft_is_dropped() -> None:
    state = make_state()
    await save_draft(state, WizardDraft())
    await save_results(state, StoredResults((3, 1, 2)))

    assert await load_draft(state) == WizardDraft()
    await drop_draft(state)

    with pytest.raises(StaleDraftError):
        await load_draft(state)
    assert (await load_results(state)).signal_ids == (3, 1, 2)


async def test_a_newer_result_gets_a_new_token_and_restarts_paging() -> None:
    state = make_state()
    first = await next_results(state, (1, 2, 3))
    served = first.advanced()
    await save_results(state, served)

    second = await next_results(state, (4, 5))

    assert (first.token, first.next_page) == (1, 1)
    assert served.next_page == 2
    assert (second.token, second.next_page, second.signal_ids) == (2, 1, (4, 5))
    assert await load_results(state) == second


def test_result_tokens_cycle_without_repeating_the_previous_one() -> None:
    last = StoredResults((1,), RESULTS_TOKEN_MODULUS - 1)
    assert last.following((2,)).token == 0


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"signal_ids": "1"},
        {"signal_ids": [1, "x"]},
        {"signal_ids": [1], "token": "x"},
        {"signal_ids": [1], "next_page": None},
    ],
)
def test_corrupt_results_are_stale(data: object) -> None:
    with pytest.raises(StaleDraftError):
        StoredResults.from_data(data)
