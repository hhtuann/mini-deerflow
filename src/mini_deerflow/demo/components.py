"""Safe, native Streamlit renderers for the local mentor demo."""

from __future__ import annotations

from collections.abc import Sequence

import streamlit as st

from mini_deerflow.demo.graph_projection import (
    ExecutionGraphNode,
    ExecutionGraphProjection,
    project_static_workflow,
)
from mini_deerflow.demo.view_models import DemoRunView, TraceEventView

_STATUS_COLORS = {
    "pending": "#94a3b8",
    "running": "#2563eb",
    "completed": "#16a34a",
    "failed": "#dc2626",
    "cancelled": "#ea580c",
    "skipped": "#64748b",
}
_NODE_SYMBOLS = {
    "workflow_node": "roundRect",
    "delegation": "diamond",
    "branch": "circle",
    "tool": "rect",
    "run": "roundRect",
    "fan_in": "triangle",
}


def render_status_strip(
    *,
    operation: str,
    state: str,
    thread_id: str,
    current_step: int | None = None,
    total_steps: int | None = None,
) -> None:
    """Render a compact status summary from controlled scalar values."""

    columns = st.columns(4)
    columns[0].metric("Operation", operation)
    columns[1].metric("State", state)
    columns[2].metric("Thread", thread_id or "—")
    progress = "—"
    if current_step is not None and total_steps is not None:
        progress = f"{current_step} / {total_steps}"
    columns[3].metric("Plan progress", progress)


def render_empty_demo() -> None:
    """Render the initial safe empty state."""

    st.info("Run a new thread or resume an existing thread to populate the demo.")


def render_overview(view: DemoRunView, *, key_prefix: str = "demo") -> None:
    """Render goal, plan, limits, and public limitations."""

    st.subheader("Goal")
    st.text(view.goal)

    st.subheader("Plan")
    st.dataframe(
        [
            {
                "Step": step.step_number,
                "Title": step.title,
                "Objective": step.objective,
                "Success criteria": step.success_criteria,
                "Status": step.status,
            }
            for step in view.plan
        ],
        hide_index=True,
        width="stretch",
        key=f"{key_prefix}-plan-table",
    )

    budget = view.budget
    first_row = st.columns(4)
    first_row[0].metric("Current step", f"{budget.current_step} / {budget.total_steps}")
    first_row[1].metric(
        "Step tool calls",
        f"{budget.step_tool_calls} / {budget.max_step_tool_calls}",
    )
    first_row[2].metric(
        "Total tool calls",
        f"{budget.total_tool_calls} / {budget.max_total_tool_calls}",
    )
    first_row[3].metric(
        "Replans",
        f"{budget.replan_cycles} / {budget.max_replan_cycles}",
    )
    st.metric("Delegation concurrency limit", budget.max_delegation_concurrency)

    st.subheader("Limitations")
    if view.limitations:
        for limitation in view.limitations:
            st.text(limitation)
    else:
        st.text("No public limitations were recorded.")


def render_evidence_and_citations(view: DemoRunView) -> None:
    """Render bounded evidence and accepted citations."""

    st.subheader("Evidence")
    if not view.evidence:
        st.text("No successful evidence was recorded.")
    for index, evidence in enumerate(view.evidence, start=1):
        with st.expander(f"Evidence {index}"):
            st.text(f"Title: {evidence.title}")
            st.text(f"Canonical URL: {evidence.canonical_url}")
            st.text(f"Source tool: {evidence.source_tool}")
            st.text(f"Used in final citations: {'yes' if evidence.is_cited else 'no'}")
            provenance = evidence.provenance
            st.text(
                "Provenance: "
                f"step {provenance.step_number}, "
                f"step call {provenance.step_tool_call_number}, "
                f"total call {provenance.total_tool_call_number}"
            )
            st.text(evidence.excerpt)

    st.subheader("Validated citations")
    if view.accepted_citations:
        for citation in view.accepted_citations:
            st.text(citation)
    else:
        st.text("No citations were accepted.")
    st.metric("Rejected citation count", view.rejected_citation_count)


def render_review_and_replan(view: DemoRunView) -> None:
    """Render structured review outcomes without model rationale or prompts."""

    for review in view.reviews:
        st.text(f"Review {review.review_number}: {review.route}")
        for finding in review.findings:
            st.text(f"Finding ({finding.category}): {finding.description}")
            st.text(
                "Related steps: "
                + ", ".join(str(number) for number in finding.related_step_numbers)
            )
    for replan in view.replans:
        st.text(f"Replan {replan.replan_number}")
        st.text(
            "Replaced steps: "
            + ", ".join(str(number) for number in replan.replaced_step_numbers)
        )
        st.text("Replacement steps: " + ", ".join(replan.replacement_step_titles))


def render_delegation(view: DemoRunView, *, key_prefix: str = "demo") -> None:
    """Render safe branch summaries and fan-in results."""

    if not view.delegations:
        st.text("No delegation waves were recorded.")
        return

    for wave_number, wave in enumerate(view.delegations, start=1):
        st.subheader(f"Delegation wave {wave_number}")
        st.text(f"Delegation ID: {wave.delegation_id}")
        st.text(f"Parent step: {wave.parent_step_number}")
        st.dataframe(
            [
                {
                    "Branch": branch.branch_id,
                    "Status": branch.status,
                    "Tool calls used": branch.tool_calls_used,
                    "Accepted finding": branch.finding_summary or "",
                }
                for branch in wave.branches
            ],
            hide_index=True,
            width="stretch",
            key=f"{key_prefix}-delegation-table-{wave_number}",
        )
        budget_columns = st.columns(3)
        budget_columns[0].metric("Reserved", wave.reserved_tool_calls)
        budget_columns[1].metric("Used", wave.used_tool_calls)
        budget_columns[2].metric("Charged", wave.charged_tool_calls)
        st.text(
            "Fan-in: "
            f"{wave.successful_branch_count} successful, "
            f"{wave.failed_branch_count} failed, "
            f"{wave.cancelled_branch_count} cancelled"
        )
        for limitation in wave.limitations:
            st.text(limitation)


def render_trace(
    traces: Sequence[TraceEventView],
    *,
    key_prefix: str = "demo-trace",
) -> None:
    """Render an ordered timeline using only the closed trace view schema."""

    if not traces:
        st.text("No trace events are available yet.")
        return

    run_ids = tuple(dict.fromkeys(event.run_id for event in traces))
    kinds = tuple(dict.fromkeys(event.kind for event in traces))
    outcomes = tuple(dict.fromkeys(event.outcome for event in traces))
    filter_columns = st.columns(3)
    selected_runs = filter_columns[0].multiselect(
        "Run",
        run_ids,
        default=run_ids,
        key=f"{key_prefix}-runs",
    )
    selected_kinds = filter_columns[1].multiselect(
        "Kind",
        kinds,
        default=kinds,
        key=f"{key_prefix}-kinds",
    )
    selected_outcomes = filter_columns[2].multiselect(
        "Outcome",
        outcomes,
        default=outcomes,
        key=f"{key_prefix}-outcomes",
    )
    selected_run_ids = set(selected_runs)
    selected_kind_names = set(selected_kinds)
    selected_outcome_names = set(selected_outcomes)
    selected = [
        event
        for event in traces
        if (not selected_run_ids or event.run_id in selected_run_ids)
        and (not selected_kind_names or event.kind in selected_kind_names)
        and (not selected_outcome_names or event.outcome in selected_outcome_names)
    ]
    st.dataframe(
        [
            {
                "Thread": event.thread_id,
                "Turn": event.turn_id or "",
                "Run": event.run_id,
                "Sequence": event.sequence,
                "Kind": event.kind,
                "Outcome": event.outcome,
                "Phase": event.phase or "",
                "Node": event.node or "",
                "Tool": event.tool_name or "",
                "Route": event.route or "",
                "Duration ms": event.duration_ms,
                "Step calls": event.step_tool_calls,
                "Total calls": event.total_tool_calls,
                "Evidence": event.evidence_count,
                "Citations": event.citation_count,
                "Rejected citations": event.rejected_citation_count,
                "Branches": event.branch_count,
                "Succeeded branches": event.successful_branch_count,
                "Failed branches": event.failed_branch_count,
                "Error category": event.error_category or "",
                "Error code": event.error_code or "",
            }
            for event in selected
        ],
        hide_index=True,
        width="stretch",
        key=f"{key_prefix}-table",
    )


def build_graph_chart_spec(
    graph: ExecutionGraphProjection,
    *,
    selected_node_id: str | None = None,
) -> dict[str, object]:
    """Build a JSON-only ECharts network spec from the safe graph view."""

    categories = tuple(dict.fromkeys(node.kind for node in graph.nodes))
    category_index = {name: index for index, name in enumerate(categories)}
    nodes = [
        {
            "id": node.id,
            "name": node.label,
            "value": f"{node.kind.replace('_', ' ')} · {node.status}",
            "category": category_index[node.kind],
            "symbol": _NODE_SYMBOLS[node.kind],
            "symbolSize": 62 if node.id == selected_node_id else 48,
            "itemStyle": {
                "color": _STATUS_COLORS[node.status],
                "borderColor": "#f8fafc",
                "borderWidth": 4 if node.id == selected_node_id else 1,
                "shadowBlur": 12 if node.status == "running" else 0,
                "shadowColor": _STATUS_COLORS[node.status],
            },
            "label": {
                "show": True,
                "position": "bottom",
                "distance": 7,
                "width": 120,
                "overflow": "break",
                "fontSize": 11,
            },
        }
        for node in graph.nodes
    ]
    links = [
        {
            "source": edge.source,
            "target": edge.target,
            "value": edge.label or edge.relation.replace("_", " "),
            "lineStyle": {
                "width": 2.5 if edge.selected else 1.2,
                "opacity": 0.9 if edge.selected else 0.55,
                "curveness": 0.08,
                "type": "solid" if edge.selected else "dashed",
            },
            "label": {
                "show": bool(edge.label),
                "formatter": edge.label or "",
                "fontSize": 10,
            },
        }
        for edge in graph.edges
    ]
    return {
        "animationDurationUpdate": 350,
        "aria": {
            "enabled": True,
            "description": (
                "Agent execution graph. Node color indicates pending, running, "
                "completed, failed, cancelled, or skipped status."
            ),
        },
        "tooltip": {"trigger": "item", "formatter": "{b}<br/>{c}"},
        "legend": [
            {
                "data": [name.replace("_", " ") for name in categories],
                "bottom": 0,
            }
        ],
        "series": [
            {
                "type": "graph",
                "layout": "force",
                "roam": True,
                "draggable": True,
                "cursor": "grab",
                "data": nodes,
                "links": links,
                "categories": [{"name": name.replace("_", " ")} for name in categories],
                "edgeSymbol": ["none", "arrow"],
                "edgeSymbolSize": [0, 8],
                "emphasis": {"focus": "adjacency", "lineStyle": {"width": 4}},
                "force": {
                    "repulsion": 520,
                    "edgeLength": [90, 180],
                    "gravity": 0.08,
                },
            }
        ],
    }


def render_agent_graph(
    runtime_graph: ExecutionGraphProjection,
    *,
    key_prefix: str = "agent-graph",
) -> None:
    """Render static topology or observed execution with a safe inspector."""

    graph_mode = st.segmented_control(
        "Graph view",
        options=("Workflow", "Current run"),
        default="Current run" if runtime_graph.nodes else "Workflow",
        key=f"{key_prefix}-mode",
        help=(
            "Workflow shows possible canonical routes. Current run shows only "
            "entities reconstructed from structured execution events."
        ),
    )
    graph = project_static_workflow() if graph_mode == "Workflow" else runtime_graph
    if not graph.nodes:
        st.info("No structured execution events are available for this run yet.")
        return

    metrics = graph.metrics
    metric_columns = st.columns(6)
    metric_columns[0].metric("Nodes", metrics.nodes)
    metric_columns[1].metric("Running", metrics.running)
    metric_columns[2].metric("Completed", metrics.completed)
    metric_columns[3].metric("Failed", metrics.failed)
    metric_columns[4].metric("Tool calls", metrics.tool_calls)
    metric_columns[5].metric("Sub-agents", metrics.sub_agents)
    st.caption(f"Evidence observed: {metrics.evidence}")

    node_by_id = {node.id: node for node in graph.nodes}
    graph_column, inspector_column = st.columns([2.2, 1])
    with inspector_column:
        selected_node_id = st.selectbox(
            "Inspect node",
            options=tuple(node_by_id),
            format_func=lambda node_id: (
                f"{node_by_id[node_id].label} · {node_by_id[node_id].status}"
            ),
            key=f"{key_prefix}-{graph_mode}-selected-node",
        )
        selected_node = node_by_id[selected_node_id]
        _render_node_inspector(selected_node)

    with graph_column:
        st.echarts_chart(
            build_graph_chart_spec(graph, selected_node_id=selected_node_id),
            height=620,
            key=f"{key_prefix}-{graph_mode}-chart",
            renderer="svg",
        )
        st.caption(
            "Drag nodes or pan/zoom the graph. Use the node selector to highlight "
            "an entity and inspect its safe details."
        )


def _render_node_inspector(node: ExecutionGraphNode) -> None:
    st.subheader(node.label)
    badge_color = {
        "pending": "gray",
        "running": "blue",
        "completed": "green",
        "failed": "red",
        "cancelled": "orange",
        "skipped": "gray",
    }[node.status]
    st.badge(node.status, color=badge_color)
    if node.safe_summary:
        st.caption(node.safe_summary)
    if node.started_at is not None:
        st.text(f"Started: {node.started_at}")
    if node.completed_at is not None:
        st.text(f"Completed: {node.completed_at}")
    if node.duration_ms is not None:
        st.text(f"Duration: {node.duration_ms} ms")
    for field in node.details:
        st.text(f"{field.label}: {field.value}")
    with st.expander("Technical IDs", expanded=False):
        for field in node.technical_details:
            st.text(f"{field.label}: {field.value}")


def render_artifact(view: DemoRunView, *, key_prefix: str = "demo") -> None:
    """Render a structured preview and the exact source without executing Markdown."""

    artifact = view.artifact
    if not artifact.available:
        st.text("No artifact is available.")
        return

    st.subheader("Structured preview")
    st.text(f"Goal: {view.goal}")
    for finding in artifact.finding_summaries:
        st.text(f"Finding: {finding}")
    for citation in artifact.citation_urls:
        st.text(f"Validated citation: {citation}")
    for limitation in artifact.limitation_notices:
        st.text(f"Limitation: {limitation}")

    st.subheader("Exact Markdown source")
    st.code(artifact.markdown_source, language="markdown")
    st.download_button(
        "Download Markdown",
        data=artifact.markdown_source,
        file_name="deterministic-mentor-report.md",
        mime="text/markdown",
        key=f"{key_prefix}-download-artifact",
        on_click="ignore",
    )


def render_demo_tabs(
    view: DemoRunView | None,
    *,
    live_traces: Sequence[TraceEventView] | None = None,
    key_prefix: str = "demo",
) -> None:
    """Render only the execution-detail sections relevant to this turn."""

    if view is None:
        render_empty_demo()
        return

    traces = tuple(view.traces) if live_traces is None else tuple(live_traces)
    sections: list[tuple[str, object]] = [
        (
            "Agent graph",
            lambda: render_agent_graph(
                view.execution_graph,
                key_prefix=f"{key_prefix}-graph",
            ),
        ),
        ("Execution", lambda: render_overview(view, key_prefix=key_prefix)),
    ]
    if view.evidence or view.accepted_citations or view.rejected_citation_count:
        sections.append(
            ("Evidence & Citations", lambda: render_evidence_and_citations(view))
        )
    if view.delegations:
        sections.append(
            ("Delegation", lambda: render_delegation(view, key_prefix=key_prefix))
        )
    if view.reviews or view.replans:
        sections.append(("Review & Replan", lambda: render_review_and_replan(view)))
    if traces:
        sections.append(
            (
                "Trace",
                lambda: render_trace(traces, key_prefix=f"{key_prefix}-trace"),
            )
        )
    if view.artifact.available:
        sections.append(
            (
                "Research report",
                lambda: render_artifact(view, key_prefix=key_prefix),
            )
        )

    tabs = st.tabs([label for label, _ in sections])
    for tab, (_, renderer) in zip(tabs, sections, strict=True):
        with tab:
            renderer()
