import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from mini_deerflow.actions import (
    AgentAction,
    CompleteStepAction,
    ToolCallAction,
)
from mini_deerflow.decision import ActionContext
from mini_deerflow.persistence import (
    CheckpointStorageError,
    ThreadAlreadyExistsError,
    create_thread_config,
    list_thread_ids,
    open_sqlite_checkpointer,
)
from mini_deerflow.runtime import RuntimeLimits, build_agent_runtime
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.tools import (
    ToolInput,
    ToolRegistry,
    ToolResult,
)

_STRICT_TEST_CHILD_ENV = "MINI_DEERFLOW_STRICT_CHECKPOINT_TEST_CHILD"
_STRICT_TEST_DATABASE_ENV = "MINI_DEERFLOW_STRICT_CHECKPOINT_TEST_DATABASE"


class EchoInput(ToolInput):
    text: str


class CountingEchoTool:
    name = "echo"
    description = "Return the supplied text."
    input_model = EchoInput
    timeout_seconds = 1.0
    idempotent = True

    def __init__(self) -> None:
        self.call_count = 0

    async def run(self, tool_input: ToolInput) -> ToolResult:
        self.call_count += 1
        return ToolResult.ok(tool_input.model_dump())


class CountingPlanner:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, goal: str) -> Plan:
        self.call_count += 1
        return create_three_step_plan(goal)


class CrashAfterCheckpointedWorkSelector:
    def __init__(self) -> None:
        self.selected_step_numbers: list[int] = []

    async def select_action(
        self,
        context: ActionContext,
    ) -> AgentAction:
        step_number = context.step.step_number
        self.selected_step_numbers.append(step_number)

        if self.selected_step_numbers == [1]:
            return ToolCallAction(
                type="tool_call",
                tool_name="echo",
                arguments={"text": "checkpointed step one evidence"},
            )

        if self.selected_step_numbers == [1, 1]:
            return CompleteStepAction(
                type="complete_step",
                summary="Completed step one with checkpointed tool evidence.",
            )

        if self.selected_step_numbers == [1, 1, 2]:
            raise RuntimeError("deliberate decision interruption")

        raise AssertionError("unexpected initial action selection")


class CompletingCheckpointedResumeSelector:
    def __init__(self) -> None:
        self.selected_step_numbers: list[int] = []

    async def select_action(
        self,
        context: ActionContext,
    ) -> AgentAction:
        step_number = context.step.step_number
        self.selected_step_numbers.append(step_number)

        if step_number == 1:
            return ToolCallAction(
                type="tool_call",
                tool_name="echo",
                arguments={"text": "duplicated step one evidence"},
            )

        return CompleteStepAction(
            type="complete_step",
            summary=f"Completed research step {step_number} after resume.",
        )


class CrashAfterFirstStepSelector:
    def __init__(self) -> None:
        self.selected_step_numbers: list[int] = []

    async def select_action(
        self,
        context: ActionContext,
    ) -> CompleteStepAction:
        step_number = context.step.step_number
        self.selected_step_numbers.append(step_number)

        if step_number == 1:
            return CompleteStepAction(
                type="complete_step",
                summary="Completed step one before the simulated crash.",
            )

        raise RuntimeError(
            "simulated process crash",
        )


class CompletingAfterResumeSelector:
    def __init__(self) -> None:
        self.selected_step_numbers: list[int] = []

    async def select_action(
        self,
        context: ActionContext,
    ) -> CompleteStepAction:
        step_number = context.step.step_number
        self.selected_step_numbers.append(step_number)

        return CompleteStepAction(
            type="complete_step",
            summary=(f"Completed research step {step_number} after resume."),
        )


def create_three_step_plan(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=number,
                title=f"Research step {number}",
                objective=(f"Collect verifiable evidence for research step {number}."),
                success_criteria=("A clear and verifiable result is recorded."),
            )
            for number in range(1, 4)
        ],
    )


def reject_replanning(_: str) -> Plan:
    raise AssertionError(
        "planner must not run while resuming a persisted thread",
    )


def test_sqlite_resume_does_not_repeat_completed_steps(
    tmp_path: Path,
) -> None:
    async def run_scenario() -> None:
        checkpoint_path = tmp_path / "checkpoints.sqlite"
        thread_id = "crash-resume-test"
        goal = "Research deterministic SQLite crash and resume behavior."
        limits = RuntimeLimits(
            max_tool_calls_per_step=2,
            max_total_tool_calls=6,
            recursion_limit=60,
        )

        crashing_selector = CrashAfterFirstStepSelector()

        async with open_sqlite_checkpointer(
            checkpoint_path,
        ) as checkpointer:
            first_runtime = build_agent_runtime(
                create_three_step_plan,
                crashing_selector,
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=limits,
            )

            with pytest.raises(
                RuntimeError,
                match="simulated process crash",
            ):
                await first_runtime.run(
                    goal,
                    thread_id=thread_id,
                )

        completing_selector = CompletingAfterResumeSelector()

        async with open_sqlite_checkpointer(
            checkpoint_path,
        ) as checkpointer:
            resumed_runtime = build_agent_runtime(
                reject_replanning,
                completing_selector,
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=limits,
            )

            result = await resumed_runtime.resume(
                thread_id=thread_id,
            )

        assert checkpoint_path.is_file()

        assert crashing_selector.selected_step_numbers == [
            1,
            2,
        ]
        assert completing_selector.selected_step_numbers == [
            2,
            3,
        ]

        assert result["current_step"] == 3
        assert result["notes"] == [
            "Completed step one before the simulated crash.",
            "Completed research step 2 after resume.",
            "Completed research step 3 after resume.",
        ]
        assert result["total_tool_calls"] == 0
        assert result["pending_action"] is None
        assert result["final_answer"] is not None

    asyncio.run(run_scenario())


def test_sqlite_resume_retries_failed_decision_without_repeating_work(
    tmp_path: Path,
) -> None:
    async def run_scenario() -> None:
        checkpoint_path = tmp_path / "checkpointed-tool-resume.sqlite"
        thread_id = "checkpointed-tool-resume-test"
        goal = "Research deterministic checkpointed tool resume behavior."
        completed_step_one_summary = (
            "Completed step one with checkpointed tool evidence."
        )
        expected_plan = create_three_step_plan(goal)
        limits = RuntimeLimits(
            max_tool_calls_per_step=2,
            max_total_tool_calls=6,
            recursion_limit=60,
        )
        planner = CountingPlanner()
        crashing_selector = CrashAfterCheckpointedWorkSelector()
        initial_tool = CountingEchoTool()

        async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            first_runtime = build_agent_runtime(
                planner,
                crashing_selector,
                ToolRegistry([initial_tool]),
                checkpointer=checkpointer,
                limits=limits,
            )

            with pytest.raises(
                RuntimeError,
                match="deliberate decision interruption",
            ):
                await first_runtime.run(goal, thread_id=thread_id)

            config = create_thread_config(
                thread_id,
                recursion_limit=limits.recursion_limit,
            )
            checkpoint_before_close = await checkpointer.aget_tuple(config)

            assert checkpoint_before_close is not None
            state_before_close = checkpoint_before_close.checkpoint["channel_values"]
            assert state_before_close["plan"] == expected_plan
            assert state_before_close["current_step"] == 1
            assert state_before_close["pending_action"] is None
            assert state_before_close["notes"] == [completed_step_one_summary]
            assert state_before_close["total_tool_calls"] == 1
            assert len(state_before_close["tool_observations"]) == 1

        assert checkpoint_path.is_file()
        assert planner.call_count == 1
        assert crashing_selector.selected_step_numbers == [1, 1, 2]
        assert initial_tool.call_count == 1

        resumed_selector = CompletingCheckpointedResumeSelector()
        resumed_tool = CountingEchoTool()

        async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            assert await list_thread_ids(checkpointer) == (thread_id,)

            checkpoint_after_reopen = await checkpointer.aget_tuple(config)

            assert checkpoint_after_reopen is not None
            state_after_reopen = checkpoint_after_reopen.checkpoint["channel_values"]
            assert state_after_reopen["plan"] == expected_plan
            assert state_after_reopen["current_step"] == 1
            assert state_after_reopen["pending_action"] is None
            assert state_after_reopen["notes"] == [completed_step_one_summary]
            assert state_after_reopen["total_tool_calls"] == 1
            assert len(state_after_reopen["tool_observations"]) == 1

            resumed_runtime = build_agent_runtime(
                planner,
                resumed_selector,
                ToolRegistry([resumed_tool]),
                checkpointer=checkpointer,
                limits=limits,
            )
            result = await resumed_runtime.resume(thread_id=thread_id)

        assert planner.call_count == 1
        assert crashing_selector.selected_step_numbers == [1, 1, 2]
        assert resumed_selector.selected_step_numbers == [2, 3]
        assert initial_tool.call_count == 1
        assert resumed_tool.call_count == 0

        assert result["plan"] == expected_plan
        assert result["current_step"] == len(expected_plan.steps)
        assert result["pending_action"] is None
        assert result["notes"] == [
            completed_step_one_summary,
            "Completed research step 2 after resume.",
            "Completed research step 3 after resume.",
        ]
        assert result["notes"].count(completed_step_one_summary) == 1
        assert result["total_tool_calls"] == 1
        assert len(result["tool_observations"]) == 1
        assert result["tool_observations"][0].result == ToolResult.ok(
            {"text": "checkpointed step one evidence"}
        )
        assert result["errors"] == []
        assert result["final_answer"] is not None

    asyncio.run(run_scenario())


def test_strict_mode_sqlite_listing_and_resume_preserve_domain_state(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    thread_id = "strict-crash-resume-test"
    goal = "Research strict checkpoint deserialization behavior."
    limits = RuntimeLimits(
        max_tool_calls_per_step=2,
        max_total_tool_calls=6,
        recursion_limit=60,
    )

    if os.environ.get(_STRICT_TEST_CHILD_ENV) != "1":
        checkpoint_path = tmp_path / "strict-checkpoints.sqlite"

        async def create_checkpoint() -> None:
            async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
                crashing_runtime = build_agent_runtime(
                    create_three_step_plan,
                    CrashAfterFirstStepSelector(),
                    ToolRegistry(),
                    checkpointer=checkpointer,
                    limits=limits,
                )

                with pytest.raises(RuntimeError, match="simulated process crash"):
                    await crashing_runtime.run(goal, thread_id=thread_id)

        asyncio.run(create_checkpoint())

        assert checkpoint_path.is_file()

        environment = os.environ.copy()
        environment[_STRICT_TEST_CHILD_ENV] = "1"
        environment[_STRICT_TEST_DATABASE_ENV] = str(checkpoint_path)
        environment["LANGGRAPH_STRICT_MSGPACK"] = "true"
        node_id = (
            f"{Path(__file__).resolve()}::"
            "test_strict_mode_sqlite_listing_and_resume_preserve_domain_state"
        )

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                node_id,
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert completed.stderr == ""
        assert "1 passed" in completed.stdout
        return

    assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "true"
    caplog.set_level(logging.WARNING)

    async def run_scenario() -> None:
        checkpoint_path = Path(os.environ[_STRICT_TEST_DATABASE_ENV])
        expected_plan = create_three_step_plan(goal)

        completing_selector = CompletingAfterResumeSelector()

        async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
            assert await list_thread_ids(checkpointer) == (thread_id,)

            resumed_runtime = build_agent_runtime(
                reject_replanning,
                completing_selector,
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=limits,
            )
            result = await resumed_runtime.resume(thread_id=thread_id)

        assert isinstance(result["plan"], Plan)
        assert result["plan"] == expected_plan
        assert result["notes"] == [
            "Completed step one before the simulated crash.",
            "Completed research step 2 after resume.",
            "Completed research step 3 after resume.",
        ]
        assert result["pending_action"] is None
        assert result["final_answer"] is not None
        assert completing_selector.selected_step_numbers == [2, 3]

    asyncio.run(run_scenario())

    messages = [record.getMessage() for record in caplog.records]
    assert not any("Deserializing unregistered type" in message for message in messages)
    assert not any("Blocked deserialization" in message for message in messages)


def test_sqlite_run_rejects_existing_thread_without_overwriting_checkpoint(
    tmp_path: Path,
) -> None:
    async def run_scenario() -> None:
        checkpoint_path = tmp_path / "checkpoints.sqlite"
        thread_id = "duplicate-run-test"
        goal = "Research deterministic duplicate run behavior."
        limits = RuntimeLimits(
            max_tool_calls_per_step=2,
            max_total_tool_calls=6,
            recursion_limit=60,
        )

        first_selector = CompletingAfterResumeSelector()

        async with open_sqlite_checkpointer(
            checkpoint_path,
        ) as checkpointer:
            first_runtime = build_agent_runtime(
                create_three_step_plan,
                first_selector,
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=limits,
            )

            first_result = await first_runtime.run(
                goal,
                thread_id=thread_id,
            )

            config = create_thread_config(
                thread_id,
                recursion_limit=limits.recursion_limit,
            )
            original_checkpoint = await checkpointer.aget_tuple(config)

            assert original_checkpoint is not None
            assert first_result["final_answer"] is not None

        duplicate_selector = CompletingAfterResumeSelector()

        async with open_sqlite_checkpointer(
            checkpoint_path,
        ) as checkpointer:
            duplicate_runtime = build_agent_runtime(
                reject_replanning,
                duplicate_selector,
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=limits,
            )

            with pytest.raises(
                ThreadAlreadyExistsError,
                match=r"thread 'duplicate-run-test' already has a checkpoint",
            ):
                await duplicate_runtime.run(
                    goal,
                    thread_id=thread_id,
                )

            checkpoint_after_duplicate = await checkpointer.aget_tuple(config)

            assert checkpoint_after_duplicate is not None
            assert checkpoint_after_duplicate == original_checkpoint
            assert duplicate_selector.selected_step_numbers == []

        resumed_selector = CompletingAfterResumeSelector()

        async with open_sqlite_checkpointer(
            checkpoint_path,
        ) as checkpointer:
            resumed_runtime = build_agent_runtime(
                reject_replanning,
                resumed_selector,
                ToolRegistry(),
                checkpointer=checkpointer,
                limits=limits,
            )

            resumed_result = await resumed_runtime.resume(
                thread_id=thread_id,
            )

        assert resumed_result["goal"] == goal
        assert resumed_result["final_answer"] == first_result["final_answer"]
        assert resumed_selector.selected_step_numbers == []

    asyncio.run(run_scenario())


def test_sqlite_graph_storage_error_is_normalized(
    tmp_path: Path,
) -> None:
    original_error = sqlite3.OperationalError(
        "deterministic graph checkpoint write failure",
    )

    async def run_scenario() -> CheckpointStorageError:
        checkpoint_path = tmp_path / "graph-storage-error.sqlite"

        with patch.object(
            AsyncSqliteSaver,
            "aput",
            new=AsyncMock(side_effect=original_error),
        ):
            async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
                runtime = build_agent_runtime(
                    create_three_step_plan,
                    CompletingAfterResumeSelector(),
                    ToolRegistry(),
                    checkpointer=checkpointer,
                )

                with pytest.raises(
                    CheckpointStorageError,
                    match="write checkpoint data",
                ) as raised_error:
                    await runtime.run(
                        "Research graph checkpoint storage failures.",
                        thread_id="graph-storage-failure",
                    )

        return raised_error.value

    actual_error = asyncio.run(run_scenario())

    assert actual_error.__cause__ is original_error


def test_sqlite_lists_sorted_unique_threads_after_reopening_database(
    tmp_path: Path,
) -> None:
    async def run_scenario() -> None:
        checkpoint_path = tmp_path / "checkpoints.sqlite"
        thread_ids = (
            "research-002",
            "research-001",
        )
        limits = RuntimeLimits(
            max_tool_calls_per_step=2,
            max_total_tool_calls=6,
            recursion_limit=60,
        )

        async with open_sqlite_checkpointer(
            checkpoint_path,
        ) as checkpointer:
            for thread_id in thread_ids:
                runtime = build_agent_runtime(
                    create_three_step_plan,
                    CompletingAfterResumeSelector(),
                    ToolRegistry(),
                    checkpointer=checkpointer,
                    limits=limits,
                )

                result = await runtime.run(
                    f"Research {thread_id}.",
                    thread_id=thread_id,
                )

                assert result["final_answer"] is not None

        async with open_sqlite_checkpointer(
            checkpoint_path,
        ) as checkpointer:
            listed_thread_ids = await list_thread_ids(checkpointer)

        assert listed_thread_ids == (
            "research-001",
            "research-002",
        )

    asyncio.run(run_scenario())
