import asyncio
import json
from collections.abc import Sequence

from streamlit.testing.v1 import AppTest

from mini_deerflow.actions import CompleteStepAction
from mini_deerflow.agent_workflow import build_agent_workflow
from mini_deerflow.decision import ActionContext
from mini_deerflow.demo.components import build_graph_chart_spec
from mini_deerflow.demo.graph_projection import (
    ExecutionGraphNode,
    ExecutionGraphProjection,
    project_execution_graph,
    project_static_workflow,
)
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.service import DemoRuntimeService, RunDemoCommand
from mini_deerflow.review import (
    ReplacementWork,
    ReplanRequest,
    ReviewContext,
    ReviewVerdict,
)
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.tools import ToolRegistry
from mini_deerflow.tracing import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
)


class _Selector:
    async def select_action(self, context: ActionContext) -> CompleteStepAction:
        del context
        return CompleteStepAction(type="complete_step", summary="done", sources=[])


class _Reviewer:
    async def review_evidence(self, context: ReviewContext) -> ReviewVerdict:
        del context
        return ReviewVerdict(verdict="finish", rationale="done")


def _replanner(request: ReplanRequest) -> ReplacementWork:
    del request
    return ReplacementWork(
        steps=[
            PlanStep(
                step_number=1,
                title="Replacement",
                objective="Replace work.",
                success_criteria="Finish.",
            )
        ]
    )


def _planner(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=1,
                title="Research",
                objective="Research safely.",
                success_criteria="Finish.",
            )
        ],
    )


def _event(
    sequence: int,
    event_type: ExecutionEventType,
    status: ExecutionStatus,
    *,
    run_id: str = "parent-run",
    parent_run_id: str | None = None,
    node_id: str | None = None,
    delegation_id: str | None = None,
    branch_id: str | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    route: str | None = None,
    duration_ms: int | None = None,
    evidence_count: int | None = None,
    branch_count: int | None = None,
    successful_branch_count: int | None = None,
    failed_branch_count: int | None = None,
    cancelled_branch_count: int | None = None,
    reserved_tool_calls: int | None = None,
    used_tool_calls: int | None = None,
    charged_tool_calls: int | None = None,
    metadata: dict[str, object] | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=f"event-{sequence}",
        event_type=event_type,
        status=status,
        root_run_id="parent-run",
        run_id=run_id,
        parent_run_id=parent_run_id,
        thread_id="graph-test",
        turn_id="turn-graph",
        sequence=sequence,
        node_id=node_id,
        delegation_id=delegation_id,
        branch_id=branch_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        route=route,
        duration_ms=duration_ms,
        evidence_count=evidence_count,
        branch_count=branch_count,
        successful_branch_count=successful_branch_count,
        failed_branch_count=failed_branch_count,
        cancelled_branch_count=cancelled_branch_count,
        reserved_tool_calls=reserved_tool_calls,
        used_tool_calls=used_tool_calls,
        charged_tool_calls=charged_tool_calls,
        metadata=metadata or {},
        operation="run" if event_type.name.startswith("RUN_") else None,
    )


def _representative_events() -> tuple[ExecutionEvent, ...]:
    alpha_run = "branch:parent-run:wave-1:alpha"
    beta_run = "branch:parent-run:wave-1:beta"
    return (
        _event(1, ExecutionEventType.RUN_STARTED, ExecutionStatus.STARTED),
        _event(
            2,
            ExecutionEventType.NODE_STARTED,
            ExecutionStatus.STARTED,
            node_id="planner",
        ),
        _event(
            3,
            ExecutionEventType.NODE_COMPLETED,
            ExecutionStatus.COMPLETED,
            node_id="planner",
            duration_ms=3,
        ),
        _event(
            4,
            ExecutionEventType.NODE_STARTED,
            ExecutionStatus.STARTED,
            node_id="execute_tool",
        ),
        _event(
            5,
            ExecutionEventType.TOOL_STARTED,
            ExecutionStatus.STARTED,
            node_id="execute_tool",
            tool_call_id="tool:parent-run:1",
            tool_name="delegate_research",
        ),
        _event(
            6,
            ExecutionEventType.DELEGATION_STARTED,
            ExecutionStatus.STARTED,
            node_id="execute_tool",
            delegation_id="wave-1",
            branch_count=2,
            reserved_tool_calls=2,
        ),
        _event(
            7,
            ExecutionEventType.BRANCH_STARTED,
            ExecutionStatus.STARTED,
            run_id=alpha_run,
            parent_run_id="parent-run",
            node_id="delegated_researcher",
            delegation_id="wave-1",
            branch_id="alpha",
        ),
        _event(
            8,
            ExecutionEventType.BRANCH_STARTED,
            ExecutionStatus.STARTED,
            run_id=beta_run,
            parent_run_id="parent-run",
            node_id="delegated_researcher",
            delegation_id="wave-1",
            branch_id="beta",
        ),
        _event(
            9,
            ExecutionEventType.TOOL_STARTED,
            ExecutionStatus.STARTED,
            run_id=beta_run,
            parent_run_id="parent-run",
            node_id="delegated_researcher",
            delegation_id="wave-1",
            branch_id="beta",
            tool_call_id=f"tool:{beta_run}:1",
            tool_name="web_search",
        ),
        _event(
            10,
            ExecutionEventType.TOOL_STARTED,
            ExecutionStatus.STARTED,
            run_id=alpha_run,
            parent_run_id="parent-run",
            node_id="delegated_researcher",
            delegation_id="wave-1",
            branch_id="alpha",
            tool_call_id=f"tool:{alpha_run}:1",
            tool_name="web_search",
        ),
        _event(
            11,
            ExecutionEventType.TOOL_COMPLETED,
            ExecutionStatus.COMPLETED,
            run_id=alpha_run,
            parent_run_id="parent-run",
            node_id="delegated_researcher",
            delegation_id="wave-1",
            branch_id="alpha",
            tool_call_id=f"tool:{alpha_run}:1",
            tool_name="web_search",
            duration_ms=4,
            evidence_count=1,
        ),
        _event(
            12,
            ExecutionEventType.EVIDENCE_PRODUCED,
            ExecutionStatus.OBSERVED,
            run_id=alpha_run,
            parent_run_id="parent-run",
            node_id="delegated_researcher",
            delegation_id="wave-1",
            branch_id="alpha",
            tool_call_id=f"tool:{alpha_run}:1",
            tool_name="web_search",
            evidence_count=1,
        ),
        _event(
            13,
            ExecutionEventType.BRANCH_COMPLETED,
            ExecutionStatus.COMPLETED,
            run_id=alpha_run,
            parent_run_id="parent-run",
            node_id="delegated_researcher",
            delegation_id="wave-1",
            branch_id="alpha",
            used_tool_calls=1,
            evidence_count=1,
        ),
        _event(
            14,
            ExecutionEventType.TOOL_FAILED,
            ExecutionStatus.FAILED,
            run_id=beta_run,
            parent_run_id="parent-run",
            node_id="delegated_researcher",
            delegation_id="wave-1",
            branch_id="beta",
            tool_call_id=f"tool:{beta_run}:1",
            tool_name="web_search",
            duration_ms=2,
        ),
        _event(
            15,
            ExecutionEventType.BRANCH_FAILED,
            ExecutionStatus.FAILED,
            run_id=beta_run,
            parent_run_id="parent-run",
            node_id="delegated_researcher",
            delegation_id="wave-1",
            branch_id="beta",
            used_tool_calls=1,
            metadata={
                "failure_category": "controlled_failure",
                "authorization": "Bearer unsafe-secret",
                "route_reason": "Bearer unsafe-secret",
            },
        ),
        _event(
            16,
            ExecutionEventType.FAN_IN_COMPLETED,
            ExecutionStatus.COMPLETED,
            delegation_id="wave-1",
            successful_branch_count=1,
            failed_branch_count=1,
            cancelled_branch_count=0,
            evidence_count=1,
            metadata={"evidence_promoted": 1, "limitations_produced": 1},
        ),
        _event(
            17,
            ExecutionEventType.DELEGATION_COMPLETED,
            ExecutionStatus.PARTIAL,
            node_id="execute_tool",
            delegation_id="wave-1",
            branch_count=2,
            successful_branch_count=1,
            failed_branch_count=1,
            cancelled_branch_count=0,
            reserved_tool_calls=2,
            used_tool_calls=2,
            charged_tool_calls=2,
            evidence_count=1,
        ),
        _event(
            18,
            ExecutionEventType.TOOL_COMPLETED,
            ExecutionStatus.COMPLETED,
            node_id="execute_tool",
            tool_call_id="tool:parent-run:1",
            tool_name="delegate_research",
            duration_ms=20,
            evidence_count=1,
        ),
        _event(
            19,
            ExecutionEventType.NODE_COMPLETED,
            ExecutionStatus.COMPLETED,
            node_id="execute_tool",
            duration_ms=24,
        ),
        _event(
            20,
            ExecutionEventType.NODE_STARTED,
            ExecutionStatus.STARTED,
            node_id="review",
        ),
        _event(
            21,
            ExecutionEventType.NODE_COMPLETED,
            ExecutionStatus.COMPLETED,
            node_id="review",
            duration_ms=2,
        ),
        _event(
            22,
            ExecutionEventType.ROUTE_SELECTED,
            ExecutionStatus.SELECTED,
            node_id="review",
            route="replan",
            metadata={
                "from_node": "review",
                "to_node": "replan",
                "decision_type": "review_verdict",
                "route_reason": "reviewer_verdict",
                "review_verdict": "replan",
            },
        ),
        _event(
            23,
            ExecutionEventType.NODE_STARTED,
            ExecutionStatus.STARTED,
            node_id="replan",
        ),
        _event(
            24,
            ExecutionEventType.NODE_COMPLETED,
            ExecutionStatus.COMPLETED,
            node_id="replan",
            duration_ms=2,
        ),
        _event(
            25,
            ExecutionEventType.RUN_COMPLETED,
            ExecutionStatus.COMPLETED,
            duration_ms=50,
        ),
    )


def _technical_value(node: ExecutionGraphNode, label: str) -> str | None:
    return next(
        (field.value for field in node.technical_details if field.label == label),
        None,
    )


def _branch_node(graph: ExecutionGraphProjection, branch_id: str) -> ExecutionGraphNode:
    return next(
        node
        for node in graph.nodes
        if node.kind == "branch" and _technical_value(node, "Branch ID") == branch_id
    )


def test_static_projection_matches_compiled_canonical_workflow() -> None:
    compiled = build_agent_workflow(
        _planner,
        _Selector(),
        ToolRegistry(),
        reviewer=_Reviewer(),
        replanner=_replanner,
    )
    actual_edges = {(edge.source, edge.target) for edge in compiled.get_graph().edges}
    projection = project_static_workflow()
    projected_edges = {(edge.source, edge.target) for edge in projection.edges}

    assert projected_edges == actual_edges
    assert {node.id for node in projection.nodes} == set(compiled.get_graph().nodes)
    assert {
        (edge.source, edge.target, edge.label)
        for edge in projection.edges
        if edge.source == "review"
    } == {
        ("review", "decide_action", "continue"),
        ("review", "replan", "replan"),
        ("review", "synthesize", "finish"),
    }


def test_projection_merges_lifecycles_and_builds_delegation_hierarchy() -> None:
    graph = project_execution_graph(_representative_events())
    alpha = _branch_node(graph, "alpha")
    beta = _branch_node(graph, "beta")
    delegation = next(node for node in graph.nodes if node.kind == "delegation")
    fan_in = next(node for node in graph.nodes if node.kind == "fan_in")
    tools = [node for node in graph.nodes if node.kind == "tool"]

    assert alpha.status == "completed"
    assert beta.status == "failed"
    assert delegation.status == "completed"
    assert delegation.safe_summary is not None and "1 failed" in delegation.safe_summary
    assert fan_in.status == "completed"
    assert len([node for node in graph.nodes if node.label == "Planner"]) == 1
    assert len(tools) == 3
    assert {node.parent_id for node in tools if node.label == "Web search"} == {
        alpha.id,
        beta.id,
    }
    assert graph.metrics.tool_calls == 3
    assert graph.metrics.sub_agents == 2
    assert graph.metrics.evidence == 1

    relations = {
        (edge.source, edge.target, edge.relation, edge.label) for edge in graph.edges
    }
    assert (delegation.id, alpha.id, "spawns", "sub-agent") in relations
    assert (delegation.id, beta.id, "spawns", "sub-agent") in relations
    assert (alpha.id, fan_in.id, "returns_evidence", "fan-in") in relations
    assert (beta.id, fan_in.id, "returns_evidence", "fan-in") in relations
    assert any(
        edge.relation == "selected_route" and edge.label == "replan"
        for edge in graph.edges
    )


def test_projection_is_order_independent_and_duplicate_delivery_is_idempotent() -> None:
    events = _representative_events()
    expected = project_execution_graph(events)
    replayed_and_reversed: Sequence[ExecutionEvent] = tuple(reversed(events)) + (
        events[6],
        events[10],
        events[14],
    )

    assert project_execution_graph(replayed_and_reversed) == expected
    assert (
        len(
            {
                _technical_value(node, "Branch ID")
                for node in expected.nodes
                if node.kind == "branch"
            }
        )
        == 2
    )


def test_projection_does_not_expose_unallowlisted_or_secret_metadata() -> None:
    graph = project_execution_graph(_representative_events())
    rendered = repr(graph)

    assert "unsafe-secret" not in rendered
    assert "authorization" not in rendered.lower()
    assert "prompt" not in rendered.lower()
    assert "provider response" not in rendered.lower()


def test_chart_spec_is_json_only_and_highlights_selected_node() -> None:
    graph = project_execution_graph(_representative_events())
    selected = graph.nodes[0].id
    spec = build_graph_chart_spec(graph, selected_node_id=selected)
    serialized = json.dumps(spec)
    series = spec["series"]

    assert serialized
    assert isinstance(series, list)
    selected_spec = next(
        node
        for node in series[0]["data"]
        if node["id"] == selected  # type: ignore[index]
    )
    assert selected_spec["itemStyle"]["borderWidth"] == 4
    assert series[0]["roam"] is True  # type: ignore[index]


def test_offline_walkthrough_projects_real_child_tools_and_partial_failure(
    tmp_path,
) -> None:
    service = DemoRuntimeService(OfflineDemoBackend(tmp_path / "graph-offline"))
    view = asyncio.run(
        service.run(
            RunDemoCommand(
                thread_id="offline-graph-proof",
                turn_id="turn-offline-graph",
            )
        )
    )
    graph = view.execution_graph
    alpha = _branch_node(graph, "alpha")
    beta = _branch_node(graph, "beta")
    delegation = next(node for node in graph.nodes if node.kind == "delegation")
    main_run = next(node for node in graph.nodes if node.kind == "run")
    child_tools = [
        node
        for node in graph.nodes
        if node.kind == "tool" and node.parent_id in {alpha.id, beta.id}
    ]

    assert alpha.status == "completed"
    assert beta.status == "failed"
    assert delegation.status == "completed"
    assert delegation.safe_summary is not None and "1 failed" in delegation.safe_summary
    assert main_run.status == "completed"
    assert {node.parent_id for node in child_tools} == {alpha.id, beta.id}
    assert {node.status for node in child_tools} == {"completed", "failed"}
    assert any(node.kind == "fan_in" for node in graph.nodes)
    selected_routes = {
        edge.label for edge in graph.edges if edge.relation == "selected_route"
    }
    assert "replan" in selected_routes
    assert "execute tool" in selected_routes
    assert graph.metrics.sub_agents == 2
    assert graph.metrics.evidence >= 1


def test_streamlit_graph_renderer_smoke() -> None:
    def script() -> None:
        from mini_deerflow.demo.components import render_agent_graph
        from mini_deerflow.demo.graph_projection import project_static_workflow

        render_agent_graph(project_static_workflow(), key_prefix="graph-smoke")

    app = AppTest.from_function(script).run(timeout=10)

    assert not app.exception
    assert app.segmented_control[0].value == "Current run"
    assert app.selectbox[0].label == "Inspect node"
    assert [metric.label for metric in app.metric[:6]] == [
        "Nodes",
        "Running",
        "Completed",
        "Failed",
        "Tool calls",
        "Sub-agents",
    ]
