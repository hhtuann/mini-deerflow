import json
from collections.abc import Callable
from unittest.mock import Mock

import pytest
from langchain_core.exceptions import OutputParserException
from pydantic import ValidationError

from mini_deerflow.planner import (
    PLANNER_SYSTEM_PROMPT,
    create_research_plan,
)
from mini_deerflow.schemas import Plan, PlanStep


def make_plan() -> Plan:
    return Plan(
        goal="Research how planning improves tool-using AI agents.",
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


def test_create_research_plan_sends_goal_and_tool_catalog() -> None:
    model = Mock()
    structured_model = Mock()
    expected_plan = make_plan()
    available_tools = [
        {
            "name": "list_files",
            "description": "List files in the workspace.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                    }
                },
                "required": ["directory"],
            },
        }
    ]

    model.with_structured_output.return_value = structured_model
    structured_model.invoke.return_value = expected_plan

    actual_plan = create_research_plan(
        model,
        "  Research how planning improves tool-using AI agents.  ",
        available_tools=available_tools,
    )

    assert actual_plan == expected_plan

    model.with_structured_output.assert_called_once_with(
        Plan,
        method="json_mode",
    )

    structured_model.invoke.assert_called_once()

    messages = structured_model.invoke.call_args.args[0]

    assert messages[0] == (
        "system",
        PLANNER_SYSTEM_PROMPT,
    )

    planning_context = json.loads(
        messages[-1][1],
    )

    assert planning_context == {
        "goal": "Research how planning improves tool-using AI agents.",
        "available_tools": available_tools,
    }


def test_create_research_plan_defaults_to_empty_tool_catalog() -> None:
    model = Mock()
    structured_model = Mock()

    model.with_structured_output.return_value = structured_model
    structured_model.invoke.return_value = make_plan()

    create_research_plan(
        model,
        "Research how planning improves tool-using AI agents.",
    )

    messages = structured_model.invoke.call_args.args[0]
    planning_context = json.loads(
        messages[-1][1],
    )

    assert planning_context["available_tools"] == []


def test_create_research_plan_rejects_short_goal() -> None:
    model = Mock()

    with pytest.raises(
        ValueError,
        match="at least 10 characters",
    ):
        create_research_plan(
            model,
            "short",
        )

    model.with_structured_output.assert_not_called()


def test_create_research_plan_rejects_unexpected_result_type() -> None:
    model = Mock()
    structured_model = Mock()

    model.with_structured_output.return_value = structured_model
    structured_model.invoke.return_value = {
        "goal": "not validated",
    }

    with pytest.raises(
        TypeError,
        match="expected Plan",
    ):
        create_research_plan(
            model,
            "Research an unexpected planner response.",
        )

    structured_model.invoke.assert_called_once()


def test_planner_prompt_requires_all_plan_step_fields() -> None:
    required_fields = (
        "step_number",
        "title",
        "objective",
        "success_criteria",
    )

    for field_name in required_fields:
        assert field_name in PLANNER_SYSTEM_PROMPT

    assert "Do not omit any required field" in PLANNER_SYSTEM_PROMPT
    assert "Do not add fields outside the required schema" in (PLANNER_SYSTEM_PROMPT)


def test_planner_prompt_specifies_json_output_contract() -> None:
    json_contract_fragments = (
        '"goal"',
        '"steps"',
        '"step_number"',
        '"title"',
        '"objective"',
        '"success_criteria"',
    )

    assert "Return exactly one JSON object" in PLANNER_SYSTEM_PROMPT

    for fragment in json_contract_fragments:
        assert fragment in PLANNER_SYSTEM_PROMPT

    assert "Do not wrap it in markdown code fences" in PLANNER_SYSTEM_PROMPT


def make_validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as exc_info:
        Plan.model_validate(
            {
                "goal": "Research how planning improves tool-using AI agents.",
                "steps": [{"step_number": 1}],
            }
        )

    return exc_info.value


@pytest.mark.parametrize(
    "failure_factory",
    [
        pytest.param(
            make_validation_error,
            id="validation-error",
        ),
        pytest.param(
            lambda: OutputParserException("Model returned invalid JSON"),
            id="parser-error",
        ),
    ],
)
def test_create_research_plan_recovers_within_bounded_attempts(
    failure_factory: Callable[[], Exception],
) -> None:
    model = Mock()
    structured_model = Mock()
    expected_plan = make_plan()

    model.with_structured_output.return_value = structured_model
    structured_model.invoke.side_effect = [failure_factory(), expected_plan]

    actual_plan = create_research_plan(
        model,
        "Research how planning improves tool-using AI agents.",
    )

    assert actual_plan == expected_plan
    assert structured_model.invoke.call_count == 2


def test_create_research_plan_fails_after_bounded_attempts() -> None:
    model = Mock()
    structured_model = Mock()
    errors = [
        make_validation_error(),
        make_validation_error(),
    ]

    model.with_structured_output.return_value = structured_model
    structured_model.invoke.side_effect = errors

    with pytest.raises(
        ValueError,
        match="model failed to return a valid Plan after 2 attempts",
    ) as exc_info:
        create_research_plan(
            model,
            "Research how planning improves tool-using AI agents.",
        )

    assert structured_model.invoke.call_count == 2
    assert exc_info.value.__cause__ is errors[-1]
