from unittest.mock import Mock

import pytest

from mini_deerflow.planner import create_research_plan
from mini_deerflow.schemas import Plan, PlanStep


def make_plan() -> Plan:
    return Plan(
        goal="Research how planning improves a tool-using AI agent.",
        steps=[
            PlanStep(
                step_number=number,
                title=f"Research step {number}",
                objective=f"Collect evidence for research step {number}.",
                success_criteria="At least one verifiable result is recorded.",
            )
            for number in range(1, 4)
        ],
    )


def test_create_research_plan_uses_structured_output():
    model = Mock()
    structured_model = Mock()
    expected_plan = make_plan()

    model.with_structured_output.return_value = structured_model
    structured_model.invoke.return_value = expected_plan

    actual_plan = create_research_plan(
        model,
        "  Research how planning improves a tool-using AI agent.  ",
    )

    assert actual_plan == expected_plan

    model.with_structured_output.assert_called_once_with(
        Plan,
        method="function_calling",
    )

    messages = structured_model.invoke.call_args.args[0]

    assert messages[-1] == (
        "human",
        "Research how planning improves a tool-using AI agent.",
    )


def test_create_research_plan_rejects_short_goal():
    model = Mock()

    with pytest.raises(
        ValueError,
        match="at least 10 characters",
    ):
        create_research_plan(model, "short")

    model.with_structured_output.assert_not_called()


def test_create_research_plan_rejects_unexpected_result_type():
    model = Mock()
    structured_model = Mock()

    model.with_structured_output.return_value = structured_model
    structured_model.invoke.return_value = {"goal": "not validated"}

    with pytest.raises(
        TypeError,
        match="expected Plan",
    ):
        create_research_plan(
            model,
            "Research a sufficiently detailed technical topic.",
        )
