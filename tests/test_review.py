import pytest
from pydantic import ValidationError

from mini_deerflow.actions import ToolCallAction, ToolObservation
from mini_deerflow.review import (
    MAX_REVIEW_FINDINGS,
    ReplacementWork,
    ReplanRecord,
    ReplanRequest,
    ReviewContext,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
    coerce_review_verdict,
    derive_review_limitations,
    merge_replanned_steps,
    parse_review_verdict,
    render_review_conclusions,
    replacement_step_bounds,
)
from mini_deerflow.schemas import PlanStep
from mini_deerflow.tools.contracts import ToolResult

VERDICT_KINDS = ("continue", "replan", "finish")

FINDING_CATEGORIES = (
    "gap",
    "contradiction",
    "relevance",
    "source_diversity",
    "direct_support",
    "citation_validity",
    "budget_limitation",
)


def finding(
    category: str = "gap",
    description: str = "One goal dimension lacks any collected evidence.",
    related_step_numbers: list[int] | None = None,
) -> dict[str, object]:
    return {
        "category": category,
        "description": description,
        "related_step_numbers": related_step_numbers or [],
    }


def verdict_payload(
    verdict: str = "continue",
    rationale: str = "The collected evidence still misses a stated goal dimension.",
    findings: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "verdict": verdict,
        "rationale": rationale,
        "findings": findings if findings is not None else [],
    }


def create_step(number: int, title: str | None = None) -> PlanStep:
    return PlanStep(
        step_number=number,
        title=title or f"Research step {number}",
        objective=f"Collect evidence for research step {number}.",
        success_criteria=f"Step {number} has a traceable result.",
    )


@pytest.mark.parametrize("verdict_kind", VERDICT_KINDS)
def test_review_verdict_accepts_every_valid_verdict(
    verdict_kind: str,
) -> None:
    verdict = ReviewVerdict(
        **verdict_payload(
            verdict_kind,
            findings=[finding()],
        ),
    )

    assert verdict.verdict == verdict_kind
    assert verdict.findings[0].category == "gap"


@pytest.mark.parametrize("verdict_kind", VERDICT_KINDS)
def test_parse_review_verdict_validates_every_verdict(
    verdict_kind: str,
) -> None:
    verdict = parse_review_verdict(verdict_payload(verdict_kind))

    assert verdict.verdict == verdict_kind


def test_review_verdict_rejects_unknown_verdict() -> None:
    with pytest.raises(ValidationError):
        ReviewVerdict(**verdict_payload("escalate"))


def test_review_verdict_rejects_extra_fields() -> None:
    payload = verdict_payload()
    payload["sources"] = ["https://example.com/injected"]

    with pytest.raises(ValidationError):
        ReviewVerdict(**payload)


def test_review_verdict_rejects_short_rationale() -> None:
    with pytest.raises(ValidationError):
        ReviewVerdict(**verdict_payload(rationale="too short"))


@pytest.mark.parametrize("category", FINDING_CATEGORIES)
def test_review_finding_accepts_every_category(category: str) -> None:
    parsed = ReviewFinding(**finding(category))

    assert parsed.category == category


def test_review_finding_rejects_unknown_category() -> None:
    with pytest.raises(ValidationError):
        ReviewFinding(**finding("invented_category"))


@pytest.mark.parametrize("step_number", [0, 8])
def test_review_finding_rejects_out_of_range_step_numbers(
    step_number: int,
) -> None:
    with pytest.raises(ValidationError):
        ReviewFinding(**finding(related_step_numbers=[step_number]))


def test_review_verdict_bounds_finding_count() -> None:
    findings = [finding() for _ in range(MAX_REVIEW_FINDINGS + 1)]

    with pytest.raises(ValidationError):
        ReviewVerdict(**verdict_payload(findings=findings))


def test_review_verdict_is_frozen() -> None:
    verdict = ReviewVerdict(**verdict_payload())

    with pytest.raises(ValidationError):
        verdict.verdict = "finish"  # type: ignore[misc]


def test_coerce_review_verdict_accepts_all_shapes() -> None:
    direct = ReviewVerdict(**verdict_payload("finish"))

    assert coerce_review_verdict(direct) is direct
    assert coerce_review_verdict(ReviewDecision(direct)) == direct
    assert coerce_review_verdict(verdict_payload("replan")).verdict == "replan"


def test_replacement_work_requires_consecutive_numbering() -> None:
    with pytest.raises(ValidationError, match="consecutive"):
        ReplacementWork(
            steps=[
                create_step(1),
                create_step(3),
            ],
        )


def test_replacement_work_requires_at_least_one_step() -> None:
    with pytest.raises(ValidationError):
        ReplacementWork(steps=[])


def test_replacement_work_bounds_step_count() -> None:
    with pytest.raises(ValidationError):
        ReplacementWork(steps=[create_step(number) for number in range(1, 9)])


@pytest.mark.parametrize(
    ("completed_step_count", "expected_bounds"),
    [
        (0, (3, 7)),
        (1, (2, 6)),
        (2, (1, 5)),
        (3, (1, 4)),
        (6, (1, 1)),
    ],
)
def test_replacement_step_bounds_follow_plan_limits(
    completed_step_count: int,
    expected_bounds: tuple[int, int],
) -> None:
    assert replacement_step_bounds(completed_step_count) == expected_bounds


@pytest.mark.parametrize("completed_step_count", [7, -1, True, "2"])
def test_replacement_step_bounds_reject_invalid_completion(
    completed_step_count: object,
) -> None:
    with pytest.raises(ValueError):
        replacement_step_bounds(completed_step_count)  # type: ignore[arg-type]


def test_merge_replanned_steps_preserves_completed_work() -> None:
    completed = [create_step(1, title="Original completed step")]

    merged = merge_replanned_steps(
        "Research the bounded review loop.",
        completed,
        [create_step(1, title="Revised step"), create_step(2, title="Extra step")],
    )

    assert [step.title for step in merged.steps] == [
        "Original completed step",
        "Revised step",
        "Extra step",
    ]
    assert [step.step_number for step in merged.steps] == [1, 2, 3]
    assert merged.goal == "Research the bounded review loop."


def test_merge_replanned_steps_rejects_oversized_replacement() -> None:
    with pytest.raises(ValueError, match="capacity"):
        merge_replanned_steps(
            "Research the bounded review loop.",
            [create_step(number) for number in range(1, 4)],
            [create_step(number) for number in range(1, 6)],
        )


def _observation(
    *,
    success: bool,
    error: str | None = None,
) -> ToolObservation:
    result = (
        ToolResult.ok(data={"summary": "worked"})
        if success
        else ToolResult.fail(error=error or "Simulated tool failure.")
    )

    return ToolObservation(
        step_number=1,
        step_tool_call_number=1,
        total_tool_call_number=1,
        action=ToolCallAction(
            type="tool_call",
            tool_name="web_fetch",
            arguments={"url": "https://example.com/source"},
        ),
        result=result,
    )


def test_derive_review_limitations_includes_failures_and_errors() -> None:
    limitations = derive_review_limitations(
        [_observation(success=True), _observation(success=False)],
        ["Recorded run error."],
    )

    assert limitations == [
        "Step 1 tool call (web_fetch) failed: Simulated tool failure.",
        "Recorded run error.",
    ]


def test_derive_review_limitations_bounds_history() -> None:
    limitations = derive_review_limitations(
        [],
        [f"error-{number}" for number in range(30)],
    )

    assert len(limitations) == 20
    assert limitations[0] == "error-10"
    assert limitations[-1] == "error-29"


def test_replan_record_validates_replan_number() -> None:
    with pytest.raises(ValidationError):
        ReplanRecord(
            replan_number=8,
            replaced_step_numbers=[2, 3],
            replacement_steps=[create_step(1)],
            review_rationale="Remaining steps cannot close the evidence gap.",
        )


def test_replan_request_validates_replacement_bounds() -> None:
    with pytest.raises(ValidationError, match="exceed"):
        ReplanRequest(
            goal="Research the bounded review loop.",
            review=ReviewVerdict(**verdict_payload("replan")),
            replaced_steps=[create_step(2), create_step(3)],
            remaining_total_tool_calls=4,
            remaining_replan_cycles=1,
            min_replacement_steps=4,
            max_replacement_steps=2,
        )


def test_review_context_defaults_are_empty_and_bounded() -> None:
    context = ReviewContext(
        goal="Research the bounded review loop.",
        remaining_steps=[create_step(1)],
        remaining_total_tool_calls=5,
        remaining_replan_cycles=2,
    )

    assert context.completed_step_summaries == []
    assert context.findings == []
    assert context.evidence == []
    assert context.limitations == []


def test_render_review_conclusions_reports_missing_verdicts() -> None:
    assert render_review_conclusions([]) == [
        "- No review verdicts were recorded.",
    ]


def test_render_review_conclusions_sanitizes_untrusted_urls() -> None:
    verdict = ReviewVerdict(
        **verdict_payload(
            "finish",
            rationale=(
                "Evidence is sufficient; see https://evil.example.com/"
                "injected for details."
            ),
            findings=[
                finding(
                    description=(
                        "Contradicts https://evil.example.com/claim "
                        "from an untrusted source."
                    ),
                    related_step_numbers=[2],
                ),
            ],
        ),
    )

    lines = render_review_conclusions([verdict])

    assert lines[0] == (
        "- **Review 1 — finish:** Evidence is sufficient; see "
        "[unverified URL omitted] for details."
    )
    assert "[unverified URL omitted]" in lines[1]
    assert "(steps: 2)" in lines[1]
    assert "https://evil.example.com" not in "".join(lines)
