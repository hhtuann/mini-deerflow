import asyncio
import hashlib
import inspect
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal, Protocol, TextIO, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from mini_deerflow.context_budget import ContextBudgetExceededError

logger = logging.getLogger(__name__)

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
    ROUTE = "route"
    BRANCH = "branch"
    FAN_IN = "fan_in"
    EVIDENCE = "evidence"


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
    SELECTED = "selected"
    CANCELLED = "cancelled"


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


class ExecutionEventType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_FAILED = "node_failed"
    ROUTE_SELECTED = "route_selected"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    DELEGATION_STARTED = "delegation_started"
    DELEGATION_COMPLETED = "delegation_completed"
    BRANCH_STARTED = "branch_started"
    BRANCH_COMPLETED = "branch_completed"
    BRANCH_FAILED = "branch_failed"
    EVIDENCE_PRODUCED = "evidence_produced"
    FAN_IN_COMPLETED = "fan_in_completed"
    CHECKPOINT_OBSERVED = "checkpoint_observed"
    CONTEXT_PROJECTED = "context_projected"
    REVIEW_COMPLETED = "review_completed"
    REPLAN_COMPLETED = "replan_completed"
    CITATION_VALIDATED = "citation_validated"
    ARTIFACT_COMPLETED = "artifact_completed"


class ExecutionStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    SELECTED = "selected"
    OBSERVED = "observed"


EventMetadataValue = str | int | float | bool | None
MAX_EVENT_METADATA_ITEMS = 24
MAX_EVENT_METADATA_TEXT_CHARS = 240
_SAFE_METADATA_KEYS = frozenset(
    {
        "from_node",
        "to_node",
        "decision_type",
        "route_reason",
        "review_verdict",
        "step_number",
        "tool_call_number",
        "success",
        "failure_category",
        "requested_branches",
        "successful_branches",
        "failed_branches",
        "cancelled_branches",
        "budget_reserved",
        "budget_used",
        "budget_charged",
        "budget_remaining",
        "evidence_count",
        "citation_count",
        "source_count",
        "evidence_promoted",
        "limitations_produced",
    }
)
_SENSITIVE_METADATA_PATTERN = re.compile(
    r"(?:authorization|bearer|api[_-]?key|token|password|secret|credential)",
    re.IGNORECASE,
)


def project_execution_metadata(
    metadata: Mapping[str, object] | None,
) -> dict[str, EventMetadataValue]:
    """Return the bounded scalar allowlist permitted in execution events."""

    if metadata is None:
        return {}
    projected: dict[str, EventMetadataValue] = {}
    for key, value in metadata.items():
        if len(projected) >= MAX_EVENT_METADATA_ITEMS:
            break
        if key not in _SAFE_METADATA_KEYS or _SENSITIVE_METADATA_PATTERN.search(key):
            continue
        if value is None or isinstance(value, bool | int | float):
            projected[key] = value
            continue
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if _SENSITIVE_METADATA_PATTERN.search(normalized):
            normalized = "[redacted]"
        projected[key] = normalized[:MAX_EVENT_METADATA_TEXT_CHARS]
    return projected


class ExecutionEvent(BaseModel):
    """One safe structured execution event and the trace source of truth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 2
    event_id: TraceIdentifier = Field(default_factory=lambda: uuid.uuid4().hex)
    event_type: ExecutionEventType
    status: ExecutionStatus
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    root_run_id: TraceIdentifier
    run_id: TraceIdentifier
    parent_run_id: TraceIdentifier | None = None
    thread_id: TraceIdentifier
    sequence: int = Field(ge=1)
    node_id: TraceName | None = None
    delegation_id: TraceIdentifier | None = None
    branch_id: TraceIdentifier | None = None
    tool_call_id: TraceIdentifier | None = None
    metadata: dict[str, EventMetadataValue] = Field(default_factory=dict)

    # Compatibility projection used by the existing CLI/demo trace consumers.
    kind: TraceKind
    phase: TracePhase
    outcome: TraceOutcome
    operation: Literal["run", "continue", "resume"] | None = None
    turn_id: TraceIdentifier | None = None
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

    @model_validator(mode="before")
    @classmethod
    def populate_execution_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        fields = dict(value)
        run_id = fields.get("run_id")
        if "root_run_id" not in fields and isinstance(run_id, str):
            fields["root_run_id"] = run_id
        if "node_id" not in fields and isinstance(fields.get("node"), str):
            fields["node_id"] = fields["node"]
        if "event_type" in fields and any(
            name not in fields for name in ("kind", "phase", "outcome")
        ):
            kind, phase, outcome = _legacy_shape_for_event(
                ExecutionEventType(fields["event_type"])
            )
            fields.setdefault("kind", kind)
            fields.setdefault("phase", phase)
            fields.setdefault("outcome", outcome)
        if "event_type" not in fields:
            fields["event_type"] = _event_type_from_legacy(fields)
        if "status" not in fields:
            fields["status"] = _status_for_event(
                fields["event_type"],
                fields.get("outcome"),
            )
        metadata = fields.get("metadata")
        fields["metadata"] = project_execution_metadata(
            metadata if isinstance(metadata, Mapping) else None
        )
        return fields

    @model_validator(mode="after")
    def validate_timestamp(self) -> "ExecutionEvent":
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return self


# Backward-compatible name for existing trace consumers. There is one model/pipeline.
ExecutionTrace = ExecutionEvent


@runtime_checkable
class TraceSink(Protocol):
    def emit(self, event: ExecutionEvent) -> None:
        """Consume one already-redacted trace event."""


class NullTraceSink:
    def emit(self, event: ExecutionEvent) -> None:
        del event


class InMemoryTraceSink:
    def __init__(self) -> None:
        self.events: list[ExecutionEvent] = []

    def emit(self, event: ExecutionEvent) -> None:
        self.events.append(event)


class JsonLinesTraceSink:
    """Write opt-in traces to an existing stream; never owns a trace file."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, event: ExecutionEvent) -> None:
        self._stream.write(event.model_dump_json() + "\n")
        self._stream.flush()


@dataclass(slots=True)
class _SequenceCounter:
    value: int = 0


@dataclass(slots=True)
class _ActiveTrace:
    root_run_id: str
    run_id: str
    parent_run_id: str | None
    thread_id: str
    operation: Literal["run", "continue", "resume"]
    turn_id: str | None = None
    delegation_id: str | None = None
    branch_id: str | None = None
    node_id: str | None = None
    counter: _SequenceCounter = field(default_factory=_SequenceCounter)


_active_trace: ContextVar[_ActiveTrace | None] = ContextVar(
    "mini_deerflow_active_trace",
    default=None,
)
_active_tracer: ContextVar[Any] = ContextVar(
    "mini_deerflow_execution_tracer",
    default=None,
)


class ExecutionTracer:
    """Bind run identity and emit typed events without persisting raw content."""

    def __init__(
        self,
        sink: TraceSink | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        event_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        resolved_sink = sink or NullTraceSink()
        if not isinstance(resolved_sink, TraceSink):
            raise TypeError("sink must satisfy TraceSink")
        self._sink = resolved_sink
        self._clock = clock
        self._wall_clock = wall_clock
        self._run_id_factory = run_id_factory
        self._event_id_factory = event_id_factory
        self._events: list[ExecutionEvent] = []

    def events_for_run(self, run_id: str) -> tuple[ExecutionEvent, ...]:
        """Return the already-redacted events captured for one run."""

        return tuple(event for event in self._events if event.root_run_id == run_id)

    @contextmanager
    def run_scope(
        self,
        thread_id: str,
        operation: Literal["run", "continue", "resume"],
        *,
        turn_id: str | None = None,
    ):
        run_id = self._run_id_factory()
        active = _ActiveTrace(
            root_run_id=run_id,
            run_id=run_id,
            parent_run_id=None,
            thread_id=thread_id,
            operation=operation,
            turn_id=turn_id,
            counter=_SequenceCounter(),
        )
        token = _active_trace.set(active)
        tracer_token = _active_tracer.set(self)
        started_at = self._clock()
        self.emit_event(
            ExecutionEventType.RUN_STARTED,
            operation=operation,
            turn_id=turn_id,
        )
        try:
            yield active.run_id
        except BaseException as error:
            category, code, outcome = _classify_exception(error)
            self.emit_event(
                ExecutionEventType.RUN_FAILED,
                outcome=outcome,
                operation=operation,
                turn_id=turn_id,
                duration_ms=self._duration_ms(started_at),
                error_category=category,
                error_code=code,
            )
            raise
        else:
            self.emit_event(
                ExecutionEventType.RUN_COMPLETED,
                operation=operation,
                turn_id=turn_id,
                duration_ms=self._duration_ms(started_at),
            )
        finally:
            _active_tracer.reset(tracer_token)
            _active_trace.reset(token)

    @contextmanager
    def branch_scope(self, delegation_id: str, branch_id: str):
        """Bind one deterministic child run while sharing the root sequence."""

        parent = _active_trace.get()
        if parent is None:
            yield None
            return
        run_id = _derived_identifier(
            "branch",
            parent.run_id,
            delegation_id,
            branch_id,
        )
        child = _ActiveTrace(
            root_run_id=parent.root_run_id,
            run_id=run_id,
            parent_run_id=parent.run_id,
            thread_id=parent.thread_id,
            operation=parent.operation,
            turn_id=parent.turn_id,
            delegation_id=delegation_id,
            branch_id=branch_id,
            node_id="delegated_researcher",
            counter=parent.counter,
        )
        token = _active_trace.set(child)
        try:
            yield run_id
        finally:
            _active_trace.reset(token)

    def tool_call_id(self, call_number: int) -> str | None:
        active = _active_trace.get()
        if active is None:
            return None
        return _derived_identifier("tool", active.run_id, str(call_number))

    def emit_event(
        self,
        event_type: ExecutionEventType,
        *,
        outcome: TraceOutcome | None = None,
        status: ExecutionStatus | None = None,
        **fields: object,
    ) -> None:
        kind, phase, default_outcome = _legacy_shape_for_event(event_type)
        self.emit(
            kind=kind,
            phase=phase,
            outcome=outcome or default_outcome,
            event_type=event_type,
            status=status,
            **cast(Any, fields),
        )

    def emit(
        self,
        *,
        kind: TraceKind,
        phase: TracePhase,
        outcome: TraceOutcome,
        event_type: ExecutionEventType | None = None,
        status: ExecutionStatus | None = None,
        metadata: Mapping[str, object] | None = None,
        **fields: object,
    ) -> None:
        active = _active_trace.get()
        if active is None:
            return
        active.counter.value += 1
        fields.setdefault("turn_id", active.turn_id)
        if active.node_id is not None:
            fields.setdefault("node_id", active.node_id)
        if active.delegation_id is not None:
            fields.setdefault("delegation_id", active.delegation_id)
        if active.branch_id is not None:
            fields.setdefault("branch_id", active.branch_id)
        event = ExecutionEvent(
            event_id=self._event_id_factory(),
            event_type=event_type or _event_type_from_parts(kind, phase, outcome),
            status=status
            or _status_for_event(
                event_type or _event_type_from_parts(kind, phase, outcome),
                outcome,
            ),
            timestamp=self._wall_clock(),
            root_run_id=active.root_run_id,
            run_id=active.run_id,
            parent_run_id=active.parent_run_id,
            thread_id=active.thread_id,
            sequence=active.counter.value,
            kind=kind,
            phase=phase,
            outcome=outcome,
            metadata=project_execution_metadata(metadata),
            **fields,
        )
        self._events.append(event)
        try:
            self._sink.emit(event)
        except Exception as error:  # noqa: BLE001 - observability is best-effort
            logger.warning(
                "Execution event sink failed (%s); agent execution continues",
                type(error).__name__,
            )

    def wrap_node(
        self,
        node: str,
        handler: Callable[[Any], object],
    ) -> Callable[[Any], object]:
        async def traced(state: Any) -> object:
            started_at = self._clock()
            counters = _safe_state_counters(state)
            self.emit_event(
                ExecutionEventType.NODE_STARTED,
                node=node,
                **cast(Any, counters),
            )
            try:
                result = handler(state)
                if inspect.isawaitable(result):
                    result = await result
            except BaseException as error:
                category, code, outcome = _classify_exception(error)
                self.emit_event(
                    ExecutionEventType.NODE_FAILED,
                    outcome=outcome,
                    node=node,
                    duration_ms=self._duration_ms(started_at),
                    error_category=category,
                    error_code=code,
                    **cast(Any, counters),
                )
                raise
            self.emit_event(
                ExecutionEventType.NODE_COMPLETED,
                node=node,
                duration_ms=self._duration_ms(started_at),
                **cast(Any, _safe_state_counters(state, result)),
            )
            return result

        return cast(Callable[[Any], object], traced)

    def wrap_route(
        self,
        from_node: str,
        router: Callable[[Any], str],
        *,
        decision_type: str,
        route_reason: str,
    ) -> Callable[[Any], str]:
        """Emit one safe route edge after the existing router decides."""

        def traced(state: Any) -> str:
            to_node = router(state)
            metadata: dict[str, object] = {
                "from_node": from_node,
                "to_node": to_node,
                "decision_type": decision_type,
                "route_reason": route_reason,
            }
            if from_node == "review":
                metadata["review_verdict"] = to_node
            self.emit_event(
                ExecutionEventType.ROUTE_SELECTED,
                node=from_node,
                metadata=metadata,
            )
            return to_node

        return traced

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


def current_execution_tracer() -> ExecutionTracer | None:
    """Return the tracer bound to the current run/task context, if any."""

    return _active_tracer.get()


def _derived_identifier(prefix: str, *parts: str) -> str:
    candidate = ":".join((prefix, *parts))
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _event_type_from_legacy(fields: Mapping[str, object]) -> ExecutionEventType:
    kind = TraceKind(fields.get("kind", TraceKind.RUN))
    phase = TracePhase(fields.get("phase", TracePhase.OUTCOME))
    outcome = TraceOutcome(fields.get("outcome", TraceOutcome.SUCCEEDED))
    return _event_type_from_parts(kind, phase, outcome)


def _event_type_from_parts(
    kind: TraceKind,
    phase: TracePhase,
    outcome: TraceOutcome,
) -> ExecutionEventType:
    if kind is TraceKind.RUN:
        if phase is TracePhase.START:
            return ExecutionEventType.RUN_STARTED
        return (
            ExecutionEventType.RUN_COMPLETED
            if outcome is TraceOutcome.SUCCEEDED
            else ExecutionEventType.RUN_FAILED
        )
    if kind is TraceKind.NODE:
        if phase is TracePhase.START:
            return ExecutionEventType.NODE_STARTED
        return (
            ExecutionEventType.NODE_COMPLETED
            if outcome is TraceOutcome.SUCCEEDED
            else ExecutionEventType.NODE_FAILED
        )
    if kind is TraceKind.TOOL:
        if phase is TracePhase.START:
            return ExecutionEventType.TOOL_STARTED
        return (
            ExecutionEventType.TOOL_COMPLETED
            if outcome is TraceOutcome.SUCCEEDED
            else ExecutionEventType.TOOL_FAILED
        )
    if kind is TraceKind.DELEGATION:
        return (
            ExecutionEventType.DELEGATION_STARTED
            if phase is TracePhase.START
            else ExecutionEventType.DELEGATION_COMPLETED
        )
    if kind is TraceKind.BRANCH:
        if phase is TracePhase.START:
            return ExecutionEventType.BRANCH_STARTED
        return (
            ExecutionEventType.BRANCH_COMPLETED
            if outcome is TraceOutcome.SUCCEEDED
            else ExecutionEventType.BRANCH_FAILED
        )
    return {
        TraceKind.ROUTE: ExecutionEventType.ROUTE_SELECTED,
        TraceKind.FAN_IN: ExecutionEventType.FAN_IN_COMPLETED,
        TraceKind.EVIDENCE: ExecutionEventType.EVIDENCE_PRODUCED,
        TraceKind.CHECKPOINT: ExecutionEventType.CHECKPOINT_OBSERVED,
        TraceKind.CONTEXT_BUDGET: ExecutionEventType.CONTEXT_PROJECTED,
        TraceKind.REVIEW: ExecutionEventType.REVIEW_COMPLETED,
        TraceKind.REPLAN: ExecutionEventType.REPLAN_COMPLETED,
        TraceKind.CITATION: ExecutionEventType.CITATION_VALIDATED,
        TraceKind.ARTIFACT: ExecutionEventType.ARTIFACT_COMPLETED,
    }[kind]


def _status_for_event(
    event_type: ExecutionEventType | str,
    outcome: object | None,
) -> ExecutionStatus:
    resolved = ExecutionEventType(event_type)
    if resolved.value.endswith("_started"):
        return ExecutionStatus.STARTED
    if resolved is ExecutionEventType.ROUTE_SELECTED:
        return ExecutionStatus.SELECTED
    if resolved in {
        ExecutionEventType.CHECKPOINT_OBSERVED,
        ExecutionEventType.CONTEXT_PROJECTED,
        ExecutionEventType.CITATION_VALIDATED,
    }:
        return ExecutionStatus.OBSERVED
    if outcome in {TraceOutcome.PARTIAL_FAILURE, "partial_failure"}:
        return ExecutionStatus.PARTIAL
    if outcome in {TraceOutcome.CANCELLED, "cancelled"}:
        return ExecutionStatus.CANCELLED
    if resolved.value.endswith("_failed"):
        return ExecutionStatus.FAILED
    return ExecutionStatus.COMPLETED


def _legacy_shape_for_event(
    event_type: ExecutionEventType,
) -> tuple[TraceKind, TracePhase, TraceOutcome]:
    if event_type is ExecutionEventType.RUN_STARTED:
        return TraceKind.RUN, TracePhase.START, TraceOutcome.STARTED
    if event_type is ExecutionEventType.RUN_COMPLETED:
        return TraceKind.RUN, TracePhase.END, TraceOutcome.SUCCEEDED
    if event_type is ExecutionEventType.RUN_FAILED:
        return TraceKind.RUN, TracePhase.END, TraceOutcome.FAILED
    if event_type is ExecutionEventType.NODE_STARTED:
        return TraceKind.NODE, TracePhase.START, TraceOutcome.STARTED
    if event_type is ExecutionEventType.NODE_COMPLETED:
        return TraceKind.NODE, TracePhase.END, TraceOutcome.SUCCEEDED
    if event_type is ExecutionEventType.NODE_FAILED:
        return TraceKind.NODE, TracePhase.END, TraceOutcome.FAILED
    if event_type is ExecutionEventType.ROUTE_SELECTED:
        return TraceKind.ROUTE, TracePhase.OUTCOME, TraceOutcome.SELECTED
    if event_type is ExecutionEventType.TOOL_STARTED:
        return TraceKind.TOOL, TracePhase.START, TraceOutcome.STARTED
    if event_type is ExecutionEventType.TOOL_COMPLETED:
        return TraceKind.TOOL, TracePhase.END, TraceOutcome.SUCCEEDED
    if event_type is ExecutionEventType.TOOL_FAILED:
        return TraceKind.TOOL, TracePhase.END, TraceOutcome.FAILED
    if event_type is ExecutionEventType.DELEGATION_STARTED:
        return TraceKind.DELEGATION, TracePhase.START, TraceOutcome.STARTED
    if event_type is ExecutionEventType.DELEGATION_COMPLETED:
        return TraceKind.DELEGATION, TracePhase.END, TraceOutcome.SUCCEEDED
    if event_type is ExecutionEventType.BRANCH_STARTED:
        return TraceKind.BRANCH, TracePhase.START, TraceOutcome.STARTED
    if event_type is ExecutionEventType.BRANCH_COMPLETED:
        return TraceKind.BRANCH, TracePhase.END, TraceOutcome.SUCCEEDED
    if event_type is ExecutionEventType.BRANCH_FAILED:
        return TraceKind.BRANCH, TracePhase.END, TraceOutcome.FAILED
    return {
        ExecutionEventType.EVIDENCE_PRODUCED: (
            TraceKind.EVIDENCE,
            TracePhase.OUTCOME,
            TraceOutcome.SUCCEEDED,
        ),
        ExecutionEventType.FAN_IN_COMPLETED: (
            TraceKind.FAN_IN,
            TracePhase.END,
            TraceOutcome.SUCCEEDED,
        ),
        ExecutionEventType.CHECKPOINT_OBSERVED: (
            TraceKind.CHECKPOINT,
            TracePhase.OUTCOME,
            TraceOutcome.SUCCEEDED,
        ),
        ExecutionEventType.CONTEXT_PROJECTED: (
            TraceKind.CONTEXT_BUDGET,
            TracePhase.OUTCOME,
            TraceOutcome.WITHIN_BUDGET,
        ),
        ExecutionEventType.REVIEW_COMPLETED: (
            TraceKind.REVIEW,
            TracePhase.OUTCOME,
            TraceOutcome.SUCCEEDED,
        ),
        ExecutionEventType.REPLAN_COMPLETED: (
            TraceKind.REPLAN,
            TracePhase.OUTCOME,
            TraceOutcome.SUCCEEDED,
        ),
        ExecutionEventType.CITATION_VALIDATED: (
            TraceKind.CITATION,
            TracePhase.OUTCOME,
            TraceOutcome.ACCEPTED,
        ),
        ExecutionEventType.ARTIFACT_COMPLETED: (
            TraceKind.ARTIFACT,
            TracePhase.OUTCOME,
            TraceOutcome.NOT_REQUESTED,
        ),
    }[event_type]
