"""Where the user is in 🗄 Baza: the navigation record kept in FSM data.

Buttons carry no entity ids (a catalog id can be 40 bytes), so the screen that was drawn last
remembers the ids of its numbered items and a ``token``. A button of any older screen carries
another token and is refused as stale, which also covers a bot restart on in-memory storage.
The record is plain JSON-compatible data, so persistent FSM storage keeps it across restarts.
"""

from dataclasses import asdict, dataclass, replace
from typing import Any, Final, Self

from aiogram.fsm.context import FSMContext

from app.bot.callbacks import DbCb

NAV_KEY: Final = "db_nav"
TOKEN_MODULUS: Final = 1000
"""Tokens cycle: a button only has to differ from the previous screen's."""

SCREEN_LEAGUES: Final = "leagues"
SCREEN_TEAMS: Final = "teams"
SCREEN_FIXTURES: Final = "fixtures"
SCREEN_FIXTURE: Final = "fixture"
SCREEN_ODDS: Final = "odds"
SCREEN_CALENDAR: Final = "calendar"

SCREEN_CODES: Final = {
    SCREEN_LEAGUES: "l",
    SCREEN_TEAMS: "t",
    SCREEN_FIXTURES: "f",
    SCREEN_FIXTURE: "c",
    SCREEN_ODDS: "o",
    SCREEN_CALENDAR: "k",
}
"""One-letter screen codes carried by buttons (see :class:`app.bot.callbacks.DbCb`)."""

SCOPE_LEAGUE: Final = "league"
SCOPE_TEAM: Final = "team"
SCOPE_ALL: Final = "all"


@dataclass(frozen=True, slots=True)
class BrowseNav:
    """The screen shown last and what its buttons stand for."""

    token: int = 0
    screen: str = SCREEN_LEAGUES
    scope: str = SCOPE_ALL
    scope_id: str | None = None
    scope_name: str = ""
    query: str = ""
    day: int = 0
    """Date ordinal of the day filter; 0 = every day."""
    page: int = 0
    parent_page: int = 0
    """Page of the league/team list the fixtures were opened from."""
    odds_page: int = 0
    fixture_id: str | None = None
    cal_month: int = 0
    ids: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    """Display names parallel to ``ids`` (the league or team a pick opens)."""

    def to_data(self) -> dict[str, Any]:
        """JSON-compatible form for FSM data."""
        return asdict(self)

    @classmethod
    def from_data(cls, data: object) -> Self | None:
        """Rebuild from FSM data; ``None`` when absent or damaged (treated as stale)."""
        if not isinstance(data, dict):
            return None
        try:
            ids = tuple(str(item) for item in data.get("ids", ()))
            labels = tuple(str(item) for item in data.get("labels", ()))
            return cls(**{**data, "ids": ids, "labels": labels})
        except (TypeError, ValueError):
            return None

    @property
    def next_token(self) -> int:
        """The token of the screen that follows this one."""
        return self.token % TOKEN_MODULUS + 1

    def pick(self, index: int) -> tuple[str, str] | None:
        """``(entity id, display name)`` behind numbered button ``index`` of the visible page."""
        if not 0 <= index < len(self.ids):
            return None
        return self.ids[index], self.labels[index] if index < len(self.labels) else ""

    def button(self, action: str, n: int = 0) -> DbCb:
        """The callback of a button on the screen shown now."""
        return DbCb(action=action, n=n, t=self.token, s=SCREEN_CODES[self.screen])

    def owns(self, callback_data: DbCb) -> bool:
        """True when a button was drawn on the screen shown now (else it only redraws it)."""
        return callback_data.s == SCREEN_CODES[self.screen]


async def save_nav(state: FSMContext, nav: BrowseNav) -> None:
    """Remember ``nav`` as the current screen."""
    await state.update_data({NAV_KEY: nav.to_data()})


async def load_nav(state: FSMContext) -> BrowseNav | None:
    """The current screen record, or ``None`` when there is none (restart, never opened)."""
    return BrowseNav.from_data((await state.get_data()).get(NAV_KEY))


async def open_screen(state: FSMContext, nav: BrowseNav) -> BrowseNav:
    """Start a new screen (a menu entry): ``nav`` with the token after the previous screen's."""
    previous = await load_nav(state)
    opened = replace(nav, token=(previous or BrowseNav()).next_token)
    await save_nav(state, opened)
    return opened
