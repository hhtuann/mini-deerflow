import pytest
from langgraph.errors import GraphRecursionError

from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import create_initial_state
from mini_deerflow.workflow import build_research_workflow


def create_test_plan(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=1,
                title="Collect sources",
                objective="Collect official sources.",
                success_criteria="Official sources are available.",
            ),
            PlanStep(
                step_number=2,
                title="Compare evidence",
                objective="Compare the collected evidence.",
                success_criteria="Key differences are identified.",
            ),
            PlanStep(
                step_number=3,
                title="Write recommendation",
                objective="Produce a recommendation.",
                success_criteria="A justified recommendation is written.",
            ),
        ],
    )


def test_workflow_executes_every_plan_step() -> None:
    planner_calls: list[str] = []

    def fake_planner(goal: str) -> Plan:
        planner_calls.append(goal)
        return create_test_plan(goal)

    graph = build_research_workflow(fake_planner)
    result = graph.invoke(
        create_initial_state("Research LangGraph"),
        config={"recursion_limit": 10},
    )

    assert planner_calls == ["Research LangGraph"]
    assert result["current_step"] == 3
    assert result["notes"] == [
        "Stub observation for step 1: Collect sources",
        "Stub observation for step 2: Compare evidence",
        "Stub observation for step 3: Write recommendation",
    ]
    assert result["final_answer"] is not None
    assert "Research goal: Research LangGraph" in result["final_answer"]
    assert "Stub observation for step 3" in result["final_answer"]


def test_workflow_stream_exposes_node_transitions() -> None:
    graph = build_research_workflow(create_test_plan)

    updates = list(
        graph.stream(
            create_initial_state("Research streaming"),
            config={"recursion_limit": 10},
            stream_mode="updates",
        )
    )

    executed_nodes = [next(iter(update)) for update in updates]

    assert executed_nodes == [
        "planner",
        "execute_stub",
        "execute_stub",
        "execute_stub",
        "synthesize",
    ]


def test_workflow_stops_when_recursion_limit_is_too_low() -> None:
    graph = build_research_workflow(create_test_plan)

    with pytest.raises(GraphRecursionError):
        graph.invoke(
            create_initial_state("Research recursion limits"),
            config={"recursion_limit": 2},
        )
