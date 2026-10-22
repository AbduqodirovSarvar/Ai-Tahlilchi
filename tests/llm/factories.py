"""Shared builders and a scripted backend for the LLM tests (no network, no SDK)."""

import json
from datetime import UTC, datetime

from pydantic import BaseModel

from app.clients.gemini import GeminiError, GeminiResponse, GeminiUsage
from app.llm.inputs import Absence, AbsenceStatus, CandidateInput, TeamForm


def make_candidate(candidate_id: str = "c1", **overrides: object) -> CandidateInput:
    """A valid candidate; override any field by keyword."""
    data: dict[str, object] = {
        "candidate_id": candidate_id,
        "league": "Premier League",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "kickoff": datetime(2026, 10, 24, 14, 0, tzinfo=UTC),
        "market": "1X2",
        "selection": "home",
        "odds": 2.1,
        "p_model": 0.55,
        "p_market": 0.5,
        "ev": 0.155,
        "confidence_level": "O'rta",
        "confidence_reasons": ("forma yetarli",),
        "lineup_known": True,
        "forms": (TeamForm(team="Arsenal", results="WWDWW", goals_for=11, goals_against=3),),
        "absences": (
            Absence(team="Chelsea", player="Palmer", status=AbsenceStatus.OUT, reason="jarohat"),
        ),
    }
    data.update(overrides)
    return CandidateInput.model_validate(data)


def review_json(*items: tuple[str, str, list[str], str]) -> str:
    """Model answer text for ``(candidate_id, verdict, risk_flags, izoh)`` tuples."""
    reviews = [
        {"candidate_id": cid, "verdict": verdict, "risk_flags": flags, "izoh": note}
        for cid, verdict, flags, note in items
    ]
    return '{"reviews": ' + json.dumps(reviews, ensure_ascii=False) + "}"


class ScriptedBackend:
    """Plays a script of answers (text) or failures; records every request."""

    model = "gemini-fake"

    def __init__(
        self, *script: str | BaseException, input_tokens: int = 100, output_tokens: int = 50
    ) -> None:
        self.script = list(script)
        self.requests: list[dict[str, str]] = []
        self.usage = GeminiUsage(input_tokens=input_tokens, output_tokens=output_tokens)

    async def generate_json(
        self, *, system_instruction: str, contents: str, response_schema: type[BaseModel]
    ) -> GeminiResponse:
        self.requests.append(
            {"system": system_instruction, "contents": contents, "schema": response_schema.__name__}
        )
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return GeminiResponse(text=item, model=self.model, usage=self.usage, attempts=1)


def gemini_down() -> GeminiError:
    return GeminiError("Gemini request failed after 3 attempt(s): TimeoutError")
