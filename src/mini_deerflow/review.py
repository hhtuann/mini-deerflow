from collections.abc import Callable, Sequence
from typing import Annotated, Literal, Protocol, Self, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StringConstraints,
    model_validator,
)

from mini_deerflow.actions import (
    CompletionSummary,
    ToolObservation,
)
from mini_deerflow.evidence import (
    EvidenceRecord,
    StepFinding,
    sanitize_finding_summary,
)
from mini_deerflow.schemas import Plan, PlanStep

ReviewRoute = Literal[
    "continue",
    "replan",
    "finish",
]

ReviewFindingCategory = Literal[
    "gap",
    "contradiction",
    "relevance",
    "source_diversity",
    "direct_support",
    "citation_validity",
    "budget_limitation",
]

ReviewGoal = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=1_000,
    ),
]

ReviewRationale = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=10,
        max_length=2_000,
    ),
]

FindingDescription = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=5,
        max_length=1_000,
    ),
]

MAX_REVIEW_FINDINGS = 20
MAX_REVIEW_LIMITATIONS = 20
MAX_RELATED_STEP_NUMBERS = 7


class ReviewModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )


class ReviewFinding(ReviewModel):
    """One traceable evidence-quality observation produced by a review."""

    category: ReviewFindingCategory
    description: FindingDescription
    related_step_numbers: list[int] = Field(
        default_factory=list,
        max_length=MAX_RELATED_STEP_NUMBERS,
    )

    @model_validator(mode="after")
    def validate_related_step_numbers(self) -> Self:
        for step_number in self.related_step_numbers:
            if not 1 <= step_number <= 7:
                raise ValueError(
                    "related step numbers must reference plan steps 1-7",
                )

        return self


class ReviewVerdict(ReviewModel):
    """The structured outcome of one evidence-quality review."""

    verdict: ReviewRoute
    rationale: ReviewRationale
    findings: list[ReviewFinding] = Field(
        default_factory=list,
        max_length=MAX_REVIEW_FINDINGS,
    )


class ReviewDecision(RootModel[ReviewVerdict]):
    model_config = ConfigDict(
        frozen=True,
    )


def parse_review_verdict(payload: object) -> ReviewVerdict:
    return ReviewDecision.model_validate(payload).root


def coerce_review_verdict(response: object) -> ReviewVerdict:
    """Convert one structured model response into a validated review verdict."""

    if isinstance(response, ReviewVerdict):
        return response

    if isinstance(response, ReviewDecision):
        return response.root

    return parse_review_verdict(response)


class ReplacementWork(ReviewModel):
    """A validated replacement for only the remaining steps of a plan."""

    steps: list[PlanStep] = Field(
        min_length=1,
        max_length=7,
    )

    @model_validator(mode="after")
    def validate_step_numbers(self) -> Self:
        actual = [step.step_number for step in self.steps]
        expected = list(range(1, len(self.steps) + 1))

        if actual != expected:
            raise ValueError(
                "replacement step_number must be consecutive starting at 1; "
                f"expected {expected}, received {actual}"
            )

        return self


class ReplanRecord(ReviewModel):
    """One persisted replan event in the bounded review/replan history."""

    replan_number: int = Field(
        ge=1,
        le=7,
    )
    replaced_step_numbers: list[int] = Field(
        default_factory=list,
        max_length=MAX_RELATED_STEP_NUMBERS,
    )
    replacement_steps: list[PlanStep] = Field(
        min_length=1,
        max_length=7,
    )
    review_rationale: ReviewRationale


def replacement_step_bounds(
    completed_step_count: int,
) -> tuple[int, int]:
    """Return the inclusive replacement-step count bounds for one replan."""

    if (
        isinstance(completed_step_count, bool)
        or not isinstance(completed_step_count, int)
        or completed_step_count < 0
        or completed_step_count > 7
    ):
        raise ValueError(
            "completed_step_count must be an integer between 0 and 7",
        )

    minimum = max(1, 3 - completed_step_count)
    maximum = 7 - completed_step_count

    if minimum > maximum:
        raise ValueError(
            "the seven-step plan limit leaves no room for a replacement",
        )

    return minimum, maximum


def merge_replanned_steps(
    goal: str,
    completed_steps: Sequence[PlanStep],
    replacement_steps: Sequence[PlanStep],
) -> Plan:
    """Compose the next plan from preserved completed work and new steps."""

    _, maximum = replacement_step_bounds(len(completed_steps))

    if len(replacement_steps) > maximum:
        raise ValueError(
            "replacement exceeds the remaining plan capacity",
        )

    renumbered = [
        step.model_copy(
            update={
                "step_number": len(completed_steps) + position,
            },
        )
        for position, step in enumerate(replacement_steps, start=1)
    ]

    return Plan(
        goal=goal,
        steps=[*completed_steps, *renumbered],
    )


def derive_review_limitations(
    observations: Sequence[ToolObservation],
    errors: Sequence[str],
    *,
    max_limitations: int = MAX_REVIEW_LIMITATIONS,
) -> list[str]:
    """Describe failed observations and recorded errors as bounded limitations."""

    limitations = [
        f"Step {observation.step_number} tool call "
        f"({observation.action.tool_name}) failed: {observation.result.error}"
        for observation in observations
        if not observation.result.success
    ]
    limitations.extend(errors)

    return limitations[-max_limitations:]


class ReviewContext(ReviewModel):
    """The bounded, evidence-focused input given to one review."""

    goal: ReviewGoal
    remaining_steps: list[PlanStep] = Field(
        max_length=MAX_RELATED_STEP_NUMBERS,
    )
    completed_step_summaries: list[CompletionSummary] = Field(
        default_factory=list,
        max_length=MAX_RELATED_STEP_NUMBERS,
    )
    findings: list[StepFinding] = Field(
        default_factory=list,
        max_length=MAX_RELATED_STEP_NUMBERS,
    )
    evidence: list[EvidenceRecord] = Field(
        default_factory=list,
        max_length=50,
    )
    limitations: list[str] = Field(
        default_factory=list,
        max_length=MAX_REVIEW_LIMITATIONS,
    )
    remaining_total_tool_calls: int = Field(
        ge=0,
    )
    remaining_replan_cycles: int = Field(
        ge=0,
    )


class ReplanRequest(ReviewModel):
    """The bounded input given to the replanner after a replan verdict."""

    goal: ReviewGoal
    review: ReviewVerdict
    completed_step_summaries: list[CompletionSummary] = Field(
        default_factory=list,
        max_length=MAX_RELATED_STEP_NUMBERS,
    )
    replaced_steps: list[PlanStep] = Field(
        max_length=MAX_RELATED_STEP_NUMBERS,
    )
    available_tools: list[dict[str, JsonValue]] = Field(
        default_factory=list,
        max_length=50,
    )
    remaining_total_tool_calls: int = Field(
        ge=0,
    )
    remaining_replan_cycles: int = Field(
        ge=0,
    )
    min_replacement_steps: int = Field(
        ge=1,
        le=7,
    )
    max_replacement_steps: int = Field(
        ge=1,
        le=7,
    )

    @model_validator(mode="after")
    def validate_replacement_bounds(self) -> Self:
        if self.min_replacement_steps > self.max_replacement_steps:
            raise ValueError(
                "min_replacement_steps cannot exceed max_replacement_steps",
            )

        return self


@runtime_checkable
class EvidenceReviewer(Protocol):
    async def review_evidence(
        self,
        context: ReviewContext,
    ) -> ReviewVerdict:
        """Judge whether the accumulated evidence is sufficient."""


Replanner = Callable[[ReplanRequest], ReplacementWork]


def render_review_conclusions(
    review_verdicts: Sequence[ReviewVerdict],
) -> list[str]:
    """Render sanitized review conclusions for the deterministic report."""

    if not review_verdicts:
        return ["- No review verdicts were recorded."]

    lines: list[str] = []

    for review_number, verdict in enumerate(review_verdicts, start=1):
        lines.append(
            f"- **Review {review_number} — {verdict.verdict}:** "
            f"{sanitize_finding_summary(verdict.rationale)}"
        )

        for finding in verdict.findings:
            steps = ", ".join(
                str(step_number) for step_number in finding.related_step_numbers
            )
            scope = f" (steps: {steps})" if steps else ""
            lines.append(
                f"  - [{finding.category}] "
                f"{sanitize_finding_summary(finding.description)}{scope}"
            )

    return lines
