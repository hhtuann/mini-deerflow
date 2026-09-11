import logging
from collections.abc import Callable
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from mini_deerflow.actions import (
    CompleteStepAction,
    ToolCallAction,
    ToolObservation,
)
from mini_deerflow.decision import (
    ActionSelector,
    build_action_context,
)
from mini_deerflow.schemas import Plan
from mini_deerflow.state import AgentState
from mini_deerflow.tools import ToolRegistry, ToolRunner

logger = logging.getLogger(__name__)

Planner = Callable[[str], Plan]

DecisionRoute = Literal[
    "execute_tool",
    "complete_step",
    "budget_exhausted",
]

CompletionRoute = Literal[
    "decide_action",
    "synthesize",
]


def build_agent_workflow(
    planner: Planner,
    action_selector: ActionSelector,
    registry: ToolRegistry,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    max_tool_calls_per_step: int = 5,
    max_total_tool_calls: int = 20,
) -> CompiledStateGraph:
    """Build the bounded plan-act-observe research workflow."""

    _validate_workflow_limit(
        "max_tool_calls_per_step",
        max_tool_calls_per_step,
    )
    _validate_workflow_limit(
        "max_total_tool_calls",
        max_total_tool_calls,
    )

    if not isinstance(action_selector, ActionSelector):
        raise TypeError(
            "action_selector must satisfy ActionSelector",
        )

    tool_runner = ToolRunner(registry)

    def planner_node(
        state: AgentState,
    ) -> dict[str, object]:
        logger.info(
            "Planning research goal: %s",
            state["goal"],
        )

        plan = planner(state["goal"])

        return {
            "plan": plan,
            "current_step": 0,
            "pending_action": None,
            "tool_calls_in_current_step": 0,
        }

    async def decide_action_node(
        state: AgentState,
    ) -> dict[str, object]:
        context = build_action_context(
            state,
            registry,
            max_tool_calls_per_step=max_tool_calls_per_step,
            max_total_tool_calls=max_total_tool_calls,
        )

        logger.info(
            "Selecting action for plan step %s",
            context.step.step_number,
        )

        action = await action_selector.select_action(
            context,
        )

        if not isinstance(
            action,
            ToolCallAction | CompleteStepAction,
        ):
            raise TypeError(
                "action selector returned an invalid action",
            )

        return {
            "pending_action": action,
        }

    def route_after_decision(
        state: AgentState,
    ) -> DecisionRoute:
        action = state["pending_action"]

        if action is None:
            raise RuntimeError(
                "decision routing requires a pending action",
            )

        if isinstance(action, CompleteStepAction):
            return "complete_step"

        if not isinstance(action, ToolCallAction):
            raise TypeError(
                "pending action has an invalid type",
            )

        step_budget_exhausted = (
            state["tool_calls_in_current_step"] >= max_tool_calls_per_step
        )
        total_budget_exhausted = state["total_tool_calls"] >= max_total_tool_calls

        if step_budget_exhausted or total_budget_exhausted:
            return "budget_exhausted"

        return "execute_tool"

    async def execute_tool_node(
        state: AgentState,
    ) -> dict[str, object]:
        plan = state["plan"]
        action = state["pending_action"]

        if plan is None:
            raise RuntimeError(
                "tool execution requires a plan",
            )

        if not isinstance(action, ToolCallAction):
            raise TypeError(
                "tool execution requires a tool call action",
            )

        current_step = state["current_step"]

        if current_step < 0 or current_step >= len(plan.steps):
            raise RuntimeError(
                "current_step is outside the plan",
            )

        step = plan.steps[current_step]
        step_tool_call_number = state["tool_calls_in_current_step"] + 1
        total_tool_call_number = state["total_tool_calls"] + 1

        logger.info(
            "Executing tool %s for plan step %s",
            action.tool_name,
            step.step_number,
        )

        result = await tool_runner.run(
            action.tool_name,
            action.arguments,
        )

        observation = ToolObservation(
            step_number=step.step_number,
            step_tool_call_number=step_tool_call_number,
            total_tool_call_number=total_tool_call_number,
            action=action,
            result=result,
        )

        return {
            "pending_action": None,
            "tool_observations": [observation],
            "tool_calls_in_current_step": (step_tool_call_number),
            "total_tool_calls": total_tool_call_number,
        }

    def complete_step_node(
        state: AgentState,
    ) -> dict[str, object]:
        plan = state["plan"]
        action = state["pending_action"]

        if plan is None:
            raise RuntimeError(
                "step completion requires a plan",
            )

        if not isinstance(action, CompleteStepAction):
            raise TypeError(
                "step completion requires a complete action",
            )

        current_step = state["current_step"]

        if current_step < 0 or current_step >= len(plan.steps):
            raise RuntimeError(
                "current_step is outside the plan",
            )

        step = plan.steps[current_step]

        logger.info(
            "Completing plan step %s: %s",
            step.step_number,
            step.title,
        )

        return {
            "pending_action": None,
            "current_step": current_step + 1,
            "tool_calls_in_current_step": 0,
            "notes": [action.summary],
            "sources": [str(source) for source in action.sources],
        }

    def route_after_completion(
        state: AgentState,
    ) -> CompletionRoute:
        plan = state["plan"]

        if plan is None:
            raise RuntimeError(
                "completion routing requires a plan",
            )

        current_step = state["current_step"]

        if current_step > len(plan.steps):
            raise RuntimeError(
                "current_step moved beyond the plan",
            )

        if current_step < len(plan.steps):
            return "decide_action"

        return "synthesize"

    def budget_exhausted_node(
        state: AgentState,
    ) -> dict[str, object]:
        plan = state["plan"]

        if plan is None:
            raise RuntimeError(
                "budget handling requires a plan",
            )

        current_step = state["current_step"]

        if current_step < 0 or current_step >= len(plan.steps):
            raise RuntimeError(
                "current_step is outside the plan",
            )

        step = plan.steps[current_step]
        exhausted_limits: list[str] = []

        if state["tool_calls_in_current_step"] >= max_tool_calls_per_step:
            exhausted_limits.append("per-step limit")

        if state["total_tool_calls"] >= max_total_tool_calls:
            exhausted_limits.append("total-run limit")

        rendered_limits = ", ".join(exhausted_limits)

        error = (
            f"Tool-call budget exhausted for plan step "
            f"{step.step_number}: {rendered_limits}."
        )

        logger.warning(error)

        return {
            "pending_action": None,
            "errors": [error],
        }

    def synthesize_node(
        state: AgentState,
    ) -> dict[str, object]:
        completed_notes = state["notes"]
        unique_sources = list(dict.fromkeys(state["sources"]))
        errors = state["errors"]
        observations = state["tool_observations"]

        successful_calls = sum(
            observation.result.success for observation in observations
        )
        failed_calls = len(observations) - successful_calls

        note_lines = [f"- {note}" for note in completed_notes] or [
            "- No plan step was completed."
        ]
        source_lines = [f"- {source}" for source in unique_sources] or [
            "- No sources were recorded."
        ]
        error_lines = [f"- {error}" for error in errors] or [
            "- No execution errors were recorded."
        ]

        final_answer = "\n".join(
            [
                f"Research goal: {state['goal']}",
                "",
                "Completed step summaries:",
                *note_lines,
                "",
                "Sources:",
                *source_lines,
                "",
                "Execution:",
                (f"- Tool calls: {state['total_tool_calls']}"),
                f"- Successful tool calls: {successful_calls}",
                f"- Failed tool calls: {failed_calls}",
                "",
                "Errors:",
                *error_lines,
            ]
        )

        return {
            "final_answer": final_answer,
        }

    builder = StateGraph(AgentState)

    builder.add_node("planner", planner_node)
    builder.add_node("decide_action", decide_action_node)
    builder.add_node("execute_tool", execute_tool_node)
    builder.add_node("complete_step", complete_step_node)
    builder.add_node(
        "budget_exhausted",
        budget_exhausted_node,
    )
    builder.add_node("synthesize", synthesize_node)

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "decide_action")

    builder.add_conditional_edges(
        "decide_action",
        route_after_decision,
        {
            "execute_tool": "execute_tool",
            "complete_step": "complete_step",
            "budget_exhausted": "budget_exhausted",
        },
    )

    builder.add_edge("execute_tool", "decide_action")

    builder.add_conditional_edges(
        "complete_step",
        route_after_completion,
        {
            "decide_action": "decide_action",
            "synthesize": "synthesize",
        },
    )

    builder.add_edge(
        "budget_exhausted",
        "synthesize",
    )
    builder.add_edge("synthesize", END)

    return builder.compile(
        checkpointer=checkpointer,
    )


def _validate_workflow_limit(
    name: str,
    value: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"{name} must be a positive integer",
        )
