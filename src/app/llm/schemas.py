"""Output contracts of the LLM step: what the model may say, and what the pipeline gets back.

The wire models (``ReviewResponse``, ``PostmortemOutput``) double as the Gemini
``response_schema``. They deliberately have no field for probabilities: the model cannot
change a number it is never asked to return. Stricter content rules (sentence count, length)
are enforced in code after parsing, because provider schemas support only a subset of
JSON-schema keywords.
"""

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class Verdict(StrEnum):
    """Qualitative judgement of a candidate bet."""

    CONFIRM = "tasdiq"
    CAUTION = "ehtiyot"
    REJECT = "rad"


class RiskFlag(StrEnum):
    """Fixed vocabulary of risks the numbers do not show."""

    INJURY = "jarohat"
    SUSPENSION = "diskvalifikatsiya"
    ROTATION = "rotatsiya"
    MOTIVATION = "motivatsiya"
    DERBY = "derbi"
    LINEUP_UNKNOWN = "tarkib_nomalum"
    INSUFFICIENT_DATA = "malumot_yetarli_emas"
    CONTRADICTORY_DATA = "ziddiyatli_malumot"


class PostmortemCategory(StrEnum):
    """Why a losing bet lost (plan 6.13, layer 4)."""

    BAD_LUCK = "omadsizlik"
    MISSING_INFO = "ma'lumot yetishmadi"
    MODEL_ERROR = "model xatosi"


class ReviewStatus(StrEnum):
    """How trustworthy a :class:`LlmReview` is."""

    OK = "ok"
    NOTE_DROPPED = "note_dropped"
    FALLBACK = "fallback"


# Flags added by code (not the model) to explain why a note was removed.
FLAG_UNKNOWN_NAME: Final = "unknown_name_in_note"
FLAG_NOTE_TOO_LONG: Final = "note_too_long"
FLAG_NOTE_SUSPICIOUS: Final = "note_suspicious"


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ReviewItem(_Wire):
    """The model's verdict for one candidate."""

    candidate_id: str = Field(description="Copy of the candidate's id from the input.")
    verdict: Verdict
    risk_flags: list[RiskFlag] = Field(description="Risks found in the data; may be empty.")
    izoh: str = Field(
        description="At most two sentences, Uzbek (Latin), based only on the input data."
    )


class ReviewResponse(_Wire):
    """Whole answer of one review request."""

    reviews: list[ReviewItem]


class PostmortemOutput(_Wire):
    """The model's classification of one lost bet."""

    category: PostmortemCategory
    reason: str = Field(description="One or two sentences, Uzbek (Latin), from the input only.")


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt_version: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class LlmReview(_Result):
    """Review of one candidate as the pipeline consumes it.

    ``status=FALLBACK`` means the LLM step produced nothing usable for this candidate: the
    signal must be sent with its numbers only (``verdict`` and ``note`` are ``None``).
    ``NOTE_DROPPED`` keeps the verdict but removes a note that failed a guard (``flags``
    says which). ``tokens`` of a batch call are attributed to its first review only, so
    summing over a result tuple gives the true cost.
    """

    candidate_id: str
    status: ReviewStatus
    verdict: Verdict | None = None
    risk_flags: tuple[RiskFlag, ...] = ()
    note: str | None = None
    flags: tuple[str, ...] = ()
    fallback_reason: str | None = None


class PostmortemResult(_Result):
    """Classification of one lost bet."""

    category: PostmortemCategory
    reason: str
