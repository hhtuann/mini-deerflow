import asyncio
from collections import deque
from pathlib import Path

import pytest

from mini_deerflow.actions import (
    AgentAction,
    CompleteStepAction,
    ToolCallAction,
)
from mini_deerflow.agent_workflow import build_agent_workflow
from mini_deerflow.decision import ActionContext
from mini_deerflow.persistence import open_sqlite_checkpointer
from mini_deerflow.runtime import RuntimeLimits, build_agent_runtime
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import AgentState, create_initial_state
from mini_deerflow.tools import ToolRegistry, WebSearchTool, WriteFileTool
from mini_deerflow.web import SearchResult
from mini_deerflow.workspace import Workspace


class StaticSearchProvider:
    def __init__(self) -> None:
        self.call_count = 0

    async def search(
        self,
        query: str,
        *,
        max_results: int,
    ) -> list[SearchResult]:
        self.call_count += 1
        return [
            SearchResult(
                title="Source A",
                url="https://example.com/a#search",
                snippet=f"Evidence A for {query}",
            ),
            SearchResult(
                title="Duplicate A",
                url="https://EXAMPLE.com:443/a",
                snippet="Newer evidence A",
            ),
            SearchResult(
                title="Source B",
                url="https://example.com/b",
                snippet="Evidence B",
            ),
        ][:max_results]


class QueueSelector:
    def __init__(self, actions: list[AgentAction | BaseException]) -> None:
        self._actions = deque(actions)
        self.contexts: list[ActionContext] = []

    async def select_action(self, context: ActionContext) -> AgentAction:
        self.contexts.append(context)
        action = self._actions.popleft()

        if isinstance(action, BaseException):
            raise action

        return action


def plan(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=number,
                title=f"Step {number}",
                objective=f"Collect evidence for step {number}.",
                success_criteria=f"Step {number} has a traceable result.",
            )
            for number in range(1, 4)
        ],
    )


def completion(number: int, sources: list[str] | None = None) -> CompleteStepAction:
    return CompleteStepAction(
        type="complete_step",
        summary=f"Completed evidence-backed research step {number}.",
        sources=sources or [],
    )


def pipeline_actions() -> list[AgentAction]:
    return [
        ToolCallAction(
            type="tool_call",
            tool_name="web_search",
            arguments={"query": "traceable evidence", "max_results": 3},
        ),
        completion(1, ["https://example.com/a"]),
        completion(2, ["https://example.com/b"]),
        completion(3, ["https://example.com/invented"]),
    ]


def invoke_pipeline(
    workspace: Workspace,
    *,
    artifact_path: str | None,
    actions: list[AgentAction] | None = None,
) -> tuple[AgentState, QueueSelector]:
    selector = QueueSelector(
        pipeline_actions() if actions is None else actions,
    )
    provider = StaticSearchProvider()
    action_tools = [WebSearchTool(provider)]
    registry_tools = list(action_tools)

    if artifact_path is not None:
        registry_tools.append(WriteFileTool(workspace))
    graph = build_agent_workflow(
        plan,
        selector,
        ToolRegistry(registry_tools),
        action_registry=ToolRegistry(action_tools),
        artifact_path=artifact_path,
    )
    result = asyncio.run(
        graph.ainvoke(
            create_initial_state("Research multi-source evidence."),
            config={"recursion_limit": 100},
        )
    )
    return result, selector


def test_pipeline_tracks_multiple_sources_deduplicates_and_rejects_unknown(
    tmp_path: Path,
) -> None:
    result, selector = invoke_pipeline(
        Workspace(tmp_path / "workspace"),
        artifact_path=None,
    )

    assert [record.canonical_url for record in result["evidence"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert result["sources"] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert len(result["errors"]) == 1
    assert "absent from successful web evidence" in result["errors"][0]
    assert "https://example.com/invented" not in result["final_answer"]

    evidence_counts = [len(context.evidence) for context in selector.contexts]
    assert evidence_counts == [0, 2, 2, 2]

    report = result["final_answer"]
    assert report is not None
    assert "## Findings" in report
    assert "## Evidence" in report
    assert "## Citations" in report
    assert "## Gaps and limitations" in report
    assert "tool call 1, observation 1" in report


def test_artifact_is_written_only_when_enabled(tmp_path: Path) -> None:
    writable_workspace = Workspace(tmp_path / "writable")
    writable_result, _ = invoke_pipeline(
        writable_workspace,
        artifact_path="reports/research.md",
    )

    assert writable_result["artifact_path"] == "reports/research.md"
    assert (
        writable_workspace.read_text("reports/research.md")
        == (writable_result["final_answer"])
    )
    assert writable_workspace.list_files() == ("reports/research.md",)

    read_only_workspace = Workspace(tmp_path / "read-only")
    before = read_only_workspace.list_files()
    read_only_result, _ = invoke_pipeline(
        read_only_workspace,
        artifact_path=None,
    )

    assert read_only_result["artifact_path"] is None
    assert read_only_workspace.list_files() == before


def test_research_pipeline_denies_model_write_outside_artifact_path(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    actions = [
        ToolCallAction(
            type="tool_call",
            tool_name="write_file",
            arguments={
                "path": "langgraph_docs_report.md",
                "content": "Model-controlled content must not be written.",
            },
        ),
        completion(1),
        completion(2),
        completion(3),
    ]

    result, selector = invoke_pipeline(
        workspace,
        artifact_path="reports/research.md",
        actions=actions,
    )

    assert selector.contexts[0].available_tools == [
        definition
        for definition in selector.contexts[0].available_tools
        if definition["name"] != "write_file"
    ]
    assert result["tool_observations"][0].result.success is False
    assert result["tool_observations"][0].result.error == (
        "Tool is not available for research actions."
    )
    assert result["tool_observations"][0].result.metadata["error_type"] == (
        "ActionToolDeniedError"
    )
    assert result["artifact_path"] == "reports/research.md"
    assert workspace.list_files() == ("reports/research.md",)
    assert not (workspace.root / "langgraph_docs_report.md").exists()


def test_artifact_path_traversal_is_a_controlled_execution_error(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    result, _ = invoke_pipeline(
        workspace,
        artifact_path="../escaped.md",
    )

    assert result["artifact_path"] is None
    assert not (tmp_path / "escaped.md").exists()
    assert "Research artifact could not be written" in result["errors"][-1]
    assert "Research artifact could not be written" in result["final_answer"]


class CrashAfterEvidenceSelector:
    def __init__(self) -> None:
        self.call_count = 0

    async def select_action(self, context: ActionContext) -> AgentAction:
        self.call_count += 1

        if self.call_count == 1:
            return ToolCallAction(
                type="tool_call",
                tool_name="web_search",
                arguments={"query": "resume evidence", "max_results": 3},
            )

        if self.call_count == 2:
            return completion(1, ["https://example.com/a"])

        assert len(context.evidence) == 2
        raise RuntimeError("interrupt after evidence checkpoint")


class ResumeWithEvidenceSelector:
    def __init__(self) -> None:
        self.contexts: list[ActionContext] = []

    async def select_action(self, context: ActionContext) -> AgentAction:
        self.contexts.append(context)
        return completion(
            context.step.step_number,
            ["https://example.com/b"] if context.step.step_number == 2 else [],
        )


def reject_replanning(goal: str) -> Plan:
    raise AssertionError(f"Planner unexpectedly reran for {goal}")


def test_checkpoint_resume_preserves_evidence_and_citation_provenance(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[AgentState, ResumeWithEvidenceSelector]:
        checkpoint_path = tmp_path / "evidence.sqlite"
        limits = RuntimeLimits(
            max_tool_calls_per_step=3,
            max_total_tool_calls=5,
            recursion_limit=80,
        )
        provider = StaticSearchProvider()

        async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            runtime = build_agent_runtime(
                plan,
                CrashAfterEvidenceSelector(),
                ToolRegistry([WebSearchTool(provider)]),
                checkpointer=checkpointer,
                limits=limits,
            )

            with pytest.raises(
                RuntimeError,
                match="interrupt after evidence checkpoint",
            ):
                await runtime.run(
                    "Research persisted evidence.",
                    thread_id="evidence-resume",
                )

        resumed_selector = ResumeWithEvidenceSelector()

        async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            runtime = build_agent_runtime(
                reject_replanning,
                resumed_selector,
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=limits,
            )
            result = await runtime.resume(thread_id="evidence-resume")

        return result, resumed_selector

    result, resumed_selector = asyncio.run(scenario())

    assert len(result["evidence"]) == 2
    assert result["evidence"][0].provenance.total_tool_call_number == 1
    assert result["sources"] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert all(len(context.evidence) == 2 for context in resumed_selector.contexts)
    assert "https://example.com/b" in result["final_answer"]
