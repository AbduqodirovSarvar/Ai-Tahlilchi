"""Pure parts of 🔍 Maxsus tahlil (plan §6.3, F7.1): the flow's draft, its screens and result text.

The draft lives in FSM data as plain JSON-safe values (like the wizard's), so a persistent
storage can replace the in-memory one. Buttons carry an option *index*, never a catalog id
(``callback_data`` is 64 bytes, ids may be 40 of them).
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from itertools import groupby
from typing import Any, Final, Self
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import CustomCb
from app.bot.draft import StaleDraftError
from app.bot.formatters import (
    escape,
    kickoff_stamp,
    odds,
    percent,
    selection_label,
)
from app.bot.keyboards import checked_label, markup_of, single_button
from app.services.interfaces import SignalView

KIND_PROMPT: Final = "prompt"
KIND_TEAMS: Final = "teams"
KIND_FIXTURES: Final = "fixtures"
SOURCE_TEAM: Final = "team"
SOURCE_QUERY: Final = "query"
SOURCE_DAY: Final = "day"
MAX_CUSTOM_FIXTURES: Final = 10
"""Fixtures analysed in one custom run (each can cost provider requests)."""
MAX_LISTED_FIXTURES: Final = 20
MAX_PICK_LINES: Final = 30
"""A fixture block lists at most this many selections so it fits one message."""
UPCOMING_PER_TEAM: Final = 3
MAX_TEAM_HITS: Final = 5


@dataclass(frozen=True, slots=True)
class CustomOption:
    """One button of the flow: a team or a fixture found in the catalog."""

    option_id: str
    label: str
    sport: str
    name: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class CustomDraft:
    """Where the user is in 🔍 Maxsus tahlil and what is on screen."""

    message_id: int | None = None
    kind: str = KIND_PROMPT
    source: str = SOURCE_QUERY
    title: str = ""
    options: tuple[CustomOption, ...] = ()
    selected: tuple[str, ...] = ()
    truncated: int = 0

    def to_data(self) -> dict[str, Any]:
        """JSON-safe form for FSM storage."""
        data = asdict(self)
        data["options"] = [list(asdict(option).values()) for option in self.options]
        return data

    @classmethod
    def from_data(cls, data: object) -> Self:
        """Decode, or raise :class:`StaleDraftError` when missing or corrupt."""
        if not isinstance(data, dict):
            raise StaleDraftError
        try:
            options = tuple(CustomOption(*(str(part) for part in row)) for row in data["options"])
            draft = cls(
                message_id=data["message_id"],
                kind=str(data["kind"]),
                source=str(data["source"]),
                title=str(data["title"]),
                options=options,
                selected=tuple(str(item) for item in data["selected"]),
                truncated=int(data["truncated"]),
            )
        except (KeyError, TypeError, ValueError):
            raise StaleDraftError from None
        if draft.kind not in {KIND_PROMPT, KIND_TEAMS, KIND_FIXTURES}:
            raise StaleDraftError
        return draft

    def toggled(self, index: int) -> Self:
        """The draft with option ``index`` switched on or off.

        Raises:
            StaleDraftError: the index names no option (a button of an older list).
        """
        if not 0 <= index < len(self.options):
            raise StaleDraftError
        option_id = self.options[index].option_id
        if option_id in self.selected:
            return replace(self, selected=tuple(i for i in self.selected if i != option_id))
        return replace(self, selected=(*self.selected, option_id))

    @property
    def sports(self) -> frozenset[str]:
        """Sports of the selected fixtures (a run is priced per sport)."""
        chosen = set(self.selected)
        return frozenset(o.sport for o in self.options if o.option_id in chosen)


# --------------------------------------------------------------------------- screens
def prompt_keyboard() -> InlineKeyboardMarkup:
    """The start screen: quick day lists and cancel."""
    return markup_of(
        [
            [
                single_button(texts.BTN_CUSTOM_TODAY, CustomCb(action="today")),
                single_button(texts.BTN_CUSTOM_TOMORROW, CustomCb(action="tomorrow")),
            ],
            [single_button(texts.BTN_CANCEL, CustomCb(action="cancel"))],
        ]
    )


def _teams_keyboard(draft: CustomDraft) -> InlineKeyboardMarkup:
    rows = [
        [single_button(option.label, CustomCb(action="team", index=index))]
        for index, option in enumerate(draft.options)
    ]
    return markup_of(
        [
            *rows,
            [
                single_button(texts.BTN_CUSTOM_AGAIN, CustomCb(action="again")),
                single_button(texts.BTN_CANCEL, CustomCb(action="cancel")),
            ],
        ]
    )


def _fixtures_keyboard(draft: CustomDraft) -> InlineKeyboardMarkup:
    chosen = set(draft.selected)
    rows = [
        [
            single_button(
                checked_label(option.label, on=option.option_id in chosen),
                CustomCb(action="toggle", index=index),
            )
        ]
        for index, option in enumerate(draft.options)
    ]
    if draft.selected:
        run = texts.BTN_CUSTOM_RUN.format(count=len(draft.selected))
        rows.append([single_button(run, CustomCb(action="run"))])
    rows.append(
        [
            single_button(texts.BTN_CUSTOM_AGAIN, CustomCb(action="again")),
            single_button(texts.BTN_CANCEL, CustomCb(action="cancel")),
        ]
    )
    return markup_of(rows)


def _fixtures_text(draft: CustomDraft) -> str:
    title = escape(draft.title)
    heading = {
        SOURCE_TEAM: texts.CUSTOM_TEAM_FIXTURES.format(team=title),
        SOURCE_DAY: texts.CUSTOM_DAY_FIXTURES.format(day=title),
    }.get(draft.source, texts.CUSTOM_FIXTURES_FOUND.format(query=title))
    lines = [
        heading,
        texts.CUSTOM_SELECTED.format(count=len(draft.selected), max=MAX_CUSTOM_FIXTURES),
    ]
    if draft.truncated:
        lines.append(texts.CUSTOM_TRUNCATED.format(count=MAX_LISTED_FIXTURES))
    return "\n".join(lines)


def render_custom(draft: CustomDraft) -> tuple[str, InlineKeyboardMarkup]:
    """Text and keyboard of the current screen."""
    if draft.kind == KIND_TEAMS:
        return texts.CUSTOM_TEAMS_FOUND.format(query=escape(draft.title)), _teams_keyboard(draft)
    if draft.kind == KIND_FIXTURES:
        return _fixtures_text(draft), _fixtures_keyboard(draft)
    return texts.CUSTOM_PROMPT, prompt_keyboard()


# --------------------------------------------------------------------------- result text
def _mark(signal: SignalView) -> str:
    if signal.recommended:
        return texts.CUSTOM_MARK_VALUE
    return texts.CUSTOM_MARK_NEGATIVE if signal.negative_ev else texts.CUSTOM_MARK_NO_VALUE


def _pick_line(signal: SignalView) -> str:
    return texts.CUSTOM_PICK_LINE.format(
        mark=_mark(signal),
        selection=selection_label(signal),
        probability=percent(signal.probability),
        odds=odds(signal.odds),
        ev=percent(signal.ev, signed=True, decimals=1),
    )


def format_custom_fixture(signals: Sequence[SignalView], zone: ZoneInfo) -> str:
    """One fixture of a custom analysis: every priced selection with its probability.

    ``signals`` all belong to one fixture. Selections without value are listed too (that is
    the point of a custom analysis); when none passes the thresholds the message says
    "Value topilmadi".
    """
    first = signals[0]
    ordered = sorted(signals, key=lambda signal: -signal.probability)
    shown = ordered[:MAX_PICK_LINES]
    lines = [
        texts.CUSTOM_FIXTURE_HEADER.format(
            icon=texts.SPORT_ICONS[first.sport],
            fixture=escape(first.fixture_label),
            time=kickoff_stamp(first.kickoff, zone),
            league=escape(first.league),
        ),
        *(_pick_line(signal) for signal in shown),
    ]
    if len(ordered) > len(shown):
        lines.append(texts.CUSTOM_MORE_PICKS.format(count=len(ordered) - len(shown)))
    if any(signal.recommended for signal in signals):
        lines.append(texts.CUSTOM_LEGEND)
    else:
        lines.append(texts.CUSTOM_NO_VALUE_NOTE)
    return "\n".join(lines)


def by_fixture(signals: Sequence[SignalView]) -> list[list[SignalView]]:
    """Signals grouped per fixture in the order the fixtures first appear."""
    ordered = sorted(signals, key=lambda signal: (signal.kickoff, signal.fixture_id))
    return [list(group) for _, group in groupby(ordered, key=lambda signal: signal.fixture_id)]
