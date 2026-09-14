import logging
from collections.abc import Callable
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from mini_deerflow.schemas import Plan
from mini_deerflow.state import AgentState

logger = logging.getLogger(__name__)

Planner = Callable[[str], Plan]
ExecutionRoute = Literal["execute_stub", "synthesize"]


def build_research_workflow(
    planner: Planner,
) -> CompiledStateGraph:
    """Build the first deterministic research workflow."""

    def planner_node(state: AgentState) -> dict[str, object]:
        logger.info("Planning a bounded research goal")

        plan = planner(state["goal"])

        return {
            "plan": plan,
            "current_step": 0,
        }

    def execute_stub_node(
        state: AgentState,
    ) -> dict[str, object]:
        plan = state["plan"]

        if plan is None:
            raise RuntimeError(
                "execute_stub requires a plan",
            )

        current_step = state["current_step"]

        if current_step < 0 or current_step >= len(plan.steps):
            raise RuntimeError(
                "current_step is outside the plan",
            )

        step = plan.steps[current_step]
        observation = f"Stub observation for step {step.step_number}: {step.title}"

        logger.info("Executing stub step %s", step.step_number)

        return {
            "notes": [observation],
            "current_step": current_step + 1,
        }

    def route_after_execution(
        state: AgentState,
    ) -> ExecutionRoute:
        plan = state["plan"]

        if plan is None:
            raise RuntimeError(
                "execution routing requires a plan",
            )

        current_step = state["current_step"]

        if current_step > len(plan.steps):
            raise RuntimeError(
                "current_step moved beyond the plan",
            )

        if current_step < len(plan.steps):
            return "execute_stub"

        return "synthesize"

    def synthesize_node(
        state: AgentState,
    ) -> dict[str, object]:
        logger.info(
            "Synthesizing %s observations",
            len(state["notes"]),
        )

        rendered_notes = "\n".join(f"- {note}" for note in state["notes"])

        final_answer = (
            f"Research goal: {state['goal']}\n\nExecution notes:\n{rendered_notes}"
        )

        return {
            "final_answer": final_answer,
        }

    builder = StateGraph(AgentState)

    builder.add_node("planner", planner_node)
    builder.add_node("execute_stub", execute_stub_node)
    builder.add_node("synthesize", synthesize_node)

    builder.add_edge(START, "planner")
    builder.add_edge("planner", "execute_stub")

    builder.add_conditional_edges(
        "execute_stub",
        route_after_execution,
        {
            "execute_stub": "execute_stub",
            "synthesize": "synthesize",
        },
    )

    builder.add_edge("synthesize", END)

    return builder.compile()
