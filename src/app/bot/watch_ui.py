"""Pure parts of ⭐ Sevimlilar (plan §6.7, F7.3): list, item screen and picker keyboards.

Buttons that name a watched entity carry ``crc32(entity_id)`` (8 hex characters) and its kind,
never the id itself (up to 40 bytes) and never a list position: the entity is looked up in a
fresh watchlist when the button is pressed, so a list that changed in between can never make
a tap hit another entry.
"""

import zlib
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final, Self

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import WatchCb
from app.bot.custom_ui import CustomOption
from app.bot.draft import StaleDraftError
from app.bot.formatters import escape, sport_label
from app.bot.keyboards import checked_label, markup_of, single_button
from app.services.interfaces import WatchItem, WatchKind, WatchNotification

MAX_LISTED_ITEMS: Final = 20
MAX_PICK_OPTIONS: Final = 20
KIND_CODES: Final[dict[WatchKind, str]] = {WatchKind.TEAM: "t", WatchKind.LEAGUE: "l"}
FLAG_CODES: Final[dict[WatchNotification, str]] = {
    WatchNotification.LINEUPS: "l",
    WatchNotification.ODDS_MOVE: "o",
    WatchNotification.RESULT: "r",
}
PICK_TEAM: Final = "pick_team"
PICK_LEAGUE: Final = "pick_league"


def entity_key(entity_id: str) -> str:
    """Short stable key of a catalog id for ``callback_data``."""
    return f"{zlib.crc32(entity_id.encode()):08x}"


def kind_of(code: str) -> WatchKind | None:
    """The :class:`WatchKind` a button's kind code names."""
    return next((kind for kind, value in KIND_CODES.items() if value == code), None)


def flag_of(code: str) -> WatchNotification | None:
    """The notification a button's flag code names."""
    return next((flag for flag, value in FLAG_CODES.items() if value == code), None)


def find_item(items: Sequence[WatchItem], kind_code: str, key: str) -> WatchItem | None:
    """The watchlist entry a button names, or ``None`` when it is gone."""
    kind = kind_of(kind_code)
    return next(
        (
            item
            for item in items
            if item.target.kind is kind and entity_key(item.target.entity_id) == key
        ),
        None,
    )


def _flags_text(item: WatchItem) -> str:
    if not item.notifications:
        return texts.WATCH_FLAGS_NONE
    return " · ".join(
        texts.WATCH_NOTIFY_LABELS[flag] for flag in WatchNotification if flag in item.notifications
    )


def format_watchlist(items: Sequence[WatchItem]) -> str:
    """The list screen: each entry with its notification switches."""
    if not items:
        return texts.WATCH_EMPTY
    shown = items[:MAX_LISTED_ITEMS]
    lines = [texts.WATCH_TITLE.format(count=len(items))]
    lines.extend(
        texts.WATCH_ITEM_LINE.format(
            icon=texts.WATCH_KIND_ICONS[item.target.kind],
            name=escape(item.name),
            flags=_flags_text(item),
        )
        for item in shown
    )
    if len(items) > len(shown):
        lines.append(texts.WATCH_MORE.format(count=len(items) - len(shown)))
    lines.append("")
    lines.append(texts.WATCH_FOOTER)
    return "\n".join(lines)


def watchlist_keyboard(items: Sequence[WatchItem]) -> InlineKeyboardMarkup:
    """One button per entry, then the add buttons."""
    rows = [
        [
            single_button(
                f"{texts.WATCH_KIND_ICONS[item.target.kind]} {item.name}",
                WatchCb(
                    action="open",
                    kind=KIND_CODES[item.target.kind],
                    key=entity_key(item.target.entity_id),
                ),
            )
        ]
        for item in items[:MAX_LISTED_ITEMS]
    ]
    rows.append(
        [
            single_button(texts.BTN_WATCH_ADD_TEAM, WatchCb(action="add_team")),
            single_button(texts.BTN_WATCH_ADD_LEAGUE, WatchCb(action="add_league")),
        ]
    )
    return markup_of(rows)


def format_item(item: WatchItem) -> str:
    """The entry's screen header."""
    return texts.WATCH_ITEM_TITLE.format(
        icon=texts.WATCH_KIND_ICONS[item.target.kind],
        name=escape(item.name),
        sport=sport_label(item.sport),
    )


def item_keyboard(item: WatchItem) -> InlineKeyboardMarkup:
    """The notification switches, remove and back."""
    kind, key = KIND_CODES[item.target.kind], entity_key(item.target.entity_id)
    switches = [
        single_button(
            checked_label(texts.WATCH_NOTIFY_LABELS[flag], on=flag in item.notifications),
            WatchCb(action="notify", kind=kind, key=key, flag=FLAG_CODES[flag]),
        )
        for flag in WatchNotification
    ]
    return markup_of(
        [
            switches,
            [
                single_button(texts.BTN_WATCH_REMOVE, WatchCb(action="remove", kind=kind, key=key)),
                single_button(texts.BTN_WATCH_BACK, WatchCb(action="list")),
            ],
        ]
    )


# --------------------------------------------------------------------------- pickers
@dataclass(frozen=True, slots=True, kw_only=True)
class WatchDraft:
    """The add flow: ``team_query`` (waiting for a name) or a pick list (teams or leagues)."""

    message_id: int | None = None
    kind: str = PICK_TEAM
    query: str = ""
    options: tuple[CustomOption, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """JSON-safe form for FSM storage."""
        data = asdict(self)
        data["options"] = [list(asdict(option).values()) for option in self.options]
        return data

    @classmethod
    def from_data(cls, data: object) -> Self:
        """Decode, or raise :class:`StaleDraftError`."""
        if not isinstance(data, dict):
            raise StaleDraftError
        try:
            options = tuple(CustomOption(*(str(part) for part in row)) for row in data["options"])
            draft = cls(
                message_id=data["message_id"],
                kind=str(data["kind"]),
                query=str(data["query"]),
                options=options,
            )
        except (KeyError, TypeError, ValueError):
            raise StaleDraftError from None
        if draft.kind not in {PICK_TEAM, PICK_LEAGUE}:
            raise StaleDraftError
        return draft

    def option(self, index: int) -> CustomOption:
        """Option ``index``.

        Raises:
            StaleDraftError: no such option (a button of an older list).
        """
        if not 0 <= index < len(self.options):
            raise StaleDraftError
        return self.options[index]


def pick_keyboard(draft: WatchDraft) -> InlineKeyboardMarkup:
    """The options as buttons (``add`` with the option index) and a way back."""
    kind = "t" if draft.kind == PICK_TEAM else "l"
    rows = [
        [single_button(option.label, WatchCb(action="add", kind=kind, key=str(index)))]
        for index, option in enumerate(draft.options[:MAX_PICK_OPTIONS])
    ]
    rows.append([single_button(texts.BTN_WATCH_BACK, WatchCb(action="list"))])
    return markup_of(rows)


def ask_keyboard() -> InlineKeyboardMarkup:
    """While a team name is awaited: just a way back."""
    return markup_of([[single_button(texts.BTN_WATCH_BACK, WatchCb(action="list"))]])


def pick_text(draft: WatchDraft) -> str:
    """Heading of a pick list."""
    if draft.kind == PICK_LEAGUE:
        return texts.WATCH_PICK_LEAGUE
    return texts.WATCH_PICK_TEAM.format(query=escape(draft.query))
