import sqlite3
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable

from langgraph.checkpoint.base import BaseCheckpointSaver

from mini_deerflow.agent_workflow import build_agent_workflow
from mini_deerflow.answer_synthesis import (
    AnswerSynthesizer,
    LLMAnswerSynthesizer,
)
from mini_deerflow.config import Settings
from mini_deerflow.context_budget import ContextBudget
from mini_deerflow.conversation import (
    ActiveTurnError,
    ConversationAlreadyExistsError,
    ConversationContext,
    ConversationNotFoundError,
    ConversationRecord,
    IdempotencyConflictError,
    SessionBudgetExceededError,
    SQLiteConversationRepository,
    TurnNotFoundError,
    TurnRecord,
)
from mini_deerflow.decision import ActionSelector
from mini_deerflow.delegation import (
    BoundedResearcherSubagent,
    DelegateResearchTool,
    ResearcherSubagent,
)
from mini_deerflow.llm_reviewer import LLMReviewer
from mini_deerflow.llm_selector import LLMActionSelector
from mini_deerflow.model import create_chat_model
from mini_deerflow.persistence import (
    AsyncCheckpointReader,
    CheckpointStorageError,
    CheckpointUnavailableError,
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
    checkpoint_lookup_config,
    create_thread_config,
    open_sqlite_checkpointer,
)
from mini_deerflow.planner import PlanningBudget, create_research_plan
from mini_deerflow.replanner import create_replacement_plan
from mini_deerflow.review import EvidenceReviewer, Replanner
from mini_deerflow.schemas import Plan
from mini_deerflow.state import AgentState, create_initial_state
from mini_deerflow.structured_output import StructuredChatModel
from mini_deerflow.tools import (
    ListFilesTool,
    ReadFileTool,
    ToolRegistry,
    WebFetchTool,
    WebSearchTool,
    WikiLookupTool,
    WikiSearchTool,
    WriteFileTool,
)
from mini_deerflow.tracing import (
    ExecutionTrace,
    ExecutionTracer,
    TraceErrorCategory,
    TraceErrorCode,
    TraceKind,
    TraceOutcome,
    TracePhase,
)
from mini_deerflow.web import WebProvider
from mini_deerflow.web_safety import (
    PublicWebTargetValidator,
    SystemHostResolver,
    WebTargetValidator,
)
from mini_deerflow.wikipedia import MediaWikiProvider, WikipediaProvider
from mini_deerflow.workspace import Workspace

Planner = Callable[[str], Plan]
ModelFactory = Callable[[Settings], StructuredChatModel]


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """Bound execution resources for one agent run."""

    max_tool_calls_per_step: int = 5
    max_total_tool_calls: int = 20
    max_replan_cycles: int = 2
    recursion_limit: int = 100
    max_delegation_concurrency: int = 2
    max_session_tool_calls: int = 100
    max_conversation_context_chars: int = 12_000

    def __post_init__(self) -> None:
        for name in (
            "max_tool_calls_per_step",
            "max_total_tool_calls",
            "max_replan_cycles",
            "recursion_limit",
            "max_session_tool_calls",
            "max_conversation_context_chars",
        ):
            value = getattr(self, name)

            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")

            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")

        if isinstance(self.max_delegation_concurrency, bool) or not isinstance(
            self.max_delegation_concurrency, int
        ):
            raise TypeError("max_delegation_concurrency must be an integer")

        if not 1 <= self.max_delegation_concurrency <= 3:
            raise ValueError("max_delegation_concurrency must be between 1 and 3")


@runtime_checkable
class AgentGraph(Protocol):
    """Minimal compiled-graph interface required by AgentRuntime."""

    async def ainvoke(
        self,
        state: AgentState | None,
        *,
        config: dict[str, object],
    ) -> object:
        """Execute or resume the graph."""


@dataclass(frozen=True, slots=True)
class ConversationTurnSnapshot:
    """One persisted turn with its isolated checkpointed state and safe trace."""

    record: TurnRecord
    state: AgentState | None
    traces: tuple[ExecutionTrace, ...]


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """A persisted conversation assembled from ledger and turn checkpoints."""

    conversation: ConversationRecord
    turns: tuple[ConversationTurnSnapshot, ...]


async def _read_checkpoint(
    checkpointer: AsyncCheckpointReader,
    config: dict[str, object],
) -> object | None:
    """Read one checkpoint while preserving a narrow storage boundary."""

    try:
        return await checkpointer.aget_tuple(config)
    except sqlite3.Error as error:
        raise CheckpointStorageError(
            "could not read checkpoint data",
        ) from error


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Execute a compiled research graph with bounded runtime settings."""

    graph: AgentGraph
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)
    checkpointer: AsyncCheckpointReader | None = None
    tracer: ExecutionTracer = field(default_factory=ExecutionTracer)
    conversation_repository: SQLiteConversationRepository | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.graph, AgentGraph):
            raise TypeError("graph must support async invocation")

        if not isinstance(self.limits, RuntimeLimits):
            raise TypeError("limits must be RuntimeLimits")

        if self.checkpointer is not None and not isinstance(
            self.checkpointer,
            AsyncCheckpointReader,
        ):
            raise TypeError(
                "checkpointer must support async checkpoint lookup",
            )

        if not isinstance(self.tracer, ExecutionTracer):
            raise TypeError("tracer must be ExecutionTracer")
        if self.conversation_repository is not None and not isinstance(
            self.conversation_repository,
            SQLiteConversationRepository,
        ):
            raise TypeError(
                "conversation_repository must be SQLiteConversationRepository"
            )

    async def run(
        self,
        goal: str,
        *,
        thread_id: str,
        turn_id: str | None = None,
    ) -> AgentState:
        """Run one research goal and return its final state."""

        if self.conversation_repository is not None:
            resolved_turn_id = turn_id or _new_turn_id()
            if self.checkpointer is not None:
                legacy_config = create_thread_config(
                    thread_id,
                    recursion_limit=self.limits.recursion_limit,
                )
                if await _read_checkpoint(self.checkpointer, legacy_config) is not None:
                    raise ThreadAlreadyExistsError(
                        f"thread {thread_id!r} already has a checkpoint"
                    )
            try:
                turn = await self.conversation_repository.create_conversation_with_turn(
                    thread_id=thread_id,
                    turn_id=resolved_turn_id,
                    user_message=goal,
                    session_tool_call_limit=self.limits.max_session_tool_calls,
                    reserved_tool_calls=self.limits.max_total_tool_calls,
                )
            except ConversationAlreadyExistsError as error:
                try:
                    existing = await self.conversation_repository.get_turn(
                        thread_id,
                        resolved_turn_id,
                    )
                except TurnNotFoundError:
                    raise ThreadAlreadyExistsError(
                        f"thread {thread_id!r} already has a checkpoint"
                    ) from error
                if existing.user_message != goal.strip():
                    raise IdempotencyConflictError(
                        "turn_id is already bound to a different goal"
                    ) from error
                if existing.status == "completed":
                    return await self._load_turn_state(existing)
                raise ActiveTurnError("the first turn is already active") from error
            return await self._execute_reserved_turn(
                turn,
                operation="run",
                context=ConversationContext(),
            )

        with self.tracer.run_scope(thread_id, "run", turn_id=turn_id):
            initial_state = create_initial_state(goal)

            config = create_thread_config(
                thread_id,
                recursion_limit=self.limits.recursion_limit,
            )

            if self.checkpointer is not None:
                checkpoint = await _read_checkpoint(
                    self.checkpointer,
                    config,
                )

                if checkpoint is not None:
                    self.tracer.emit(
                        kind=TraceKind.CHECKPOINT,
                        phase=TracePhase.OUTCOME,
                        outcome=TraceOutcome.FAILED,
                        operation="run",
                        error_category=TraceErrorCategory.PERSISTENCE,
                        error_code=TraceErrorCode.CHECKPOINT_FAILURE,
                    )
                    raise ThreadAlreadyExistsError(
                        f"thread {thread_id!r} already has a checkpoint",
                    )
                checkpoint_outcome = TraceOutcome.READY
            else:
                checkpoint_outcome = TraceOutcome.SKIPPED

            self.tracer.emit(
                kind=TraceKind.CHECKPOINT,
                phase=TracePhase.OUTCOME,
                outcome=checkpoint_outcome,
                operation="run",
            )

            result = await self.graph.ainvoke(
                initial_state,
                config=config,
            )

            return _validate_agent_state_result(result)

    async def continue_thread(
        self,
        thread_id: str,
        user_message: str,
        *,
        turn_id: str | None = None,
    ) -> AgentState:
        """Create a new conversational turn on one persisted public thread."""

        if self.conversation_repository is None:
            raise CheckpointUnavailableError(
                "continue_thread requires a configured conversation repository"
            )
        resolved_turn_id = turn_id or _new_turn_id()
        turn, created = await self.conversation_repository.reserve_follow_up(
            thread_id=thread_id,
            turn_id=resolved_turn_id,
            user_message=user_message,
            reserved_tool_calls=self.limits.max_total_tool_calls,
        )
        if not created:
            if turn.status == "completed":
                return await self._load_turn_state(turn)
            raise ActiveTurnError("the submitted turn is already active")
        try:
            context = await self.conversation_repository.load_context(
                thread_id,
                before_sequence=turn.sequence,
                max_characters=self.limits.max_conversation_context_chars,
            )
        except BaseException:
            await self.conversation_repository.mark_turn_failed(
                thread_id=thread_id,
                turn_id=turn.turn_id,
                safe_error="Turn context could not be prepared.",
            )
            raise
        return await self._execute_reserved_turn(
            turn,
            operation="continue",
            context=context,
        )

    async def resume(
        self,
        *,
        thread_id: str,
    ) -> AgentState:
        """Continue an existing persisted thread."""

        if self.conversation_repository is not None:
            try:
                turns = await self.conversation_repository.list_turns(thread_id)
            except ConversationNotFoundError:
                pass
            else:
                active = next(
                    (
                        turn
                        for turn in reversed(turns)
                        if turn.status in {"running", "interrupted"}
                    ),
                    None,
                )
                if active is None:
                    if not turns:
                        raise ThreadNotFoundError(f"thread {thread_id!r} has no turns")
                    return await self._load_turn_state(turns[-1])
                if self.limits.max_total_tool_calls > active.reserved_tool_calls:
                    raise SessionBudgetExceededError(
                        "resume limit exceeds the turn's durable reservation"
                    )
                context = await self.conversation_repository.load_context(
                    thread_id,
                    before_sequence=active.sequence,
                    max_characters=self.limits.max_conversation_context_chars,
                )
                if await self._try_load_turn_state(active) is None:
                    return await self._execute_reserved_turn(
                        active,
                        operation="resume",
                        context=context,
                    )
                return await self._resume_conversation_turn(active, context)

        with self.tracer.run_scope(thread_id, "resume"):
            config = create_thread_config(
                thread_id,
                recursion_limit=self.limits.recursion_limit,
            )

            if self.checkpointer is None:
                self.tracer.emit(
                    kind=TraceKind.CHECKPOINT,
                    phase=TracePhase.OUTCOME,
                    outcome=TraceOutcome.FAILED,
                    operation="resume",
                    error_category=TraceErrorCategory.PERSISTENCE,
                    error_code=TraceErrorCode.CHECKPOINT_FAILURE,
                )
                raise CheckpointUnavailableError(
                    "resume requires a configured checkpointer",
                )

            checkpoint = await _read_checkpoint(
                self.checkpointer,
                config,
            )

            if checkpoint is None:
                self.tracer.emit(
                    kind=TraceKind.CHECKPOINT,
                    phase=TracePhase.OUTCOME,
                    outcome=TraceOutcome.FAILED,
                    operation="resume",
                    error_category=TraceErrorCategory.PERSISTENCE,
                    error_code=TraceErrorCode.CHECKPOINT_FAILURE,
                )
                raise ThreadNotFoundError(
                    f"thread {thread_id!r} has no checkpoint",
                )

            self.tracer.emit(
                kind=TraceKind.CHECKPOINT,
                phase=TracePhase.OUTCOME,
                outcome=TraceOutcome.RESUMED,
                operation="resume",
            )

            result = await self.graph.ainvoke(
                None,
                config=config,
            )

            return _validate_agent_state_result(result)

    async def list_conversations(self) -> tuple[ConversationRecord, ...]:
        if self.conversation_repository is None:
            return ()
        return await self.conversation_repository.list_conversations()

    async def load_conversation(self, thread_id: str) -> ConversationSnapshot:
        if self.conversation_repository is None:
            raise CheckpointUnavailableError(
                "conversation loading requires a configured repository"
            )
        conversation = await self.conversation_repository.get_conversation(thread_id)
        turns = await self.conversation_repository.list_turns(thread_id)
        snapshots: list[ConversationTurnSnapshot] = []
        for turn in turns:
            state = await self._try_load_turn_state(turn)
            traces = tuple(
                ExecutionTrace.model_validate_json(item) for item in turn.trace_events
            )
            snapshots.append(
                ConversationTurnSnapshot(
                    record=turn,
                    state=state,
                    traces=traces,
                )
            )
        return ConversationSnapshot(
            conversation=conversation,
            turns=tuple(snapshots),
        )

    async def record_turn_traces(
        self,
        thread_id: str,
        turn_id: str,
        events: tuple[ExecutionTrace, ...],
    ) -> None:
        if self.conversation_repository is None:
            return
        existing = await self.conversation_repository.get_turn(thread_id, turn_id)
        serialized = tuple(event.model_dump_json() for event in events)
        merged = tuple(dict.fromkeys(existing.trace_events + serialized))
        await self.conversation_repository.store_trace_events(
            thread_id,
            turn_id,
            merged,
        )

    async def _execute_reserved_turn(
        self,
        turn: TurnRecord,
        *,
        operation: Literal["run", "continue", "resume"],
        context: ConversationContext,
    ) -> AgentState:
        config = create_thread_config(
            turn.thread_id,
            recursion_limit=self.limits.recursion_limit,
            checkpoint_namespace=turn.checkpoint_namespace,
        )
        state = create_initial_state(
            turn.user_message,
            turn_id=turn.turn_id,
            turn_sequence=turn.sequence,
            conversation_context=context,
        )
        run_id = ""
        try:
            with self.tracer.run_scope(
                turn.thread_id,
                operation,
                turn_id=turn.turn_id,
            ) as run_id:
                self.tracer.emit(
                    kind=TraceKind.CHECKPOINT,
                    phase=TracePhase.OUTCOME,
                    outcome=TraceOutcome.READY,
                    operation=operation,
                )
                result = _validate_agent_state_result(
                    await self.graph.ainvoke(state, config=config)
                )
        except BaseException:
            interrupted = await self._try_checkpoint_state(config)
            await self.record_turn_traces(
                turn.thread_id,
                turn.turn_id,
                self.tracer.events_for_run(run_id),
            )
            await self.conversation_repository.mark_turn_interrupted(
                thread_id=turn.thread_id,
                turn_id=turn.turn_id,
                total_tool_calls=(
                    interrupted.get("total_tool_calls", 0)
                    if interrupted is not None
                    else 0
                ),
            )
            raise
        assert self.conversation_repository is not None
        await self.conversation_repository.complete_turn(
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            assistant_response=result.get("final_answer") or "",
            total_tool_calls=result.get("total_tool_calls", 0),
        )
        await self.record_turn_traces(
            turn.thread_id,
            turn.turn_id,
            self.tracer.events_for_run(run_id),
        )
        return result

    async def _resume_conversation_turn(
        self,
        turn: TurnRecord,
        context: ConversationContext,
    ) -> AgentState:
        del context  # The interrupted checkpoint already owns its projected context.
        config = create_thread_config(
            turn.thread_id,
            recursion_limit=self.limits.recursion_limit,
            checkpoint_namespace=turn.checkpoint_namespace,
        )
        run_id = ""
        try:
            with self.tracer.run_scope(
                turn.thread_id,
                "resume",
                turn_id=turn.turn_id,
            ) as run_id:
                self.tracer.emit(
                    kind=TraceKind.CHECKPOINT,
                    phase=TracePhase.OUTCOME,
                    outcome=TraceOutcome.RESUMED,
                    operation="resume",
                )
                result = _validate_agent_state_result(
                    await self.graph.ainvoke(None, config=config)
                )
        except BaseException:
            interrupted = await self._try_checkpoint_state(config)
            await self.record_turn_traces(
                turn.thread_id,
                turn.turn_id,
                self.tracer.events_for_run(run_id),
            )
            assert self.conversation_repository is not None
            await self.conversation_repository.mark_turn_interrupted(
                thread_id=turn.thread_id,
                turn_id=turn.turn_id,
                total_tool_calls=(
                    interrupted.get("total_tool_calls", 0)
                    if interrupted is not None
                    else turn.total_tool_calls
                ),
            )
            raise
        assert self.conversation_repository is not None
        await self.conversation_repository.complete_turn(
            thread_id=turn.thread_id,
            turn_id=turn.turn_id,
            assistant_response=result.get("final_answer") or "",
            total_tool_calls=result.get("total_tool_calls", 0),
        )
        await self.record_turn_traces(
            turn.thread_id,
            turn.turn_id,
            self.tracer.events_for_run(run_id),
        )
        return result

    async def _load_turn_state(self, turn: TurnRecord) -> AgentState:
        state = await self._try_load_turn_state(turn)
        if state is None:
            raise CheckpointUnavailableError("turn checkpoint is unavailable")
        return state

    async def _try_load_turn_state(self, turn: TurnRecord) -> AgentState | None:
        config = create_thread_config(
            turn.thread_id,
            recursion_limit=self.limits.recursion_limit,
            checkpoint_namespace=turn.checkpoint_namespace,
        )
        return await self._try_checkpoint_state(config)

    async def _try_checkpoint_state(
        self,
        config: dict[str, object],
    ) -> AgentState | None:
        if self.checkpointer is None:
            return None
        checkpoint_tuple = await _read_checkpoint(
            self.checkpointer,
            checkpoint_lookup_config(config),
        )
        if checkpoint_tuple is None:
            return None
        checkpoint = getattr(checkpoint_tuple, "checkpoint", None)
        if not isinstance(checkpoint, dict):
            return None
        channel_values = checkpoint.get("channel_values")
        if not isinstance(channel_values, dict):
            return None
        return cast(AgentState, channel_values)


def _validate_agent_state_result(
    result: object,
) -> AgentState:
    if not isinstance(result, dict):
        raise TypeError("graph must return a state dictionary")

    return cast(AgentState, result)


def build_agent_runtime(
    planner: Planner,
    action_selector: ActionSelector,
    registry: ToolRegistry,
    *,
    action_registry: ToolRegistry | None = None,
    reviewer: EvidenceReviewer | None = None,
    replanner: Replanner | None = None,
    answer_synthesizer: AnswerSynthesizer | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    limits: RuntimeLimits | None = None,
    context_budget: ContextBudget | None = None,
    artifact_path: str | None = None,
    tracer: ExecutionTracer | None = None,
    conversation_repository: SQLiteConversationRepository | None = None,
) -> AgentRuntime:
    """Build a testable runtime from explicitly supplied dependencies."""

    resolved_limits = limits or RuntimeLimits()
    resolved_tracer = tracer or ExecutionTracer()

    graph = build_agent_workflow(
        planner,
        action_selector,
        registry,
        action_registry=action_registry,
        reviewer=reviewer,
        replanner=replanner,
        answer_synthesizer=answer_synthesizer,
        checkpointer=checkpointer,
        max_tool_calls_per_step=(resolved_limits.max_tool_calls_per_step),
        max_total_tool_calls=resolved_limits.max_total_tool_calls,
        max_replan_cycles=resolved_limits.max_replan_cycles,
        context_budget=context_budget,
        artifact_path=artifact_path,
        tracer=resolved_tracer,
    )

    return AgentRuntime(
        graph=graph,
        limits=resolved_limits,
        checkpointer=checkpointer,
        tracer=resolved_tracer,
        conversation_repository=conversation_repository,
    )


def create_default_agent_runtime(
    settings: Settings,
    workspace_root: str | Path,
    *,
    allow_write: bool = False,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    limits: RuntimeLimits | None = None,
    context_budget: ContextBudget | None = None,
    model_factory: ModelFactory = create_chat_model,
    web_provider: WebProvider | None = None,
    web_target_validator: WebTargetValidator | None = None,
    wiki_provider: WikipediaProvider | None = None,
    researcher_subagent: ResearcherSubagent | None = None,
    artifact_path: str = "reports/research-report.md",
    tracer: ExecutionTracer | None = None,
    conversation_repository: SQLiteConversationRepository | None = None,
) -> AgentRuntime:
    """Create the default local Mini DeerFlow runtime."""

    if not isinstance(allow_write, bool):
        raise TypeError("allow_write must be a boolean")

    model = model_factory(settings)
    structured_output_mode = settings.structured_output_mode
    workspace = Workspace(workspace_root)

    resolved_limits = limits or RuntimeLimits()
    if web_provider is None:
        resolved_wiki_provider = wiki_provider or MediaWikiProvider(
            timeout_seconds=settings.wiki_request_timeout,
        )
        research_tools = [
            WikiSearchTool(resolved_wiki_provider),
            WikiLookupTool(resolved_wiki_provider),
        ]
    else:
        # Explicit dependency injection keeps the deterministic legacy web
        # scenarios available for safety/resume tests. The production default
        # never enters this branch and therefore registers only Wikipedia tools.
        resolved_target_validator = web_target_validator or PublicWebTargetValidator(
            SystemHostResolver()
        )
        research_tools = [
            WebSearchTool(web_provider),
            WebFetchTool(web_provider, resolved_target_validator),
        ]

    branch_registry = ToolRegistry(research_tools)
    resolved_context_budget = context_budget or ContextBudget()
    resolved_researcher = researcher_subagent or BoundedResearcherSubagent(
        LLMActionSelector(
            model,
            structured_output_mode=structured_output_mode,
        ),
        branch_registry,
        context_budget=resolved_context_budget,
    )
    delegation_tool = DelegateResearchTool(
        resolved_researcher,
        branch_registry,
        max_concurrency=resolved_limits.max_delegation_concurrency,
        context_budget=resolved_context_budget,
    )

    action_tools = [
        ListFilesTool(workspace),
        ReadFileTool(workspace),
        *research_tools,
        delegation_tool,
    ]
    execution_tools = list(action_tools)

    if allow_write:
        execution_tools.append(
            WriteFileTool(workspace),
        )

    action_registry = ToolRegistry(action_tools)
    registry = ToolRegistry(execution_tools)

    planner = partial(
        create_research_plan,
        model,
        available_tools=action_registry.definitions(),
        planning_budget=PlanningBudget(
            max_tool_calls_per_step=resolved_limits.max_tool_calls_per_step,
            max_total_tool_calls=resolved_limits.max_total_tool_calls,
            max_replan_cycles=resolved_limits.max_replan_cycles,
        ),
        structured_output_mode=structured_output_mode,
    )

    action_selector = LLMActionSelector(
        model,
        structured_output_mode=structured_output_mode,
    )
    reviewer = LLMReviewer(
        model,
        structured_output_mode=structured_output_mode,
    )
    answer_synthesizer = LLMAnswerSynthesizer(
        model,
        structured_output_mode=structured_output_mode,
    )
    replanner = partial(
        create_replacement_plan,
        model,
        structured_output_mode=structured_output_mode,
    )

    return build_agent_runtime(
        planner,
        action_selector,
        registry,
        action_registry=action_registry,
        reviewer=reviewer,
        replanner=replanner,
        answer_synthesizer=answer_synthesizer,
        checkpointer=checkpointer,
        limits=resolved_limits,
        context_budget=resolved_context_budget,
        artifact_path=artifact_path if allow_write else None,
        tracer=tracer,
        conversation_repository=conversation_repository,
    )


@asynccontextmanager
async def open_default_agent_runtime(
    settings: Settings,
    workspace_root: str | Path,
    checkpoint_path: str | Path,
    *,
    allow_write: bool = False,
    limits: RuntimeLimits | None = None,
    context_budget: ContextBudget | None = None,
    model_factory: ModelFactory = create_chat_model,
    web_provider: WebProvider | None = None,
    web_target_validator: WebTargetValidator | None = None,
    wiki_provider: WikipediaProvider | None = None,
    researcher_subagent: ResearcherSubagent | None = None,
    artifact_path: str = "reports/research-report.md",
    tracer: ExecutionTracer | None = None,
) -> AsyncIterator[AgentRuntime]:
    """Open a persistent runtime and close its checkpointer on exit."""

    async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
        conversation_repository = SQLiteConversationRepository(checkpoint_path)
        yield create_default_agent_runtime(
            settings,
            workspace_root,
            allow_write=allow_write,
            checkpointer=checkpointer,
            limits=limits,
            context_budget=context_budget,
            model_factory=model_factory,
            web_provider=web_provider,
            web_target_validator=web_target_validator,
            wiki_provider=wiki_provider,
            researcher_subagent=researcher_subagent,
            artifact_path=artifact_path,
            tracer=tracer,
            conversation_repository=conversation_repository,
        )


def _new_turn_id() -> str:
    return f"t-{uuid.uuid4().hex}"
