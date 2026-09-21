"""Live web backend used by the explicitly selected Streamlit mode."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mini_deerflow.config import Settings
from mini_deerflow.conversation import ConversationRecord, SQLiteConversationRepository
from mini_deerflow.demo.service import (
    ContinueDemoCommand,
    ResumeDemoCommand,
    RunDemoCommand,
    _BackendResult,
)
from mini_deerflow.runtime import (
    ConversationSnapshot,
    RuntimeLimits,
    open_default_agent_runtime,
)
from mini_deerflow.tracing import ExecutionTrace, ExecutionTracer, TraceSink


@dataclass(slots=True)
class _RecordingTraceSink:
    """Retain redacted traces for projection and optionally forward them to the UI."""

    events: list[ExecutionTrace]
    downstream: TraceSink | None = None

    def emit(self, event: ExecutionTrace) -> None:
        self.events.append(event)
        if self.downstream is not None:
            self.downstream.emit(event)


class LiveDemoBackend:
    """Compose the live runtime with credentials from ``Settings``.

    This backend has no credential fields. ``Settings`` reads the local
    environment/``.env`` file while the UI receives only projected run results.
    """

    def __init__(
        self,
        storage_root: str | Path,
        *,
        settings: Settings | None = None,
        limits: RuntimeLimits | None = None,
    ) -> None:
        self._storage_root = Path(storage_root).resolve(strict=False)
        self._checkpoint_path = self._storage_root / "checkpoints.sqlite"
        self._workspaces_root = self._storage_root / "workspaces"
        self._settings = settings
        self._limits = limits or RuntimeLimits()

    @property
    def limits(self) -> RuntimeLimits:
        return self._limits

    async def run(
        self,
        command: RunDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        return await self._execute(
            operation="run",
            thread_id=command.thread_id,
            goal=command.goal,
            turn_id=command.turn_id,
            trace_sink=trace_sink,
        )

    async def continue_thread(
        self,
        command: ContinueDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        return await self._execute(
            operation="continue",
            thread_id=command.thread_id,
            goal=command.user_message,
            turn_id=command.turn_id,
            trace_sink=trace_sink,
        )

    async def resume(
        self,
        command: ResumeDemoCommand,
        *,
        trace_sink: TraceSink | None = None,
    ) -> _BackendResult:
        return await self._execute(
            operation="resume",
            thread_id=command.thread_id,
            goal=None,
            turn_id=None,
            trace_sink=trace_sink,
        )

    async def list_threads(self) -> tuple[str, ...]:
        records = await self.list_conversations()
        return tuple(record.thread_id for record in records)

    async def list_conversations(self) -> tuple[ConversationRecord, ...]:
        repository = SQLiteConversationRepository(self._checkpoint_path)
        return await repository.list_conversations()

    async def load_conversation(self, thread_id: str) -> ConversationSnapshot:
        async with open_default_agent_runtime(
            self._resolved_settings(),
            self._workspaces_root / thread_id,
            self._checkpoint_path,
            allow_write=True,
            limits=self._limits,
        ) as runtime:
            return await runtime.load_conversation(thread_id)

    async def _execute(
        self,
        *,
        operation: Literal["run", "continue", "resume"],
        thread_id: str,
        goal: str | None,
        turn_id: str | None,
        trace_sink: TraceSink | None,
    ) -> _BackendResult:
        events: list[ExecutionTrace] = []
        tracer = ExecutionTracer(_RecordingTraceSink(events, trace_sink))

        async with open_default_agent_runtime(
            self._resolved_settings(),
            self._workspaces_root / thread_id,
            self._checkpoint_path,
            allow_write=True,
            limits=self._limits,
            tracer=tracer,
        ) as runtime:
            try:
                if operation == "run":
                    assert goal is not None and turn_id is not None
                    state = await runtime.run(
                        goal,
                        thread_id=thread_id,
                        turn_id=turn_id,
                    )
                elif operation == "continue":
                    assert goal is not None and turn_id is not None
                    state = await runtime.continue_thread(
                        thread_id,
                        goal,
                        turn_id=turn_id,
                    )
                else:
                    state = await runtime.resume(thread_id=thread_id)
            except BaseException:
                repository = runtime.conversation_repository
                if repository is not None:
                    active = await repository.get_active_turn(thread_id)
                    if active is not None:
                        await runtime.record_turn_traces(
                            thread_id,
                            active.turn_id,
                            tuple(events),
                        )
                raise
            resolved_turn_id = state.get("turn_id")
            if isinstance(resolved_turn_id, str):
                await runtime.record_turn_traces(
                    thread_id,
                    resolved_turn_id,
                    tuple(events),
                )

        return _BackendResult(
            state=state,
            traces=tuple(events),
            limits=self._limits,
        )

    def _resolved_settings(self) -> Settings:
        return self._settings or Settings()
