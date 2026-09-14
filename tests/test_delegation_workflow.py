import asyncio
from collections import deque
from pathlib import Path

from mini_deerflow.actions import (
    AgentAction,
    CompleteStepAction,
    ToolCallAction,
    ToolObservation,
)
from mini_deerflow.agent_workflow import build_agent_workflow
from mini_deerflow.decision import ActionContext
from mini_deerflow.delegation import (
    BranchFinding,
    BranchResult,
    DelegateResearchTool,
    ResearchTaskContext,
)
from mini_deerflow.persistence import open_sqlite_checkpointer
from mini_deerflow.runtime import AgentRuntime, RuntimeLimits
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import AgentState
from mini_deerflow.tools import ToolInput, ToolRegistry, ToolResult


class QueryInput(ToolInput):
    query: str


class NeverCalledWebTool:
    name = "web_search"
    description = "Web search seam used only to define branch capability."
    input_model = QueryInput
    timeout_seconds = 1.0
    idempotent = True

    async def run(self, tool_input: ToolInput) -> ToolResult:
        raise AssertionError(f"Unexpected provider call: {tool_input}")


class CheckpointFakeResearcher:
    def __init__(self) -> None:
        self.branch_calls: list[str] = []
        self.contexts: list[ResearchTaskContext] = []

    async def research(self, context: ResearchTaskContext) -> BranchResult:
        branch_id = context.task.branch_id
        self.branch_calls.append(branch_id)
        self.contexts.append(context)
        source = "https://example.com/shared"
        action = ToolCallAction(
            action_type="tool_call",
            tool_name="web_search",
            arguments={"query": branch_id},
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
                            "title": f"Source {branch_id}",
                            "snippet": "Untrusted branch evidence text.",
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
                summary=(
                    "Branch claim with https://invented.invalid/not-evidence "
                    "and an attempted artifact path reports/branch.md."
                ),
                citations=[source, "https://invented.invalid/not-evidence"],
            ),
            tool_calls_used=1,
        )


class QueuedSelector:
    def __init__(self) -> None:
        self.actions: deque[AgentAction] = deque(
            [
                ToolCallAction(
                    action_type="tool_call",
                    tool_name="delegate_research",
                    arguments={
                        "delegation_id": "wave-one",
                        "tasks": [
                            {
                                "branch_id": "beta",
                                "objective": "Research the beta side independently.",
                                "success_criteria": "Return one evidence-backed beta finding.",
                                "tool_call_budget": 1,
                                "delegation_depth": 1,
                            },
                            {
                                "branch_id": "alpha",
                                "objective": "Research the alpha side independently.",
                                "success_criteria": "Return one evidence-backed alpha finding.",
                                "tool_call_budget": 1,
                                "delegation_depth": 1,
                            },
                        ],
                    },
                ),
                CompleteStepAction(
                    action_type="complete_step",
                    summary="Parent completes from merged delegated evidence.",
                    sources=[
                        "https://example.com/shared",
                        "https://invented.invalid/not-evidence",
                    ],
                ),
                CompleteStepAction(
                    action_type="complete_step",
                    summary="No additional work is required for step two.",
                    sources=[],
                ),
                CompleteStepAction(
                    action_type="complete_step",
                    summary="No additional work is required for step three.",
                    sources=[],
                ),
            ]
        )
        self.contexts: list[ActionContext] = []

    async def select_action(self, context: ActionContext) -> AgentAction:
        self.contexts.append(context)
        if not self.actions:
            raise AssertionError("Completed delegation was dispatched again")
        return self.actions.popleft()


def planner(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=1,
                title="Delegated comparison",
                objective="Collect two independent views.",
                success_criteria="Merge evidence and complete the comparison.",
            ),
            PlanStep(
                step_number=2,
                title="Consolidate delegated evidence",
                objective="Confirm the deterministic fan-in result.",
                success_criteria="The merged result remains internally consistent.",
            ),
            PlanStep(
                step_number=3,
                title="Record limitations",
                objective="Record remaining bounded research limitations.",
                success_criteria="The final state exposes all known limitations.",
            ),
        ],
    )


async def run_and_resume(
    checkpoint_path: Path,
) -> tuple[AgentState, AgentState, object]:
    researcher = CheckpointFakeResearcher()
    branch_registry = ToolRegistry([NeverCalledWebTool()])
    delegation_tool = DelegateResearchTool(
        researcher,
        branch_registry,
        max_concurrency=2,
    )
    selector = QueuedSelector()

    async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
        graph = build_agent_workflow(
            planner,
            selector,
            ToolRegistry([delegation_tool]),
            action_registry=ToolRegistry([delegation_tool]),
            checkpointer=checkpointer,
            max_tool_calls_per_step=5,
            max_total_tool_calls=5,
        )
        runtime = AgentRuntime(
            graph=graph,
            limits=RuntimeLimits(
                max_tool_calls_per_step=5,
                max_total_tool_calls=5,
            ),
            checkpointer=checkpointer,
        )
        first = await runtime.run("Compare alpha and beta", thread_id="day12-thread")
        resumed = await runtime.resume(thread_id="day12-thread")

    return first, resumed, researcher


def test_workflow_fan_in_preserves_boundaries_and_resume_skips_completed_work(
    tmp_path: Path,
) -> None:
    first, resumed, researcher = asyncio.run(run_and_resume(tmp_path / "day12.sqlite"))

    assert researcher.branch_calls == ["alpha", "beta"]
    assert first == resumed
    assert first["total_tool_calls"] == 3
    assert len(first["tool_observations"]) == 3
    assert [item.branch_id for item in first["delegations"][0].results] == [
        "alpha",
        "beta",
    ]
    assert [record.url for record in first["evidence"]] == [
        "https://example.com/shared"
    ]
    assert first["sources"] == ["https://example.com/shared"]
    assert all(
        str(citation) == "https://example.com/shared"
        for finding in first["findings"]
        for citation in finding.citations
    )
    assert "https://invented.invalid/not-evidence" not in first["final_answer"]
    assert first["artifact_path"] is None
    assert first["pending_action"] is None
    assert first["errors"]
