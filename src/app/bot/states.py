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
BET_KEY = "bet"
RESULTS_TOKEN_MODULUS = 1000
"""Result tokens cycle: they only have to differ from the previous result's."""


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


class BetFlow(StatesGroup):
    """🎟 Waiting for the odds (and stake) the user really got, typed as ``1.90 50000``."""

    value_input = State()
    cashout_input = State()


class CustomFlow(StatesGroup):
    """🔍 Maxsus tahlil: typing a team or fixture, choosing from the list, running."""

    query = State()
    pick = State()
    running = State()


class WatchFlow(StatesGroup):
    """⭐ Sevimlilar: waiting for a team name to add."""

    team_query = State()


class TemplateFlow(StatesGroup):
    """💾 Shablonlar: waiting for a typed schedule time."""

    time_input = State()


class ReportFlow(StatesGroup):
    """📈 Hisobot: waiting for a typed date range."""

    range_input = State()


class ExpressFlow(StatesGroup):
    """🧩 Ekspress: choosing the signals to combine."""

    pick = State()


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
    """Signal ids of the last analysis in display order, for pagination.

    ``token`` identifies this result: every "➡️ Yana" button carries it, so a button of an
    older result is refused. ``next_page`` is the only page a button may still request,
    which makes a double tap send a page once.
    """

    signal_ids: tuple[int, ...]
    token: int = 0
    next_page: int = 1

    def to_data(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {
            "signal_ids": list(self.signal_ids),
            "token": self.token,
            "next_page": self.next_page,
        }

    def following(self, signal_ids: tuple[int, ...]) -> Self:
        """The record of a newer result (a new token, paging restarts)."""
        return type(self)(signal_ids, (self.token + 1) % RESULTS_TOKEN_MODULUS)

    def advanced(self) -> Self:
        """The same result with its next page served."""
        return type(self)(self.signal_ids, self.token, self.next_page + 1)

    @classmethod
    def from_data(cls, data: object) -> Self:
        """Decode, or raise :class:`StaleDraftError` when missing/corrupt."""
        if not isinstance(data, dict):
            raise StaleDraftError
        ids = data.get("signal_ids")
        if not isinstance(ids, list) or not all(isinstance(item, int) for item in ids):
            raise StaleDraftError
        token, next_page = data.get("token", 0), data.get("next_page", 1)
        if not isinstance(token, int) or not isinstance(next_page, int):
            raise StaleDraftError
        return cls(tuple(ids), token, next_page)


async def save_results(state: FSMContext, results: StoredResults) -> None:
    """Remember the last analysis result for pagination."""
    await state.update_data({RESULTS_KEY: results.to_data()})


async def next_results(state: FSMContext, signal_ids: tuple[int, ...]) -> StoredResults:
    """Replace the stored result by a newer one (with a fresh token) and return it."""
    try:
        previous = await load_results(state)
    except StaleDraftError:
        previous = StoredResults(())
    results = previous.following(signal_ids)
    await save_results(state, results)
    return results


async def load_results(state: FSMContext) -> StoredResults:
    """The last analysis result (raises :class:`StaleDraftError` if gone)."""
    data = await state.get_data()
    return StoredResults.from_data(data.get(RESULTS_KEY))


@dataclass(frozen=True, slots=True)
class PendingBet:
    """The bet prompt whose odds/stake the user is typing."""

    signal_id: int
    message_id: int


async def start_bet_input(state: FSMContext, pending: PendingBet) -> None:
    """Wait for the typed odds/stake of ``pending``."""
    await state.update_data(
        {BET_KEY: {"signal_id": pending.signal_id, "message_id": pending.message_id}}
    )
    await state.set_state(BetFlow.value_input)


async def load_bet_input(state: FSMContext) -> PendingBet:
    """The prompt being answered (raises :class:`StaleDraftError` if the data is gone)."""
    data = (await state.get_data()).get(BET_KEY)
    if not isinstance(data, dict):
        raise StaleDraftError
    signal_id, message_id = data.get("signal_id"), data.get("message_id")
    if not isinstance(signal_id, int) or not isinstance(message_id, int):
        raise StaleDraftError
    return PendingBet(signal_id, message_id)


async def drop_bet_input(state: FSMContext) -> None:
    """Leave the typed-bet step."""
    data = await state.get_data()
    data.pop(BET_KEY, None)
    await state.set_state(None)
    await state.set_data(data)
