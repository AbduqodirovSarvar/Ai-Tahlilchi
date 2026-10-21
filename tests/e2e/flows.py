"""User journeys shared by the end-to-end bot tests."""

from typing import Final

from app.bot import texts
from app.domain import Sport
from app.services.interfaces import SortKey
from tests.e2e.bot_client import Client

FOOTBALL: Final = texts.SPORT_LABELS[Sport.FOOTBALL]
SEVEN_DAYS: Final = texts.BTN_NEXT_DAYS.format(days=7)

WIZARD_SCREENS: Final = (
    "dates",
    "calendar",
    "sport",
    "probability",
    "probability_input",
    "ev",
    "ev_input",
    "scope",
    "leagues",
    "teams",
    "fixtures",
    "count",
    "count_input",
    "markets",
    "sort",
    "confirm",
    "edit",
    "template_name",
)
"""Every screen of the 📊 Tahlil wizard (``running`` is not a screen)."""

BACK_TARGET: Final[dict[str, str | None]] = {
    "dates": None,
    "calendar": "dates",
    "sport": "dates",
    "probability": "sport",
    "probability_input": "probability",
    "ev": "probability",
    "ev_input": "ev",
    "scope": "ev",
    "leagues": "scope",
    "teams": "scope",
    "fixtures": "scope",
    "count": "scope",
    "count_input": "count",
    "markets": "count",
    "sort": "markets",
    "confirm": "sort",
    "edit": "confirm",
    "template_name": "confirm",
}
"""Where ⬅️ Orqaga leads from each screen per plan §6.2 (``None``: leaves the wizard)."""


def _paths() -> dict[str, list[str]]:
    probability = [SEVEN_DAYS, FOOTBALL]
    scope = [*probability, "50%", "5%"]
    count = [*scope, texts.BTN_SCOPE_ALL]
    confirm = [*count, texts.BTN_ALL, texts.BTN_DONE, texts.SORT_LABELS[SortKey.EV]]
    return {
        "dates": [],
        "calendar": [texts.BTN_CALENDAR],
        "sport": [SEVEN_DAYS],
        "probability": probability,
        "probability_input": [*probability, texts.BTN_CUSTOM],
        "ev": [*probability, "50%"],
        "ev_input": [*probability, "50%", texts.BTN_CUSTOM],
        "scope": scope,
        "leagues": [*scope, texts.BTN_SCOPE_LEAGUES],
        "teams": [*scope, texts.BTN_SCOPE_TEAMS],
        "fixtures": [*scope, texts.BTN_SCOPE_FIXTURES],
        "count": count,
        "count_input": [*count, texts.BTN_CUSTOM],
        "markets": [*count, texts.BTN_ALL],
        "sort": [*count, texts.BTN_ALL, texts.BTN_DONE],
        "confirm": confirm,
        "edit": [*confirm, texts.BTN_EDIT],
        "template_name": [*confirm, "Shablon"],
    }


PATHS: Final = _paths()


async def reach(client: Client, screen: str) -> None:
    """Drive a fresh wizard to ``screen`` (teams: with ambiguous candidates on screen)."""
    await client.send(texts.MENU_ANALYSIS)
    for label in PATHS[screen]:
        await client.press(label)
    if screen == "teams":
        await client.send("Real")
    assert await client.state() == f"AnalysisFlow:{screen}"


async def to_confirm(
    client: Client, *, date_button: str = texts.BTN_TODAY, probability: str = "50%"
) -> None:
    """Plain wizard run up to the confirmation screen."""
    await client.send(texts.MENU_ANALYSIS)
    for label in (
        date_button,
        FOOTBALL,
        probability,
        "5%",
        texts.BTN_SCOPE_ALL,
        texts.BTN_ALL,
        texts.BTN_DONE,
        texts.SORT_LABELS[SortKey.EV],
    ):
        await client.press(label)
