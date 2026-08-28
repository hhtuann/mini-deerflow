from typing import Annotated, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
)

from mini_deerflow.actions import AgentAction, ToolObservation
from mini_deerflow.schemas import PlanStep
from mini_deerflow.state import AgentState
from mini_deerflow.tools import ToolRegistry

ResearchGoal = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=1_000,
    ),
]


class ActionContext(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    goal: ResearchGoal
    step: PlanStep
    available_tools: list[dict[str, JsonValue]] = Field(
        max_length=50,
    )
    observations: list[ToolObservation] = Field(
        default_factory=list,
    )
    remaining_step_tool_calls: int = Field(
        ge=0,
    )
    remaining_total_tool_calls: int = Field(
        ge=0,
    )


@runtime_checkable
class ActionSelector(Protocol):
    async def select_action(
        self,
        context: ActionContext,
    ) -> AgentAction:
        """Select the next structured action."""


def build_action_context(
    state: AgentState,
    registry: ToolRegistry,
    *,
    max_tool_calls_per_step: int,
    max_total_tool_calls: int,
) -> ActionContext:
    _validate_positive_limit(
        "max_tool_calls_per_step",
        max_tool_calls_per_step,
    )
    _validate_positive_limit(
        "max_total_tool_calls",
        max_total_tool_calls,
    )

    def _validate_execution_counters(
        step_tool_calls: int,
        total_tool_calls: int,
    ) -> None:
        if step_tool_calls < 0 or total_tool_calls < 0:
            raise RuntimeError(
                "tool call counters cannot be negative",
            )

        if step_tool_calls > total_tool_calls:
            raise RuntimeError(
                "step tool calls cannot exceed total tool calls",
            )

    plan = state["plan"]

    if plan is None:
        raise RuntimeError(
            "action context requires a plan",
        )

    current_step = state["current_step"]

    if current_step < 0 or current_step >= len(plan.steps):
        raise RuntimeError(
            "current_step is outside the plan",
        )

    step = plan.steps[current_step]

    step_observations = [
        observation
        for observation in state["tool_observations"]
        if observation.step_number == step.step_number
    ]

    step_tool_calls = state["tool_calls_in_current_step"]
    total_tool_calls = state["total_tool_calls"]

    _validate_execution_counters(
        step_tool_calls,
        total_tool_calls,
    )

    remaining_step_tool_calls = max(
        0,
        max_tool_calls_per_step - step_tool_calls,
    )
    remaining_total_tool_calls = max(
        0,
        max_total_tool_calls - total_tool_calls,
    )

    return ActionContext(
        goal=state["goal"],
        step=step,
        available_tools=registry.definitions(),
        observations=step_observations,
        remaining_step_tool_calls=remaining_step_tool_calls,
        remaining_total_tool_calls=remaining_total_tool_calls,
    )


def _validate_positive_limit(
    name: str,
    value: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"{name} must be a positive integer",
        )
