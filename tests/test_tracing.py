import asyncio
import io
import json
from collections import deque
from pathlib import Path

import pytest
from pydantic import ValidationError

from mini_deerflow.actions import (
    AgentAction,
    CompleteStepAction,
    ToolCallAction,
    ToolObservation,
)
from mini_deerflow.context_budget import ContextBudget, ContextBudgetExceededError
from mini_deerflow.decision import ActionContext
from mini_deerflow.delegation import (
    BranchFinding,
    BranchResult,
    DelegateResearchTool,
    ResearchTaskContext,
)
from mini_deerflow.review import ReplacementWork, ReviewVerdict
from mini_deerflow.runtime import build_agent_runtime
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.tools import (
    ToolRegistry,
    ToolResult,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
)
from mini_deerflow.tracing import (
    ExecutionTrace,
    ExecutionTracer,
    InMemoryTraceSink,
    JsonLinesTraceSink,
    TraceKind,
    TraceOutcome,
    TracePhase,
)
from mini_deerflow.web import (
    FetchedPage,
    SearchResult,
    WebProviderErrorCategory,
    WebSearchError,
)
from mini_deerflow.web_safety import PublicWebTargetValidator, SafeWebTarget
from mini_deerflow.workspace import Workspace


class CompletingSelector:
    async def select_action(self, context: ActionContext) -> CompleteStepAction:
        return CompleteStepAction(
            type="complete_step",
            summary=f"Completed bounded step {context.step.step_number}.",
            sources=[],
        )


class QueueSelector:
    def __init__(self, actions: list[AgentAction]) -> None:
        self.actions = deque(actions)
        self.call_count = 0

    async def select_action(self, context: ActionContext) -> AgentAction:
        del context
        self.call_count += 1
        return self.actions.popleft()


class PublicResolver:
    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return ("93.184.216.34",)


class TickingClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += 0.001
        return current


def plan(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=number,
                title=f"Step {number}",
                objective=f"Complete bounded objective number {number}.",
                success_criteria="The bounded objective is complete.",
            )
            for number in range(1, 4)
        ],
    )


def test_trace_schema_rejects_uncontrolled_payload_fields() -> None:
    with pytest.raises(ValidationError):
        ExecutionTrace(
            run_id="run-1",
            thread_id="thread-1",
            sequence=1,
            kind=TraceKind.RUN,
            phase=TracePhase.START,
            outcome=TraceOutcome.STARTED,
            response_body="raw secret body",
        )


def test_runtime_emits_typed_ordered_redacted_trace() -> None:
    sink = InMemoryTraceSink()
    tracer = ExecutionTracer(
        sink,
        clock=TickingClock(),
        run_id_factory=lambda: "deterministic-run",
    )
    runtime = build_agent_runtime(
        plan,
        CompletingSelector(),
        ToolRegistry(),
        tracer=tracer,
    )

    state = asyncio.run(
        runtime.run(
            "Goal containing bearer-secret and untrusted payload.",
            thread_id="trace-thread",
        )
    )

    assert state["final_answer"] is not None
    assert [event.sequence for event in sink.events] == list(
        range(1, len(sink.events) + 1)
    )
    assert {event.run_id for event in sink.events} == {"deterministic-run"}
    assert {event.thread_id for event in sink.events} == {"trace-thread"}
    assert sink.events[0].kind is TraceKind.RUN
    assert sink.events[0].outcome is TraceOutcome.STARTED
    assert sink.events[-1].kind is TraceKind.RUN
    assert sink.events[-1].outcome is TraceOutcome.SUCCEEDED
    assert any(event.kind is TraceKind.NODE for event in sink.events)
    assert any(event.kind is TraceKind.CITATION for event in sink.events)
    assert any(event.kind is TraceKind.ARTIFACT for event in sink.events)
    rendered = "\n".join(event.model_dump_json() for event in sink.events)
    assert "bearer-secret" not in rendered
    assert "untrusted payload" not in rendered


def test_json_lines_sink_emits_one_valid_object_per_event() -> None:
    stream = io.StringIO()
    tracer = ExecutionTracer(
        JsonLinesTraceSink(stream),
        run_id_factory=lambda: "run-jsonl",
    )

    with tracer.run_scope("thread-jsonl", "run"):
        tracer.emit(
            kind=TraceKind.CHECKPOINT,
            phase=TracePhase.OUTCOME,
            outcome=TraceOutcome.SKIPPED,
            operation="run",
        )

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(records) == 3
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert all(record["thread_id"] == "thread-jsonl" for record in records)


def test_safe_evidence_citation_and_artifact_outcomes_are_traced(
    tmp_path: Path,
) -> None:
    class SearchProvider:
        async def search(
            self,
            query: str,
            *,
            max_results: int,
        ) -> list[SearchResult]:
            del query, max_results
            return [
                SearchResult(
                    title="Public source",
                    url="https://example.com/source",
                    snippet="Bounded evidence text.",
                )
            ]

    source = "https://example.com/source"
    selector = QueueSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="web_search",
                arguments={"query": "bounded evidence", "max_results": 1},
            ),
            CompleteStepAction(
                type="complete_step",
                summary="Completed the evidence-backed first step.",
                sources=[source],
            ),
            CompleteStepAction(
                type="complete_step",
                summary="Completed the evidence-backed second step.",
                sources=[source],
            ),
            CompleteStepAction(
                type="complete_step",
                summary="Completed the evidence-backed third step.",
                sources=[source],
            ),
        ]
    )
    search_tool = WebSearchTool(SearchProvider())
    workspace = Workspace(tmp_path / "workspace")
    sink = InMemoryTraceSink()
    tracer = ExecutionTracer(sink, run_id_factory=lambda: "safe-run")
    runtime = build_agent_runtime(
        plan,
        selector,
        ToolRegistry([search_tool, WriteFileTool(workspace)]),
        action_registry=ToolRegistry([search_tool]),
        artifact_path="reports/evaluation.md",
        tracer=tracer,
    )

    state = asyncio.run(
        runtime.run("Evaluate the safe evidence path.", thread_id="safe")
    )

    assert [record.canonical_url for record in state["evidence"]] == [source]
    assert state["sources"] == [source]
    assert state["artifact_path"] == "reports/evaluation.md"
    assert workspace.read_text("reports/evaluation.md") == state["final_answer"]
    assert any(
        event.kind is TraceKind.TOOL and event.outcome is TraceOutcome.SUCCEEDED
        for event in sink.events
    )
    assert any(
        event.kind is TraceKind.CITATION and event.outcome is TraceOutcome.ACCEPTED
        for event in sink.events
    )
    assert any(
        event.kind is TraceKind.ARTIFACT and event.outcome is TraceOutcome.WRITTEN
        for event in sink.events
    )


def test_invented_citation_rejection_is_traced_and_not_rendered() -> None:
    invented = "https://invented.invalid/not-evidence"
    selector = QueueSelector(
        [
            CompleteStepAction(
                type="complete_step",
                summary="Completed a step with an unsupported citation.",
                sources=[invented],
            ),
            CompleteStepAction(
                type="complete_step",
                summary="Completed the second bounded step safely.",
                sources=[],
            ),
            CompleteStepAction(
                type="complete_step",
                summary="Completed the third bounded step safely.",
                sources=[],
            ),
        ]
    )
    sink = InMemoryTraceSink()
    runtime = build_agent_runtime(
        plan,
        selector,
        ToolRegistry(),
        tracer=ExecutionTracer(sink, run_id_factory=lambda: "citation-run"),
    )

    state = asyncio.run(
        runtime.run("Evaluate citation rejection.", thread_id="citation")
    )

    assert state["sources"] == []
    assert invented not in state["final_answer"]
    rejection = next(
        event
        for event in sink.events
        if event.kind is TraceKind.CITATION and event.outcome is TraceOutcome.REJECTED
    )
    assert rejection.rejected_citation_count == 1
    assert rejection.citation_count == 0


def test_unsafe_url_denial_has_a_distinct_redacted_trace() -> None:
    class TrackingFetchProvider:
        call_count = 0

        async def fetch(self, target: SafeWebTarget) -> FetchedPage:
            self.call_count += 1
            return FetchedPage(
                url=target.url,
                content="private body",
                status_code=200,
            )

    provider = TrackingFetchProvider()
    fetch_tool = WebFetchTool(
        provider,
        PublicWebTargetValidator(PublicResolver()),
    )
    selector = QueueSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="web_fetch",
                arguments={"url": "http://127.0.0.1/admin"},
            ),
            *[
                CompleteStepAction(
                    type="complete_step",
                    summary=f"Completed safe fallback step {number}.",
                    sources=[],
                )
                for number in range(1, 4)
            ],
        ]
    )
    sink = InMemoryTraceSink()
    runtime = build_agent_runtime(
        plan,
        selector,
        ToolRegistry([fetch_tool]),
        tracer=ExecutionTracer(sink, run_id_factory=lambda: "unsafe-run"),
    )

    state = asyncio.run(runtime.run("Evaluate unsafe URL denial.", thread_id="unsafe"))

    assert provider.call_count == 0
    safety_event = next(
        event
        for event in sink.events
        if event.kind is TraceKind.TOOL and event.outcome is TraceOutcome.SAFETY_DENIED
    )
    assert safety_event.error_category.value == "safety"
    assert "127.0.0.1" not in safety_event.model_dump_json()
    assert "private body" not in state["final_answer"]


def test_provider_failure_has_a_distinct_redacted_trace() -> None:
    class FailingSearchProvider:
        async def search(
            self,
            query: str,
            *,
            max_results: int,
        ) -> list[SearchResult]:
            del query, max_results
            raise WebSearchError(
                "Bearer private-key raw-response-body",
                category=WebProviderErrorCategory.AUTHENTICATION,
            )

    failing_tool = WebSearchTool(FailingSearchProvider())
    failing_selector = QueueSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="web_search",
                arguments={"query": "provider failure", "max_results": 1},
            ),
            *[
                CompleteStepAction(
                    type="complete_step",
                    summary=f"Completed provider fallback step {number}.",
                    sources=[],
                )
                for number in range(1, 4)
            ],
        ]
    )
    failure_sink = InMemoryTraceSink()
    failure_runtime = build_agent_runtime(
        plan,
        failing_selector,
        ToolRegistry([failing_tool]),
        tracer=ExecutionTracer(
            failure_sink,
            run_id_factory=lambda: "provider-run",
        ),
    )

    failure_state = asyncio.run(
        failure_runtime.run("Evaluate provider failure.", thread_id="provider")
    )

    provider_event = next(
        event
        for event in failure_sink.events
        if event.kind is TraceKind.TOOL
        and event.outcome is TraceOutcome.PROVIDER_FAILED
    )
    assert provider_event.error_category.value == "provider"
    rendered = " ".join(
        [
            provider_event.model_dump_json(),
            failure_state["tool_observations"][0].result.model_dump_json(),
        ]
    )
    assert "private-key" not in rendered
    assert "raw-response-body" not in rendered


def test_context_budget_refusal_is_traced_before_selector_invocation() -> None:
    selector = CompletingSelector()
    sink = InMemoryTraceSink()
    runtime = build_agent_runtime(
        plan,
        selector,
        ToolRegistry(),
        context_budget=ContextBudget(
            max_total_chars=1_026,
            max_item_chars=1,
            max_excerpt_chars=1,
            retained_recent_items=1,
        ),
        tracer=ExecutionTracer(sink, run_id_factory=lambda: "refusal-run"),
    )

    with pytest.raises(ContextBudgetExceededError):
        asyncio.run(runtime.run("G" * 1_000, thread_id="context-refusal"))

    refused = [
        event
        for event in sink.events
        if event.outcome is TraceOutcome.REFUSED
        and event.error_category is not None
        and event.error_category.value == "context_budget"
    ]
    assert refused


def test_context_compaction_is_distinct_from_refusal_in_trace() -> None:
    class LargeSearchProvider:
        async def search(
            self,
            query: str,
            *,
            max_results: int,
        ) -> list[SearchResult]:
            del query
            return [
                SearchResult(
                    title=f"Source {index}",
                    url=f"https://example.com/{index}",
                    snippet="E" * 4_000,
                )
                for index in range(max_results)
            ]

    search_tool = WebSearchTool(LargeSearchProvider())
    selector = QueueSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="web_search",
                arguments={"query": "large bounded evidence", "max_results": 10},
            ),
            *[
                CompleteStepAction(
                    type="complete_step",
                    summary=f"Completed compacted context step {number}.",
                    sources=[],
                )
                for number in range(1, 4)
            ],
        ]
    )
    sink = InMemoryTraceSink()
    runtime = build_agent_runtime(
        plan,
        selector,
        ToolRegistry([search_tool]),
        context_budget=ContextBudget(
            max_total_chars=20_000,
            max_item_chars=800,
            retained_recent_items=5,
            max_excerpt_chars=600,
        ),
        tracer=ExecutionTracer(sink, run_id_factory=lambda: "compaction-run"),
    )

    state = asyncio.run(
        runtime.run("Evaluate deterministic compaction.", thread_id="compaction")
    )

    assert state["final_answer"] is not None
    compaction = [
        event
        for event in sink.events
        if event.kind is TraceKind.CONTEXT_BUDGET
        and event.outcome is TraceOutcome.COMPACTED
    ]
    assert compaction
    assert all(event.error_category is None for event in compaction)


def test_reviewer_replan_and_finish_routes_are_traced() -> None:
    class Reviewer:
        def __init__(self) -> None:
            self.verdicts = deque(
                [
                    ReviewVerdict(
                        verdict="replan",
                        rationale="Replace the remaining bounded work.",
                        findings=[],
                    ),
                    ReviewVerdict(
                        verdict="finish",
                        rationale="The deterministic evaluation can finish.",
                        findings=[],
                    ),
                ]
            )

        async def review_evidence(self, context: object) -> ReviewVerdict:
            del context
            return self.verdicts.popleft()

    selector = QueueSelector(
        [
            CompleteStepAction(
                type="complete_step",
                summary="Completed the original first step safely.",
                sources=[],
            ),
            CompleteStepAction(
                type="complete_step",
                summary="Completed the revised second step safely.",
                sources=[],
            ),
        ]
    )
    replacement = ReplacementWork(
        steps=[
            PlanStep(
                step_number=number,
                title=f"Revised {number}",
                objective=f"Complete revised bounded objective {number}.",
                success_criteria="The revised evidence gap is addressed.",
            )
            for number in range(1, 3)
        ]
    )
    sink = InMemoryTraceSink()
    runtime = build_agent_runtime(
        plan,
        selector,
        ToolRegistry(),
        reviewer=Reviewer(),
        replanner=lambda request: replacement,
        tracer=ExecutionTracer(sink, run_id_factory=lambda: "review-run"),
    )

    state = asyncio.run(runtime.run("Evaluate reviewer routing.", thread_id="review"))

    assert len(state["replans"]) == 1
    review_routes = [
        event.outcome for event in sink.events if event.kind is TraceKind.REVIEW
    ]
    assert review_routes == [TraceOutcome.REPLAN, TraceOutcome.FINISH]
    assert any(event.kind is TraceKind.REPLAN for event in sink.events)


def test_delegation_partial_failure_and_fan_in_are_traced() -> None:
    source = "https://example.com/delegated"

    class Researcher:
        async def research(self, context: ResearchTaskContext) -> BranchResult:
            branch_id = context.task.branch_id
            if branch_id == "beta":
                return BranchResult(
                    branch_id=branch_id,
                    status="controlled_failure",
                    error="Controlled branch limitation.",
                )
            action = ToolCallAction(
                type="tool_call",
                tool_name="web_search",
                arguments={"query": "delegated evidence"},
            )
            observation = ToolObservation(
                step_number=1,
                step_tool_call_number=1,
                total_tool_call_number=1,
                branch_id=branch_id,
                branch_tool_call_number=1,
                action=action,
                result=ToolResult.ok(
                    data={
                        "results": [
                            {
                                "url": source,
                                "title": "Delegated source",
                                "snippet": "Bounded delegated evidence.",
                            }
                        ]
                    }
                ),
            )
            return BranchResult(
                branch_id=branch_id,
                status="success",
                observations=[observation],
                finding=BranchFinding(
                    summary="Successful delegated evidence finding.",
                    citations=[source],
                ),
                tool_calls_used=1,
            )

    class UnusedSearchProvider:
        async def search(
            self,
            query: str,
            *,
            max_results: int,
        ) -> list[SearchResult]:
            raise AssertionError((query, max_results))

    branch_registry = ToolRegistry([WebSearchTool(UnusedSearchProvider())])
    delegation_tool = DelegateResearchTool(
        Researcher(),
        branch_registry,
        max_concurrency=2,
    )
    selector = QueueSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="delegate_research",
                arguments={
                    "delegation_id": "eval-wave",
                    "tasks": [
                        {
                            "branch_id": branch_id,
                            "objective": f"Research {branch_id} evidence independently.",
                            "success_criteria": "Return a bounded branch outcome.",
                            "tool_call_budget": 1,
                            "delegation_depth": 1,
                        }
                        for branch_id in ("beta", "alpha")
                    ],
                },
            ),
            CompleteStepAction(
                type="complete_step",
                summary="Completed parent fan-in with partial evidence.",
                sources=[source],
            ),
            CompleteStepAction(
                type="complete_step",
                summary="Completed the second parent step safely.",
                sources=[],
            ),
            CompleteStepAction(
                type="complete_step",
                summary="Completed the third parent step safely.",
                sources=[],
            ),
        ]
    )
    sink = InMemoryTraceSink()
    runtime = build_agent_runtime(
        plan,
        selector,
        ToolRegistry([delegation_tool]),
        action_registry=ToolRegistry([delegation_tool]),
        tracer=ExecutionTracer(sink, run_id_factory=lambda: "delegation-run"),
    )

    state = asyncio.run(
        runtime.run("Evaluate deterministic fan-in.", thread_id="delegation")
    )

    assert [record.canonical_url for record in state["evidence"]] == [source]
    assert state["sources"] == [source]
    assert any("Controlled branch limitation" in error for error in state["errors"])
    event = next(event for event in sink.events if event.kind is TraceKind.DELEGATION)
    assert event.outcome is TraceOutcome.PARTIAL_FAILURE
    assert event.successful_branch_count == 1
    assert event.failed_branch_count == 1
    assert event.cancelled_branch_count == 0
