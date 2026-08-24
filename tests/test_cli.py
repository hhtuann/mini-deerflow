import json
from unittest.mock import patch

from mini_deerflow.cli import main
from mini_deerflow.schemas import Plan, PlanStep


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


def test_main_prints_plan_as_json(capsys):
    expected_plan = make_plan()

    with patch(
        "mini_deerflow.cli.run_research_planner",
        return_value=expected_plan,
    ):
        exit_code = main(["Research planning in tool-using AI agents."])

    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert output["goal"] == expected_plan.goal
    assert len(output["steps"]) == 3


def test_main_reports_validation_error(capsys):
    with patch(
        "mini_deerflow.cli.run_research_planner",
        side_effect=ValueError("goal is invalid"),
    ):
        exit_code = main(["Invalid research goal"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Error: goal is invalid" in captured.err
