"""How far the learning loop is allowed to act: its mode and the post-rollback hold (ADR 0013).

``shadow`` is the default: the weekly job computes everything and journals what it *would*
change, but the analysis path keeps the raw model. ``apply`` is a human decision taken after
reviewing that shadow log.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final

DEFAULT_MANUAL_HOLD: Final = timedelta(days=30)
"""How long a segment or the calibration is left alone after a person rolled a change back."""


class LearningMode(StrEnum):
    """Whether the learning loop runs and whether its result reaches the analysis."""

    OFF = "off"
    SHADOW = "shadow"
    APPLY = "apply"


@dataclass(frozen=True, slots=True)
class LearningSettings:
    """The mode of the loop and the freeze after a manual rollback."""

    mode: LearningMode = LearningMode.SHADOW
    manual_hold: timedelta = DEFAULT_MANUAL_HOLD
