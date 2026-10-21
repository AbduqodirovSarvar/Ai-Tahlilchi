"""FSM states and the typed accessors for what the bot keeps in FSM data.

FSM data holds two independent records: the wizard ``draft`` and the last analysis
``results`` (for "➡️ Yana" pagination). Cancelling or finishing the wizard drops only the
draft, so result pagination keeps working. Storage is in-memory for now (ADR 0006); all
access goes through these helpers, so a persistent storage only has to be passed to
``build_dispatcher``.
"""

from dataclasses import dataclass
from typing import Any, Self

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.bot.draft import StaleDraftError, WizardDraft

DRAFT_KEY = "draft"
RESULTS_KEY = "results"
SETTINGS_FIELD_KEY = "settings_field"


class AnalysisFlow(StatesGroup):
    """📊 Tahlil wizard steps (plan §6.2)."""

    dates = State()
    calendar = State()
    sport = State()
    probability = State()
    probability_input = State()
    ev = State()
    ev_input = State()
    scope = State()
    leagues = State()
    teams = State()
    fixtures = State()
    count = State()
    count_input = State()
    markets = State()
    sort = State()
    confirm = State()
    edit = State()
    template_name = State()
    running = State()


class SettingsFlow(StatesGroup):
    """⚙️ Settings: waiting for a typed value of ``settings_field``."""

    value_input = State()


async def load_draft(state: FSMContext) -> WizardDraft:
    """The current wizard draft.

    Raises:
        StaleDraftError: there is none (finished, cancelled, or the bot restarted).
    """
    data = await state.get_data()
    return WizardDraft.from_data(data.get(DRAFT_KEY))


async def save_draft(state: FSMContext, draft: WizardDraft) -> None:
    """Store the draft (JSON-safe)."""
    await state.update_data({DRAFT_KEY: draft.to_data()})


async def drop_draft(state: FSMContext) -> None:
    """Leave the wizard: clear its state and draft, keep the last results."""
    data = await state.get_data()
    data.pop(DRAFT_KEY, None)
    await state.set_state(None)
    await state.set_data(data)


@dataclass(frozen=True, slots=True)
class StoredResults:
    """Signal ids of the last analysis in display order, for pagination."""

    signal_ids: tuple[int, ...]

    def to_data(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {"signal_ids": list(self.signal_ids)}

    @classmethod
    def from_data(cls, data: object) -> Self:
        """Decode, or raise :class:`StaleDraftError` when missing/corrupt."""
        if not isinstance(data, dict):
            raise StaleDraftError
        ids = data.get("signal_ids")
        if not isinstance(ids, list) or not all(isinstance(item, int) for item in ids):
            raise StaleDraftError
        return cls(tuple(ids))


async def save_results(state: FSMContext, results: StoredResults) -> None:
    """Remember the last analysis result for pagination."""
    await state.update_data({RESULTS_KEY: results.to_data()})


async def load_results(state: FSMContext) -> StoredResults:
    """The last analysis result (raises :class:`StaleDraftError` if gone)."""
    data = await state.get_data()
    return StoredResults.from_data(data.get(RESULTS_KEY))
