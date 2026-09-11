import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from mini_deerflow.cli import (
    build_parser,
    list_research_threads,
    main,
    resume_research_agent,
    run_research_agent,
)
from mini_deerflow.config import Settings
from mini_deerflow.persistence import (
    CheckpointStorageError,
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
)
from mini_deerflow.runtime import (
    AgentRuntime,
    RuntimeLimits,
)
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import AgentState, create_initial_state


def make_plan() -> Plan:
    return Plan(
        goal="Research planning in tool-using AI agents.",
        steps=[
            PlanStep(
                step_number=number,
                title=f"Research step {number}",
                objective=f"Collect evidence for research step {number}.",
                success_criteria="At least one verifiable result is recorded.",
            )
            for number in range(1, 4)
        ],
    )


def make_final_state() -> AgentState:
    state = create_initial_state("Research planning in tool-using AI agents.")
    state["final_answer"] = "The bounded agent run completed."

    return state


def test_main_plan_prints_plan_as_json(capsys) -> None:
    expected_plan = make_plan()

    with patch(
        "mini_deerflow.cli.run_research_planner",
        return_value=expected_plan,
    ):
        exit_code = main(
            [
                "plan",
                "Research planning in tool-using AI agents.",
            ]
        )

    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert output["goal"] == expected_plan.goal
    assert len(output["steps"]) == 3


def test_main_plan_reports_validation_error(capsys) -> None:
    with patch(
        "mini_deerflow.cli.run_research_planner",
        side_effect=ValueError("goal is invalid"),
    ):
        exit_code = main(
            [
                "plan",
                "Invalid research goal",
            ]
        )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Error: goal is invalid" in captured.err


def test_main_run_prints_final_answer(
    capsys,
    tmp_path: Path,
) -> None:
    expected_state = make_final_state()

    with patch(
        "mini_deerflow.cli.run_research_agent",
        new_callable=AsyncMock,
        return_value=expected_state,
    ) as run_agent:
        exit_code = main(
            [
                "run",
                "Research planning in tool-using AI agents.",
                "--workspace",
                str(tmp_path),
                "--allow-write",
                "--max-tool-calls-per-step",
                "2",
                "--max-total-tool-calls",
                "4",
                "--recursion-limit",
                "30",
                "--thread-id",
                "cli-test",
                "--checkpoint-db",
                str(tmp_path / "checkpoints.sqlite"),
            ]
        )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == ("The bounded agent run completed.\n")
    assert captured.err == ""

    run_agent.assert_awaited_once()

    call = run_agent.await_args

    assert call.args == ("Research planning in tool-using AI agents.",)
    assert call.kwargs["workspace_root"] == tmp_path
    assert call.kwargs["thread_id"] == "cli-test"
    assert call.kwargs["allow_write"] is True
    assert call.kwargs["limits"] == RuntimeLimits(
        max_tool_calls_per_step=2,
        max_total_tool_calls=4,
        recursion_limit=30,
    )
    assert call.kwargs["checkpoint_path"] == tmp_path / "checkpoints.sqlite"


def test_main_run_rejects_missing_final_answer(
    capsys,
) -> None:
    state = make_final_state()
    state["final_answer"] = None

    with patch(
        "mini_deerflow.cli.run_research_agent",
        new_callable=AsyncMock,
        return_value=state,
    ):
        exit_code = main(
            [
                "run",
                "Research planning in tool-using AI agents.",
                "--thread-id",
                "cli-test",
            ]
        )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Error: agent completed without a final answer" in captured.err


def test_main_rejects_non_positive_runtime_limit(
    capsys,
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "run",
                "Research planning in tool-using AI agents.",
                "--max-total-tool-calls",
                "0",
                "--thread-id",
                "cli-test",
            ]
        )

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "value must be greater than zero" in captured.err


def test_run_research_agent_builds_and_runs_runtime(
    tmp_path: Path,
) -> None:
    goal = "Research planning in tool-using AI agents."
    thread_id = "cli-runtime-test"
    checkpoint_path = tmp_path / "checkpoints.sqlite"
    expected_state = make_final_state()

    settings = Settings(
        api_key="test-api-key",
        _env_file=None,
    )
    limits = RuntimeLimits(
        max_tool_calls_per_step=2,
        max_total_tool_calls=4,
        recursion_limit=30,
    )

    runtime = Mock(spec=AgentRuntime)
    runtime.run = AsyncMock(
        return_value=expected_state,
    )

    runtime_context = MagicMock()
    runtime_context.__aenter__.return_value = runtime
    runtime_context.__aexit__.return_value = None

    with (
        patch(
            "mini_deerflow.cli.Settings",
            return_value=settings,
        ) as settings_class,
        patch(
            "mini_deerflow.cli.open_default_agent_runtime",
            return_value=runtime_context,
        ) as open_runtime,
    ):
        actual_state = asyncio.run(
            run_research_agent(
                goal,
                workspace_root=tmp_path,
                checkpoint_path=checkpoint_path,
                thread_id=thread_id,
                allow_write=True,
                limits=limits,
            )
        )

    assert actual_state is expected_state
    settings_class.assert_called_once_with()

    open_runtime.assert_called_once_with(
        settings,
        tmp_path,
        checkpoint_path,
        allow_write=True,
        limits=limits,
    )

    runtime_context.__aenter__.assert_awaited_once_with()
    runtime_context.__aexit__.assert_awaited_once()

    runtime.run.assert_awaited_once_with(
        goal,
        thread_id=thread_id,
    )


def test_main_run_requires_thread_id(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "run",
                "Research planning in tool-using AI agents.",
            ]
        )

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "--thread-id" in captured.err


def test_main_run_rejects_invalid_thread_id(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "run",
                "Research planning in tool-using AI agents.",
                "--thread-id",
                "../another-thread",
            ]
        )

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "--thread-id" in captured.err


def test_main_run_reports_existing_thread_without_traceback(capsys) -> None:
    with patch(
        "mini_deerflow.cli.run_research_agent",
        new_callable=AsyncMock,
        side_effect=ThreadAlreadyExistsError(
            "thread 'existing-thread' already has a checkpoint",
        ),
    ):
        exit_code = main(
            [
                "run",
                "Research planning in tool-using AI agents.",
                "--thread-id",
                "existing-thread",
            ]
        )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Error: thread 'existing-thread' already has a checkpoint" in captured.err
    assert "Traceback" not in captured.err


def test_main_run_reports_persistence_failure_without_traceback(capsys) -> None:
    with patch(
        "mini_deerflow.cli.run_research_agent",
        new_callable=AsyncMock,
        side_effect=CheckpointStorageError("could not write checkpoint data"),
    ):
        exit_code = main(
            [
                "run",
                "Research persistence error handling.",
                "--thread-id",
                "storage-failure",
            ]
        )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("Error:")
    assert "could not write checkpoint data" in captured.err
    assert "Traceback" not in captured.err


def test_main_threads_prints_json(capsys, tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "checkpoints.sqlite"

    with patch(
        "mini_deerflow.cli.list_research_threads",
        new_callable=AsyncMock,
        return_value=("research-001", "research-002"),
    ) as list_threads:
        exit_code = main(
            [
                "threads",
                "--checkpoint-db",
                str(checkpoint_path),
            ]
        )

    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert output == {
        "threads": [
            "research-001",
            "research-002",
        ],
        "count": 2,
    }
    list_threads.assert_awaited_once_with(checkpoint_path)


def test_main_threads_handles_empty_database_without_runtime_setup(
    capsys,
) -> None:
    with (
        patch(
            "mini_deerflow.cli.list_research_threads",
            new_callable=AsyncMock,
            return_value=(),
        ) as list_threads,
        patch("mini_deerflow.cli.Settings") as settings_class,
        patch("mini_deerflow.cli.open_default_agent_runtime") as open_runtime,
    ):
        exit_code = main(["threads"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert output == {
        "threads": [],
        "count": 0,
    }
    list_threads.assert_awaited_once_with(Path(".mini-deerflow/checkpoints.sqlite"))
    settings_class.assert_not_called()
    open_runtime.assert_not_called()


def test_main_threads_reports_persistence_failure_without_traceback(capsys) -> None:
    with patch(
        "mini_deerflow.cli.list_research_threads",
        new_callable=AsyncMock,
        side_effect=CheckpointStorageError("could not list checkpoint threads"),
    ):
        exit_code = main(["threads"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("Error:")
    assert "could not list checkpoint threads" in captured.err
    assert "Traceback" not in captured.err


def test_list_research_threads_opens_checkpointer_and_lists_threads(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "checkpoints.sqlite"
    expected_thread_ids = ("research-001", "research-002")
    checkpointer = Mock()
    runtime_context = MagicMock()
    runtime_context.__aenter__.return_value = checkpointer
    runtime_context.__aexit__.return_value = None

    with (
        patch(
            "mini_deerflow.cli.open_sqlite_checkpointer",
            return_value=runtime_context,
        ) as open_checkpointer,
        patch(
            "mini_deerflow.cli.list_thread_ids",
            new_callable=AsyncMock,
            return_value=expected_thread_ids,
        ) as list_threads,
    ):
        actual_thread_ids = asyncio.run(list_research_threads(checkpoint_path))

    assert actual_thread_ids == expected_thread_ids
    open_checkpointer.assert_called_once_with(checkpoint_path)
    runtime_context.__aenter__.assert_awaited_once_with()
    runtime_context.__aexit__.assert_awaited_once()
    list_threads.assert_awaited_once_with(checkpointer)


def test_threads_parser_only_accepts_checkpoint_database() -> None:
    arguments = build_parser().parse_args(["threads"])

    assert arguments.command == "threads"
    assert arguments.checkpoint_db == Path(".mini-deerflow/checkpoints.sqlite")
    assert not hasattr(arguments, "thread_id")
    assert not hasattr(arguments, "workspace")
    assert not hasattr(arguments, "max_total_tool_calls")


def test_main_threads_help_exits_successfully(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["threads", "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "List persisted research thread identifiers." in captured.out
    assert "--checkpoint-db" in captured.out
    assert "--thread-id" not in captured.out


def test_main_resume_prints_final_answer(
    capsys,
    tmp_path: Path,
) -> None:
    expected_state = make_final_state()

    with patch(
        "mini_deerflow.cli.resume_research_agent",
        new_callable=AsyncMock,
        return_value=expected_state,
    ) as resume_agent:
        exit_code = main(
            [
                "resume",
                "--workspace",
                str(tmp_path),
                "--allow-write",
                "--max-tool-calls-per-step",
                "2",
                "--max-total-tool-calls",
                "4",
                "--recursion-limit",
                "30",
                "--thread-id",
                "resume-cli-test",
                "--checkpoint-db",
                str(tmp_path / "checkpoints.sqlite"),
            ]
        )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == "The bounded agent run completed.\n"
    assert captured.err == ""

    resume_agent.assert_awaited_once_with(
        workspace_root=tmp_path,
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        thread_id="resume-cli-test",
        allow_write=True,
        limits=RuntimeLimits(
            max_tool_calls_per_step=2,
            max_total_tool_calls=4,
            recursion_limit=30,
        ),
    )


def test_resume_research_agent_builds_and_resumes_runtime(
    tmp_path: Path,
) -> None:
    thread_id = "resume-runtime-test"
    checkpoint_path = tmp_path / "checkpoints.sqlite"
    expected_state = make_final_state()

    settings = Settings(
        api_key="test-api-key",
        _env_file=None,
    )
    limits = RuntimeLimits(
        max_tool_calls_per_step=2,
        max_total_tool_calls=4,
        recursion_limit=30,
    )

    runtime = Mock(spec=AgentRuntime)
    runtime.resume = AsyncMock(
        return_value=expected_state,
    )

    runtime_context = MagicMock()
    runtime_context.__aenter__.return_value = runtime
    runtime_context.__aexit__.return_value = None

    with (
        patch(
            "mini_deerflow.cli.Settings",
            return_value=settings,
        ) as settings_class,
        patch(
            "mini_deerflow.cli.open_default_agent_runtime",
            return_value=runtime_context,
        ) as open_runtime,
    ):
        actual_state = asyncio.run(
            resume_research_agent(
                workspace_root=tmp_path,
                checkpoint_path=checkpoint_path,
                thread_id=thread_id,
                allow_write=True,
                limits=limits,
            )
        )

    assert actual_state is expected_state
    settings_class.assert_called_once_with()
    open_runtime.assert_called_once_with(
        settings,
        tmp_path,
        checkpoint_path,
        allow_write=True,
        limits=limits,
    )
    runtime_context.__aenter__.assert_awaited_once_with()
    runtime_context.__aexit__.assert_awaited_once()
    runtime.resume.assert_awaited_once_with(thread_id=thread_id)
    runtime.run.assert_not_called()


def test_main_resume_reports_unknown_thread_without_traceback(capsys) -> None:
    with patch(
        "mini_deerflow.cli.resume_research_agent",
        new_callable=AsyncMock,
        side_effect=ThreadNotFoundError("thread 'missing' has no checkpoint"),
    ):
        exit_code = main(
            [
                "resume",
                "--thread-id",
                "missing",
            ]
        )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Error: thread 'missing' has no checkpoint" in captured.err
    assert "Traceback" not in captured.err


def test_main_resume_reports_persistence_failure_without_traceback(capsys) -> None:
    with patch(
        "mini_deerflow.cli.resume_research_agent",
        new_callable=AsyncMock,
        side_effect=CheckpointStorageError("could not read checkpoint data"),
    ):
        exit_code = main(
            [
                "resume",
                "--thread-id",
                "storage-failure",
            ]
        )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("Error:")
    assert "could not read checkpoint data" in captured.err
    assert "Traceback" not in captured.err


def test_main_resume_requires_thread_id(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["resume"])

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "--thread-id" in captured.err


def test_main_resume_rejects_invalid_thread_id(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "resume",
                "--thread-id",
                "../another-thread",
            ]
        )

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "--thread-id" in captured.err
