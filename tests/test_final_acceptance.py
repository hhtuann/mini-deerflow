import asyncio
import json
from collections import Counter, defaultdict, deque
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from mini_deerflow.actions import (
    ActionDecision,
    CompleteStepAction,
    ToolCallAction,
    ToolObservation,
)
from mini_deerflow.config import Settings
from mini_deerflow.context_budget import ContextBudget
from mini_deerflow.delegation import (
    BranchFinding,
    BranchResult,
    ResearchTaskContext,
)
from mini_deerflow.review import (
    ReplacementWork,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
)
from mini_deerflow.runtime import RuntimeLimits, open_default_agent_runtime
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.tools import ToolResult
from mini_deerflow.tracing import (
    ExecutionTracer,
    InMemoryTraceSink,
    TraceKind,
    TraceOutcome,
)
from mini_deerflow.web import (
    FetchedPage,
    SearchResult,
    WebProviderErrorCategory,
    WebSearchError,
)
from mini_deerflow.web_safety import PublicWebTargetValidator, SafeWebTarget
from mini_deerflow.workspace import Workspace

GOAL = "Demonstrate the bounded Mini DeerFlow MVP with verifiable evidence."
THREAD_ID = "day-14-final-acceptance"
SEARCH_SOURCE = "https://evidence.example/search"
FETCH_SOURCE = "https://evidence.example/report"
DELEGATED_SOURCE = "https://evidence.example/delegated"
UNSAFE_URL = "http://127.0.0.1/private"
INVENTED_SOURCE = "https://invented.invalid/fact"
SECRET_CANARY = "sk-day14-must-not-leak"
RAW_PAYLOAD_CANARY = "RAW_PROVIDER_PAYLOAD_MUST_NOT_LEAK"
MACHINE_PATH_CANARY = r"C:\Users\demo\.env"
LONG_EVIDENCE_TAIL = "DAY14_RAW_EVIDENCE_TAIL"
FALSE_FACT_CANARY = "BETA_FALSE_VERIFIED_FACT"


def _step(number: int, title: str) -> PlanStep:
    return PlanStep(
        step_number=number,
        title=title,
        objective=f"Collect deterministic evidence for {title.lower()}.",
        success_criteria="Record a bounded, evidence-backed result or limitation.",
    )


class ScenarioRunnable:
    def __init__(self, model: "ScenarioModel", schema: type[object]) -> None:
        self._model = model
        self._schema = schema

    def invoke(self, messages: object) -> object:
        return self._model.respond(self._schema, messages)

    async def ainvoke(self, messages: object) -> object:
        return self._model.respond(self._schema, messages)


class ScenarioModel:
    def __init__(self) -> None:
        self.responses: dict[type[object], deque[object]] = {
            Plan: deque(
                [
                    Plan(
                        goal=GOAL,
                        steps=[
                            _step(1, "Collect primary web evidence"),
                            _step(2, "Compare independent evidence"),
                            _step(3, "Conclude with explicit limitations"),
                        ],
                    )
                ]
            ),
            ActionDecision: deque(
                [
                    ToolCallAction(
                        type="tool_call",
                        tool_name="web_fetch",
                        arguments={"url": UNSAFE_URL},
                    ),
                    ToolCallAction(
                        type="tool_call",
                        tool_name="web_search",
                        arguments={
                            "query": (
                                f"{SECRET_CANARY} {RAW_PAYLOAD_CANARY} "
                                f"{MACHINE_PATH_CANARY}"
                            )
                        },
                    ),
                    ToolCallAction(
                        type="tool_call",
                        tool_name="web_search",
                        arguments={"query": "bounded final mvp evidence"},
                    ),
                    ToolCallAction(
                        type="tool_call",
                        tool_name="web_fetch",
                        arguments={"url": FETCH_SOURCE},
                    ),
                    CompleteStepAction(
                        type="complete_step",
                        summary="Collected safe search and fetch evidence for the MVP.",
                        sources=[FETCH_SOURCE, INVENTED_SOURCE, UNSAFE_URL],
                    ),
                    ToolCallAction(
                        type="tool_call",
                        tool_name="delegate_research",
                        arguments={
                            "delegation_id": "day14-wave",
                            "tasks": [
                                {
                                    "branch_id": branch_id,
                                    "objective": (
                                        f"Research independent MVP evidence for {branch_id}."
                                    ),
                                    "success_criteria": (
                                        "Return one bounded finding or a controlled limitation."
                                    ),
                                    "tool_call_budget": 1,
                                    "delegation_depth": 1,
                                }
                                for branch_id in ("beta", "alpha")
                            ],
                        },
                    ),
                    RuntimeError(
                        "deterministic interruption after delegation checkpoint"
                    ),
                    CompleteStepAction(
                        type="complete_step",
                        summary=(
                            "Integrated the successful branch and retained the failed "
                            "branch as a limitation."
                        ),
                        sources=[DELEGATED_SOURCE],
                    ),
                    CompleteStepAction(
                        type="complete_step",
                        summary=(
                            "Concluded the bounded MVP demonstration from validated evidence."
                        ),
                        sources=[SEARCH_SOURCE],
                    ),
                ]
            ),
            ReviewDecision: deque(
                [
                    ReviewVerdict(
                        verdict="replan",
                        rationale=(
                            "The first evidence pass is useful, but independent coverage "
                            "and an explicit limitation route are still required."
                        ),
                        findings=[
                            ReviewFinding(
                                category="gap",
                                description=(
                                    "Add independent bounded research and preserve any "
                                    "partial failure as a limitation."
                                ),
                                related_step_numbers=[2, 3],
                            )
                        ],
                    ),
                    ReviewVerdict(
                        verdict="continue",
                        rationale=(
                            "Validated delegated evidence is present and the final "
                            "limitation summary remains to be completed."
                        ),
                    ),
                    ReviewVerdict(
                        verdict="finish",
                        rationale=(
                            "The evidence-backed findings and explicit limitation now "
                            "cover the bounded demonstration goal."
                        ),
                    ),
                ]
            ),
            ReplacementWork: deque(
                [
                    ReplacementWork(
                        steps=[
                            _step(1, "Delegate independent evidence checks"),
                            _step(2, "Render findings and limitations"),
                        ]
                    )
                ]
            ),
        }
        self.calls: list[tuple[type[object], object]] = []
        self.invocation_counts: Counter[type[object]] = Counter()

    def with_structured_output(
        self,
        schema: type[object],
        *,
        method: str,
    ) -> ScenarioRunnable:
        assert method == "json_mode"
        assert schema in self.responses
        return ScenarioRunnable(self, schema)

    def respond(self, schema: type[object], messages: object) -> object:
        self.calls.append((schema, messages))
        self.invocation_counts[schema] += 1
        if not self.responses[schema]:
            raise AssertionError(f"No deterministic {schema.__name__} response remains")
        response = self.responses[schema].popleft()
        if isinstance(response, BaseException):
            raise response
        return response


class FakePublicResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int) -> Sequence[str]:
        self.calls.append((hostname, port))
        return ("93.184.216.34",)


class DeterministicWebProvider:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, int]] = []
        self.fetch_calls: list[str] = []

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        self.search_calls.append((query, max_results))
        if SECRET_CANARY in query:
            raise WebSearchError(
                f"{SECRET_CANARY} {RAW_PAYLOAD_CANARY} {MACHINE_PATH_CANARY}",
                category=WebProviderErrorCategory.AUTHENTICATION,
            )
        return [
            SearchResult(
                title="Deterministic MVP source",
                url=SEARCH_SOURCE,
                snippet="Search evidence " + "S" * 3_500,
            )
        ]

    async def fetch(self, target: SafeWebTarget) -> FetchedPage:
        self.fetch_calls.append(str(target.url))
        return FetchedPage(
            url=target.url,
            title="Long deterministic evidence",
            content=(
                "Raw evidence opening. " + "E" * 12_000 + f" {LONG_EVIDENCE_TAIL}"
            ),
            status_code=200,
            content_type="text/markdown",
        )


class PartialResearcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def research(self, context: ResearchTaskContext) -> BranchResult:
        branch_id = context.task.branch_id
        self.calls.append(branch_id)
        await asyncio.sleep(0)
        if branch_id == "beta":
            return BranchResult(
                branch_id=branch_id,
                status="controlled_failure",
                error="Controlled branch limitation: beta returned no usable evidence.",
                finding=BranchFinding(
                    summary=FALSE_FACT_CANARY,
                    citations=[INVENTED_SOURCE],
                ),
            )
        action = ToolCallAction(
            type="tool_call",
            tool_name="web_search",
            arguments={"query": "delegated bounded evidence"},
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
                    "query": "delegated bounded evidence",
                    "results": [
                        {
                            "url": DELEGATED_SOURCE,
                            "title": "Delegated deterministic source",
                            "snippet": "Independent evidence from the alpha branch.",
                        }
                    ],
                    "count": 1,
                }
            ),
        )
        return BranchResult(
            branch_id=branch_id,
            status="success",
            observations=[observation],
            finding=BranchFinding(
                summary="The alpha branch found independent supporting evidence.",
                citations=[DELEGATED_SOURCE],
            ),
            tool_calls_used=1,
        )


def _recorded_llm_payloads(
    model: ScenarioModel,
) -> list[tuple[str, dict[str, object]]]:
    payloads: list[tuple[str, dict[str, object]]] = []
    for schema, messages in model.calls:
        if schema is Plan:
            continue
        assert isinstance(messages, list)
        if schema is ReplacementWork:
            raw_payload = messages[-1][1]
        else:
            human = messages[-1]
            assert isinstance(human, HumanMessage)
            opening = (
                "<action_context>\n"
                if schema is ActionDecision
                else "<review_context>\n"
            )
            closing = (
                "\n</action_context>"
                if schema is ActionDecision
                else "\n</review_context>"
            )
            raw_payload = human.content.split(opening, 1)[1].split(closing, 1)[0]
        assert isinstance(raw_payload, str)
        payload = json.loads(raw_payload)
        assert isinstance(payload, dict)
        payloads.append((raw_payload, payload))
    return payloads


def test_final_mvp_acceptance_survives_interruption_and_resume(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[dict[str, object], list[object], ScenarioModel]:
        workspace_root = tmp_path / "workspace"
        checkpoint_path = tmp_path / "checkpoints.sqlite"
        settings = Settings(api_key="test-api-key", _env_file=None)
        limits = RuntimeLimits(
            max_tool_calls_per_step=4,
            max_total_tool_calls=8,
            max_replan_cycles=1,
            recursion_limit=120,
            max_delegation_concurrency=2,
        )
        context_budget = ContextBudget(
            max_total_chars=8_000,
            max_item_chars=800,
            retained_recent_items=5,
            max_excerpt_chars=600,
        )
        model = ScenarioModel()
        provider = DeterministicWebProvider()
        resolver = FakePublicResolver()
        researcher = PartialResearcher()
        sink = InMemoryTraceSink()

        def model_factory(received_settings: Settings) -> ChatOpenAI:
            assert received_settings is settings
            return cast(ChatOpenAI, model)

        with pytest.raises(
            RuntimeError,
            match="deterministic interruption after delegation checkpoint",
        ):
            async with open_default_agent_runtime(
                settings,
                workspace_root,
                checkpoint_path,
                allow_write=True,
                limits=limits,
                context_budget=context_budget,
                model_factory=model_factory,
                web_provider=provider,
                web_target_validator=PublicWebTargetValidator(resolver),
                researcher_subagent=researcher,
                artifact_path="reports/day-14-demo.md",
                tracer=ExecutionTracer(
                    sink,
                    run_id_factory=lambda: "day14-interrupted-run",
                ),
            ) as runtime:
                await runtime.run(GOAL, thread_id=THREAD_ID)

        calls_before_resume = (
            list(provider.search_calls),
            list(provider.fetch_calls),
            list(researcher.calls),
            list(resolver.calls),
        )

        async with open_default_agent_runtime(
            settings,
            workspace_root,
            checkpoint_path,
            allow_write=True,
            limits=limits,
            context_budget=context_budget,
            model_factory=model_factory,
            web_provider=provider,
            web_target_validator=PublicWebTargetValidator(resolver),
            researcher_subagent=researcher,
            artifact_path="reports/day-14-demo.md",
            tracer=ExecutionTracer(
                sink,
                run_id_factory=lambda: "day14-resumed-run",
            ),
        ) as runtime:
            state = await runtime.resume(thread_id=THREAD_ID)

        assert calls_before_resume == (
            provider.search_calls,
            provider.fetch_calls,
            researcher.calls,
            resolver.calls,
        )
        assert provider.fetch_calls == [FETCH_SOURCE]
        assert [query for query, _ in provider.search_calls] == [
            f"{SECRET_CANARY} {RAW_PAYLOAD_CANARY} {MACHINE_PATH_CANARY}",
            "bounded final mvp evidence",
        ]
        assert researcher.calls == ["alpha", "beta"]
        assert model.invocation_counts[Plan] == 1
        assert model.invocation_counts[ReplacementWork] == 1

        assert state["final_answer"] is not None
        assert state["plan"] is not None
        assert state["current_step"] == len(state["plan"].steps)
        assert state["pending_action"] is None
        assert state["pending_review_verdict"] is None
        assert state["total_tool_calls"] == 6
        assert state["total_tool_calls"] <= limits.max_total_tool_calls
        assert state["tool_calls_in_current_step"] <= limits.max_tool_calls_per_step
        assert all(
            observation.step_tool_call_number <= limits.max_tool_calls_per_step
            and observation.total_tool_call_number <= limits.max_total_tool_calls
            for observation in state["tool_observations"]
        )
        assert len(state["replans"]) == limits.max_replan_cycles

        evidence_urls = {record.canonical_url for record in state["evidence"]}
        assert set(state["sources"]) <= evidence_urls
        assert {SEARCH_SOURCE, FETCH_SOURCE, DELEGATED_SOURCE} <= evidence_urls
        assert all(
            set(finding.citations) <= evidence_urls for finding in state["findings"]
        )
        assert any(
            record.excerpt.endswith(LONG_EVIDENCE_TAIL)
            and record.provenance.tool_name == "web_fetch"
            and record.provenance.step_number == 1
            for record in state["evidence"]
        )

        delegation = state["delegations"][0]
        assert delegation.fan_in.successful_branches == ["alpha"]
        assert delegation.fan_in.failed_branches == ["beta"]
        assert delegation.fan_in.cancelled_branches == []
        assert delegation.reserved_tool_calls == 2
        assert delegation.used_tool_calls == 1
        assert delegation.charged_tool_calls == 1
        beta_result = next(
            result for result in delegation.results if result.branch_id == "beta"
        )
        assert beta_result.finding is not None
        assert beta_result.finding.summary == FALSE_FACT_CANARY
        assert any("Controlled branch limitation" in error for error in state["errors"])
        assert all(finding.branch_id != "beta" for finding in state["findings"])

        report = cast(str, state["final_answer"])
        workspace = Workspace(workspace_root)
        assert state["artifact_path"] == "reports/day-14-demo.md"
        assert workspace.list_files() == ("reports/day-14-demo.md",)
        assert workspace.read_text("reports/day-14-demo.md") == report
        assert "## Gaps and limitations" in report
        assert "Controlled branch limitation" in report
        assert LONG_EVIDENCE_TAIL in report
        assert FALSE_FACT_CANARY not in report

        payload_records = _recorded_llm_payloads(model)
        rendered_payloads = [raw_payload for raw_payload, _ in payload_records]
        payloads = [payload for _, payload in payload_records]
        assert all(
            len(payload) <= context_budget.max_total_chars
            for payload in rendered_payloads
        )
        assert all(LONG_EVIDENCE_TAIL not in payload for payload in rendered_payloads)
        projections = [
            payload.get("context_projection")
            for payload in payloads
            if isinstance(payload.get("context_projection"), dict)
        ]
        assert any(
            projection["omitted_items"] > 0 or projection["truncated_items"] > 0
            for projection in projections
        )

        events = sink.events
        outcomes_by_kind: dict[TraceKind, set[TraceOutcome]] = defaultdict(set)
        for event in events:
            outcomes_by_kind[event.kind].add(event.outcome)
        assert TraceOutcome.SAFETY_DENIED in outcomes_by_kind[TraceKind.TOOL]
        assert TraceOutcome.PROVIDER_FAILED in outcomes_by_kind[TraceKind.TOOL]
        assert TraceOutcome.SUCCEEDED in outcomes_by_kind[TraceKind.TOOL]
        assert TraceOutcome.COMPACTED in outcomes_by_kind[TraceKind.CONTEXT_BUDGET]
        assert TraceOutcome.REPLAN in outcomes_by_kind[TraceKind.REVIEW]
        assert TraceOutcome.CONTINUE in outcomes_by_kind[TraceKind.REVIEW]
        assert TraceOutcome.FINISH in outcomes_by_kind[TraceKind.REVIEW]
        assert TraceOutcome.SUCCEEDED in outcomes_by_kind[TraceKind.REPLAN]
        assert TraceOutcome.PARTIAL_FAILURE in outcomes_by_kind[TraceKind.DELEGATION]
        assert TraceOutcome.RESUMED in outcomes_by_kind[TraceKind.CHECKPOINT]
        assert TraceOutcome.REJECTED in outcomes_by_kind[TraceKind.CITATION]
        assert TraceOutcome.ACCEPTED in outcomes_by_kind[TraceKind.CITATION]
        assert TraceOutcome.WRITTEN in outcomes_by_kind[TraceKind.ARTIFACT]
        assert sum(event.kind is TraceKind.ARTIFACT for event in events) == 1

        interrupted_run = [
            event for event in events if event.run_id == "day14-interrupted-run"
        ]
        resumed_run = [event for event in events if event.run_id == "day14-resumed-run"]
        assert interrupted_run[-1].kind is TraceKind.RUN
        assert interrupted_run[-1].outcome is TraceOutcome.FAILED
        assert resumed_run[-1].kind is TraceKind.RUN
        assert resumed_run[-1].outcome is TraceOutcome.SUCCEEDED
        assert not any(
            event.kind in {TraceKind.TOOL, TraceKind.DELEGATION}
            for event in resumed_run
        )
        assert all(event.thread_id == THREAD_ID for event in events)
        assert [event.sequence for event in interrupted_run] == list(
            range(1, len(interrupted_run) + 1)
        )
        assert [event.sequence for event in resumed_run] == list(
            range(1, len(resumed_run) + 1)
        )

        for forbidden_state_key in ("trace", "events", "trace_events"):
            assert forbidden_state_key not in state
        public_output = (
            report + "\n" + "\n".join(event.model_dump_json() for event in events)
        )
        for canary in (
            UNSAFE_URL,
            INVENTED_SOURCE,
            SECRET_CANARY,
            RAW_PAYLOAD_CANARY,
            MACHINE_PATH_CANARY,
            FALSE_FACT_CANARY,
            str(workspace_root),
            str(checkpoint_path),
        ):
            assert canary not in public_output

        return cast(dict[str, object], state), list(events), model

    state, events, model = asyncio.run(scenario())

    assert state["final_answer"] is not None
    assert events
    assert not model.responses[Plan]
    assert not model.responses[ActionDecision]
    assert not model.responses[ReviewDecision]
    assert not model.responses[ReplacementWork]
