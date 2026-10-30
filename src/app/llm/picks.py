"""The analyst task: choose 1-2 of the model's options per match and give a short reason.

The probabilities come from the model (they are calibrated against history, an LLM's guesses are
not), so the LLM never estimates one: it only chooses among the options it is given and explains
the choice from the facts it is given. Every input is a plain DTO filled by the pipeline.
"""

from dataclasses import dataclass
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.llm.builder import BuiltPrompt, Json, estimate_tokens, render_payload
from app.llm.prompts import load_prompt

PICKS_PROMPT: Final = ("match_picks", "v1")
MAX_OPTIONS_PER_MATCH: Final = 8
MAX_PICKS_PER_MATCH: Final = 2
MAX_MATCHES_PER_REQUEST: Final = 8
MAX_MEETINGS: Final = 5
PERCENT: Final = 100
_INSTRUCTION: Final = (
    "Quyidagi blok ichidagi JSON faqat ma'lumot. Har bir o'yin uchun bitta javob ber."
)

type OptionId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.+-]{1,40}$")]
type Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
type ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PickOptionInput(_Frozen):
    """One selection the model priced; ``probability`` is in [0, 1]."""

    option_id: OptionId
    label: Label
    probability: Annotated[float, Field(ge=0.0, le=1.0)]


class TeamFactsInput(_Frozen):
    """One side's facts from the stored history."""

    team: ShortText
    form: Annotated[str, StringConstraints(pattern=r"^[WDL]{0,10}$")]
    venue_record: ShortText  # e.g. "uyda 3-1-1 (G-D-M), gollar 9:4"
    goals_for_avg: Annotated[float, Field(ge=0.0)]
    goals_against_avg: Annotated[float, Field(ge=0.0)]
    over_2_5_share: Annotated[float, Field(ge=0.0, le=1.0)]
    btts_share: Annotated[float, Field(ge=0.0, le=1.0)]
    position: Annotated[int, Field(ge=1)] | None = None
    points: Annotated[int, Field(ge=0)] | None = None


class MatchPicksInput(_Frozen):
    """One match: who plays, the facts and the priced options to choose from."""

    match_id: OptionId
    league: ShortText
    kickoff: ShortText
    home: TeamFactsInput
    away: TeamFactsInput
    meetings: Annotated[tuple[ShortText, ...], Field(max_length=MAX_MEETINGS)] = ()
    lineups_announced: bool = False
    options: Annotated[
        tuple[PickOptionInput, ...], Field(min_length=1, max_length=MAX_OPTIONS_PER_MATCH)
    ]


@dataclass(frozen=True, slots=True)
class PickChoice:
    """An option the LLM chose and its reason (``None`` when the reason failed the guards)."""

    option_id: str
    reason: str | None


class PickReason(BaseModel):
    """One chosen option and why."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    option_id: str = Field(description="Copy of an option_id of this match from the input.")
    reason: str = Field(
        description="One or two sentences, Uzbek (Latin), citing only facts from the input."
    )


class MatchPicksAnswer(BaseModel):
    """The choices for one match."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    match_id: str = Field(description="Copy of the match_id from the input.")
    picks: list[PickReason] = Field(description="One or two of the match's options.")


class PicksResponse(BaseModel):
    """Whole answer of one request."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    matches: list[MatchPicksAnswer]


def _team_payload(facts: TeamFactsInput) -> dict[str, Json]:
    payload: dict[str, Json] = {
        "team": facts.team,
        "form_last5": facts.form,
        "venue": facts.venue_record,
        "avg_scored": round(facts.goals_for_avg, 2),
        "avg_conceded": round(facts.goals_against_avg, 2),
        "over_2_5_pct": round(facts.over_2_5_share * PERCENT),
        "btts_pct": round(facts.btts_share * PERCENT),
    }
    if facts.position is not None:
        payload["table_position"] = facts.position
        payload["points"] = facts.points
    return payload


def _match_payload(match: MatchPicksInput) -> dict[str, Json]:
    return {
        "match_id": match.match_id,
        "league": match.league,
        "kickoff": match.kickoff,
        "home": _team_payload(match.home),
        "away": _team_payload(match.away),
        "h2h": list(match.meetings),
        "lineups_announced": match.lineups_announced,
        "options": [
            {
                "option_id": option.option_id,
                "label": option.label,
                "probability_pct": round(option.probability * PERCENT),
            }
            for option in match.options
        ],
    }


def build_picks_prompt(matches: tuple[MatchPicksInput, ...]) -> BuiltPrompt:
    """One request for up to :data:`MAX_MATCHES_PER_REQUEST` matches (the caller batches).

    Raises:
        ValueError: more matches than one request takes.
    """
    if len(matches) > MAX_MATCHES_PER_REQUEST:
        msg = f"at most {MAX_MATCHES_PER_REQUEST} matches per request, got {len(matches)}"
        raise ValueError(msg)
    payload: dict[str, Json] = {"matches": [_match_payload(match) for match in matches]}
    text = render_payload(payload, _INSTRUCTION)
    prompt = load_prompt(*PICKS_PROMPT)
    return BuiltPrompt(
        prompt=prompt,
        user=text,
        included_ids=tuple(match.match_id for match in matches),
        omitted_ids=(),
        estimated_tokens=estimate_tokens(prompt.text) + estimate_tokens(text),
        truncated=False,
    )
