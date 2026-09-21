import asyncio
from pathlib import Path

import pytest

from mini_deerflow.actions import CompleteStepAction
from mini_deerflow.cli import list_research_threads
from mini_deerflow.conversation import (
    ConversationContext,
    IdempotencyConflictError,
    SessionBudgetExceededError,
    SQLiteConversationRepository,
)
from mini_deerflow.decision import ActionContext
from mini_deerflow.persistence import ThreadAlreadyExistsError, open_sqlite_checkpointer
from mini_deerflow.runtime import RuntimeLimits, build_agent_runtime
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.tools import ToolRegistry
from mini_deerflow.tracing import ExecutionTracer, InMemoryTraceSink


class ContextCapturingPlanner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ConversationContext | None]] = []

    def __call__(
        self,
        goal: str,
        *,
        conversation_context: ConversationContext | None = None,
    ) -> Plan:
        self.calls.append((goal, conversation_context))
        return Plan(
            goal=goal,
            steps=[
                PlanStep(
                    step_number=number,
                    title=f"Step {number}",
                    objective=f"Complete bounded objective {number} for this turn.",
                    success_criteria="The bounded objective is complete.",
                )
                for number in range(1, 4)
            ],
        )


class CompletingSelector:
    def __init__(self) -> None:
        self.contexts: list[ActionContext] = []

    async def select_action(self, context: ActionContext) -> CompleteStepAction:
        self.contexts.append(context)
        return CompleteStepAction(
            type="complete_step",
            summary=f"Completed turn step {context.step.step_number}.",
            sources=[],
        )


def test_runtime_continues_same_thread_with_persisted_bounded_context(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "runtime.sqlite"
        limits = RuntimeLimits(max_total_tool_calls=5, max_session_tool_calls=30)
        first_planner = ContextCapturingPlanner()
        first_sink = InMemoryTraceSink()
        async with open_sqlite_checkpointer(database) as checkpointer:
            runtime = build_agent_runtime(
                first_planner,
                CompletingSelector(),
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=limits,
                tracer=ExecutionTracer(first_sink),
                conversation_repository=SQLiteConversationRepository(database),
            )
            first = await runtime.run(
                "Remember that my project is called Alpha.",
                thread_id="conversation-alpha",
                turn_id="turn-001",
            )
            second = await runtime.continue_thread(
                "conversation-alpha",
                "What is the name of my project?",
                turn_id="turn-002",
            )
            duplicate = await runtime.continue_thread(
                "conversation-alpha",
                "What is the name of my project?",
                turn_id="turn-002",
            )

        assert first["turn_id"] == "turn-001"
        assert second["turn_id"] == "turn-002"
        assert duplicate["turn_id"] == "turn-002"
        second_context = first_planner.calls[1][1]
        assert second_context is not None
        assert [message.turn_id for message in second_context.messages] == [
            "turn-001",
            "turn-001",
        ]
        assert second["evidence"] == []
        assert {event.turn_id for event in first_sink.events} == {
            "turn-001",
            "turn-002",
        }

        second_planner = ContextCapturingPlanner()
        async with open_sqlite_checkpointer(database) as checkpointer:
            reconstructed = build_agent_runtime(
                second_planner,
                CompletingSelector(),
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=limits,
                conversation_repository=SQLiteConversationRepository(database),
            )
            third = await reconstructed.continue_thread(
                "conversation-alpha",
                "Summarize what you remember about the project.",
                turn_id="turn-003",
            )
            snapshot = await reconstructed.load_conversation("conversation-alpha")

        repository = SQLiteConversationRepository(database)
        reserved, created = await repository.reserve_follow_up(
            thread_id="conversation-alpha",
            turn_id="turn-004",
            user_message="Recover this reserved turn after a pre-checkpoint restart.",
            reserved_tool_calls=limits.max_total_tool_calls,
        )
        assert created is True
        assert reserved.status == "running"
        recovery_planner = ContextCapturingPlanner()
        async with open_sqlite_checkpointer(database) as checkpointer:
            recovery_runtime = build_agent_runtime(
                recovery_planner,
                CompletingSelector(),
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=limits,
                conversation_repository=SQLiteConversationRepository(database),
            )
            recovered = await recovery_runtime.resume(thread_id="conversation-alpha")
            snapshot = await recovery_runtime.load_conversation("conversation-alpha")

        public_threads = await list_research_threads(database)

        assert third["turn_id"] == "turn-003"
        assert recovered["turn_id"] == "turn-004"
        assert [turn.record.turn_id for turn in snapshot.turns] == [
            "turn-001",
            "turn-002",
            "turn-003",
            "turn-004",
        ]
        assert all(turn.state is not None for turn in snapshot.turns)
        assert all(turn.traces for turn in snapshot.turns)
        assert all(
            event.turn_id == turn.record.turn_id
            for turn in snapshot.turns
            for event in turn.traces
        )
        assert len(second_planner.calls[0][1].messages) == 4  # type: ignore[union-attr]
        assert public_threads == ("conversation-alpha",)

    asyncio.run(scenario())


def test_conversation_runtime_does_not_shadow_a_legacy_public_checkpoint(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "legacy.sqlite"
        planner = ContextCapturingPlanner()
        async with open_sqlite_checkpointer(database) as checkpointer:
            legacy = build_agent_runtime(
                planner,
                CompletingSelector(),
                ToolRegistry(),
                checkpointer=checkpointer,
            )
            await legacy.run("Complete the legacy thread.", thread_id="legacy-thread")

        async with open_sqlite_checkpointer(database) as checkpointer:
            upgraded = build_agent_runtime(
                ContextCapturingPlanner(),
                CompletingSelector(),
                ToolRegistry(),
                checkpointer=checkpointer,
                conversation_repository=SQLiteConversationRepository(database),
            )
            with pytest.raises(ThreadAlreadyExistsError):
                await upgraded.run(
                    "Do not shadow the old checkpoint.",
                    thread_id="legacy-thread",
                    turn_id="turn-new",
                )
            resumed = await upgraded.resume(thread_id="legacy-thread")
            assert resumed["final_answer"] is not None

        assert await SQLiteConversationRepository(database).list_conversations() == ()

    asyncio.run(scenario())


def test_active_first_turn_binds_payload_and_resume_honors_reservation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "reservation.sqlite"
        repository = SQLiteConversationRepository(database)
        await repository.create_conversation_with_turn(
            thread_id="reserved-thread",
            turn_id="reserved-turn",
            user_message="The original reserved payload.",
            session_tool_call_limit=10,
            reserved_tool_calls=2,
        )
        async with open_sqlite_checkpointer(database) as checkpointer:
            runtime = build_agent_runtime(
                ContextCapturingPlanner(),
                CompletingSelector(),
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=RuntimeLimits(max_total_tool_calls=3),
                conversation_repository=repository,
            )
            with pytest.raises(IdempotencyConflictError):
                await runtime.run(
                    "A different payload must be rejected.",
                    thread_id="reserved-thread",
                    turn_id="reserved-turn",
                )
            with pytest.raises(SessionBudgetExceededError):
                await runtime.resume(thread_id="reserved-thread")

    asyncio.run(scenario())
