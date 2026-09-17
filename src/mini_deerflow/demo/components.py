"""Safe, native Streamlit renderers for the local mentor demo."""

from __future__ import annotations

from collections.abc import Sequence

import streamlit as st

from mini_deerflow.demo.view_models import DemoRunView, TraceEventView


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


def render_overview(view: DemoRunView) -> None:
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
        key="demo-plan-table",
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
    """Render bounded evidence, accepted citations, and review chronology."""

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

    st.subheader("Review and replan chronology")
    if not view.reviews:
        st.text("No review cycles were recorded.")
    for review in view.reviews:
        st.text(f"Review {review.review_number}: {review.route}")
        st.text(review.rationale)
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
        st.text(replan.review_rationale)


def render_delegation(view: DemoRunView) -> None:
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
            key=f"demo-delegation-table-{wave_number}",
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


def render_artifact(view: DemoRunView) -> None:
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
        key="demo-download-artifact",
        on_click="ignore",
    )


def render_demo_tabs(
    view: DemoRunView | None,
    *,
    live_traces: Sequence[TraceEventView] | None = None,
) -> None:
    """Render the one-page demo's exactly five required tabs."""

    overview, evidence, delegation, trace, artifact = st.tabs(
        [
            "Overview",
            "Evidence & Citations",
            "Delegation",
            "Trace",
            "Artifact",
        ]
    )
    with overview:
        render_empty_demo() if view is None else render_overview(view)
    with evidence:
        render_empty_demo() if view is None else render_evidence_and_citations(view)
    with delegation:
        render_empty_demo() if view is None else render_delegation(view)
    with trace:
        completed_traces = () if view is None else tuple(view.traces)
        traces = completed_traces if live_traces is None else tuple(live_traces)
        render_trace(traces)
    with artifact:
        render_empty_demo() if view is None else render_artifact(view)
