"""Express arithmetic shared by the fake and the real services (contract v1.6, plan §6.11).

A single definition of the rules: how many legs, which picks may be combined and how the
combined probability, odds and EV are computed. Legs of different matches are independent,
so probabilities and odds multiply and the EV compounds (``prod(1 + ev_i) - 1``, exact for
quarter-line Asian legs too, where ``p * odds - 1`` is not the leg's EV); two picks of one
match are correlated (a 1X2 win and "over 2.5" move together) and are refused instead of
being priced as if they were independent.
"""

import math
from collections.abc import Sequence
from datetime import datetime

from app.services.errors import ValidationFailedError
from app.services.interfaces import (
    MAX_EXPRESS_LEGS,
    MIN_EXPRESS_LEGS,
    ExpressLeg,
    ExpressView,
    SignalId,
    SignalState,
    SignalView,
)

FIELD = "signal_ids"


def check_ids(signal_ids: Sequence[SignalId]) -> tuple[SignalId, ...]:
    """The distinct ids in order, after checking their number.

    Raises:
        ValidationFailedError: fewer than :data:`MIN_EXPRESS_LEGS`, more than
            :data:`MAX_EXPRESS_LEGS`, or the same signal twice.
    """
    distinct = tuple(dict.fromkeys(signal_ids))
    if len(distinct) != len(signal_ids):
        raise ValidationFailedError(FIELD, "the same signal twice")
    if not MIN_EXPRESS_LEGS <= len(distinct) <= MAX_EXPRESS_LEGS:
        raise ValidationFailedError(
            FIELD, f"an express needs {MIN_EXPRESS_LEGS}..{MAX_EXPRESS_LEGS} signals"
        )
    return distinct


def _check_playable(view: SignalView, now: datetime) -> None:
    """A pick must still be a live recommendation."""
    if view.retro or not view.recommended:
        raise ValidationFailedError(FIELD, f"signal {view.signal_id} is not a recommendation")
    if view.state is SignalState.CANCELLED or view.result is not None or view.kickoff <= now:
        raise ValidationFailedError(FIELD, f"signal {view.signal_id} can no longer be played")


def evaluate(views: Sequence[SignalView], now: datetime) -> ExpressView:
    """Price an express of ``views`` (already looked up, in the user's order).

    Raises:
        ValidationFailedError: wrong number of legs, a pick that cannot be played any more
            or two picks of one match.
    """
    check_ids([view.signal_id for view in views])
    fixtures = [view.fixture_id for view in views]
    if len(set(fixtures)) != len(fixtures):
        raise ValidationFailedError(FIELD, "two picks of one match are correlated")
    for view in views:
        _check_playable(view, now)
    probability = math.prod(view.probability for view in views)
    odds = math.prod(view.odds for view in views)
    return ExpressView(
        legs=tuple(
            ExpressLeg(
                signal_id=view.signal_id,
                fixture_label=view.fixture_label,
                market=view.market,
                selection=view.selection,
                odds=view.odds,
                probability=view.probability,
                fair_odds=view.fair_odds,
            )
            for view in views
        ),
        combined_odds=odds,
        combined_probability=probability,
        ev=math.prod(1.0 + view.ev for view in views) - 1.0,
    )
