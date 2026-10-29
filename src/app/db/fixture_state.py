"""How a fixture's stored status and scores change when new provider data arrives.

One pure function, :func:`merge_outcome`, decides it for every write path, so the table
never holds a combination the domain cannot read back (``app.db.mappers.result_from_row``):

- A result is authoritative: its status and all four scores are written as given. xG, cards
  and the referee are merged (a new value wins, a missing one keeps the stored value)
  because providers publish them later than the score, or not at all.
- Without a result, a status never contradicts the scores: ``finished`` is only kept when
  scores are known (an unknown result stays in its previous state, ``scheduled`` for a new
  fixture, until a result arrives); ``scheduled``/``postponed``/``cancelled`` clear scores
  and xG; ``live``/``abandoned`` keep whatever is stored.
"""

from dataclasses import dataclass, replace
from typing import Final

from app.domain import FixtureResult, FixtureStatus, ResultStatus

type OutcomeValues = dict[str, object]

UNPLAYED_STATUSES: Final = frozenset(
    {FixtureStatus.SCHEDULED, FixtureStatus.POSTPONED, FixtureStatus.CANCELLED}
)
_UNPLAYED_RESULTS: Final = frozenset({ResultStatus.POSTPONED, ResultStatus.CANCELLED})


@dataclass(frozen=True, slots=True)
class FixtureOutcome:
    """The status and score columns of one ``fixtures`` row."""

    status: FixtureStatus
    home_score: int | None = None
    away_score: int | None = None
    home_ht_score: int | None = None
    away_ht_score: int | None = None
    home_xg: float | None = None
    away_xg: float | None = None
    home_yellow: int | None = None
    away_yellow: int | None = None
    home_red: int | None = None
    away_red: int | None = None
    referee: str | None = None

    @property
    def has_score(self) -> bool:
        """Whether both full-time scores are known."""
        return self.home_score is not None and self.away_score is not None

    def values(self) -> OutcomeValues:
        """Column values for an insert or update."""
        return {
            "status": self.status,
            "home_score": self.home_score,
            "away_score": self.away_score,
            "home_ht_score": self.home_ht_score,
            "away_ht_score": self.away_ht_score,
            "home_xg": self.home_xg,
            "away_xg": self.away_xg,
            "home_yellow": self.home_yellow,
            "away_yellow": self.away_yellow,
            "home_red": self.home_red,
            "away_red": self.away_red,
            "referee": self.referee,
        }


OUTCOME_COLUMNS: Final = tuple(FixtureOutcome(FixtureStatus.SCHEDULED).values())


def merge_outcome(
    stored: FixtureOutcome | None, status: FixtureStatus, result: FixtureResult | None
) -> FixtureOutcome:
    """The row state after receiving ``status`` (and maybe ``result``) for a fixture."""
    if result is not None:
        return _apply_result(stored, result)
    if status in UNPLAYED_STATUSES:
        return FixtureOutcome(status)
    if status is FixtureStatus.FINISHED:
        if stored is not None and stored.has_score:
            return replace(stored, status=status)
        return stored or FixtureOutcome(FixtureStatus.SCHEDULED)
    if stored is None:
        return FixtureOutcome(status)
    return replace(stored, status=status)


def _apply_result(stored: FixtureOutcome | None, result: FixtureResult) -> FixtureOutcome:
    xg_source = None if result.status in _UNPLAYED_RESULTS else stored
    old = xg_source or FixtureOutcome(FixtureStatus.SCHEDULED)
    return FixtureOutcome(
        status=FixtureStatus(result.status.value),
        home_score=result.home_score,
        away_score=result.away_score,
        home_ht_score=result.home_ht_score,
        away_ht_score=result.away_ht_score,
        home_xg=_first_known(result.home_xg, xg_source.home_xg if xg_source else None),
        away_xg=_first_known(result.away_xg, xg_source.away_xg if xg_source else None),
        home_yellow=_first_known(result.home_yellow, old.home_yellow),
        away_yellow=_first_known(result.away_yellow, old.away_yellow),
        home_red=_first_known(result.home_red, old.home_red),
        away_red=_first_known(result.away_red, old.away_red),
        referee=_first_known(result.referee, old.referee),
    )


def _first_known[T](new: T | None, old: T | None) -> T | None:
    return old if new is None else new
