"""What the learning loop tells the analysis path: a calibration and per-segment rules.

The weekly job (:mod:`app.pipeline.learning`) writes ``calibration_versions`` and
``segment_stats``; every analysis reads them back as one immutable :class:`LearningOverlay`:

- ``calibration``: the active probability map, applied inside ``value_fixture``;
- ``rules``: segments whose EV bar was raised or that are disabled, applied to the candidates
  of a fixture before the best one is picked (:meth:`LearningOverlay.filter`).

A learning problem must never stop analysis: :class:`DbLearningSource` falls back to the empty
overlay (the raw model, the user's own thresholds) and logs why. Pure except for the source.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repo import CalibrationRecord, SegmentRecord, active_calibration, list_segment_rows
from app.domain import ExternalRef, Fixture, MarketType, Sport
from app.logging import get_logger
from app.models.calibration import CalibrationVersion, validate_params
from app.models.exceptions import ModelInputError
from app.models.segments import SegmentKey, odds_bucket
from app.pipeline.valuation import Candidate

logger = get_logger(__name__)

NO_FLOOR: Final = 0.0


@dataclass(frozen=True, slots=True)
class SegmentRule:
    """How one segment is restricted: off, or an EV bar above the user's own."""

    enabled: bool = True
    ev_floor: float = NO_FLOOR


def segment_key(sport: Sport, league: ExternalRef, market: MarketType, odds: float) -> SegmentKey:
    """The segment a selection at ``odds`` belongs to (the one definition, shared with the job)."""
    return SegmentKey(sport.value, str(league), market.value, odds_bucket(odds))


@dataclass(frozen=True, slots=True)
class LearningOverlay:
    """The calibration and segment rules in force for one analysis."""

    calibration: CalibrationVersion | None = None
    rules: Mapping[SegmentKey, SegmentRule] = field(default_factory=dict)

    def allows(self, candidate: Candidate, fixture: Fixture) -> bool:
        """False when the candidate's segment is disabled or its EV is below the segment's bar."""
        rule = self.rules.get(
            segment_key(fixture.sport, fixture.league.ref, candidate.market.type, candidate.odds)
        )
        if rule is None:
            return True
        return rule.enabled and candidate.ev >= rule.ev_floor

    def filter(self, candidates: Sequence[Candidate], fixture: Fixture) -> list[Candidate]:
        """The candidates that may be recommended, in their original order."""
        return [candidate for candidate in candidates if self.allows(candidate, fixture)]


EMPTY_OVERLAY: Final = LearningOverlay()


class LearningSource(Protocol):
    """Where an analysis gets its overlay from."""

    async def load(self) -> LearningOverlay:
        """The overlay in force now (never raises for learning-data problems)."""
        ...


class NoLearning:
    """The raw model: no calibration, no segment rules."""

    async def load(self) -> LearningOverlay:
        """Always the empty overlay."""
        return EMPTY_OVERLAY


def version_of(record: CalibrationRecord) -> CalibrationVersion:
    """A usable :class:`CalibrationVersion` from a stored row.

    Raises:
        ModelInputError: the stored knots are corrupt.
    """
    stored: object = record.params  # JSON column: whatever was written, not what the type says
    if not isinstance(stored, Mapping):
        msg = f"stored calibration params must be an object, got {type(stored).__name__}"
        raise ModelInputError(msg)
    params = {"x": list(stored.get("x", [])), "y": list(stored.get("y", []))}
    validate_params(params)
    return CalibrationVersion(
        version=record.version,
        method=record.method,
        params=params,
        n_samples=record.n_samples,
        brier_before=record.brier_before,
        brier_after=record.brier_after,
        created_at=record.created_at,
        passed_gate=record.passed_gate,
    )


def rules_of(rows: Sequence[SegmentRecord]) -> dict[SegmentKey, SegmentRule]:
    """Rules for the restricted segments among ``rows`` (unrestricted ones need no rule)."""
    return {
        SegmentKey(
            row.sport.value, str(row.league_ref), row.market.value, row.odds_bucket
        ): SegmentRule(enabled=row.enabled, ev_floor=row.ev_threshold)
        for row in rows
        if not row.enabled or row.ev_threshold > NO_FLOOR
    }


async def load_overlay(source: LearningSource) -> LearningOverlay:
    """The overlay of ``source``, or the empty one (logged) if it fails for any reason.

    The analysis path calls this instead of ``source.load()``: whatever is wrong with the learning
    data, a recommendation must still be produced from the raw model.
    """
    try:
        return await source.load()
    except Exception as error:
        logger.warning("learning_overlay_failed", error=repr(error))
        return EMPTY_OVERLAY


class DbLearningSource:
    """Reads the active calibration and the restricted segments from the database."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Read through ``sessions``; each :meth:`load` is one short read-only transaction."""
        self._sessions = sessions

    async def load(self) -> LearningOverlay:
        """The stored overlay, or the empty one (logged) if it cannot be read or is corrupt."""
        try:
            async with self._sessions() as session:
                record = await active_calibration(session)
                rows = await list_segment_rows(session)
            calibration = None if record is None else version_of(record)
            return LearningOverlay(calibration=calibration, rules=rules_of(rows))
        except (SQLAlchemyError, ModelInputError, TypeError, ValueError) as error:
            logger.warning("learning_overlay_unavailable", error=repr(error))
            return EMPTY_OVERLAY
