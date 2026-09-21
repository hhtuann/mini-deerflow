import asyncio
from pathlib import Path

import pytest

from mini_deerflow.conversation import (
    ActiveTurnError,
    IdempotencyConflictError,
    InvalidConversationTransitionError,
    SessionBudgetExceededError,
    SQLiteConversationRepository,
    sanitize_conversation_title,
)
from mini_deerflow.evidence import render_research_report


def test_conversation_repository_persists_turns_context_and_idempotency(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "conversation.sqlite"
        repository = SQLiteConversationRepository(database)
        first = await repository.create_conversation_with_turn(
            thread_id="alpha-thread",
            turn_id="turn-001",
            user_message="Remember that my project is called Alpha.",
            session_tool_call_limit=20,
            reserved_tool_calls=5,
        )
        assert first.sequence == 1
        await repository.complete_turn(
            thread_id="alpha-thread",
            turn_id="turn-001",
            assistant_response="I will remember the project name Alpha.",
            total_tool_calls=2,
        )

        second, created = await repository.reserve_follow_up(
            thread_id="alpha-thread",
            turn_id="turn-002",
            user_message="What is the name of my project?",
            reserved_tool_calls=5,
        )
        assert created is True
        assert second.sequence == 2
        context = await repository.load_context(
            "alpha-thread",
            before_sequence=second.sequence,
        )
        assert [message.role for message in context.messages] == [
            "user",
            "assistant",
        ]
        assert "Alpha" in context.messages[0].content

        duplicate, duplicate_created = await repository.reserve_follow_up(
            thread_id="alpha-thread",
            turn_id="turn-002",
            user_message="What is the name of my project?",
            reserved_tool_calls=5,
        )
        assert duplicate_created is False
        assert duplicate.turn_id == second.turn_id

        await repository.complete_turn(
            thread_id="alpha-thread",
            turn_id="turn-002",
            assistant_response="Your project is called Alpha.",
            total_tool_calls=1,
        )
        await repository.store_trace_events(
            "alpha-thread",
            "turn-002",
            ('{"thread_id":"alpha-thread","turn_id":"turn-002"}',),
        )

        reopened = SQLiteConversationRepository(database)
        conversation = await reopened.get_conversation("alpha-thread")
        turns = await reopened.list_turns("alpha-thread")
        assert conversation.session_tool_calls == 3
        assert [turn.turn_id for turn in turns] == ["turn-001", "turn-002"]
        assert turns[1].trace_events == (
            '{"thread_id":"alpha-thread","turn_id":"turn-002"}',
        )

    asyncio.run(scenario())


def test_repository_prevents_parallel_turns_and_enforces_session_budget(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = SQLiteConversationRepository(tmp_path / "budget.sqlite")
        await repository.create_conversation_with_turn(
            thread_id="budget-thread",
            turn_id="turn-001",
            user_message="Run the first bounded turn.",
            session_tool_call_limit=5,
            reserved_tool_calls=3,
        )
        with pytest.raises(ActiveTurnError):
            await repository.reserve_follow_up(
                thread_id="budget-thread",
                turn_id="turn-002",
                user_message="This turn must wait.",
                reserved_tool_calls=3,
            )
        await repository.complete_turn(
            thread_id="budget-thread",
            turn_id="turn-001",
            assistant_response="Completed.",
            total_tool_calls=3,
        )
        with pytest.raises(SessionBudgetExceededError):
            await repository.reserve_follow_up(
                thread_id="budget-thread",
                turn_id="turn-002",
                user_message="This full reservation cannot fit.",
                reserved_tool_calls=3,
            )

    asyncio.run(scenario())


def test_idempotency_conflict_long_ids_and_failed_budget_are_safe(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = SQLiteConversationRepository(tmp_path / "edge-cases.sqlite")
        long_turn_id = "t" * 128
        await repository.create_conversation_with_turn(
            thread_id="edge-thread",
            turn_id=long_turn_id,
            user_message="First message for the long identifier.",
            session_tool_call_limit=10,
            reserved_tool_calls=5,
        )
        await repository.complete_turn(
            thread_id="edge-thread",
            turn_id=long_turn_id,
            assistant_response="First response.",
            total_tool_calls=2,
        )
        context = await repository.load_context("edge-thread")
        assert len(context.messages) == 2
        assert all(len(message.message_id) <= 128 for message in context.messages)

        with pytest.raises(IdempotencyConflictError):
            await repository.reserve_follow_up(
                thread_id="edge-thread",
                turn_id=long_turn_id,
                user_message="A different payload must not replay the old result.",
                reserved_tool_calls=5,
            )

        failed, _ = await repository.reserve_follow_up(
            thread_id="edge-thread",
            turn_id="failed-turn",
            user_message="This turn will fail after one tool call.",
            reserved_tool_calls=5,
        )
        await repository.mark_turn_failed(
            thread_id=failed.thread_id,
            turn_id=failed.turn_id,
            total_tool_calls=1,
        )
        await repository.mark_turn_failed(
            thread_id=failed.thread_id,
            turn_id=failed.turn_id,
            total_tool_calls=1,
        )
        conversation = await repository.get_conversation("edge-thread")
        assert conversation.session_tool_calls == 3
        with pytest.raises(InvalidConversationTransitionError):
            await repository.mark_turn_interrupted(
                thread_id=failed.thread_id,
                turn_id=failed.turn_id,
            )

        await repository.create_conversation_with_turn(
            thread_id="charge-thread",
            turn_id="charge-turn",
            user_message="Enforce the terminal reservation.",
            session_tool_call_limit=1,
            reserved_tool_calls=1,
        )
        with pytest.raises(SessionBudgetExceededError):
            await repository.complete_turn(
                thread_id="charge-thread",
                turn_id="charge-turn",
                assistant_response="Must not commit.",
                total_tool_calls=2,
            )
        with pytest.raises(SessionBudgetExceededError):
            await repository.mark_turn_failed(
                thread_id="charge-thread",
                turn_id="charge-turn",
                total_tool_calls=2,
            )
        charge_conversation = await repository.get_conversation("charge-thread")
        assert charge_conversation.session_tool_calls == 0

    asyncio.run(scenario())


def test_title_is_deterministic_sanitized_and_bounded() -> None:
    title = sanitize_conversation_title(
        "  Research\nRAG\x00 for enterprise " + "x" * 90
    )
    assert "\n" not in title
    assert "\x00" not in title
    assert title.startswith("Research RAG for enterprise")
    assert len(title) <= 65


def test_legacy_internal_report_is_not_reused_as_follow_up_context(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = SQLiteConversationRepository(tmp_path / "legacy.sqlite")
        await repository.create_conversation_with_turn(
            thread_id="legacy-thread",
            turn_id="turn-001",
            user_message="Run the legacy research turn.",
            session_tool_call_limit=10,
            reserved_tool_calls=5,
        )
        report = render_research_report(
            goal="Legacy internal goal",
            findings=[],
            evidence=[],
            errors=[],
            total_tool_calls=3,
            successful_tool_calls=3,
            failed_tool_calls=0,
        )
        await repository.complete_turn(
            thread_id="legacy-thread",
            turn_id="turn-001",
            assistant_response=report,
            total_tool_calls=3,
        )

        context = await repository.load_context("legacy-thread")
        assistant = context.messages[1].content
        assert "prior research turn completed" in assistant
        assert "# Research Report" not in assistant
        assert "Tool calls:" not in assistant

    asyncio.run(scenario())
