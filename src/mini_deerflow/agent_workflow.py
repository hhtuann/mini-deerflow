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
from mini_deerflow.evidence import (
    StepFinding,
    extract_evidence_records,
    render_research_report,
    sanitize_finding_summary,
    validate_citations,
)
from mini_deerflow.review import (
    EvidenceReviewer,
    ReplacementWork,
    Replanner,
    ReplanRecord,
    ReplanRequest,
    ReviewContext,
    ReviewRoute,
    ReviewVerdict,
    derive_review_limitations,
    merge_replanned_steps,
    render_review_conclusions,
    replacement_step_bounds,
)
from mini_deerflow.schemas import Plan
from mini_deerflow.state import AgentState
from mini_deerflow.tools import ToolRegistry, ToolRunner
from mini_deerflow.tools.contracts import ToolResult

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
    action_registry: ToolRegistry | None = None,
    reviewer: EvidenceReviewer | None = None,
    replanner: Replanner | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    max_tool_calls_per_step: int = 5,
    max_total_tool_calls: int = 20,
    max_replan_cycles: int = 2,
    artifact_path: str | None = None,
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
    _validate_workflow_limit(
        "max_replan_cycles",
        max_replan_cycles,
    )

    if not isinstance(action_selector, ActionSelector):
        raise TypeError(
            "action_selector must satisfy ActionSelector",
        )

    if (reviewer is None) != (replanner is None):
        raise ValueError(
            "reviewer and replanner must be configured together",
        )

    if reviewer is not None and not isinstance(reviewer, EvidenceReviewer):
        raise TypeError(
            "reviewer must satisfy EvidenceReviewer",
        )

    if replanner is not None and not callable(replanner):
        raise TypeError(
            "replanner must be callable",
        )

    action_registry_is_restricted = action_registry is not None
    resolved_action_registry = registry if action_registry is None else action_registry

    if not isinstance(resolved_action_registry, ToolRegistry):
        raise TypeError("action_registry must be ToolRegistry")

    if any(name not in registry for name in resolved_action_registry.names()):
        raise ValueError("action_registry tools must be registered for execution")

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
            resolved_action_registry,
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
            # Entering decide_action consumes a routed "continue" review
            # verdict; durable history stays in review_verdicts. The
            # write only lands when this node succeeds, so an interrupt
            # keeps the pending verdict resumable in the checkpoint.
            "pending_review_verdict": None,
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

        if (
            action_registry_is_restricted
            and action.tool_name not in resolved_action_registry
        ):
            result = ToolResult.fail(
                error="Tool is not available for research actions.",
                metadata={
                    "tool_name": action.tool_name,
                    "error_type": "ActionToolDeniedError",
                },
            )
        else:
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
        evidence = extract_evidence_records(observation)

        return {
            "pending_action": None,
            "tool_observations": [observation],
            "evidence": evidence,
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

        accepted_sources, rejected_count = validate_citations(
            action.sources,
            list(state.get("evidence", [])),
        )
        sanitized_summary = sanitize_finding_summary(action.summary)
        errors: list[str] = []

        if rejected_count:
            errors.append(
                f"Step {step.step_number} rejected {rejected_count} "
                "citation(s) absent from successful web evidence."
            )

        return {
            "pending_action": None,
            "current_step": current_step + 1,
            "tool_calls_in_current_step": 0,
            "notes": [sanitized_summary],
            "findings": [
                StepFinding(
                    step_number=step.step_number,
                    summary=sanitized_summary,
                    citations=accepted_sources,
                )
            ],
            "sources": accepted_sources,
            "errors": errors,
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

    async def review_node(
        state: AgentState,
    ) -> dict[str, object]:
        plan = state["plan"]

        if plan is None:
            raise RuntimeError(
                "evidence review requires a plan",
            )

        current_step = state["current_step"]

        if current_step < 0 or current_step > len(plan.steps):
            raise RuntimeError(
                "current_step is outside the plan",
            )

        assert reviewer is not None

        remaining_steps = plan.steps[current_step:]
        remaining_total_tool_calls = max(
            0,
            max_total_tool_calls - state["total_tool_calls"],
        )
        remaining_replan_cycles = max(
            0,
            max_replan_cycles - len(state["replans"]),
        )

        context = ReviewContext(
            goal=state["goal"],
            remaining_steps=remaining_steps,
            completed_step_summaries=list(state["notes"]),
            findings=list(state.get("findings", [])),
            evidence=list(state.get("evidence", [])),
            limitations=derive_review_limitations(
                state["tool_observations"],
                list(state["errors"]),
            ),
            remaining_total_tool_calls=remaining_total_tool_calls,
            remaining_replan_cycles=remaining_replan_cycles,
        )

        logger.info(
            "Reviewing evidence quality after plan step %s",
            current_step,
        )

        verdict = await reviewer.review_evidence(
            context,
        )

        if not isinstance(verdict, ReviewVerdict):
            raise TypeError(
                "evidence reviewer returned an invalid verdict",
            )

        review_number = len(state["review_verdicts"]) + 1
        route = verdict.verdict
        coercion_errors: list[str] = []

        if route == "replan":
            replan_budget_exhausted = len(state["replans"]) >= max_replan_cycles
            tool_budget_exhausted = state["total_tool_calls"] >= max_total_tool_calls

            if replan_budget_exhausted:
                route = "finish"
                coercion_errors.append(
                    f"Review {review_number} requested another replan "
                    "after the replan budget was exhausted; finishing "
                    "with the available evidence."
                )
            elif tool_budget_exhausted:
                route = "finish"
                coercion_errors.append(
                    f"Review {review_number} requested a replan after "
                    "the tool-call budget was exhausted; finishing with "
                    "the available evidence."
                )
            else:
                try:
                    replacement_step_bounds(current_step)
                except ValueError:
                    route = "finish"
                    coercion_errors.append(
                        f"Review {review_number} requested a replan "
                        "that cannot fit within the seven-step plan "
                        "limit; finishing with the available evidence."
                    )
        elif route == "continue" and current_step >= len(plan.steps):
            route = "finish"
            coercion_errors.append(
                f"Review {review_number} requested continuing a plan "
                "with no remaining steps; finishing with the available "
                "evidence."
            )

        return {
            "review_verdicts": [verdict],
            "pending_review_verdict": route,
            "errors": coercion_errors,
        }

    def route_after_review(
        state: AgentState,
    ) -> ReviewRoute:
        route = state["pending_review_verdict"]

        if route is None:
            raise RuntimeError(
                "review routing requires a pending review verdict",
            )

        if route not in ("continue", "replan", "finish"):
            raise RuntimeError(
                "review routing received an invalid verdict",
            )

        return route

    def replan_node(
        state: AgentState,
    ) -> dict[str, object]:
        plan = state["plan"]

        if plan is None:
            raise RuntimeError(
                "replanning requires a plan",
            )

        if state["pending_review_verdict"] != "replan":
            raise RuntimeError(
                "replanning requires a replan verdict",
            )

        current_step = state["current_step"]

        if current_step < 0 or current_step > len(plan.steps):
            raise RuntimeError(
                "current_step is outside the plan",
            )

        if not state["review_verdicts"]:
            raise RuntimeError(
                "replanning requires a triggering review verdict",
            )

        assert replanner is not None

        verdict = state["review_verdicts"][-1]
        minimum, maximum = replacement_step_bounds(current_step)

        request = ReplanRequest(
            goal=state["goal"],
            review=verdict,
            completed_step_summaries=list(state["notes"]),
            replaced_steps=plan.steps[current_step:],
            available_tools=resolved_action_registry.definitions(),
            remaining_total_tool_calls=max(
                0,
                max_total_tool_calls - state["total_tool_calls"],
            ),
            remaining_replan_cycles=max(
                0,
                max_replan_cycles - len(state["replans"]),
            ),
            min_replacement_steps=minimum,
            max_replacement_steps=maximum,
        )

        logger.info(
            "Replanning remaining work after review %s",
            len(state["review_verdicts"]),
        )

        replacement = replanner(request)

        if not isinstance(replacement, ReplacementWork):
            raise TypeError(
                "replanner returned invalid replacement work",
            )

        completed_steps = plan.steps[:current_step]
        replaced_steps = plan.steps[current_step:]
        merged_plan = merge_replanned_steps(
            plan.goal,
            completed_steps,
            replacement.steps,
        )

        record = ReplanRecord(
            replan_number=len(state["replans"]) + 1,
            replaced_step_numbers=[step.step_number for step in replaced_steps],
            replacement_steps=merged_plan.steps[current_step:],
            review_rationale=verdict.rationale,
        )

        return {
            "plan": merged_plan,
            "pending_review_verdict": None,
            "replans": [record],
        }

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

    async def synthesize_node(
        state: AgentState,
    ) -> dict[str, object]:
        errors = list(state["errors"])
        observations = state["tool_observations"]

        successful_calls = sum(
            observation.result.success for observation in observations
        )
        failed_calls = len(observations) - successful_calls

        report_arguments = {
            "goal": state["goal"],
            "findings": list(state.get("findings", [])),
            "evidence": list(state.get("evidence", [])),
            "errors": errors,
            "total_tool_calls": state["total_tool_calls"],
            "successful_tool_calls": successful_calls,
            "failed_tool_calls": failed_calls,
            "review_conclusions": render_review_conclusions(
                state.get("review_verdicts", []),
            ),
            "review_cycles": len(state.get("review_verdicts", [])),
            "replan_cycles": len(state.get("replans", [])),
        }
        final_answer = render_research_report(**report_arguments)
        resolved_artifact_path: str | None = None

        if artifact_path is not None:
            artifact_result = await tool_runner.run(
                "write_file",
                {
                    "path": artifact_path,
                    "content": final_answer,
                },
            )

            if artifact_result.success:
                resolved_artifact_path = artifact_path
            else:
                artifact_error = (
                    "Research artifact could not be written through the "
                    "workspace boundary."
                )
                errors.append(artifact_error)
                report_arguments["errors"] = errors
                final_answer = render_research_report(
                    **report_arguments,
                )

        updates: dict[str, object] = {
            "final_answer": final_answer,
            "artifact_path": resolved_artifact_path,
            # Synthesis is the consumer of a routed "finish" review
            # verdict; the terminal state must not retain it.
            "pending_review_verdict": None,
        }

        if len(errors) > len(state["errors"]):
            updates["errors"] = errors[len(state["errors"]) :]

        return updates

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

    if reviewer is not None:
        builder.add_node("review", review_node)
        builder.add_node("replan", replan_node)

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

    if reviewer is not None:
        builder.add_edge("complete_step", "review")

        builder.add_conditional_edges(
            "review",
            route_after_review,
            {
                "continue": "decide_action",
                "replan": "replan",
                "finish": "synthesize",
            },
        )

        builder.add_edge("replan", "decide_action")
    else:
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
