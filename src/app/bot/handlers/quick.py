"""``/tahlil`` one-line command (plan §6.2 "Tezkor buyruq").

``/tahlil 25.10-31.10 futbol 60% Arsenal,Inter`` builds the same draft the wizard would
and jumps straight to its confirmation screen (cost estimate, ▶️ Boshlash, ✏️, 💾), so a
typo never spends API budget. Without arguments it opens the wizard.
"""

from dataclasses import replace

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.bot import texts
from app.bot.draft import WizardDraft
from app.bot.formatters import escape
from app.bot.handlers.analysis import start_wizard
from app.bot.notices import HIGH_PROBABILITY
from app.bot.parsing import InputError, QuickQuery, parse_quick_query
from app.bot.session import WizardSession, WizardSessionMiddleware
from app.bot.states import drop_draft
from app.bot.wizard import TeamLookup, high_probability, lookup_teams
from app.services.interfaces import (
    MIN_EV_LOWER,
    MIN_EV_UPPER,
    MIN_PROBABILITY_LOWER,
    MIN_PROBABILITY_UPPER,
    ScopeKind,
    UserSettingsView,
)

MAX_LISTED_OPTIONS = 4
COMMAND = "tahlil"


def _quick_error(error: InputError) -> InputError:
    return InputError(texts.QUICK_ERROR.format(problem=error.message))


def _apply(draft: WizardDraft, quick: QuickQuery) -> WizardDraft:
    """Overlay what the command specified on the settings-based draft."""
    if quick.dates is not None:
        draft = draft.with_dates(quick.dates)
    if quick.sports is not None:
        draft = replace(draft, sports=tuple(sorted(sport.value for sport in quick.sports)))
    if quick.min_probability is not None:
        draft = replace(draft, min_probability=quick.min_probability)
    if quick.min_ev is not None:
        draft = replace(draft, min_ev=quick.min_ev)
    if quick.max_signals is not None:
        draft = replace(draft, max_signals=quick.max_signals)
    return draft


def _check_teams(lookup: TeamLookup) -> None:
    """Every typed team must resolve to exactly one catalog team."""
    if lookup.missing:
        raise _quick_error(
            InputError(texts.QUICK_TEAM_NOT_FOUND.format(name=escape(lookup.missing[0])))
        )
    for name, options in lookup.ambiguous.items():
        listed = ", ".join(escape(option.name) for option in options[:MAX_LISTED_OPTIONS])
        raise _quick_error(
            InputError(texts.QUICK_TEAM_AMBIGUOUS.format(name=escape(name), options=listed))
        )


async def build_quick_draft(
    session: WizardSession, settings: UserSettingsView, argument: str
) -> WizardDraft:
    """Parse the argument and resolve team names into a confirmable draft.

    Raises:
        InputError: unparsable, ambiguous or unknown parts (message includes an example).
    """
    try:
        quick = parse_quick_query(
            argument,
            session.ctx.today,
            probability_bounds=(MIN_PROBABILITY_LOWER, MIN_PROBABILITY_UPPER),
            ev_bounds=(MIN_EV_LOWER, MIN_EV_UPPER),
        )
    except InputError as error:
        raise _quick_error(error) from None
    draft = _apply(WizardDraft.from_settings(settings), quick)
    if quick.team_names:
        lookup = await lookup_teams(session.ctx.services, quick.team_names, draft.sport_set)
        _check_teams(lookup)
        draft = draft.with_scope(ScopeKind.TEAMS)
        for team in lookup.found:
            if team.team_id not in draft.scope_ids:
                draft = draft.toggle_scope_item(team.team_id, team.name)
    return draft


async def on_quick(message: Message, command: CommandObject, session: WizardSession) -> None:
    """``/tahlil [parametrlar]``."""
    argument = (command.args or "").strip()
    if not argument:
        await start_wizard(message, session)
        return
    settings = await session.ctx.services.settings.get_settings(session.ctx.user_id)
    draft = await build_quick_draft(session, settings, argument)
    notice = None
    if high_probability(draft.min_probability) and session.ctx.notices.first_time(
        session.ctx.user_id, HIGH_PROBABILITY
    ):
        notice = texts.HIGH_PROBABILITY_WARNING
    await drop_draft(session.state)
    await session.go("confirm", draft, notice)


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="quick")
    router.message.middleware(WizardSessionMiddleware())
    router.message.register(on_quick, Command(COMMAND))
    return router
