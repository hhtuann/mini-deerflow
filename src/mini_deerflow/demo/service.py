"""Validated commands and the safe façade used by the Streamlit demo."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from uuid import uuid4

from mini_deerflow.conversation import (
    ActiveTurnError,
    ConversationAlreadyExistsError,
    ConversationNotFoundError,
    ConversationRecord,
    IdempotencyConflictError,
    SessionBudgetExceededError,
)
from mini_deerflow.demo.view_models import (
    ChatSessionView,
    ConversationSummaryView,
    DemoRunView,
    project_chat_session,
    project_conversation_summary,
    project_demo_run,
)
from mini_deerflow.persistence import (
    CheckpointPathError,
    CheckpointStorageError,
    CheckpointUnavailableError,
    InvalidThreadIdError,
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
    normalize_thread_id,
)
from mini_deerflow.runtime import ConversationSnapshot, RuntimeLimits
from mini_deerflow.state import AgentState
from mini_deerflow.tracing import ExecutionTrace, TraceSink

DEFAULT_DEMO_GOAL = (
    "Demonstrate the bounded Mini DeerFlow MVP with verifiable evidence."
)

_MAX_GOAL_LENGTH = 1_000
_MIN_GOAL_LENGTH = 10
_SAFE_REFERENCE_PATTERN = re.compile(r"[^A-Za-z0-9._-]")


class DemoCommandValidationError(ValueError):
    """Raised when a UI command violates the narrow demo contract."""


def _validated_goal(goal: str) -> str:
    if not isinstance(goal, str):
        raise DemoCommandValidationError("goal must be text")
    normalized = goal.strip()
    if not _MIN_GOAL_LENGTH <= len(normalized) <= _MAX_GOAL_LENGTH:
        raise DemoCommandValidationError(
            "goal must contain between 10 and 1000 characters",
        )
    return normalized


def _validated_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str):
        raise DemoCommandValidationError("thread_id must be text")
    try:
        return normalize_thread_id(thread_id)
    except InvalidThreadIdError as error:
        raise DemoCommandValidationError("thread_id is invalid") from error


@dataclass(frozen=True, slots=True)
class RunDemoCommand:
    """A validated request to create and run one demo thread."""

    thread_id: str
    goal: str = DEFAULT_DEMO_GOAL
    turn_id: str = field(default_factory=lambda: f"turn-{uuid4().hex}")

    def __post_init__(self) -> None:
        object.__setattr__(self, "thread_id", _validated_thread_id(self.thread_id))
        object.__setattr__(self, "goal", _validated_goal(self.goal))
        object.__setattr__(self, "turn_id", _validated_thread_id(self.turn_id))


@dataclass(frozen=True, slots=True)
class ContinueDemoCommand:
    """A validated follow-up on one existing conversation."""

    thread_id: str
    user_message: str
    turn_id: str = field(default_factory=lambda: f"turn-{uuid4().hex}")

    def __post_init__(self) -> None:
        object.__setattr__(self, "thread_id", _validated_thread_id(self.thread_id))
        object.__setattr__(self, "user_message", _validated_goal(self.user_message))
        object.__setattr__(self, "turn_id", _validated_thread_id(self.turn_id))


@dataclass(frozen=True, slots=True)
class ResumeDemoCommand:
    """A validated request to resume one persisted demo thread."""

    thread_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "thread_id", _validated_thread_id(self.thread_id))


@dataclass(frozen=True, slots=True)
class _BackendResult:
    """Internal raw result; the service projects it before returning."""

    state: AgentState
    traces: tuple[ExecutionTrace, ...]
    limits: RuntimeLimits


@runtime_checkable
class DemoBackend(Protocol):
    """Backend seam for offline mode and a future explicitly configured live mode."""

    @property
    def limits(self) -> RuntimeLimits:
        """Return public execution limits used for safe projection."""

    async def run(
        self,
        command: RunDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        """Execute a new thread and return an internal runtime result."""

    async def resume(
        self,
        command: ResumeDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        """Resume a persisted thread and return an internal runtime result."""

    async def continue_thread(
        self,
        command: ContinueDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        """Execute one new turn on an existing conversation."""

    async def list_threads(self) -> tuple[str, ...]:
        """List persisted thread identifiers in deterministic order."""

    async def list_conversations(self) -> tuple[ConversationRecord, ...]:
        """List durable conversation metadata."""

    async def load_conversation(self, thread_id: str) -> ConversationSnapshot:
        """Load durable turns and isolated per-turn projections."""


@dataclass(frozen=True, slots=True)
class DemoRuntimeService:
    """Keep raw runtime state behind an explicit safe projection boundary."""

    backend: DemoBackend

    def __post_init__(self) -> None:
        if not isinstance(self.backend, DemoBackend):
            raise TypeError("backend must satisfy DemoBackend")

    async def run(
        self,
        command: RunDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> DemoRunView:
        result = await self.backend.run(command, trace_sink=trace_sink)
        return project_demo_run(
            result.state,
            result.traces,
            thread_id=command.thread_id,
            limits=result.limits,
        )

    async def resume(
        self,
        command: ResumeDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> DemoRunView:
        result = await self.backend.resume(command, trace_sink=trace_sink)
        return project_demo_run(
            result.state,
            result.traces,
            thread_id=command.thread_id,
            limits=result.limits,
        )

    async def continue_thread(
        self,
        command: ContinueDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> DemoRunView:
        result = await self.backend.continue_thread(
            command,
            trace_sink=trace_sink,
        )
        return project_demo_run(
            result.state,
            result.traces,
            thread_id=command.thread_id,
            limits=result.limits,
        )

    async def list_threads(self) -> tuple[str, ...]:
        return await self.backend.list_threads()

    async def list_conversations(self) -> tuple[ConversationSummaryView, ...]:
        records = await self.backend.list_conversations()
        return tuple(project_conversation_summary(record) for record in records)

    async def load_conversation(self, thread_id: str) -> ChatSessionView:
        normalized = _validated_thread_id(thread_id)
        snapshot = await self.backend.load_conversation(normalized)
        return project_chat_session(snapshot, limits=self.backend.limits)


def map_demo_error(error: BaseException, reference_id: str = "demo") -> str:
    """Map internal failures to short messages that contain no exception details."""

    if isinstance(error, (DemoCommandValidationError, InvalidThreadIdError)):
        return "Input validation failed."
    if isinstance(error, ThreadAlreadyExistsError):
        return "Thread already exists. Choose Resume or a new ID."
    if isinstance(error, ConversationAlreadyExistsError):
        return "Conversation already exists. Open it and send a follow-up."
    if isinstance(error, ActiveTurnError):
        return "This conversation already has an active turn."
    if isinstance(error, IdempotencyConflictError):
        return "That turn ID is already bound to a different message."
    if isinstance(error, SessionBudgetExceededError):
        return "This conversation has reached its session tool-call limit."
    if isinstance(error, ConversationNotFoundError):
        return "Conversation was not found."
    if isinstance(error, ThreadNotFoundError):
        return "No checkpoint exists for this thread."
    if isinstance(
        error,
        (CheckpointPathError, CheckpointStorageError, CheckpointUnavailableError),
    ):
        return "Checkpoint operation failed."
    if isinstance(error, TimeoutError):
        return "Operation timed out."

    safe_reference = _SAFE_REFERENCE_PATTERN.sub("-", reference_id)[:64] or "demo"
    return f"Demo operation failed. Reference: {safe_reference}."
