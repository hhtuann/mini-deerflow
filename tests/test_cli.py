import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mini_deerflow.cli import (
    main,
    run_research_agent,
)
from mini_deerflow.config import Settings
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
    assert call.kwargs["allow_write"] is True
    assert call.kwargs["limits"] == RuntimeLimits(
        max_tool_calls_per_step=2,
        max_total_tool_calls=4,
        recursion_limit=30,
    )


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
            ]
        )

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "value must be greater than zero" in captured.err


def test_run_research_agent_builds_and_runs_runtime(
    tmp_path: Path,
) -> None:
    goal = "Research planning in tool-using AI agents."
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

    with (
        patch(
            "mini_deerflow.cli.Settings",
            return_value=settings,
        ) as settings_class,
        patch(
            "mini_deerflow.cli.create_default_agent_runtime",
            return_value=runtime,
        ) as create_runtime,
    ):
        actual_state = asyncio.run(
            run_research_agent(
                goal,
                workspace_root=tmp_path,
                allow_write=True,
                limits=limits,
            )
        )

    assert actual_state is expected_state
    settings_class.assert_called_once_with()
    create_runtime.assert_called_once_with(
        settings,
        tmp_path,
        allow_write=True,
        limits=limits,
    )
    runtime.run.assert_awaited_once_with(goal)
