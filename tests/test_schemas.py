import pytest
from pydantic import ValidationError

from mini_deerflow.schemas import Plan, PlanStep


def make_step(step_number: int) -> PlanStep:
    return PlanStep(
        step_number=step_number,
        title=f"Research step {step_number}",
        objective=f"Collect evidence required for research step {step_number}.",
        success_criteria="At least one verifiable result is recorded.",
    )


def test_plan_accepts_valid_steps():
    plan = Plan(
        goal="Research how planning improves a tool-using AI agent.",
        steps=[make_step(number) for number in range(1, 4)],
    )

    assert len(plan.steps) == 3
    assert plan.steps[0].step_number == 1
    assert plan.steps[-1].step_number == 3


@pytest.mark.parametrize("step_count", [2, 8])
def test_plan_rejects_invalid_step_count(step_count):
    with pytest.raises(ValidationError):
        Plan(
            goal="Research how planning improves a tool-using AI agent.",
            steps=[make_step(number) for number in range(1, step_count + 1)],
        )


def test_plan_rejects_non_consecutive_step_numbers():
    with pytest.raises(
        ValidationError,
        match="step_number must be consecutive",
    ):
        Plan(
            goal="Research how planning improves a tool-using AI agent.",
            steps=[make_step(1), make_step(3), make_step(4)],
        )


def test_plan_step_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        PlanStep(
            step_number=1,
            title="Collect sources",
            objective="Collect trustworthy sources about AI agents.",
            success_criteria="At least two sources are recorded.",
            unexpected_field="not allowed",
        )
