"""Plain input DTOs of the LLM step.

They carry already computed facts (the pipeline fills them from models, repositories and
providers); the LLM layer imports nothing from those layers. Free text fields are capped
so one oversized value cannot eat the token budget.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

MAX_NAME_CHARS: Final = 80
MAX_REASON_CHARS: Final = 120
MAX_USER_NOTE_CHARS: Final = 240
MAX_LESSON_CHARS: Final = 200
MAX_STAT_CHARS: Final = 200
MAX_CANDIDATES: Final = 10
MAX_LESSONS: Final = 5


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        msg = "must be timezone-aware"
        raise ValueError(msg)
    return value


type Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_CHARS)
]
type Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REASON_CHARS)
]
type Note = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_USER_NOTE_CHARS)
]
type Lesson = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_LESSON_CHARS)
]
type Stat = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_STAT_CHARS)
]
type Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AbsenceStatus(StrEnum):
    """Availability of a player."""

    OUT = "out"
    DOUBTFUL = "doubtful"
    SUSPENDED = "suspended"


class Absence(_Frozen):
    """A missing or doubtful player."""

    team: Name
    player: Name
    status: AbsenceStatus
    reason: Reason | None = None


class TeamForm(_Frozen):
    """Compact recent form: results oldest to newest, e.g. ``"WDLWW"``."""

    team: Name
    results: Annotated[str, StringConstraints(pattern=r"^[WDL]{1,10}$")]
    goals_for: Annotated[int, Field(ge=0)]
    goals_against: Annotated[int, Field(ge=0)]


class CandidateInput(_Frozen):
    """One value-bet candidate that passed the numeric filters."""

    candidate_id: Annotated[
        str, StringConstraints(min_length=1, max_length=64, pattern=r"^[\w.:-]+$")
    ]
    league: Name
    home_team: Name
    away_team: Name
    kickoff: Annotated[datetime, AfterValidator(_require_utc)]
    market: Name
    selection: Name
    line: float | None = None
    odds: Annotated[float, Field(gt=1.0, allow_inf_nan=False)]
    p_model: Probability
    p_market: Probability | None = None
    ev: Annotated[float, Field(allow_inf_nan=False)]
    confidence_level: Name
    confidence_reasons: tuple[Reason, ...] = ()
    lineup_known: bool = False
    forms: tuple[TeamForm, ...] = ()
    absences: tuple[Absence, ...] = ()


class ExperienceContext(_Frozen):
    """The "Tajriba konteksti" block: compact history-derived facts (filled by F8; may be empty).

    Lists are ordered by priority, most useful first; the builder trims from the end.
    """

    segment_stats: tuple[Stat, ...] = ()
    lessons: tuple[Lesson, ...] = ()
    user_notes: tuple[Note, ...] = ()


class PostmortemInput(_Frozen):
    """A lost bet with what was known before the match and what happened."""

    candidate: CandidateInput
    final_score: Annotated[str, StringConstraints(pattern=r"^\d{1,2}-\d{1,2}$")]
    xg_summary: Note | None = None
    match_events: tuple[Reason, ...] = ()
