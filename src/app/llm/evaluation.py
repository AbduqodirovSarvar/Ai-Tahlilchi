"""Golden evaluation set: loading, checking and reporting.

A golden case is a JSON file with candidates, optional experience context, per-candidate
expectations and a *recorded* model answer. CI feeds the recorded answer through the real
service (builder, validator, guards, fallback) to prove that logic; ``scripts/llm_eval.py``
sends the same cases to the live model, by hand, to compare prompt versions.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.errors import DataError
from app.llm.inputs import CandidateInput, ExperienceContext
from app.llm.schemas import LlmReview, ReviewResponse, ReviewStatus, RiskFlag, Verdict


class CaseExpectation(BaseModel):
    """What a good answer for one candidate looks like (all parts optional)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict_in: tuple[Verdict, ...] = ()
    flags_include: tuple[RiskFlag, ...] = ()
    status: ReviewStatus | None = None
    note_forbidden: tuple[str, ...] = ()


class GoldenCase(BaseModel):
    """One evaluation scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str
    candidates: tuple[CandidateInput, ...]
    context: ExperienceContext | None = None
    expected: dict[str, CaseExpectation]
    recorded_output: ReviewResponse | None = None
    recorded_raw: str | None = None
    # Cases that only make sense with a scripted answer (guards, malformed output).
    recorded_only: bool = False

    @property
    def recorded_text(self) -> str:
        """The scripted model answer, exactly as the backend would return it."""
        if self.recorded_raw is not None:
            return self.recorded_raw
        if self.recorded_output is not None:
            return self.recorded_output.model_dump_json()
        msg = f"golden case {self.id} has no recorded answer"
        raise DataError(msg)


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Outcome of checking one case."""

    case_id: str
    failures: tuple[str, ...]
    reviews: tuple[LlmReview, ...]

    @property
    def passed(self) -> bool:
        """No expectation was violated."""
        return not self.failures


def load_golden_cases(directory: Path) -> tuple[GoldenCase, ...]:
    """Read every ``*.json`` case, sorted by file name.

    Raises:
        DataError: a file is not a valid case, or a case id repeats.
    """
    cases: list[GoldenCase] = []
    for path in sorted(directory.glob("*.json")):
        try:
            cases.append(GoldenCase.model_validate_json(path.read_text(encoding="utf-8")))
        except ValueError as exc:
            msg = f"invalid golden case {path.name}: {exc}"
            raise DataError(msg) from exc
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        msg = "golden case ids must be unique"
        raise DataError(msg)
    return tuple(cases)


def _check_review(review: LlmReview | None, spec: CaseExpectation, candidate_id: str) -> list[str]:
    if review is None:
        return [f"{candidate_id}: no review returned"]
    failures: list[str] = []
    if spec.status is not None and review.status != spec.status:
        failures.append(f"{candidate_id}: status {review.status}, expected {spec.status}")
    if spec.status is None and review.status == ReviewStatus.FALLBACK:
        failures.append(f"{candidate_id}: fell back ({review.fallback_reason})")
    if spec.verdict_in and review.verdict not in spec.verdict_in:
        allowed = [v.value for v in spec.verdict_in]
        failures.append(f"{candidate_id}: verdict {review.verdict}, expected one of {allowed}")
    missing = [flag.value for flag in spec.flags_include if flag not in review.risk_flags]
    if missing:
        failures.append(f"{candidate_id}: missing risk flags {missing}")
    note = (review.note or "").casefold()
    leaked = [text for text in spec.note_forbidden if text.casefold() in note]
    if leaked:
        failures.append(f"{candidate_id}: note contains forbidden text {leaked}")
    return failures


def check_case(case: GoldenCase, reviews: Sequence[LlmReview]) -> CaseResult:
    """Compare the service's reviews with the case's expectations."""
    by_id: Mapping[str, LlmReview] = {review.candidate_id: review for review in reviews}
    failures: list[str] = []
    for candidate_id, spec in case.expected.items():
        failures.extend(_check_review(by_id.get(candidate_id), spec, candidate_id))
    return CaseResult(case_id=case.id, failures=tuple(failures), reviews=tuple(reviews))


def render_report(
    results: Sequence[CaseResult], *, model: str, prompt_version: str, day: date
) -> str:
    """Markdown report for ``docs/llm_eval/<date>.md`` (Uzbek headings, ids as in the files)."""
    passed = sum(result.passed for result in results)
    tokens = sum(r.input_tokens + r.output_tokens for res in results for r in res.reviews)
    lines = [
        f"# LLM baholash: {day.isoformat()}",
        "",
        f"- Model: `{model}`",
        f"- Prompt: `{prompt_version}`",
        f"- Natija: **{passed}/{len(results)}** holat o'tdi",
        f"- Sarflangan tokenlar: {tokens}",
        "",
        "| Holat | Natija | Izoh |",
        "|---|---|---|",
    ]
    for result in results:
        verdicts = ", ".join(
            f"{r.candidate_id}={r.verdict.value if r.verdict else r.status.value}"
            for r in result.reviews
        )
        detail = "; ".join(result.failures) if result.failures else verdicts
        status = "OK" if result.passed else "XATO"
        lines.append(f"| `{result.case_id}` | {status} | {detail.replace('|', '/')} |")
    return "\n".join(lines) + "\n"
