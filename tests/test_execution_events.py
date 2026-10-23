import asyncio
from collections import defaultdict, deque
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mini_deerflow.actions import AgentAction, CompleteStepAction, ToolCallAction
from mini_deerflow.decision import ActionContext
from mini_deerflow.delegation import BoundedResearcherSubagent, DelegateResearchTool
from mini_deerflow.runtime import build_agent_runtime
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import AgentState
from mini_deerflow.tools import ToolInput, ToolRegistry, ToolResult
from mini_deerflow.tracing import (
    MAX_EVENT_METADATA_TEXT_CHARS,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    ExecutionTracer,
    InMemoryTraceSink,
)


class SearchInput(ToolInput):
    query: str


class CoordinatedSearchTool:
    name = "web_search"
    description = "Return deterministic branch evidence after both branches start."
    input_model = SearchInput
    timeout_seconds = 1.0
    idempotent = True

    def __init__(self, *, failing_branch: str | None = None) -> None:
        self.failing_branch = failing_branch
        self.started: list[str] = []
        self.both_reached = False
        self._release = asyncio.Event()

    async def run(self, tool_input: ToolInput) -> ToolResult:
        parsed = SearchInput.model_validate(tool_input)
        branch_id = parsed.query
        self.started.append(branch_id)
        if len(self.started) == 2:
            self.both_reached = True
            self._release.set()
        await asyncio.wait_for(self._release.wait(), timeout=0.5)
        if branch_id == self.failing_branch:
            return ToolResult.fail("Controlled branch provider failure.")
        return ToolResult.ok(
            data={
                "results": [
                    {
                        "url": f"https://example.com/{branch_id}",
                        "title": f"{branch_id.title()} source",
                        "snippet": f"Evidence collected by {branch_id} branch.",
                    }
                ]
            }
        )


class BranchSelector:
    def __init__(self) -> None:
        self.calls: defaultdict[str, int] = defaultdict(int)

    async def select_action(self, context: ActionContext) -> AgentAction:
        branch_id = context.step.title.rsplit(" ", 1)[-1]
        self.calls[branch_id] += 1
        if self.calls[branch_id] == 1:
            return ToolCallAction(
                type="tool_call",
                tool_name="web_search",
                arguments={"query": branch_id},
            )
        return CompleteStepAction(
            type="complete_step",
            summary=f"Completed bounded research for branch {branch_id}.",
            sources=[f"https://example.com/{branch_id}"],
        )


class ParentSelector:
    def __init__(self, *, include_beta_source: bool = True) -> None:
        sources = ["https://example.com/alpha"]
        if include_beta_source:
            sources.append("https://example.com/beta")
        self.actions: deque[AgentAction] = deque(
            [
                ToolCallAction(
                    type="tool_call",
                    tool_name="delegate_research",
                    arguments={
                        "delegation_id": "wave-events",
                        "tasks": [
                            {
                                "branch_id": branch_id,
                                "objective": (
                                    f"Research independently for branch {branch_id}."
                                ),
                                "success_criteria": (
                                    "Return one bounded and citable evidence result."
                                ),
                                "tool_call_budget": 2,
                                "delegation_depth": 1,
                            }
                            for branch_id in ("beta", "alpha")
                        ],
                    },
                ),
                CompleteStepAction(
                    type="complete_step",
                    summary="Completed deterministic parent fan-in safely.",
                    sources=sources,
                ),
                CompleteStepAction(
                    type="complete_step",
                    summary="Completed the second bounded parent step safely.",
                    sources=[],
                ),
                CompleteStepAction(
                    type="complete_step",
                    summary="Completed the third bounded parent step safely.",
                    sources=[],
                ),
            ]
        )

    async def select_action(self, context: ActionContext) -> AgentAction:
        del context
        return self.actions.popleft()


def three_step_plan(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=number,
                title=f"Research step {number}",
                objective=f"Complete bounded research objective number {number}.",
                success_criteria="The bounded parent step is integrated safely.",
            )
            for number in range(1, 4)
        ],
    )


def run_delegation(
    *, failing_branch: str | None = None
) -> tuple[AgentState, InMemoryTraceSink, CoordinatedSearchTool]:
    search_tool = CoordinatedSearchTool(failing_branch=failing_branch)
    branch_registry = ToolRegistry([search_tool])
    researcher = BoundedResearcherSubagent(BranchSelector(), branch_registry)
    delegation_tool = DelegateResearchTool(
        researcher,
        branch_registry,
        max_concurrency=2,
    )
    sink = InMemoryTraceSink()
    event_ids = iter(range(1, 10_000))
    tracer = ExecutionTracer(
        sink,
        run_id_factory=lambda: "parent-run",
        event_id_factory=lambda: f"event-{next(event_ids)}",
    )
    runtime = build_agent_runtime(
        three_step_plan,
        ParentSelector(include_beta_source=failing_branch != "beta"),
        ToolRegistry([delegation_tool]),
        action_registry=ToolRegistry([delegation_tool]),
        tracer=tracer,
    )
    state = asyncio.run(
        runtime.run("Compare alpha and beta evidence.", thread_id="event-thread")
    )
    return state, sink, search_tool


def test_execution_event_validates_type_identity_timestamp_and_safe_metadata() -> None:
    event = ExecutionEvent(
        event_type=ExecutionEventType.ROUTE_SELECTED,
        run_id="run-1",
        thread_id="thread-1",
        sequence=1,
        timestamp=datetime(2026, 10, 6, tzinfo=UTC),
        metadata={
            "from_node": "decide_action",
            "to_node": "execute_tool",
            "route_reason": "x" * 500,
            "Authorization": "Bearer top-secret",
            "decision_type": "Bearer top-secret",
            "raw_output": {"secret": "never retain this"},
        },
    )

    assert event.status is ExecutionStatus.SELECTED
    assert event.root_run_id == "run-1"
    assert event.kind.value == "route"
    assert len(event.metadata["route_reason"]) == MAX_EVENT_METADATA_TEXT_CHARS
    assert event.metadata["decision_type"] == "[redacted]"
    assert "Authorization" not in event.metadata
    assert "raw_output" not in event.metadata
    assert "top-secret" not in event.model_dump_json()

    with pytest.raises(ValidationError):
        ExecutionEvent(
            event_type="unknown_event",
            run_id="run-1",
            thread_id="thread-1",
            sequence=1,
        )
    with pytest.raises(ValidationError):
        ExecutionEvent(
            event_type=ExecutionEventType.RUN_STARTED,
            run_id=" ",
            thread_id="thread-1",
            sequence=1,
        )
    with pytest.raises(ValidationError):
        ExecutionEvent(
            event_type=ExecutionEventType.RUN_STARTED,
            run_id="run-1",
            thread_id="thread-1",
            sequence=1,
            timestamp=datetime(2026, 10, 6, tzinfo=UTC).replace(tzinfo=None),
        )


def test_event_sink_failure_is_best_effort_and_does_not_break_execution() -> None:
    class FailingSink:
        def emit(self, event: ExecutionEvent) -> None:
            del event
            raise RuntimeError("sink unavailable")

    tracer = ExecutionTracer(
        FailingSink(),
        run_id_factory=lambda: "best-effort-run",
    )
    with tracer.run_scope("best-effort-thread", "run") as run_id:
        tracer.emit_event(ExecutionEventType.CHECKPOINT_OBSERVED)

    events = tracer.events_for_run(run_id)
    assert [event.event_type for event in events] == [
        ExecutionEventType.RUN_STARTED,
        ExecutionEventType.CHECKPOINT_OBSERVED,
        ExecutionEventType.RUN_COMPLETED,
    ]


def test_real_delegation_events_prove_hierarchy_child_tools_and_concurrency() -> None:
    state, sink, search_tool = run_delegation()
    events = sink.events

    assert search_tool.both_reached is True
    assert sorted(search_tool.started) == ["alpha", "beta"]
    assert [record.provenance.branch_id for record in state["evidence"]] == [
        "alpha",
        "beta",
    ]
    assert all(
        record.provenance.delegation_id == "wave-events" for record in state["evidence"]
    )

    delegation_started = next(
        event
        for event in events
        if event.event_type is ExecutionEventType.DELEGATION_STARTED
    )
    branch_starts = {
        event.branch_id: event
        for event in events
        if event.event_type is ExecutionEventType.BRANCH_STARTED
    }
    assert set(branch_starts) == {"alpha", "beta"}
    assert {event.run_id for event in branch_starts.values()} == {
        "branch:parent-run:wave-events:alpha",
        "branch:parent-run:wave-events:beta",
    }
    assert all(event.parent_run_id == "parent-run" for event in branch_starts.values())
    assert all(event.root_run_id == "parent-run" for event in branch_starts.values())

    for branch_id, started in branch_starts.items():
        branch_events = [event for event in events if event.branch_id == branch_id]
        child_started = next(
            event
            for event in branch_events
            if event.event_type is ExecutionEventType.TOOL_STARTED
        )
        child_completed = next(
            event
            for event in branch_events
            if event.event_type is ExecutionEventType.TOOL_COMPLETED
        )
        evidence = next(
            event
            for event in branch_events
            if event.event_type is ExecutionEventType.EVIDENCE_PRODUCED
        )
        completed = next(
            event
            for event in branch_events
            if event.event_type is ExecutionEventType.BRANCH_COMPLETED
        )
        assert child_started.delegation_id == "wave-events"
        assert child_started.run_id == started.run_id
        assert child_started.tool_call_id == f"tool:{started.run_id}:1"
        assert child_completed.tool_call_id == child_started.tool_call_id
        assert evidence.tool_call_id == child_started.tool_call_id
        assert started.sequence < child_started.sequence
        assert child_started.sequence < child_completed.sequence
        assert child_completed.sequence < evidence.sequence < completed.sequence

    fan_in = next(
        event
        for event in events
        if event.event_type is ExecutionEventType.FAN_IN_COMPLETED
    )
    delegation_completed = next(
        event
        for event in events
        if event.event_type is ExecutionEventType.DELEGATION_COMPLETED
    )
    parent_tool_completed = next(
        event
        for event in events
        if event.event_type is ExecutionEventType.TOOL_COMPLETED
        and event.run_id == "parent-run"
        and event.tool_name == "delegate_research"
    )
    execute_node_completed = next(
        event
        for event in events
        if event.event_type is ExecutionEventType.NODE_COMPLETED
        and event.node_id == "execute_tool"
    )
    parent_continued = next(
        event
        for event in events
        if event.event_type is ExecutionEventType.NODE_STARTED
        and event.node_id == "decide_action"
        and event.sequence > execute_node_completed.sequence
    )
    assert delegation_started.sequence < min(
        event.sequence for event in branch_starts.values()
    )
    assert (
        max(
            event.sequence
            for event in events
            if event.event_type
            in {ExecutionEventType.BRANCH_COMPLETED, ExecutionEventType.BRANCH_FAILED}
        )
        < fan_in.sequence
    )
    assert fan_in.metadata["evidence_promoted"] == 2
    assert delegation_completed.metadata["successful_branches"] == 2
    assert (
        fan_in.sequence < delegation_completed.sequence < parent_tool_completed.sequence
    )
    assert parent_tool_completed.sequence < execute_node_completed.sequence
    assert parent_continued.sequence > execute_node_completed.sequence

    selected_routes = {
        (event.metadata["from_node"], event.metadata["to_node"])
        for event in events
        if event.event_type is ExecutionEventType.ROUTE_SELECTED
    }
    assert ("decide_action", "execute_tool") in selected_routes
    assert ("decide_action", "complete_step") in selected_routes


def test_partial_failure_keeps_parent_running_and_is_structurally_observable() -> None:
    state, sink, _ = run_delegation(failing_branch="beta")
    events = sink.events

    assert [record.provenance.branch_id for record in state["evidence"]] == ["alpha"]
    assert state["completion_status"] == "complete"
    assert any(
        event.event_type is ExecutionEventType.BRANCH_COMPLETED
        and event.branch_id == "alpha"
        for event in events
    )
    failed = next(
        event
        for event in events
        if event.event_type is ExecutionEventType.BRANCH_FAILED
        and event.branch_id == "beta"
    )
    assert failed.metadata["failure_category"] == "controlled_failure"
    assert any(
        event.event_type is ExecutionEventType.TOOL_FAILED and event.branch_id == "beta"
        for event in events
    )
    completed = next(
        event
        for event in events
        if event.event_type is ExecutionEventType.DELEGATION_COMPLETED
    )
    assert completed.status is ExecutionStatus.PARTIAL
    assert completed.metadata["successful_branches"] == 1
    assert completed.metadata["failed_branches"] == 1
    assert any(event.event_type is ExecutionEventType.RUN_COMPLETED for event in events)


def test_branch_timeout_emits_cancelled_child_tool_and_branch_events() -> None:
    class BlockingSearchTool:
        name = "web_search"
        description = "Block until the enclosing branch timeout cancels execution."
        input_model = SearchInput
        timeout_seconds = 1.0
        idempotent = True

        async def run(self, tool_input: ToolInput) -> ToolResult:
            SearchInput.model_validate(tool_input)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    branch_registry = ToolRegistry([BlockingSearchTool()])
    delegation_tool = DelegateResearchTool(
        BoundedResearcherSubagent(BranchSelector(), branch_registry),
        branch_registry,
        branch_timeout_seconds=0.01,
        max_concurrency=2,
    )
    sink = InMemoryTraceSink()
    tracer = ExecutionTracer(sink, run_id_factory=lambda: "timeout-run")

    with tracer.run_scope("timeout-thread", "run"):
        result = asyncio.run(
            delegation_tool.run_with_parent_budget(
                {
                    "delegation_id": "timeout-delegation",
                    "tasks": [
                        {
                            "branch_id": branch_id,
                            "objective": f"Exercise timeout for {branch_id} branch.",
                            "success_criteria": "The timeout is structurally observable.",
                            "tool_call_budget": 1,
                            "delegation_depth": 1,
                        }
                        for branch_id in ("alpha", "beta")
                    ],
                },
                parent_step_number=1,
                remaining_tool_calls=3,
            )
        )

    assert result.success is True
    child_failures = [
        event
        for event in sink.events
        if event.event_type is ExecutionEventType.TOOL_FAILED
        and event.parent_run_id == "timeout-run"
    ]
    branch_failures = [
        event
        for event in sink.events
        if event.event_type is ExecutionEventType.BRANCH_FAILED
    ]
    assert len(child_failures) == 2
    assert len(branch_failures) == 2
    assert all(event.status is ExecutionStatus.CANCELLED for event in child_failures)
    assert all(event.status is ExecutionStatus.CANCELLED for event in branch_failures)
    assert all(
        event.metadata["failure_category"] == "timeout" for event in branch_failures
    )


def test_node_failure_emits_node_and_run_failures_without_hiding_error() -> None:
    def failing_planner(goal: str) -> Plan:
        del goal
        raise RuntimeError("planner exploded")

    sink = InMemoryTraceSink()
    runtime = build_agent_runtime(
        failing_planner,
        ParentSelector(),
        ToolRegistry(),
        tracer=ExecutionTracer(sink, run_id_factory=lambda: "failed-run"),
    )

    with pytest.raises(RuntimeError, match="planner exploded"):
        asyncio.run(runtime.run("Fail predictably.", thread_id="failed-thread"))

    assert any(
        event.event_type is ExecutionEventType.NODE_FAILED
        and event.node_id == "planner"
        for event in sink.events
    )
    assert sink.events[-1].event_type is ExecutionEventType.RUN_FAILED
