import asyncio
import inspect
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Literal, Protocol, TextIO, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from mini_deerflow.context_budget import ContextBudgetExceededError

TraceIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
TraceName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]


class TraceKind(str, Enum):
    RUN = "run"
    CHECKPOINT = "checkpoint"
    NODE = "node"
    TOOL = "tool"
    CONTEXT_BUDGET = "context_budget"
    DELEGATION = "delegation"
    REVIEW = "review"
    REPLAN = "replan"
    CITATION = "citation_validation"
    ARTIFACT = "artifact"


class TracePhase(str, Enum):
    START = "start"
    END = "end"
    OUTCOME = "outcome"


class TraceOutcome(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    READY = "ready"
    RESUMED = "resumed"
    SKIPPED = "skipped"
    WITHIN_BUDGET = "within_budget"
    COMPACTED = "compacted"
    REFUSED = "refused"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SAFETY_DENIED = "safety_denied"
    PROVIDER_FAILED = "provider_failed"
    TRANSPORT_FAILED = "transport_failed"
    PARTIAL_FAILURE = "partial_failure"
    CONTINUE = "continue"
    REPLAN = "replan"
    FINISH = "finish"
    WRITTEN = "written"
    NOT_REQUESTED = "not_requested"


class TraceErrorCategory(str, Enum):
    SAFETY = "safety"
    PROVIDER = "provider"
    TRANSPORT = "transport"
    CONTEXT_BUDGET = "context_budget"
    TOOL = "tool"
    PERSISTENCE = "persistence"
    RUNTIME = "runtime"
    INTERNAL = "internal"


class TraceErrorCode(str, Enum):
    URL_DENIED = "url_denied"
    WEB_PROVIDER_FAILURE = "web_provider_failure"
    WEB_TRANSPORT_FAILURE = "web_transport_failure"
    TOOL_FAILURE = "tool_failure"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    CHECKPOINT_FAILURE = "checkpoint_failure"
    RUNTIME_FAILURE = "runtime_failure"
    UNEXPECTED_FAILURE = "unexpected_failure"


class ExecutionTrace(BaseModel):
    """One redacted execution event with an intentionally closed schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: TraceIdentifier
    thread_id: TraceIdentifier
    sequence: int = Field(ge=1)
    kind: TraceKind
    phase: TracePhase
    outcome: TraceOutcome
    operation: Literal["run", "resume"] | None = None
    node: TraceName | None = None
    tool_name: TraceName | None = None
    route: Literal["continue", "replan", "finish"] | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    current_step: int | None = Field(default=None, ge=0)
    step_tool_calls: int | None = Field(default=None, ge=0)
    total_tool_calls: int | None = Field(default=None, ge=0)
    max_step_tool_calls: int | None = Field(default=None, ge=1)
    max_total_tool_calls: int | None = Field(default=None, ge=1)
    max_replan_cycles: int | None = Field(default=None, ge=1)
    evidence_count: int | None = Field(default=None, ge=0)
    citation_count: int | None = Field(default=None, ge=0)
    rejected_citation_count: int | None = Field(default=None, ge=0)
    artifact_count: int | None = Field(default=None, ge=0, le=1)
    branch_count: int | None = Field(default=None, ge=0)
    successful_branch_count: int | None = Field(default=None, ge=0)
    failed_branch_count: int | None = Field(default=None, ge=0)
    cancelled_branch_count: int | None = Field(default=None, ge=0)
    reserved_tool_calls: int | None = Field(default=None, ge=0)
    used_tool_calls: int | None = Field(default=None, ge=0)
    charged_tool_calls: int | None = Field(default=None, ge=0)
    context_omitted_items: int | None = Field(default=None, ge=0)
    context_truncated_items: int | None = Field(default=None, ge=0)
    context_estimated_tokens: int | None = Field(default=None, ge=0)
    error_category: TraceErrorCategory | None = None
    error_code: TraceErrorCode | None = None


@runtime_checkable
class TraceSink(Protocol):
    def emit(self, event: ExecutionTrace) -> None:
        """Consume one already-redacted trace event."""


class NullTraceSink:
    def emit(self, event: ExecutionTrace) -> None:
        del event


class InMemoryTraceSink:
    def __init__(self) -> None:
        self.events: list[ExecutionTrace] = []

    def emit(self, event: ExecutionTrace) -> None:
        self.events.append(event)


class JsonLinesTraceSink:
    """Write opt-in traces to an existing stream; never owns a trace file."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: ExecutionTrace) -> None:
        self._stream.write(event.model_dump_json() + "\n")
        self._stream.flush()


@dataclass(slots=True)
class _ActiveTrace:
    run_id: str
    thread_id: str
    operation: Literal["run", "resume"]
    sequence: int = 0


_active_trace: ContextVar[_ActiveTrace | None] = ContextVar(
    "mini_deerflow_active_trace",
    default=None,
)


class ExecutionTracer:
    """Bind run identity and emit typed events without persisting raw content."""

    def __init__(
        self,
        sink: TraceSink | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        resolved_sink = sink or NullTraceSink()
        if not isinstance(resolved_sink, TraceSink):
            raise TypeError("sink must satisfy TraceSink")
        self._sink = resolved_sink
        self._clock = clock
        self._run_id_factory = run_id_factory

    @contextmanager
    def run_scope(
        self,
        thread_id: str,
        operation: Literal["run", "resume"],
    ):
        active = _ActiveTrace(
            run_id=self._run_id_factory(),
            thread_id=thread_id,
            operation=operation,
        )
        token = _active_trace.set(active)
        started_at = self._clock()
        self.emit(
            kind=TraceKind.RUN,
            phase=TracePhase.START,
            outcome=TraceOutcome.STARTED,
            operation=operation,
        )
        try:
            yield active.run_id
        except BaseException as error:
            category, code, outcome = _classify_exception(error)
            self.emit(
                kind=TraceKind.RUN,
                phase=TracePhase.END,
                outcome=outcome,
                operation=operation,
                duration_ms=self._duration_ms(started_at),
                error_category=category,
                error_code=code,
            )
            raise
        else:
            self.emit(
                kind=TraceKind.RUN,
                phase=TracePhase.END,
                outcome=TraceOutcome.SUCCEEDED,
                operation=operation,
                duration_ms=self._duration_ms(started_at),
            )
        finally:
            _active_trace.reset(token)

    def emit(
        self,
        *,
        kind: TraceKind,
        phase: TracePhase,
        outcome: TraceOutcome,
        **fields: object,
    ) -> None:
        active = _active_trace.get()
        if active is None:
            return
        active.sequence += 1
        self._sink.emit(
            ExecutionTrace(
                run_id=active.run_id,
                thread_id=active.thread_id,
                sequence=active.sequence,
                kind=kind,
                phase=phase,
                outcome=outcome,
                **fields,
            )
        )

    def wrap_node(
        self,
        node: str,
        handler: Callable[[Any], object],
    ) -> Callable[[Any], object]:
        async def traced(state: Any) -> object:
            started_at = self._clock()
            counters = _safe_state_counters(state)
            self.emit(
                kind=TraceKind.NODE,
                phase=TracePhase.START,
                outcome=TraceOutcome.STARTED,
                node=node,
                **counters,
            )
            try:
                result = handler(state)
                if inspect.isawaitable(result):
                    result = await result
            except BaseException as error:
                category, code, outcome = _classify_exception(error)
                self.emit(
                    kind=TraceKind.NODE,
                    phase=TracePhase.END,
                    outcome=outcome,
                    node=node,
                    duration_ms=self._duration_ms(started_at),
                    error_category=category,
                    error_code=code,
                    **counters,
                )
                raise
            self.emit(
                kind=TraceKind.NODE,
                phase=TracePhase.END,
                outcome=TraceOutcome.SUCCEEDED,
                node=node,
                duration_ms=self._duration_ms(started_at),
                **_safe_state_counters(state, result),
            )
            return result

        return cast(Callable[[Any], object], traced)

    def _duration_ms(self, started_at: float) -> int:
        return max(0, round((self._clock() - started_at) * 1_000))


def _safe_state_counters(
    state: object,
    update: object | None = None,
) -> dict[str, int]:
    if not isinstance(state, dict):
        return {}
    merged = dict(state)
    if isinstance(update, dict):
        for key in (
            "current_step",
            "tool_calls_in_current_step",
            "total_tool_calls",
        ):
            if key in update:
                merged[key] = update[key]
    fields: dict[str, int] = {}
    for state_key, trace_key in (
        ("current_step", "current_step"),
        ("tool_calls_in_current_step", "step_tool_calls"),
        ("total_tool_calls", "total_tool_calls"),
    ):
        value = merged.get(state_key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            fields[trace_key] = value
    return fields


def _classify_exception(
    error: BaseException,
) -> tuple[TraceErrorCategory, TraceErrorCode, TraceOutcome]:
    from mini_deerflow.persistence import PersistenceError

    if isinstance(error, ContextBudgetExceededError):
        return (
            TraceErrorCategory.CONTEXT_BUDGET,
            TraceErrorCode.CONTEXT_BUDGET_EXCEEDED,
            TraceOutcome.REFUSED,
        )
    if isinstance(error, PersistenceError):
        return (
            TraceErrorCategory.PERSISTENCE,
            TraceErrorCode.CHECKPOINT_FAILURE,
            TraceOutcome.FAILED,
        )
    if isinstance(error, asyncio.CancelledError):
        return (
            TraceErrorCategory.RUNTIME,
            TraceErrorCode.RUNTIME_FAILURE,
            TraceOutcome.FAILED,
        )
    return (
        TraceErrorCategory.INTERNAL,
        TraceErrorCode.UNEXPECTED_FAILURE,
        TraceOutcome.FAILED,
    )
