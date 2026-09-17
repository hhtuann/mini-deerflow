"""Allowlisted, immutable projections for the local mentor demo.

This module is the boundary between the runtime's durable state and the
Streamlit renderer.  It deliberately projects individual fields instead of
serializing an ``AgentState`` wholesale.  In particular, messages, tool
observations, raw errors, pending actions, checkpoints, and artifact paths do
not cross this boundary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from mini_deerflow.runtime import RuntimeLimits
from mini_deerflow.state import AgentState
from mini_deerflow.tracing import ExecutionTrace, TraceKind

MAX_GOAL_CHARS = 1_000
MAX_TITLE_CHARS = 200
MAX_OBJECTIVE_CHARS = 500
MAX_EVIDENCE_EXCERPT_CHARS = 1_200
MAX_REVIEW_TEXT_CHARS = 2_000
MAX_LIMITATION_CHARS = 500

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(https?://[^)]+\)", re.IGNORECASE)
_HTTP_URL = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)

PlanStepStatus = Literal["completed", "current", "pending"]
WorkflowStatus = Literal["completed", "incomplete"]


@dataclass(frozen=True, slots=True)
class PlanStepView:
    step_number: int
    title: str
    objective: str
    success_criteria: str
    status: PlanStepStatus


@dataclass(frozen=True, slots=True)
class BudgetView:
    current_step: int
    total_steps: int
    step_tool_calls: int
    total_tool_calls: int
    max_step_tool_calls: int
    max_total_tool_calls: int
    replan_cycles: int
    max_replan_cycles: int
    max_delegation_concurrency: int


@dataclass(frozen=True, slots=True)
class EvidenceProvenanceView:
    tool_name: str
    step_number: int
    step_tool_call_number: int
    total_tool_call_number: int
    observation_index: int
    delegation_id: str | None
    branch_id: str | None
    branch_tool_call_number: int | None


@dataclass(frozen=True, slots=True)
class EvidenceView:
    canonical_url: str
    source_tool: str
    title: str
    excerpt: str
    provenance: EvidenceProvenanceView
    is_cited: bool


@dataclass(frozen=True, slots=True)
class ReviewFindingView:
    category: str
    description: str
    related_step_numbers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ReviewCycleView:
    review_number: int
    route: str
    rationale: str
    findings: tuple[ReviewFindingView, ...]


@dataclass(frozen=True, slots=True)
class ReplanView:
    replan_number: int
    replaced_step_numbers: tuple[int, ...]
    replacement_step_titles: tuple[str, ...]
    review_rationale: str


@dataclass(frozen=True, slots=True)
class DelegationBranchView:
    branch_id: str
    status: str
    tool_calls_used: int
    finding_summary: str | None


@dataclass(frozen=True, slots=True)
class DelegationWaveView:
    delegation_id: str
    parent_step_number: int
    branches: tuple[DelegationBranchView, ...]
    reserved_tool_calls: int
    used_tool_calls: int
    charged_tool_calls: int
    successful_branch_count: int
    failed_branch_count: int
    cancelled_branch_count: int
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TraceEventView:
    """The closed, scalar-only trace shape permitted in the UI."""

    run_id: str
    thread_id: str
    sequence: int
    kind: str
    phase: str
    outcome: str
    operation: str | None
    node: str | None
    tool_name: str | None
    route: str | None
    duration_ms: int | None
    current_step: int | None
    step_tool_calls: int | None
    total_tool_calls: int | None
    max_step_tool_calls: int | None
    max_total_tool_calls: int | None
    max_replan_cycles: int | None
    evidence_count: int | None
    citation_count: int | None
    rejected_citation_count: int | None
    artifact_count: int | None
    branch_count: int | None
    successful_branch_count: int | None
    failed_branch_count: int | None
    cancelled_branch_count: int | None
    reserved_tool_calls: int | None
    used_tool_calls: int | None
    charged_tool_calls: int | None
    context_omitted_items: int | None
    context_truncated_items: int | None
    context_estimated_tokens: int | None
    error_category: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ArtifactView:
    label: str
    available: bool
    finding_summaries: tuple[str, ...]
    citation_urls: tuple[str, ...]
    limitation_notices: tuple[str, ...]
    markdown_source: str


@dataclass(frozen=True, slots=True)
class DemoRunView:
    thread_id: str
    operation: str | None
    workflow_status: WorkflowStatus
    goal: str
    plan: tuple[PlanStepView, ...]
    budget: BudgetView
    evidence: tuple[EvidenceView, ...]
    accepted_citations: tuple[str, ...]
    rejected_citation_count: int
    reviews: tuple[ReviewCycleView, ...]
    replans: tuple[ReplanView, ...]
    delegations: tuple[DelegationWaveView, ...]
    traces: tuple[TraceEventView, ...]
    artifact: ArtifactView
    limitations: tuple[str, ...]


def project_trace_event(event: ExecutionTrace) -> TraceEventView:
    """Project one already-redacted trace without opening its schema."""

    return TraceEventView(
        run_id=str(event.run_id),
        thread_id=str(event.thread_id),
        sequence=event.sequence,
        kind=event.kind.value,
        phase=event.phase.value,
        outcome=event.outcome.value,
        operation=event.operation,
        node=event.node,
        tool_name=event.tool_name,
        route=event.route,
        duration_ms=event.duration_ms,
        current_step=event.current_step,
        step_tool_calls=event.step_tool_calls,
        total_tool_calls=event.total_tool_calls,
        max_step_tool_calls=event.max_step_tool_calls,
        max_total_tool_calls=event.max_total_tool_calls,
        max_replan_cycles=event.max_replan_cycles,
        evidence_count=event.evidence_count,
        citation_count=event.citation_count,
        rejected_citation_count=event.rejected_citation_count,
        artifact_count=event.artifact_count,
        branch_count=event.branch_count,
        successful_branch_count=event.successful_branch_count,
        failed_branch_count=event.failed_branch_count,
        cancelled_branch_count=event.cancelled_branch_count,
        reserved_tool_calls=event.reserved_tool_calls,
        used_tool_calls=event.used_tool_calls,
        charged_tool_calls=event.charged_tool_calls,
        context_omitted_items=event.context_omitted_items,
        context_truncated_items=event.context_truncated_items,
        context_estimated_tokens=event.context_estimated_tokens,
        error_category=(
            event.error_category.value if event.error_category is not None else None
        ),
        error_code=event.error_code.value if event.error_code is not None else None,
    )


def project_demo_run(
    state: AgentState,
    traces: Iterable[ExecutionTrace],
    *,
    thread_id: str,
    limits: RuntimeLimits,
) -> DemoRunView:
    """Create the sole state representation consumable by Streamlit."""

    trace_events = tuple(traces)
    trace_views = tuple(project_trace_event(event) for event in trace_events)
    plan = state.get("plan")
    current_step = _non_negative_int(state.get("current_step"))
    plan_steps = (
        ()
        if plan is None
        else tuple(
            PlanStepView(
                step_number=step.step_number,
                title=_plain_text(step.title, MAX_TITLE_CHARS, omit_urls=True),
                objective=_plain_text(
                    step.objective,
                    MAX_OBJECTIVE_CHARS,
                    omit_urls=True,
                ),
                success_criteria=_plain_text(
                    step.success_criteria,
                    MAX_OBJECTIVE_CHARS,
                    omit_urls=True,
                ),
                status=_step_status(step.step_number, current_step, len(plan.steps)),
            )
            for step in plan.steps
        )
    )

    evidence_records = tuple(state.get("evidence", ()))
    evidence_urls = {
        record.canonical_url
        for record in evidence_records
        if record.status == "success"
    }
    accepted_citations = tuple(
        source
        for source in dict.fromkeys(state.get("sources", ()))
        if source in evidence_urls
    )
    accepted_set = set(accepted_citations)
    evidence_views = tuple(
        EvidenceView(
            canonical_url=record.canonical_url,
            source_tool=record.source_tool,
            title=_plain_text(
                record.title or "Untitled source",
                MAX_TITLE_CHARS,
                omit_urls=True,
            ),
            excerpt=_plain_text(
                record.excerpt,
                MAX_EVIDENCE_EXCERPT_CHARS,
                omit_urls=True,
            ),
            provenance=EvidenceProvenanceView(
                tool_name=record.provenance.tool_name,
                step_number=record.provenance.step_number,
                step_tool_call_number=record.provenance.step_tool_call_number,
                total_tool_call_number=record.provenance.total_tool_call_number,
                observation_index=record.provenance.observation_index,
                delegation_id=record.provenance.delegation_id,
                branch_id=record.provenance.branch_id,
                branch_tool_call_number=(record.provenance.branch_tool_call_number),
            ),
            is_cited=record.canonical_url in accepted_set,
        )
        for record in evidence_records
        if record.status == "success"
    )

    reviews = tuple(
        ReviewCycleView(
            review_number=index,
            route=verdict.verdict,
            rationale=_plain_text(
                verdict.rationale,
                MAX_REVIEW_TEXT_CHARS,
                omit_urls=True,
            ),
            findings=tuple(
                ReviewFindingView(
                    category=finding.category,
                    description=_plain_text(
                        finding.description,
                        MAX_REVIEW_TEXT_CHARS,
                        omit_urls=True,
                    ),
                    related_step_numbers=tuple(finding.related_step_numbers),
                )
                for finding in verdict.findings
            ),
        )
        for index, verdict in enumerate(state.get("review_verdicts", ()), start=1)
    )
    replans = tuple(
        ReplanView(
            replan_number=replan.replan_number,
            replaced_step_numbers=tuple(replan.replaced_step_numbers),
            replacement_step_titles=tuple(
                _plain_text(step.title, MAX_TITLE_CHARS, omit_urls=True)
                for step in replan.replacement_steps
            ),
            review_rationale=_plain_text(
                replan.review_rationale,
                MAX_REVIEW_TEXT_CHARS,
                omit_urls=True,
            ),
        )
        for replan in state.get("replans", ())
    )

    delegation_views: list[DelegationWaveView] = []
    failed_branch_ids: set[str] = set()
    for record in state.get("delegations", ()):
        branches: list[DelegationBranchView] = []
        for result in record.results:
            finding_summary = None
            if result.status == "success" and result.finding is not None:
                finding_summary = _plain_text(
                    result.finding.summary,
                    MAX_REVIEW_TEXT_CHARS,
                    omit_urls=True,
                )
            elif result.finding is not None:
                # A failed branch's finding is intentionally not projected.
                pass
            if result.status != "success":
                failed_branch_ids.add(str(result.branch_id))
            branches.append(
                DelegationBranchView(
                    branch_id=str(result.branch_id),
                    status=result.status,
                    tool_calls_used=result.tool_calls_used,
                    finding_summary=finding_summary,
                )
            )

        failed_count = len(record.fan_in.failed_branches)
        cancelled_count = len(record.fan_in.cancelled_branches)
        limitations = tuple(
            _delegation_limitation(branch.branch_id, branch.status)
            for branch in branches
            if branch.status != "success"
        )
        delegation_views.append(
            DelegationWaveView(
                delegation_id=str(record.delegation_id),
                parent_step_number=record.parent_step_number,
                branches=tuple(branches),
                reserved_tool_calls=record.reserved_tool_calls,
                used_tool_calls=record.used_tool_calls,
                charged_tool_calls=record.charged_tool_calls,
                successful_branch_count=len(record.fan_in.successful_branches),
                failed_branch_count=failed_count,
                cancelled_branch_count=cancelled_count,
                limitations=limitations,
            )
        )

    safe_finding_summaries = tuple(
        _plain_text(finding.summary, MAX_REVIEW_TEXT_CHARS, omit_urls=True)
        for finding in state.get("findings", ())
        if finding.branch_id is None or finding.branch_id not in failed_branch_ids
    )
    limitations = _limitation_notices(trace_events, tuple(delegation_views))
    artifact_source = _artifact_source(state.get("final_answer"))
    operation = next(
        (
            event.operation
            for event in reversed(trace_events)
            if event.kind is TraceKind.RUN and event.operation is not None
        ),
        None,
    )

    return DemoRunView(
        thread_id=_plain_text(thread_id, 128, omit_urls=True),
        operation=operation,
        workflow_status="completed" if state.get("final_answer") else "incomplete",
        goal=_plain_text(state.get("goal", ""), MAX_GOAL_CHARS, omit_urls=False),
        plan=plan_steps,
        budget=BudgetView(
            current_step=current_step,
            total_steps=len(plan_steps),
            step_tool_calls=_non_negative_int(state.get("tool_calls_in_current_step")),
            total_tool_calls=_non_negative_int(state.get("total_tool_calls")),
            max_step_tool_calls=limits.max_tool_calls_per_step,
            max_total_tool_calls=limits.max_total_tool_calls,
            replan_cycles=len(state.get("replans", ())),
            max_replan_cycles=limits.max_replan_cycles,
            max_delegation_concurrency=limits.max_delegation_concurrency,
        ),
        evidence=evidence_views,
        accepted_citations=accepted_citations,
        rejected_citation_count=sum(
            event.rejected_citation_count or 0
            for event in trace_events
            if event.kind is TraceKind.CITATION
        ),
        reviews=reviews,
        replans=replans,
        delegations=tuple(delegation_views),
        traces=trace_views,
        artifact=ArtifactView(
            label="Deterministic mentor report",
            available=bool(artifact_source),
            finding_summaries=safe_finding_summaries,
            citation_urls=accepted_citations,
            limitation_notices=limitations,
            markdown_source=artifact_source,
        ),
        limitations=limitations,
    )


def _step_status(
    step_number: int,
    current_step: int,
    total_steps: int,
) -> PlanStepStatus:
    if step_number <= current_step:
        return "completed"
    if current_step < total_steps and step_number == current_step + 1:
        return "current"
    return "pending"


def _plain_text(value: object, limit: int, *, omit_urls: bool) -> str:
    if not isinstance(value, str):
        return ""
    text = _CONTROL_CHARACTERS.sub(" ", value)
    if omit_urls:
        text = _MARKDOWN_LINK.sub(r"\1 [untrusted URL omitted]", text)
        text = _HTTP_URL.sub("[untrusted URL omitted]", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _non_negative_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _delegation_limitation(branch_id: str, status: str) -> str:
    safe_branch_id = _plain_text(branch_id, 32, omit_urls=True)
    if status == "cancelled":
        return f"Branch {safe_branch_id} was cancelled; no finding was promoted."
    return (
        f"Branch {safe_branch_id} ended with a controlled limitation; "
        "no finding was promoted."
    )


def _limitation_notices(
    traces: tuple[ExecutionTrace, ...],
    delegations: tuple[DelegationWaveView, ...],
) -> tuple[str, ...]:
    notices = [
        (
            "Local deterministic learning demo; not production-ready and not "
            "DeerFlow upstream parity."
        )
    ]
    if any(
        wave.failed_branch_count or wave.cancelled_branch_count for wave in delegations
    ):
        notices.append(
            "Delegation had a partial failure; only successful fan-in findings "
            "are displayed."
        )
    if any(
        event.kind is TraceKind.CONTEXT_BUDGET and event.outcome.value == "compacted"
        for event in traces
    ):
        notices.append("Runtime context was compacted within configured bounds.")
    return tuple(
        _plain_text(notice, MAX_LIMITATION_CHARS, omit_urls=True) for notice in notices
    )


def _artifact_source(final_answer: object) -> str:
    if not isinstance(final_answer, str):
        return ""
    # This must be byte-for-character identical to the runtime's already-safe
    # deterministic artifact.  The renderer displays it only inside ``st.code``.
    return final_answer
