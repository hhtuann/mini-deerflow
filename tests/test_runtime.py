import asyncio

import pytest

from mini_deerflow.actions import CompleteStepAction
from mini_deerflow.decision import ActionContext
from mini_deerflow.runtime import (
    AgentRuntime,
    RuntimeLimits,
    build_agent_runtime,
)
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import AgentState
from mini_deerflow.tools import ToolRegistry

_USE_INPUT_STATE = object()


class FakeGraph:
    def __init__(
        self,
        response: object = _USE_INPUT_STATE,
    ) -> None:
        self.response = response
        self.received_state: AgentState | None = None
        self.received_config: dict[str, object] | None = None

    async def ainvoke(
        self,
        state: AgentState,
        *,
        config: dict[str, object],
    ) -> object:
        self.received_state = state
        self.received_config = config

        if self.response is _USE_INPUT_STATE:
            return state

        return self.response


class CompletingSelector:
    def __init__(self) -> None:
        self.contexts: list[ActionContext] = []

    async def select_action(
        self,
        context: ActionContext,
    ) -> CompleteStepAction:
        self.contexts.append(context)

        return CompleteStepAction(
            type="complete_step",
            summary=(f"Completed runtime step {context.step.step_number}."),
            sources=[],
        )


def create_plan(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=1,
                title="Prepare research",
                objective="Prepare the controlled research process.",
                success_criteria="Research preparation is complete.",
            ),
            PlanStep(
                step_number=2,
                title="Review evidence",
                objective="Review the available controlled evidence.",
                success_criteria="Available evidence is reviewed.",
            ),
            PlanStep(
                step_number=3,
                title="Finish research",
                objective="Finish the controlled research workflow.",
                success_criteria="The research workflow is complete.",
            ),
        ],
    )


def test_runtime_limits_have_safe_defaults() -> None:
    limits = RuntimeLimits()

    assert limits.max_tool_calls_per_step == 5
    assert limits.max_total_tool_calls == 20
    assert limits.recursion_limit == 100


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("max_tool_calls_per_step", True),
        ("max_total_tool_calls", 1.5),
        ("recursion_limit", "100"),
    ],
)
def test_runtime_limits_reject_invalid_types(
    field_name: str,
    invalid_value: object,
) -> None:
    values = {
        "max_tool_calls_per_step": 5,
        "max_total_tool_calls": 20,
        "recursion_limit": 100,
    }
    values[field_name] = invalid_value

    with pytest.raises(TypeError, match="must be an integer"):
        RuntimeLimits(**values)


@pytest.mark.parametrize(
    "field_name",
    [
        "max_tool_calls_per_step",
        "max_total_tool_calls",
        "recursion_limit",
    ],
)
def test_runtime_limits_reject_non_positive_values(
    field_name: str,
) -> None:
    values = {
        "max_tool_calls_per_step": 5,
        "max_total_tool_calls": 20,
        "recursion_limit": 100,
    }
    values[field_name] = 0

    with pytest.raises(ValueError, match="greater than zero"):
        RuntimeLimits(**values)


def test_agent_runtime_invokes_graph_with_initial_state() -> None:
    graph = FakeGraph()
    runtime = AgentRuntime(
        graph=graph,
        limits=RuntimeLimits(recursion_limit=37),
    )

    result = asyncio.run(runtime.run("  Research runtime composition  "))

    assert result["goal"] == "Research runtime composition"
    assert graph.received_state is not None
    assert graph.received_state["plan"] is None
    assert graph.received_config == {
        "recursion_limit": 37,
    }


def test_agent_runtime_rejects_empty_goal_before_graph_call() -> None:
    graph = FakeGraph()
    runtime = AgentRuntime(graph=graph)

    with pytest.raises(ValueError, match="goal must not be empty"):
        asyncio.run(runtime.run("   "))

    assert graph.received_state is None


def test_agent_runtime_rejects_invalid_graph_result() -> None:
    runtime = AgentRuntime(
        graph=FakeGraph(response=["not", "a", "state"]),
    )

    with pytest.raises(
        TypeError,
        match="state dictionary",
    ):
        asyncio.run(runtime.run("Research invalid graph output"))


def test_build_agent_runtime_composes_bounded_workflow() -> None:
    selector = CompletingSelector()
    limits = RuntimeLimits(
        max_tool_calls_per_step=2,
        max_total_tool_calls=3,
        recursion_limit=30,
    )

    runtime = build_agent_runtime(
        create_plan,
        selector,
        ToolRegistry(),
        limits=limits,
    )

    result = asyncio.run(runtime.run("Research runtime composition"))

    assert result["current_step"] == 3
    assert result["total_tool_calls"] == 0
    assert len(selector.contexts) == 3
    assert selector.contexts[0].remaining_step_tool_calls == 2
    assert selector.contexts[0].remaining_total_tool_calls == 3
    assert runtime.limits is limits
    assert result["final_answer"] is not None
