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

from mini_deerflow.actions import (
    ActionDecision,
    CompleteStepAction,
    ToolCallAction,
)
from mini_deerflow.answer_synthesis import AnswerSynthesisDraft, DraftClaim
from mini_deerflow.config import Settings
from mini_deerflow.context_budget import ContextBudget
from mini_deerflow.conversation import ConversationRecord, SQLiteConversationRepository
from mini_deerflow.decision import ActionContext
from mini_deerflow.delegation import (
    BoundedResearcherSubagent,
    BranchResult,
    ResearchTaskContext,
)
from mini_deerflow.demo.service import (
    DEFAULT_DEMO_GOAL,
    ContinueDemoCommand,
    ResumeDemoCommand,
    RunDemoCommand,
    _BackendResult,
)
from mini_deerflow.review import (
    ReplacementWork,
    ReviewDecision,
    ReviewFinding,
    ReviewVerdict,
)
from mini_deerflow.runtime import (
    ConversationSnapshot,
    RuntimeLimits,
    open_default_agent_runtime,
)
from mini_deerflow.sandbox import SessionSandboxResolver
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.structured_output import StructuredChatModel
from mini_deerflow.tools import ToolRegistry, WebFetchTool, WebSearchTool
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
    max_total_tool_calls=10,
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

    _SUPPORTED_SCHEMAS = (
        Plan,
        ActionDecision,
        ReviewDecision,
        ReplacementWork,
        AnswerSynthesisDraft,
    )

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
        if schema is AnswerSynthesisDraft:
            return self._answer_response(messages)
        raise AssertionError("offline scenario received an unsupported model request")

    def _answer_response(self, messages: object) -> AnswerSynthesisDraft:
        context = _tagged_message_payload(
            messages,
            opening="<answer_context>\n",
            closing="\n</answer_context>",
        )
        language = context.get("language")
        evidence = context.get("evidence")
        if language not in {"vi", "en"} or not isinstance(evidence, list):
            raise TypeError("offline answer context has an invalid shape")
        evidence_indices = list(range(1, len(evidence) + 1))
        if not evidence_indices:
            return AnswerSynthesisDraft(
                language=language,
                limitations=[
                    (
                        "Chưa thu thập được bằng chứng công khai phù hợp."
                        if language == "vi"
                        else "No suitable public evidence was collected."
                    )
                ],
            )
        return AnswerSynthesisDraft(
            language=language,
            summary=[
                DraftClaim(
                    text=(
                        "Kết quả được tổng hợp từ bằng chứng công khai đã xác thực."
                        if language == "vi"
                        else (
                            "The result is synthesized from validated public evidence."
                        )
                    ),
                    evidence_indices=evidence_indices,
                )
            ],
        )

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
                return CompleteStepAction(
                    type="complete_step",
                    summary=(
                        "Recorded deterministic safety and provider limitations "
                        "before delegated evidence collection."
                    ),
                    sources=[INVENTED_SOURCE, UNSAFE_URL],
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
                                "tool_call_budget": (1 if branch_id == "beta" else 2),
                                "delegation_depth": 1,
                            }
                            for branch_id in ("beta", "alpha")
                        ],
                    },
                )
            if remaining == 1:
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
                sources=[DELEGATED_SOURCE],
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
        if query == "delegated bounded evidence":
            return [
                SearchResult(
                    title="Delegated deterministic source",
                    url=DELEGATED_SOURCE,
                    snippet="Independent evidence from the alpha branch.",
                )
            ]
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


class _PartialBranchSelector:
    """Drive real bounded branch tools with deterministic decisions."""

    async def select_action(
        self,
        context: ActionContext,
    ) -> ToolCallAction | CompleteStepAction:
        branch_id = "beta" if "beta" in context.goal else "alpha"
        if not context.observations:
            query = (
                f"{SECRET_CANARY} controlled beta failure"
                if branch_id == "beta"
                else "delegated bounded evidence"
            )
            return ToolCallAction(
                type="tool_call",
                tool_name="web_search",
                arguments={"query": query},
            )
        return CompleteStepAction(
            type="complete_step",
            summary=(
                "The alpha branch found independent supporting evidence."
                if branch_id == "alpha"
                else "The beta branch could not verify its delegated claim."
            ),
            sources=[DELEGATED_SOURCE],
        )


class _PartialResearcher:
    def __init__(
        self,
        provider: _DeterministicWebProvider,
        resolver: _FakePublicResolver,
    ) -> None:
        self.calls: list[str] = []
        registry = ToolRegistry(
            [
                WebSearchTool(provider),
                WebFetchTool(provider, PublicWebTargetValidator(resolver)),
            ]
        )
        self._researcher = BoundedResearcherSubagent(
            _PartialBranchSelector(),
            registry,
            context_budget=DEMO_CONTEXT_BUDGET,
        )

    async def research(self, context: ResearchTaskContext) -> BranchResult:
        branch_id = context.task.branch_id
        self.calls.append(branch_id)
        await asyncio.sleep(0)
        return await self._researcher.research(context)


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
        provider = _DeterministicWebProvider()
        resolver = _FakePublicResolver()
        return cls(
            model=_ScenarioModel(
                goal,
                interrupt_after_delegation=interrupt_after_delegation,
            ),
            provider=provider,
            resolver=resolver,
            researcher=_PartialResearcher(provider, resolver),
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
        self._sandbox_resolver = SessionSandboxResolver(self._workspaces_root)
        self._resources: dict[tuple[str, str], _ScenarioResources] = {}
        self._traces: defaultdict[tuple[str, str], list[ExecutionTrace]] = defaultdict(
            list
        )
        self._operation_counts: Counter[tuple[str, str, str]] = Counter()
        self._interrupt_after_delegation = interrupt_after_delegation
        self._settings = Settings(
            api_key="offline-demo-placeholder",
            base_url="https://api.example.invalid/v1",
            model_name="offline-scripted-model",
            temperature=0.0,
            request_timeout=1.0,
            max_retries=0,
            wiki_request_timeout=1.0,
            structured_output_mode="native",
            _env_file=None,
        )

    @property
    def limits(self) -> RuntimeLimits:
        return DEMO_LIMITS

    async def run(
        self,
        command: RunDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        resource_key = (command.thread_id, command.turn_id)
        resources = self._resources.setdefault(
            resource_key,
            _ScenarioResources.create(
                command.goal,
                interrupt_after_delegation=self._interrupt_after_delegation,
            ),
        )
        return await self._execute(
            operation="run",
            thread_id=command.thread_id,
            goal=command.goal,
            turn_id=command.turn_id,
            resources=resources,
            trace_sink=trace_sink,
        )

    async def continue_thread(
        self,
        command: ContinueDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        resource_key = (command.thread_id, command.turn_id)
        resources = self._resources.setdefault(
            resource_key,
            _ScenarioResources.create(command.user_message),
        )
        return await self._execute(
            operation="continue",
            thread_id=command.thread_id,
            goal=command.user_message,
            turn_id=command.turn_id,
            resources=resources,
            trace_sink=trace_sink,
        )

    async def resume(
        self,
        command: ResumeDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        repository = SQLiteConversationRepository(self._checkpoint_path)
        active = await repository.get_active_turn(command.thread_id)
        if active is None:
            turns = await repository.list_turns(command.thread_id)
            active = turns[-1]
        resource_key = (command.thread_id, active.turn_id)
        resources = self._resources.setdefault(
            resource_key,
            _ScenarioResources.create(
                active.user_message or DEFAULT_DEMO_GOAL,
            ),
        )
        return await self._execute(
            operation="resume",
            thread_id=command.thread_id,
            goal=None,
            turn_id=active.turn_id,
            resources=resources,
            trace_sink=trace_sink,
        )

    async def list_threads(self) -> tuple[str, ...]:
        records = await self.list_conversations()
        return tuple(record.thread_id for record in records)

    async def list_conversations(self) -> tuple[ConversationRecord, ...]:
        repository = SQLiteConversationRepository(self._checkpoint_path)
        return await repository.list_conversations()

    async def load_conversation(self, thread_id: str) -> ConversationSnapshot:
        resources = _ScenarioResources.create(DEFAULT_DEMO_GOAL)

        def model_factory(settings: Settings) -> StructuredChatModel:
            if settings is not self._settings:
                raise AssertionError("offline runtime received unexpected settings")
            return cast(StructuredChatModel, resources.model)

        async with open_default_agent_runtime(
            self._settings,
            self._sandbox_resolver.resolve(thread_id),
            self._checkpoint_path,
            allow_write=True,
            limits=DEMO_LIMITS,
            context_budget=DEMO_CONTEXT_BUDGET,
            model_factory=model_factory,
            web_provider=resources.provider,
            web_target_validator=PublicWebTargetValidator(resources.resolver),
            researcher_subagent=resources.researcher,
            artifact_path=_ARTIFACT_PATH,
        ) as runtime:
            return await runtime.load_conversation(thread_id)

    def diagnostics(self, thread_id: str) -> OfflineScenarioDiagnostics | None:
        """Return count-only diagnostics without exposing prompts or payloads."""

        diagnostics = [
            resources.diagnostics()
            for (resource_thread_id, _), resources in self._resources.items()
            if resource_thread_id == thread_id
        ]
        if not diagnostics:
            return None
        return OfflineScenarioDiagnostics(
            model_calls=sum(item.model_calls for item in diagnostics),
            web_search_calls=sum(item.web_search_calls for item in diagnostics),
            web_fetch_calls=sum(item.web_fetch_calls for item in diagnostics),
            resolver_calls=sum(item.resolver_calls for item in diagnostics),
            researcher_branches=tuple(
                branch for item in diagnostics for branch in item.researcher_branches
            ),
        )

    async def _execute(
        self,
        *,
        operation: str,
        thread_id: str,
        goal: str | None,
        turn_id: str,
        resources: _ScenarioResources,
        trace_sink: TraceSink | None,
    ) -> _BackendResult:
        self._operation_counts[(thread_id, turn_id, operation)] += 1
        operation_number = self._operation_counts[(thread_id, turn_id, operation)]
        run_id = f"day15-{operation}-{operation_number:03d}"
        events = self._traces[(thread_id, turn_id)]
        recorder = _RecordingTraceSink(events, trace_sink)
        tracer = ExecutionTracer(
            recorder,
            clock=_DeterministicClock(),
            run_id_factory=lambda: run_id,
        )

        def model_factory(settings: Settings) -> StructuredChatModel:
            if settings is not self._settings:
                raise AssertionError("offline runtime received unexpected settings")
            return cast(StructuredChatModel, resources.model)

        async with open_default_agent_runtime(
            self._settings,
            self._sandbox_resolver.resolve(thread_id),
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
            try:
                if operation == "run":
                    if goal is None:
                        raise AssertionError("run requires a goal")
                    state = await runtime.run(
                        goal,
                        thread_id=thread_id,
                        turn_id=turn_id,
                    )
                elif operation == "continue":
                    if goal is None:
                        raise AssertionError("continue requires a user message")
                    state = await runtime.continue_thread(
                        thread_id,
                        goal,
                        turn_id=turn_id,
                    )
                else:
                    state = await runtime.resume(thread_id=thread_id)
            except BaseException:
                await runtime.record_turn_traces(thread_id, turn_id, tuple(events))
                raise
            await runtime.record_turn_traces(thread_id, turn_id, tuple(events))

        return _BackendResult(
            state=state,
            traces=tuple(events),
            limits=DEMO_LIMITS,
        )
