import math
from typing import Annotated, Protocol, cast, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
)

from mini_deerflow.actions import (
    AgentAction,
    CompletionSummary,
    ToolObservation,
)
from mini_deerflow.context_budget import (
    ContextBudget,
    ProjectionMetadata,
    fit_context_to_budget,
    merge_metadata,
    project_evidence_records,
    project_observations,
    project_summaries,
)
from mini_deerflow.conversation import ConversationContext
from mini_deerflow.evidence import EvidenceRecord
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
    conversation_context: ConversationContext | None = None
    step: PlanStep
    completed_step_summaries: list[CompletionSummary] = Field(
        default_factory=list,
        max_length=7,
    )
    available_tools: list[dict[str, JsonValue]] = Field(
        max_length=50,
    )
    observations: list[ToolObservation] = Field(
        default_factory=list,
    )
    evidence: list[EvidenceRecord] = Field(
        default_factory=list,
        max_length=50,
    )
    remaining_step_tool_calls: int = Field(
        ge=0,
    )
    allocated_step_tool_calls: int = Field(default=0, ge=0)
    remaining_allocated_step_tool_calls: int = Field(default=0, ge=0)
    remaining_total_tool_calls: int = Field(
        ge=0,
    )
    context_projection: ProjectionMetadata | None = None


@runtime_checkable
class ActionSelector(Protocol):
    async def select_action(
        self,
        context: ActionContext,
    ) -> AgentAction:
        """Select the next structured action."""


class StepToolCallBudget(BaseModel):
    """Deterministic fair-share quota for the currently active plan step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allocated_step_tool_calls: int = Field(ge=0)
    remaining_allocated_step_tool_calls: int = Field(ge=0)
    remaining_step_tool_calls: int = Field(ge=0)
    remaining_total_tool_calls: int = Field(ge=0)


def calculate_step_tool_call_budget(
    state: AgentState,
    *,
    max_tool_calls_per_step: int,
    max_total_tool_calls: int,
) -> StepToolCallBudget:
    """Allocate the remaining global budget fairly across remaining steps."""

    _validate_positive_limit("max_tool_calls_per_step", max_tool_calls_per_step)
    _validate_positive_limit("max_total_tool_calls", max_total_tool_calls)

    plan = state["plan"]
    if plan is None:
        raise RuntimeError("step tool budget requires a plan")

    current_step = state["current_step"]
    if current_step < 0 or current_step >= len(plan.steps):
        raise RuntimeError("current_step is outside the plan")

    step_tool_calls = state["tool_calls_in_current_step"]
    total_tool_calls = state["total_tool_calls"]
    if step_tool_calls < 0 or total_tool_calls < 0:
        raise RuntimeError("tool call counters cannot be negative")
    if step_tool_calls > total_tool_calls:
        raise RuntimeError("step tool calls cannot exceed total tool calls")

    remaining_steps = len(plan.steps) - current_step
    remaining_total = max(0, max_total_tool_calls - total_tool_calls)
    distributable_for_current_step = remaining_total + step_tool_calls
    allocated = min(
        max_tool_calls_per_step,
        math.ceil(distributable_for_current_step / remaining_steps),
    )
    remaining_hard_step = max(0, max_tool_calls_per_step - step_tool_calls)
    remaining_allocated = min(
        remaining_total,
        remaining_hard_step,
        max(0, allocated - step_tool_calls),
    )

    return StepToolCallBudget(
        allocated_step_tool_calls=allocated,
        remaining_allocated_step_tool_calls=remaining_allocated,
        remaining_step_tool_calls=remaining_hard_step,
        remaining_total_tool_calls=remaining_total,
    )


def build_action_context(
    state: AgentState,
    registry: ToolRegistry,
    *,
    max_tool_calls_per_step: int,
    max_total_tool_calls: int,
    context_budget: ContextBudget | None = None,
) -> ActionContext:
    _validate_positive_limit(
        "max_tool_calls_per_step",
        max_tool_calls_per_step,
    )
    _validate_positive_limit(
        "max_total_tool_calls",
        max_total_tool_calls,
    )

    resolved_budget = context_budget if context_budget is not None else ContextBudget()

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

    tool_budget = calculate_step_tool_call_budget(
        state,
        max_tool_calls_per_step=max_tool_calls_per_step,
        max_total_tool_calls=max_total_tool_calls,
    )

    # LLM-facing projection only: caller-owned state keeps the complete
    # evidence, observations, and summaries for checkpoint and rendering.
    projected_evidence, evidence_metadata = project_evidence_records(
        list(state.get("evidence", [])),
        resolved_budget,
    )
    projected_observations, observation_metadata = project_observations(
        step_observations,
        resolved_budget,
    )
    projected_summaries, summary_metadata = project_summaries(
        list(state["notes"]),
        resolved_budget,
    )
    projected_conversation, conversation_metadata = _project_conversation_context(
        state.get("conversation_context"),
        max_characters=resolved_budget.max_total_chars // 3,
    )

    context = ActionContext(
        goal=state["goal"],
        conversation_context=projected_conversation,
        step=step,
        completed_step_summaries=projected_summaries,
        available_tools=registry.definitions(),
        observations=projected_observations,
        evidence=projected_evidence,
        remaining_step_tool_calls=tool_budget.remaining_step_tool_calls,
        allocated_step_tool_calls=tool_budget.allocated_step_tool_calls,
        remaining_allocated_step_tool_calls=(
            tool_budget.remaining_allocated_step_tool_calls
        ),
        remaining_total_tool_calls=tool_budget.remaining_total_tool_calls,
    )

    return cast(
        ActionContext,
        fit_context_to_budget(
            context,
            resolved_budget,
            merge_metadata(
                evidence_metadata,
                observation_metadata,
                summary_metadata,
                conversation_metadata,
            ),
        ),
    )


def _project_conversation_context(
    context: ConversationContext | None,
    *,
    max_characters: int,
) -> tuple[ConversationContext | None, ProjectionMetadata]:
    """Keep newest complete turn pairs within the action-context share."""

    if context is None:
        return None, ProjectionMetadata()
    messages = list(context.messages)
    omitted_turns = context.omitted_turns
    newly_omitted = 0
    while (
        messages and sum(len(message.content) for message in messages) > max_characters
    ):
        oldest_turn_id = messages[0].turn_id
        messages = [
            message for message in messages if message.turn_id != oldest_turn_id
        ]
        omitted_turns += 1
        newly_omitted += 1
    projected = ConversationContext(
        messages=tuple(messages),
        omitted_turns=omitted_turns,
        estimated_characters=sum(len(message.content) for message in messages),
    )
    return projected, ProjectionMetadata(
        omitted_items=context.omitted_turns + newly_omitted,
    )


def _validate_positive_limit(
    name: str,
    value: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"{name} must be a positive integer",
        )
