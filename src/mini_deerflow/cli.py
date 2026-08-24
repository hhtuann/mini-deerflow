import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from mini_deerflow.config import Settings
from mini_deerflow.model import create_chat_model
from mini_deerflow.planner import create_research_plan
from mini_deerflow.schemas import Plan


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""

    parser = argparse.ArgumentParser(
        prog="mini-deerflow",
        description="Create a validated research plan.",
    )

    parser.add_argument(
        "goal",
        help="Technical research goal to convert into a plan.",
    )

    return parser


def run_research_planner(goal: str) -> Plan:
    """Build application dependencies and create a research plan."""

    settings = Settings()
    model = create_chat_model(settings)

    return create_research_plan(model, goal)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Mini DeerFlow command-line interface."""

    parser = build_parser()
    arguments = parser.parse_args(argv)

    try:
        plan = run_research_planner(arguments.goal)
    except (ValidationError, ValueError, TypeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(plan.model_dump_json(indent=2))
    return 0
