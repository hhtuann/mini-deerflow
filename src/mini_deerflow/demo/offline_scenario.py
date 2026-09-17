"""Deterministic, no-network runtime composition for the local mentor demo."""

from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

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
from mini_deerflow.delegation import BranchFinding, BranchResult, ResearchTaskContext
from mini_deerflow.demo.service import (
    DEFAULT_DEMO_GOAL,
    ResumeDemoCommand,
    RunDemoCommand,
    _BackendResult,
)
from mini_deerflow.persistence import list_thread_ids, open_sqlite_checkpointer
from mini_deerflow.review import (
    ReplacementWork,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
)
from mini_deerflow.runtime import RuntimeLimits, open_default_agent_runtime
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.tools import ToolResult
from mini_deerflow.tracing import ExecutionTrace, ExecutionTracer, TraceSink
from mini_deerflow.web import (
    FetchedPage,
    SearchResult,
    WebProviderErrorCategory,
    WebSearchError,
)
from mini_deerflow.web_safety import PublicWebTargetValidator, SafeWebTarget

SCENARIO_LABEL = "Mentor walkthrough v1"
SEARCH_SOURCE = "https://evidence.example/search"
FETCH_SOURCE = "https://evidence.example/report"
DELEGATED_SOURCE = "https://evidence.example/delegated"
UNSAFE_URL = "http://127.0.0.1/private"
INVENTED_SOURCE = "https://invented.invalid/fact"
SECRET_CANARY = "sk-day15-must-not-leak"
RAW_PAYLOAD_CANARY = "RAW_PROVIDER_PAYLOAD_MUST_NOT_LEAK"
MACHINE_PATH_CANARY = r"C:\Users\demo\.env"
LONG_EVIDENCE_TAIL = "DAY15_RAW_EVIDENCE_TAIL"
FALSE_FACT_CANARY = "BETA_FALSE_VERIFIED_FACT"

_DEFAULT_STORAGE_ROOT = Path(".mini-deerflow") / "demo"
_ARTIFACT_PATH = "reports/day-15-demo.md"

DEMO_LIMITS = RuntimeLimits(
    max_tool_calls_per_step=4,
    max_total_tool_calls=8,
    max_replan_cycles=1,
    recursion_limit=120,
    max_delegation_concurrency=2,
)
DEMO_CONTEXT_BUDGET = ContextBudget(
    max_total_chars=8_000,
    max_item_chars=800,
    retained_recent_items=5,
    max_excerpt_chars=600,
)


def _step(number: int, title: str) -> PlanStep:
    return PlanStep(
        step_number=number,
        title=title,
        objective=f"Collect deterministic evidence for {title.lower()}.",
        success_criteria="Record a bounded, evidence-backed result or limitation.",
    )


class _ScenarioRunnable:
    def __init__(self, model: _ScenarioModel, schema: type[object]) -> None:
        self._model = model
        self._schema = schema

    def invoke(self, messages: object) -> object:
        return self._model.respond(self._schema, messages)

    async def ainvoke(self, messages: object) -> object:
        return self._model.respond(self._schema, messages)


class _ScenarioModel:
    """Scripted structured-output model; it performs no transport calls."""

    _SUPPORTED_SCHEMAS = (Plan, ActionDecision, ReviewDecision, ReplacementWork)

    def __init__(
        self,
        goal: str,
        *,
        interrupt_after_delegation: bool = False,
    ) -> None:
        self._goal = goal
        self._interrupt_after_delegation = interrupt_after_delegation
        self.invocation_counts: Counter[type[object]] = Counter()

    def with_structured_output(
        self,
        schema: type[object],
        *,
        method: str,
    ) -> _ScenarioRunnable:
        if method != "json_mode" or schema not in self._SUPPORTED_SCHEMAS:
            raise AssertionError(
                "offline scenario received an unsupported model request"
            )
        return _ScenarioRunnable(self, schema)

    def respond(self, schema: type[object], messages: object) -> object:
        self.invocation_counts[schema] += 1
        if schema is Plan:
            return Plan(
                goal=self._goal,
                steps=[
                    _step(1, "Collect primary web evidence"),
                    _step(2, "Compare independent evidence"),
                    _step(3, "Conclude with explicit limitations"),
                ],
            )
        if schema is ReplacementWork:
            return ReplacementWork(
                steps=[
                    _step(1, "Delegate independent evidence checks"),
                    _step(2, "Render findings and limitations"),
                ]
            )
        if schema is ActionDecision:
            return self._action_response(messages)
        if schema is ReviewDecision:
            return self._review_response(messages)
        raise AssertionError("offline scenario received an unsupported model request")

    def _action_response(self, messages: object) -> object:
        context = _tagged_message_payload(
            messages,
            opening="<action_context>\n",
            closing="\n</action_context>",
        )
        step = context.get("step")
        remaining = context.get("remaining_step_tool_calls")
        if not isinstance(step, dict) or not isinstance(remaining, int):
            raise TypeError("offline action context has an invalid shape")
        title = step.get("title")

        if title == "Collect primary web evidence":
            if remaining == 4:
                return ToolCallAction(
                    type="tool_call",
                    tool_name="web_fetch",
                    arguments={"url": UNSAFE_URL},
                )
            if remaining == 3:
                return ToolCallAction(
                    type="tool_call",
                    tool_name="web_search",
                    arguments={
                        "query": (
                            f"{SECRET_CANARY} {RAW_PAYLOAD_CANARY} "
                            f"{MACHINE_PATH_CANARY}"
                        )
                    },
                )
            if remaining == 2:
                return ToolCallAction(
                    type="tool_call",
                    tool_name="web_search",
                    arguments={"query": "bounded final mvp evidence"},
                )
            if remaining == 1:
                return ToolCallAction(
                    type="tool_call",
                    tool_name="web_fetch",
                    arguments={"url": FETCH_SOURCE},
                )
            if remaining == 0:
                return CompleteStepAction(
                    type="complete_step",
                    summary="Collected safe search and fetch evidence for the MVP.",
                    sources=[FETCH_SOURCE, INVENTED_SOURCE, UNSAFE_URL],
                )

        if title == "Delegate independent evidence checks":
            if remaining == 4:
                return ToolCallAction(
                    type="tool_call",
                    tool_name="delegate_research",
                    arguments={
                        "delegation_id": "day15-wave",
                        "tasks": [
                            {
                                "branch_id": branch_id,
                                "objective": (
                                    "Research independent MVP evidence for "
                                    f"{branch_id}."
                                ),
                                "success_criteria": (
                                    "Return one bounded finding or a controlled "
                                    "limitation."
                                ),
                                "tool_call_budget": 1,
                                "delegation_depth": 1,
                            }
                            for branch_id in ("beta", "alpha")
                        ],
                    },
                )
            if remaining == 2:
                if self._interrupt_after_delegation:
                    raise RuntimeError(
                        "deterministic interruption after delegation checkpoint"
                    )
                return CompleteStepAction(
                    type="complete_step",
                    summary=(
                        "Integrated the successful branch and retained the failed "
                        "branch as a limitation."
                    ),
                    sources=[DELEGATED_SOURCE],
                )

        if title == "Render findings and limitations" and remaining == 4:
            return CompleteStepAction(
                type="complete_step",
                summary=(
                    "Concluded the bounded MVP demonstration from validated evidence."
                ),
                sources=[SEARCH_SOURCE],
            )

        raise AssertionError(
            f"unsupported offline action context: title={title!r}, remaining={remaining!r}"
        )

    def _review_response(self, messages: object) -> ReviewVerdict:
        context = _tagged_message_payload(
            messages,
            opening="<review_context>\n",
            closing="\n</review_context>",
        )
        remaining_steps = context.get("remaining_steps")
        remaining_replans = context.get("remaining_replan_cycles")
        if not isinstance(remaining_steps, list) or not isinstance(
            remaining_replans,
            int,
        ):
            raise TypeError("offline review context has an invalid shape")
        remaining_titles = tuple(
            step.get("title") for step in remaining_steps if isinstance(step, dict)
        )

        if remaining_replans == 1 and remaining_titles == (
            "Compare independent evidence",
            "Conclude with explicit limitations",
        ):
            return ReviewVerdict(
                verdict="replan",
                rationale=(
                    "The first evidence pass is useful, but independent coverage "
                    "and an explicit limitation route are still required."
                ),
                findings=[
                    ReviewFinding(
                        category="gap",
                        description=(
                            "Add independent bounded research and preserve any partial "
                            "failure as a limitation."
                        ),
                        related_step_numbers=[2, 3],
                    )
                ],
            )
        if remaining_replans == 0 and remaining_titles == (
            "Render findings and limitations",
        ):
            return ReviewVerdict(
                verdict="continue",
                rationale=(
                    "Validated delegated evidence is present and the final limitation "
                    "summary remains to be completed."
                ),
            )
        if remaining_replans == 0 and not remaining_titles:
            return ReviewVerdict(
                verdict="finish",
                rationale=(
                    "The evidence-backed findings and explicit limitation now cover "
                    "the bounded demonstration goal."
                ),
            )
        raise AssertionError(
            "unsupported offline review context: "
            f"remaining={remaining_titles!r}, replans={remaining_replans!r}"
        )


def _tagged_message_payload(
    messages: object,
    *,
    opening: str,
    closing: str,
) -> dict[str, object]:
    if not isinstance(messages, list):
        raise TypeError("offline model messages must be a list")
    for message in reversed(messages):
        if not isinstance(message, HumanMessage) or not isinstance(
            message.content,
            str,
        ):
            continue
        if opening not in message.content or closing not in message.content:
            continue
        raw_payload = message.content.split(opening, 1)[1].split(closing, 1)[0]
        payload = json.loads(raw_payload)
        if isinstance(payload, dict):
            return payload
        break
    raise AssertionError("offline model message omitted its structured context")


class _FakePublicResolver:
    def __init__(self) -> None:
        self.call_count = 0

    async def resolve(self, hostname: str, port: int) -> Sequence[str]:
        del hostname, port
        self.call_count += 1
        return ("93.184.216.34",)


class _DeterministicWebProvider:
    def __init__(self) -> None:
        self.search_call_count = 0
        self.fetch_call_count = 0

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        del max_results
        self.search_call_count += 1
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
        self.fetch_call_count += 1
        return FetchedPage(
            url=target.url,
            title="Long deterministic evidence",
            content=(
                "Raw evidence opening. " + "E" * 12_000 + f" {LONG_EVIDENCE_TAIL}"
            ),
            status_code=200,
            content_type="text/markdown",
        )


class _PartialResearcher:
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
                            "snippet": ("Independent evidence from the alpha branch."),
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


@dataclass(frozen=True, slots=True)
class OfflineScenarioDiagnostics:
    """Count-only diagnostics used to prove completed resume does not replay work."""

    model_calls: int
    web_search_calls: int
    web_fetch_calls: int
    resolver_calls: int
    researcher_branches: tuple[str, ...]


@dataclass(slots=True)
class _ScenarioResources:
    model: _ScenarioModel
    provider: _DeterministicWebProvider
    resolver: _FakePublicResolver
    researcher: _PartialResearcher

    @classmethod
    def create(
        cls,
        goal: str,
        *,
        interrupt_after_delegation: bool = False,
    ) -> _ScenarioResources:
        return cls(
            model=_ScenarioModel(
                goal,
                interrupt_after_delegation=interrupt_after_delegation,
            ),
            provider=_DeterministicWebProvider(),
            resolver=_FakePublicResolver(),
            researcher=_PartialResearcher(),
        )

    def diagnostics(self) -> OfflineScenarioDiagnostics:
        return OfflineScenarioDiagnostics(
            model_calls=sum(self.model.invocation_counts.values()),
            web_search_calls=self.provider.search_call_count,
            web_fetch_calls=self.provider.fetch_call_count,
            resolver_calls=self.resolver.call_count,
            researcher_branches=tuple(self.researcher.calls),
        )


class _DeterministicClock:
    def __init__(self) -> None:
        self._value = 0.0

    def __call__(self) -> float:
        value = self._value
        self._value += 0.001
        return value


class _RecordingTraceSink:
    def __init__(
        self,
        events: list[ExecutionTrace],
        downstream: TraceSink | None,
    ) -> None:
        self._events = events
        self._downstream = downstream

    def emit(self, event: ExecutionTrace) -> None:
        self._events.append(event)
        if self._downstream is not None:
            self._downstream.emit(event)


class OfflineDemoBackend:
    """Compose the real runtime with deterministic, in-process external seams."""

    def __init__(
        self,
        storage_root: str | Path = _DEFAULT_STORAGE_ROOT,
        *,
        interrupt_after_delegation: bool = False,
    ) -> None:
        if not isinstance(interrupt_after_delegation, bool):
            raise TypeError("interrupt_after_delegation must be a boolean")
        self._storage_root = Path(storage_root).resolve(strict=False)
        self._checkpoint_path = self._storage_root / "checkpoints.sqlite"
        self._workspaces_root = self._storage_root / "workspaces"
        self._resources: dict[str, _ScenarioResources] = {}
        self._traces: defaultdict[str, list[ExecutionTrace]] = defaultdict(list)
        self._operation_counts: Counter[tuple[str, str]] = Counter()
        self._interrupt_after_delegation = interrupt_after_delegation
        self._settings = Settings(
            api_key="offline-demo-placeholder",
            base_url="https://offline.invalid/v1",
            jina_api_key=None,
            model_name="offline-scripted-model",
            temperature=0.0,
            request_timeout=1.0,
            max_retries=0,
            web_request_timeout=1.0,
            web_max_response_bytes=1_024,
            _env_file=None,
        )

    async def run(
        self,
        command: RunDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        resources = self._resources.setdefault(
            command.thread_id,
            _ScenarioResources.create(
                command.goal,
                interrupt_after_delegation=self._interrupt_after_delegation,
            ),
        )
        return await self._execute(
            operation="run",
            thread_id=command.thread_id,
            goal=command.goal,
            resources=resources,
            trace_sink=trace_sink,
        )

    async def resume(
        self,
        command: ResumeDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        resources = self._resources.setdefault(
            command.thread_id,
            _ScenarioResources.create(
                DEFAULT_DEMO_GOAL,
                interrupt_after_delegation=self._interrupt_after_delegation,
            ),
        )
        return await self._execute(
            operation="resume",
            thread_id=command.thread_id,
            goal=None,
            resources=resources,
            trace_sink=trace_sink,
        )

    async def list_threads(self) -> tuple[str, ...]:
        async with open_sqlite_checkpointer(self._checkpoint_path) as checkpointer:
            return await list_thread_ids(checkpointer)

    def diagnostics(self, thread_id: str) -> OfflineScenarioDiagnostics | None:
        """Return count-only diagnostics without exposing prompts or payloads."""

        resources = self._resources.get(thread_id)
        return None if resources is None else resources.diagnostics()

    async def _execute(
        self,
        *,
        operation: str,
        thread_id: str,
        goal: str | None,
        resources: _ScenarioResources,
        trace_sink: TraceSink | None,
    ) -> _BackendResult:
        self._operation_counts[(thread_id, operation)] += 1
        operation_number = self._operation_counts[(thread_id, operation)]
        run_id = f"day15-{operation}-{operation_number:03d}"
        events = self._traces[thread_id]
        recorder = _RecordingTraceSink(events, trace_sink)
        tracer = ExecutionTracer(
            recorder,
            clock=_DeterministicClock(),
            run_id_factory=lambda: run_id,
        )

        def model_factory(settings: Settings) -> ChatOpenAI:
            if settings is not self._settings:
                raise AssertionError("offline runtime received unexpected settings")
            return cast(ChatOpenAI, resources.model)

        async with open_default_agent_runtime(
            self._settings,
            self._workspaces_root / thread_id,
            self._checkpoint_path,
            allow_write=True,
            limits=DEMO_LIMITS,
            context_budget=DEMO_CONTEXT_BUDGET,
            model_factory=model_factory,
            web_provider=resources.provider,
            web_target_validator=PublicWebTargetValidator(resources.resolver),
            researcher_subagent=resources.researcher,
            artifact_path=_ARTIFACT_PATH,
            tracer=tracer,
        ) as runtime:
            if operation == "run":
                if goal is None:
                    raise AssertionError("run requires a goal")
                state = await runtime.run(goal, thread_id=thread_id)
            else:
                state = await runtime.resume(thread_id=thread_id)

        return _BackendResult(
            state=state,
            traces=tuple(events),
            limits=DEMO_LIMITS,
        )
