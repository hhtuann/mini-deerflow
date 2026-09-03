import pytest
from pydantic import ValidationError

from mini_deerflow.actions import (
    CompleteStepAction,
    ToolCallAction,
    ToolObservation,
)
from mini_deerflow.decision import (
    ActionContext,
    ActionSelector,
    build_action_context,
)
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import AgentState, create_initial_state
from mini_deerflow.tools import (
    ToolInput,
    ToolRegistry,
    ToolResult,
)


class SearchInput(ToolInput):
    query: str


class SearchTool:
    name = "web_search"
    description = "Search the public web."
    input_model = SearchInput
    timeout_seconds = 5.0
    idempotent = True

    async def run(self, tool_input: ToolInput) -> ToolResult:
        return ToolResult.ok(
            data=tool_input.model_dump(),
        )


class CompleteSelector:
    async def select_action(
        self,
        context: ActionContext,
    ) -> CompleteStepAction:
        return CompleteStepAction(
            type="complete_step",
            summary="Completed the current research step.",
        )


def create_step(number: int) -> PlanStep:
    return PlanStep(
        step_number=number,
        title=f"Research step {number}",
        objective=f"Collect evidence for research step {number}.",
        success_criteria=f"Evidence for step {number} is collected.",
    )


def create_plan() -> Plan:
    return Plan(
        goal="Compare LangGraph and CrewAI",
        steps=[
            create_step(1),
            create_step(2),
            create_step(3),
        ],
    )


def create_planned_state() -> AgentState:
    state = create_initial_state("Compare LangGraph and CrewAI")
    state["plan"] = create_plan()
    return state


def create_observation(
    *,
    step_number: int,
    step_tool_call_number: int,
    total_tool_call_number: int,
) -> ToolObservation:
    return ToolObservation(
        step_number=step_number,
        step_tool_call_number=step_tool_call_number,
        total_tool_call_number=total_tool_call_number,
        action=ToolCallAction(
            type="tool_call",
            tool_name="web_search",
            arguments={
                "query": f"Research step {step_number}",
            },
        ),
        result=ToolResult.ok(
            data={
                "items": [],
            }
        ),
    )


def test_selector_satisfies_runtime_protocol() -> None:
    assert isinstance(
        CompleteSelector(),
        ActionSelector,
    )


def test_build_context_selects_current_step_and_observations() -> None:
    state = create_planned_state()
    state["tool_calls_in_current_step"] = 1
    state["total_tool_calls"] = 2
    state["tool_observations"] = [
        create_observation(
            step_number=1,
            step_tool_call_number=1,
            total_tool_call_number=1,
        ),
        create_observation(
            step_number=2,
            step_tool_call_number=1,
            total_tool_call_number=2,
        ),
    ]

    context = build_action_context(
        state,
        ToolRegistry([SearchTool()]),
        max_tool_calls_per_step=5,
        max_total_tool_calls=20,
    )

    assert context.goal == "Compare LangGraph and CrewAI"
    assert context.step.step_number == 1
    assert len(context.observations) == 1
    assert context.observations[0].step_number == 1
    assert context.remaining_step_tool_calls == 4
    assert context.remaining_total_tool_calls == 18

    assert len(context.available_tools) == 1
    assert context.available_tools[0]["name"] == "web_search"
    assert context.available_tools[0]["input_schema"] == SearchInput.model_json_schema()


def test_build_context_uses_current_step_index() -> None:
    state = create_planned_state()
    state["current_step"] = 1

    context = build_action_context(
        state,
        ToolRegistry(),
        max_tool_calls_per_step=5,
        max_total_tool_calls=20,
    )

    assert context.step.step_number == 2


def test_build_context_includes_completed_step_summaries() -> None:
    state = create_planned_state()
    state["current_step"] = 1
    state["notes"] = [
        "Completed step one after finding evidence.txt.",
    ]

    context = build_action_context(
        state,
        ToolRegistry(),
        max_tool_calls_per_step=5,
        max_total_tool_calls=20,
    )

    assert context.completed_step_summaries == [
        "Completed step one after finding evidence.txt.",
    ]

    state["notes"].append(
        "This later mutation must not change the context.",
    )

    assert context.completed_step_summaries == [
        "Completed step one after finding evidence.txt.",
    ]


def test_build_context_requires_plan() -> None:
    state = create_initial_state("Research LangGraph")

    with pytest.raises(
        RuntimeError,
        match="requires a plan",
    ):
        build_action_context(
            state,
            ToolRegistry(),
            max_tool_calls_per_step=5,
            max_total_tool_calls=20,
        )


@pytest.mark.parametrize("invalid_step", [-1, 3])
def test_build_context_rejects_invalid_current_step(
    invalid_step: int,
) -> None:
    state = create_planned_state()
    state["current_step"] = invalid_step

    with pytest.raises(
        RuntimeError,
        match="current_step is outside the plan",
    ):
        build_action_context(
            state,
            ToolRegistry(),
            max_tool_calls_per_step=5,
            max_total_tool_calls=20,
        )


@pytest.mark.parametrize(
    (
        "step_tool_calls",
        "total_tool_calls",
    ),
    [
        (-1, 0),
        (0, -1),
    ],
)
def test_build_context_rejects_negative_counters(
    step_tool_calls: int,
    total_tool_calls: int,
) -> None:
    state = create_planned_state()
    state["tool_calls_in_current_step"] = step_tool_calls
    state["total_tool_calls"] = total_tool_calls

    with pytest.raises(
        RuntimeError,
        match="counters cannot be negative",
    ):
        build_action_context(
            state,
            ToolRegistry(),
            max_tool_calls_per_step=5,
            max_total_tool_calls=20,
        )


def test_step_counter_cannot_exceed_total_counter() -> None:
    state = create_planned_state()
    state["tool_calls_in_current_step"] = 3
    state["total_tool_calls"] = 2

    with pytest.raises(
        RuntimeError,
        match="step tool calls cannot exceed",
    ):
        build_action_context(
            state,
            ToolRegistry(),
            max_tool_calls_per_step=5,
            max_total_tool_calls=20,
        )


def test_remaining_budgets_are_clamped_to_zero() -> None:
    state = create_planned_state()
    state["tool_calls_in_current_step"] = 7
    state["total_tool_calls"] = 8

    context = build_action_context(
        state,
        ToolRegistry(),
        max_tool_calls_per_step=5,
        max_total_tool_calls=6,
    )

    assert context.remaining_step_tool_calls == 0
    assert context.remaining_total_tool_calls == 0


@pytest.mark.parametrize(
    "invalid_limit",
    [
        0,
        -1,
        True,
        1.5,
    ],
)
def test_invalid_step_limit_is_rejected(
    invalid_limit: object,
) -> None:
    state = create_planned_state()

    with pytest.raises(
        ValueError,
        match="max_tool_calls_per_step",
    ):
        build_action_context(
            state,
            ToolRegistry(),
            max_tool_calls_per_step=invalid_limit,
            max_total_tool_calls=20,
        )


@pytest.mark.parametrize(
    "invalid_limit",
    [
        0,
        -1,
        True,
        1.5,
    ],
)
def test_invalid_total_limit_is_rejected(
    invalid_limit: object,
) -> None:
    state = create_planned_state()

    with pytest.raises(
        ValueError,
        match="max_total_tool_calls",
    ):
        build_action_context(
            state,
            ToolRegistry(),
            max_tool_calls_per_step=5,
            max_total_tool_calls=invalid_limit,
        )


def test_action_context_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ActionContext(
            goal="Compare LangGraph and CrewAI",
            step=create_step(1),
            available_tools=[],
            remaining_step_tool_calls=5,
            remaining_total_tool_calls=20,
            unexpected=True,
        )


def test_action_context_is_frozen() -> None:
    context = ActionContext(
        goal="Compare LangGraph and CrewAI",
        step=create_step(1),
        available_tools=[],
        remaining_step_tool_calls=5,
        remaining_total_tool_calls=20,
    )

    with pytest.raises(ValidationError):
        context.remaining_step_tool_calls = 4
