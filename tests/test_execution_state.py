import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from mini_deerflow.actions import (
    CompleteStepAction,
    ToolCallAction,
    ToolObservation,
)
from mini_deerflow.state import AgentState, create_initial_state
from mini_deerflow.tools import ToolResult


def create_tool_action(
    tool_name: str = "web_search",
) -> ToolCallAction:
    return ToolCallAction(
        type="tool_call",
        tool_name=tool_name,
        arguments={
            "query": "LangGraph",
        },
    )


def create_observation(
    *,
    step_tool_call_number: int = 1,
    total_tool_call_number: int = 1,
    tool_name: str = "web_search",
) -> ToolObservation:
    return ToolObservation(
        step_number=1,
        step_tool_call_number=step_tool_call_number,
        total_tool_call_number=total_tool_call_number,
        action=create_tool_action(tool_name),
        result=ToolResult.ok(
            data={
                "items": [],
            }
        ),
    )


def test_tool_observation_serializes_action_and_result() -> None:
    observation = create_observation()

    assert observation.model_dump(mode="json") == {
        "step_number": 1,
        "step_tool_call_number": 1,
        "total_tool_call_number": 1,
        "action": {
            "type": "tool_call",
            "tool_name": "web_search",
            "arguments": {
                "query": "LangGraph",
            },
        },
        "result": {
            "success": True,
            "data": {
                "items": [],
            },
            "error": None,
            "metadata": {},
        },
    }


@pytest.mark.parametrize("invalid_step_number", [0, 8])
def test_tool_observation_rejects_invalid_step_number(
    invalid_step_number: int,
) -> None:
    with pytest.raises(ValidationError):
        ToolObservation(
            step_number=invalid_step_number,
            step_tool_call_number=1,
            total_tool_call_number=1,
            action=create_tool_action(),
            result=ToolResult.ok(data={}),
        )


@pytest.mark.parametrize(
    (
        "step_tool_call_number",
        "total_tool_call_number",
    ),
    [
        (0, 1),
        (1, 0),
    ],
)
def test_tool_observation_rejects_non_positive_call_number(
    step_tool_call_number: int,
    total_tool_call_number: int,
) -> None:
    with pytest.raises(ValidationError):
        ToolObservation(
            step_number=1,
            step_tool_call_number=step_tool_call_number,
            total_tool_call_number=total_tool_call_number,
            action=create_tool_action(),
            result=ToolResult.ok(data={}),
        )


def test_step_call_number_cannot_exceed_total_call_number() -> None:
    with pytest.raises(
        ValidationError,
        match="step_tool_call_number cannot exceed",
    ):
        create_observation(
            step_tool_call_number=3,
            total_tool_call_number=2,
        )


def test_tool_observation_rejects_complete_step_action() -> None:
    complete_action = CompleteStepAction(
        type="complete_step",
        summary="Completed the current research step.",
    )

    with pytest.raises(ValidationError):
        ToolObservation(
            step_number=1,
            step_tool_call_number=1,
            total_tool_call_number=1,
            action=complete_action,
            result=ToolResult.ok(data={}),
        )


def test_tool_observation_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ToolObservation(
            step_number=1,
            step_tool_call_number=1,
            total_tool_call_number=1,
            action=create_tool_action(),
            result=ToolResult.ok(data={}),
            unexpected=True,
        )


def test_tool_observation_is_frozen() -> None:
    observation = create_observation()

    with pytest.raises(ValidationError):
        observation.step_number = 2


def test_graph_accumulates_observations_and_replaces_execution_state() -> None:
    first_action = create_tool_action("web_search")
    second_action = create_tool_action("web_fetch")

    first_observation = create_observation(
        step_tool_call_number=1,
        total_tool_call_number=1,
        tool_name="web_search",
    )
    second_observation = create_observation(
        step_tool_call_number=2,
        total_tool_call_number=2,
        tool_name="web_fetch",
    )

    complete_action = CompleteStepAction(
        type="complete_step",
        summary="Collected enough evidence for this step.",
    )

    def first_node(_: AgentState) -> dict[str, object]:
        return {
            "pending_action": first_action,
            "tool_observations": [first_observation],
            "tool_calls_in_current_step": 1,
            "total_tool_calls": 1,
        }

    def second_node(_: AgentState) -> dict[str, object]:
        return {
            "pending_action": second_action,
            "tool_observations": [second_observation],
            "tool_calls_in_current_step": 2,
            "total_tool_calls": 2,
        }

    def complete_node(_: AgentState) -> dict[str, object]:
        return {
            "pending_action": complete_action,
        }

    builder = StateGraph(AgentState)
    builder.add_node("first", first_node)
    builder.add_node("second", second_node)
    builder.add_node("complete", complete_node)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", "complete")
    builder.add_edge("complete", END)

    graph = builder.compile()
    result = graph.invoke(create_initial_state("Research execution state"))

    assert result["tool_observations"] == [
        first_observation,
        second_observation,
    ]
    assert result["pending_action"] == complete_action
    assert result["tool_calls_in_current_step"] == 2
    assert result["total_tool_calls"] == 2
