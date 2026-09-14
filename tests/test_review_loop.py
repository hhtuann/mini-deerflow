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
from mini_deerflow.context_budget import ContextBudget
from mini_deerflow.decision import ActionContext
from mini_deerflow.persistence import create_thread_config, open_sqlite_checkpointer
from mini_deerflow.review import (
    ReplacementWork,
    ReviewContext,
    ReviewFinding,
    ReviewVerdict,
)
from mini_deerflow.runtime import RuntimeLimits, build_agent_runtime
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import AgentState, create_initial_state
from mini_deerflow.tools import (
    ToolRegistry,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
)
from mini_deerflow.web import FetchedPage, SearchResult
from mini_deerflow.workspace import Workspace

GOAL = "Research multi-source evidence."


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


class QueuedReviewer:
    def __init__(self, verdicts: list[ReviewVerdict | BaseException]) -> None:
        self._verdicts = deque(verdicts)
        self.contexts: list[ReviewContext] = []

    async def review_evidence(self, context: ReviewContext) -> ReviewVerdict:
        self.contexts.append(context)
        outcome = self._verdicts.popleft()

        if isinstance(outcome, BaseException):
            raise outcome

        return outcome


class QueuedReplanner:
    def __init__(self, replacements: list[ReplacementWork | BaseException]) -> None:
        self._replacements = deque(replacements)
        self.requests: list[object] = []

    def __call__(self, request: object) -> ReplacementWork:
        self.requests.append(request)
        outcome = self._replacements.popleft()

        if isinstance(outcome, BaseException):
            raise outcome

        return outcome


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


def review_verdict(
    verdict_kind: str,
    *,
    rationale: str | None = None,
    findings: list[ReviewFinding] | None = None,
) -> ReviewVerdict:
    return ReviewVerdict(
        verdict=verdict_kind,
        rationale=rationale
        or f"The evidence review decided to {verdict_kind} the research.",
        findings=findings or [],
    )


def replacement_work(
    steps_count: int = 2,
    title_prefix: str = "Revised",
) -> ReplacementWork:
    return ReplacementWork(
        steps=[
            PlanStep(
                step_number=number,
                title=f"{title_prefix} step {number}",
                objective=f"Collect the reviewed missing dimension {number}.",
                success_criteria=f"Revised step {number} closes the gap.",
            )
            for number in range(1, steps_count + 1)
        ],
    )


def search_action() -> ToolCallAction:
    return ToolCallAction(
        type="tool_call",
        tool_name="web_search",
        arguments={"query": "traceable evidence", "max_results": 3},
    )


def run_loop(
    selector: QueueSelector,
    reviewer: QueuedReviewer,
    replanner: QueuedReplanner,
    registry: ToolRegistry,
    *,
    action_registry: ToolRegistry | None = None,
    max_tool_calls_per_step: int = 5,
    max_total_tool_calls: int = 20,
    max_replan_cycles: int = 2,
    artifact_path: str | None = None,
) -> AgentState:
    graph = build_agent_workflow(
        plan,
        selector,
        registry,
        action_registry=action_registry,
        reviewer=reviewer,
        replanner=replanner,
        max_tool_calls_per_step=max_tool_calls_per_step,
        max_total_tool_calls=max_total_tool_calls,
        max_replan_cycles=max_replan_cycles,
        artifact_path=artifact_path,
    )

    return asyncio.run(
        graph.ainvoke(
            create_initial_state(GOAL),
            config={"recursion_limit": 120},
        ),
    )


def web_registry(provider: StaticSearchProvider) -> ToolRegistry:
    return ToolRegistry([WebSearchTool(provider)])


def test_continue_verdict_keeps_current_plan_and_finishes() -> None:
    provider = StaticSearchProvider()
    selector = QueueSelector(
        [
            search_action(),
            completion(1, ["https://example.com/a"]),
            completion(2, ["https://example.com/b"]),
            completion(3, ["https://example.com/invented"]),
        ],
    )
    reviewer = QueuedReviewer(
        [
            review_verdict("continue"),
            review_verdict("continue"),
            review_verdict("finish"),
        ],
    )
    replanner = QueuedReplanner([])

    result = run_loop(
        selector,
        reviewer,
        replanner,
        web_registry(provider),
    )

    assert [verdict.verdict for verdict in result["review_verdicts"]] == [
        "continue",
        "continue",
        "finish",
    ]
    assert result["replans"] == []
    assert result["current_step"] == 3
    assert result["pending_review_verdict"] is None

    assert len(reviewer.contexts) == 3
    assert [step.title for step in reviewer.contexts[0].remaining_steps] == [
        "Step 2",
        "Step 3",
    ]
    assert reviewer.contexts[0].completed_step_summaries == [
        "Completed evidence-backed research step 1."
    ]
    assert reviewer.contexts[2].remaining_steps == []
    assert len(reviewer.contexts[2].findings) == 3
    assert len(reviewer.contexts[2].evidence) == 2
    assert reviewer.contexts[2].limitations == [
        "Step 3 rejected 1 citation(s) absent from successful web evidence."
    ]
    assert reviewer.contexts[0].remaining_total_tool_calls == 19
    assert reviewer.contexts[0].remaining_replan_cycles == 2

    report = result["final_answer"]

    assert report is not None
    assert "## Review conclusions" in report
    assert "**Review 1 — continue:**" in report
    assert "**Review 3 — finish:**" in report
    assert "- Review cycles: 3" in report
    assert "- Replan cycles: 0" in report


def test_replan_replaces_remaining_steps_and_preserves_completed_work() -> None:
    provider = StaticSearchProvider()
    selector = QueueSelector(
        [
            search_action(),
            completion(1, ["https://example.com/a"]),
            completion(2),
            completion(3),
        ],
    )
    reviewer = QueuedReviewer(
        [
            review_verdict("replan"),
            review_verdict("continue"),
            review_verdict("finish"),
        ],
    )
    replanner = QueuedReplanner([replacement_work(2)])

    result = run_loop(
        selector,
        reviewer,
        replanner,
        web_registry(provider),
    )

    assert provider.call_count == 1
    assert result["total_tool_calls"] == 1

    assert len(result["replans"]) == 1

    record = result["replans"][0]

    assert record.replan_number == 1
    assert record.replaced_step_numbers == [2, 3]
    assert [step.step_number for step in record.replacement_steps] == [2, 3]
    assert [step.title for step in record.replacement_steps] == [
        "Revised step 1",
        "Revised step 2",
    ]

    final_plan = result["plan"]

    assert final_plan is not None
    assert [step.title for step in final_plan.steps] == [
        "Step 1",
        "Revised step 1",
        "Revised step 2",
    ]
    assert [step.step_number for step in final_plan.steps] == [1, 2, 3]

    assert result["notes"] == [
        "Completed evidence-backed research step 1.",
        "Completed evidence-backed research step 2.",
        "Completed evidence-backed research step 3.",
    ]
    assert [finding.step_number for finding in result["findings"]] == [1, 2, 3]
    assert [record.canonical_url for record in result["evidence"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert result["current_step"] == 3
    assert result["pending_review_verdict"] is None

    assert len(replanner.requests) == 1

    request = replanner.requests[0]

    assert request.review.verdict == "replan"  # type: ignore[attr-defined]
    assert [step.title for step in request.replaced_steps] == [  # type: ignore[attr-defined]
        "Step 2",
        "Step 3",
    ]
    assert request.min_replacement_steps == 2  # type: ignore[attr-defined]
    assert request.max_replacement_steps == 6  # type: ignore[attr-defined]
    assert request.remaining_total_tool_calls == 19  # type: ignore[attr-defined]
    assert request.remaining_replan_cycles == 2  # type: ignore[attr-defined]

    report = result["final_answer"]

    assert report is not None
    assert "- Replan cycles: 1" in report
    assert "https://example.com/a" in report


def test_replan_budget_exhaustion_forces_finish() -> None:
    provider = StaticSearchProvider()
    selector = QueueSelector(
        [
            completion(1),
            completion(2),
        ],
    )
    reviewer = QueuedReviewer(
        [
            review_verdict("replan"),
            review_verdict("replan"),
        ],
    )
    replanner = QueuedReplanner([replacement_work(2)])

    result = run_loop(
        selector,
        reviewer,
        replanner,
        web_registry(provider),
        max_replan_cycles=1,
    )

    assert result["review_verdicts"][-1].verdict == "replan"
    assert len(result["replans"]) == 1
    assert any("replan budget was exhausted" in error for error in result["errors"])
    assert result["pending_review_verdict"] is None
    assert result["final_answer"] is not None
    assert "- Replan cycles: 1" in result["final_answer"]
    assert "replan budget was exhausted" in result["final_answer"]


def test_replan_with_exhausted_tool_budget_forces_finish() -> None:
    provider = StaticSearchProvider()
    selector = QueueSelector(
        [
            search_action(),
            completion(1),
        ],
    )
    reviewer = QueuedReviewer([review_verdict("replan")])
    replanner = QueuedReplanner([])

    result = run_loop(
        selector,
        reviewer,
        replanner,
        web_registry(provider),
        max_total_tool_calls=1,
    )

    assert reviewer.contexts[0].remaining_total_tool_calls == 0
    assert result["replans"] == []
    assert replanner.requests == []
    assert any("tool-call budget was exhausted" in error for error in result["errors"])
    assert result["pending_review_verdict"] is None
    assert result["final_answer"] is not None


def test_continue_on_exhausted_plan_forces_finish() -> None:
    provider = StaticSearchProvider()
    selector = QueueSelector(
        [
            completion(1),
            completion(2),
            completion(3),
        ],
    )
    reviewer = QueuedReviewer(
        [
            review_verdict("continue"),
            review_verdict("continue"),
            review_verdict("continue"),
        ],
    )
    replanner = QueuedReplanner([])

    result = run_loop(
        selector,
        reviewer,
        replanner,
        web_registry(provider),
    )

    assert [verdict.verdict for verdict in result["review_verdicts"]] == [
        "continue",
        "continue",
        "continue",
    ]
    assert any("no remaining steps" in error for error in result["errors"])
    assert result["replans"] == []
    assert result["pending_review_verdict"] is None
    assert result["final_answer"] is not None


def test_reviewer_cannot_introduce_citations() -> None:
    provider = StaticSearchProvider()
    selector = QueueSelector(
        [
            search_action(),
            completion(1, ["https://example.com/a"]),
            completion(2, ["https://example.com/b"]),
            completion(3),
        ],
    )
    reviewer = QueuedReviewer(
        [
            review_verdict("finish"),
        ],
    )
    replanner = QueuedReplanner([])

    result = run_loop(
        selector,
        reviewer,
        replanner,
        web_registry(provider),
    )

    assert set(ReviewVerdict.model_fields) == {
        "verdict",
        "rationale",
        "findings",
    }

    report = result["final_answer"]

    assert report is not None
    assert "evil.example.com" not in report
    assert "https://example.com/a" in report
    assert "https://example.com/b" in report
    assert "No validated citations were used" not in report


def test_review_loop_reports_findings_as_gaps_and_limitations() -> None:
    provider = StaticSearchProvider()
    selector = QueueSelector(
        [
            search_action(),
            completion(
                1,
                [
                    "https://example.com/a",
                    "https://example.com/invented",
                ],
            ),
        ],
    )
    reviewer = QueuedReviewer(
        [
            review_verdict(
                "finish",
                rationale=(
                    "Two sources support the goal but one dimension stays uncovered."
                ),
                findings=[
                    ReviewFinding(
                        category="gap",
                        description=(
                            "No evidence covers the cost dimension of "
                            "the research goal."
                        ),
                        related_step_numbers=[3],
                    ),
                    ReviewFinding(
                        category="source_diversity",
                        description=(
                            "Both citations come from the same domain family."
                        ),
                    ),
                    ReviewFinding(
                        category="budget_limitation",
                        description=(
                            "No remaining tool calls can collect more evidence."
                        ),
                    ),
                ],
            ),
        ],
    )
    replanner = QueuedReplanner([])

    result = run_loop(
        selector,
        reviewer,
        replanner,
        web_registry(provider),
    )

    report = result["final_answer"]

    assert report is not None
    assert "## Review conclusions" in report
    assert "**Review 1 — finish:**" in report
    assert "[gap] No evidence covers the cost dimension" in report
    assert "[source_diversity]" in report
    assert "[budget_limitation]" in report
    assert "(steps: 3)" in report
    assert "## Gaps and limitations" in report
    assert "absent from successful web evidence" in report


def test_review_state_survives_checkpoint_resume(tmp_path: Path) -> None:
    async def scenario() -> tuple[AgentState, StaticSearchProvider]:
        checkpoint_path = tmp_path / "review-resume.sqlite"
        provider = StaticSearchProvider()
        limits = RuntimeLimits(
            max_tool_calls_per_step=3,
            max_total_tool_calls=5,
            max_replan_cycles=2,
            recursion_limit=120,
        )

        async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            runtime = build_agent_runtime(
                plan,
                QueueSelector(
                    [
                        search_action(),
                        completion(1, ["https://example.com/a"]),
                    ],
                ),
                ToolRegistry([WebSearchTool(provider)]),
                reviewer=QueuedReviewer([review_verdict("replan")]),
                replanner=QueuedReplanner(
                    [RuntimeError("interrupt during replan")],
                ),
                checkpointer=checkpointer,
                limits=limits,
            )

            with pytest.raises(
                RuntimeError,
                match="interrupt during replan",
            ):
                await runtime.run(
                    GOAL,
                    thread_id="review-resume",
                )

        resumed_selector = QueueSelector(
            [
                completion(2),
                completion(3),
            ],
        )
        resumed_reviewer = QueuedReviewer(
            [
                review_verdict("continue"),
                review_verdict("finish"),
            ],
        )

        async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            runtime = build_agent_runtime(
                plan,
                resumed_selector,
                ToolRegistry([WebSearchTool(provider)]),
                reviewer=resumed_reviewer,
                replanner=QueuedReplanner([replacement_work(2)]),
                checkpointer=checkpointer,
                limits=limits,
            )

            result = await runtime.resume(thread_id="review-resume")

        return result, provider

    result, provider = asyncio.run(scenario())

    assert provider.call_count == 1
    assert [verdict.verdict for verdict in result["review_verdicts"]] == [
        "replan",
        "continue",
        "finish",
    ]
    assert len(result["replans"]) == 1

    record = result["replans"][0]

    assert record.replaced_step_numbers == [2, 3]
    assert [step.title for step in record.replacement_steps] == [
        "Revised step 1",
        "Revised step 2",
    ]
    assert [record.canonical_url for record in result["evidence"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert result["current_step"] == 3
    assert result["pending_review_verdict"] is None

    report = result["final_answer"]

    assert report is not None
    assert "- Review cycles: 3" in report
    assert "- Replan cycles: 1" in report


def test_budget_exhaustion_skips_review_like_day_09() -> None:
    provider = StaticSearchProvider()
    selector = QueueSelector(
        [
            search_action(),
            search_action(),
        ],
    )
    reviewer = QueuedReviewer([review_verdict("finish")])
    replanner = QueuedReplanner([])

    result = run_loop(
        selector,
        reviewer,
        replanner,
        web_registry(provider),
        max_total_tool_calls=1,
    )

    assert reviewer.contexts == []
    assert result["review_verdicts"] == []
    assert result["pending_review_verdict"] is None
    assert "No review verdicts were recorded." in result["final_answer"]
    assert "- Tool calls: 1" in result["final_answer"]


def test_review_loop_preserves_artifact_and_action_boundaries(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    provider = StaticSearchProvider()
    selector = QueueSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="write_file",
                arguments={
                    "path": "model-controlled.md",
                    "content": "Model-controlled content must not be written.",
                },
            ),
            search_action(),
            completion(1, ["https://example.com/a"]),
            completion(2, ["https://example.com/b"]),
            completion(3),
        ],
    )
    reviewer = QueuedReviewer(
        [
            review_verdict("continue"),
            review_verdict("continue"),
            review_verdict("finish"),
        ],
    )
    replanner = QueuedReplanner([])

    result = run_loop(
        selector,
        reviewer,
        replanner,
        ToolRegistry(
            [
                WebSearchTool(provider),
                WriteFileTool(workspace),
            ],
        ),
        action_registry=ToolRegistry([WebSearchTool(provider)]),
        artifact_path="reports/research.md",
    )

    denied_observation = result["tool_observations"][0]

    assert denied_observation.result.success is False
    assert denied_observation.result.error == (
        "Tool is not available for research actions."
    )
    assert denied_observation.result.metadata["error_type"] == ("ActionToolDeniedError")

    assert result["artifact_path"] == "reports/research.md"
    assert result["pending_review_verdict"] is None
    assert workspace.read_text("reports/research.md") == (result["final_answer"])
    assert workspace.list_files() == ("reports/research.md",)
    assert not (workspace.root / "model-controlled.md").exists()


def test_review_loop_validates_dependencies() -> None:
    reviewer = QueuedReviewer([])
    replanner = QueuedReplanner([])

    with pytest.raises(
        ValueError,
        match="configured together",
    ):
        build_agent_workflow(
            plan,
            QueueSelector([]),
            ToolRegistry(),
            reviewer=reviewer,
        )

    with pytest.raises(
        ValueError,
        match="configured together",
    ):
        build_agent_workflow(
            plan,
            QueueSelector([]),
            ToolRegistry(),
            replanner=replanner,
        )

    with pytest.raises(
        TypeError,
        match="EvidenceReviewer",
    ):
        build_agent_workflow(
            plan,
            QueueSelector([]),
            ToolRegistry(),
            reviewer=object(),
            replanner=replanner,
        )

    with pytest.raises(
        TypeError,
        match="replanner must be callable",
    ):
        build_agent_workflow(
            plan,
            QueueSelector([]),
            ToolRegistry(),
            reviewer=reviewer,
            replanner=object(),
        )

    with pytest.raises(
        ValueError,
        match="max_replan_cycles",
    ):
        build_agent_workflow(
            plan,
            QueueSelector([]),
            ToolRegistry(),
            reviewer=reviewer,
            replanner=replanner,
            max_replan_cycles=0,
        )


def test_invalid_reviewer_output_is_rejected() -> None:
    class InvalidReviewer:
        async def review_evidence(
            self,
            context: ReviewContext,
        ) -> object:
            return {
                "verdict": "finish",
                "rationale": "A dict is not a validated verdict.",
            }

    graph = build_agent_workflow(
        plan,
        QueueSelector([completion(1)]),
        ToolRegistry(),
        reviewer=InvalidReviewer(),
        replanner=QueuedReplanner([]),
    )

    with pytest.raises(
        TypeError,
        match="invalid verdict",
    ):
        asyncio.run(
            graph.ainvoke(
                create_initial_state(GOAL),
                config={"recursion_limit": 120},
            ),
        )


class LongPageProvider:
    """Deterministic fetch provider returning near-cap page content."""

    def __init__(self, body_length: int) -> None:
        self._body_length = body_length

    async def fetch(self, url: str) -> FetchedPage:
        return FetchedPage(
            url=url,
            title="Long source",
            content=(
                "Provenance-carrying opening. "
                + "L" * self._body_length
                + " closing tail marker."
            ),
            status_code=200,
            content_type="text/markdown",
        )


def fetch_action() -> ToolCallAction:
    return ToolCallAction(
        type="tool_call",
        tool_name="web_fetch",
        arguments={"url": "https://example.com/long"},
    )


def test_llm_contexts_are_bounded_under_pressure() -> None:
    body_length = 19_000
    provider = LongPageProvider(body_length)
    budget = ContextBudget(
        max_total_chars=8_000,
        max_item_chars=800,
        retained_recent_items=5,
        max_excerpt_chars=600,
    )
    selector = QueueSelector(
        [
            fetch_action(),
            completion(1, ["https://example.com/long"]),
        ],
    )
    reviewer = QueuedReviewer([review_verdict("finish")])
    replanner = QueuedReplanner([])

    graph = build_agent_workflow(
        plan,
        selector,
        ToolRegistry([WebFetchTool(provider)]),
        reviewer=reviewer,
        replanner=replanner,
        context_budget=budget,
    )

    result = asyncio.run(
        graph.ainvoke(
            create_initial_state(GOAL),
            config={"recursion_limit": 120},
        ),
    )

    selector_before_fetch = selector.contexts[0]

    assert selector_before_fetch.evidence == []

    selector_after_fetch = selector.contexts[1]

    assert len(selector_after_fetch.evidence) == 1
    assert len(selector_after_fetch.evidence[0].excerpt) <= 600
    assert "[truncated at 600 of" in selector_after_fetch.evidence[0].excerpt
    assert len(selector_after_fetch.observations[0].result.data["content"]) <= 800

    review_context = reviewer.contexts[0]

    assert len(review_context.evidence) == 1
    assert len(review_context.evidence[0].excerpt) <= 600
    assert "[truncated at 600 of" in review_context.evidence[0].excerpt

    projection = review_context.context_projection

    assert projection is not None
    assert projection.truncated_items >= 1
    assert projection.estimated_tokens > 0

    serialized_context = review_context.model_dump_json()

    assert len(serialized_context) <= budget.max_total_chars
    assert "closing tail marker" not in serialized_context

    state_excerpt = result["evidence"][0].excerpt

    assert len(state_excerpt) > body_length
    assert state_excerpt.endswith("closing tail marker.")
    assert result["pending_review_verdict"] is None
    assert result["total_tool_calls"] == 1

    report = result["final_answer"]

    assert report is not None
    assert "closing tail marker." in report
    assert "[truncated at" not in report


def test_workflow_rejects_invalid_context_budget() -> None:
    with pytest.raises(
        TypeError,
        match="ContextBudget",
    ):
        build_agent_workflow(
            plan,
            QueueSelector([]),
            ToolRegistry(),
            reviewer=QueuedReviewer([]),
            replanner=QueuedReplanner([]),
            context_budget=object(),
        )


def test_terminal_state_never_retains_unconsumed_review_verdict() -> None:
    scenarios: list[
        tuple[str, list[AgentAction], list[ReviewVerdict], list[ReplacementWork]]
    ] = [
        (
            "continue-consumed flow",
            [
                search_action(),
                completion(1, ["https://example.com/a"]),
                completion(2, ["https://example.com/b"]),
                completion(3),
            ],
            [
                review_verdict("continue"),
                review_verdict("continue"),
                review_verdict("finish"),
            ],
            [],
        ),
        (
            "replan flow",
            [
                search_action(),
                completion(1, ["https://example.com/a"]),
                completion(2),
                completion(3),
            ],
            [
                review_verdict("replan"),
                review_verdict("continue"),
                review_verdict("finish"),
            ],
            [replacement_work(2)],
        ),
        (
            "early finish flow",
            [
                search_action(),
                completion(1, ["https://example.com/a"]),
            ],
            [review_verdict("finish")],
            [],
        ),
    ]

    expected_history_lengths = [3, 3, 1]
    expected_replan_lengths = [0, 1, 0]

    for index, (label, actions, verdicts, replacements) in enumerate(scenarios):
        provider = StaticSearchProvider()

        result = run_loop(
            QueueSelector(actions),
            QueuedReviewer(verdicts),
            QueuedReplanner(replacements),
            web_registry(provider),
        )

        assert result["pending_review_verdict"] is None, label
        assert len(result["review_verdicts"]) == expected_history_lengths[index], label
        assert len(result["replans"]) == expected_replan_lengths[index], label
        assert result["final_answer"] is not None, label
        assert "## Review conclusions" in result["final_answer"], label


def test_pending_continue_verdict_survives_checkpoint_until_consumed(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[AgentState, StaticSearchProvider, QueuedReviewer]:
        checkpoint_path = tmp_path / "pending-continue.sqlite"
        provider = StaticSearchProvider()
        limits = RuntimeLimits(
            max_tool_calls_per_step=3,
            max_total_tool_calls=5,
            max_replan_cycles=2,
            recursion_limit=120,
        )
        config = create_thread_config(
            "pending-continue",
            recursion_limit=limits.recursion_limit,
        )

        async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            runtime = build_agent_runtime(
                plan,
                QueueSelector(
                    [
                        search_action(),
                        completion(1, ["https://example.com/a"]),
                        RuntimeError("interrupt after continue verdict"),
                    ],
                ),
                ToolRegistry([WebSearchTool(provider)]),
                reviewer=QueuedReviewer([review_verdict("continue")]),
                replanner=QueuedReplanner([]),
                checkpointer=checkpointer,
                limits=limits,
            )

            with pytest.raises(
                RuntimeError,
                match="interrupt after continue verdict",
            ):
                await runtime.run(
                    GOAL,
                    thread_id="pending-continue",
                )

        # The latest checkpoint was taken after review_node routed the
        # "continue" verdict but before decide_action consumed it.
        async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            checkpoint_tuple = await checkpointer.aget_tuple(config)
            assert checkpoint_tuple is not None

            channel_values = checkpoint_tuple.checkpoint["channel_values"]
            assert channel_values["pending_review_verdict"] == "continue"
            assert len(channel_values["review_verdicts"]) == 1

        resumed_reviewer = QueuedReviewer(
            [
                review_verdict("continue"),
                review_verdict("finish"),
            ],
        )

        async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            runtime = build_agent_runtime(
                plan,
                QueueSelector(
                    [
                        completion(2),
                        completion(3),
                    ],
                ),
                ToolRegistry([WebSearchTool(provider)]),
                reviewer=resumed_reviewer,
                replanner=QueuedReplanner([]),
                checkpointer=checkpointer,
                limits=limits,
            )

            result = await runtime.resume(thread_id="pending-continue")

        return result, provider, resumed_reviewer

    result, provider, resumed_reviewer = asyncio.run(scenario())

    # The interrupted review was not re-executed; only reviews 2 and 3 ran.
    assert len(resumed_reviewer.contexts) == 2
    assert provider.call_count == 1

    assert [verdict.verdict for verdict in result["review_verdicts"]] == [
        "continue",
        "continue",
        "finish",
    ]
    assert result["replans"] == []
    assert result["current_step"] == 3
    assert result["pending_review_verdict"] is None

    report = result["final_answer"]

    assert report is not None
    assert "**Review 1 — continue:**" in report
    assert "- Review cycles: 3" in report
