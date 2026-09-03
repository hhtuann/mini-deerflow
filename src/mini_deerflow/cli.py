import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from mini_deerflow.config import Settings
from mini_deerflow.model import create_chat_model
from mini_deerflow.planner import create_research_plan
from mini_deerflow.runtime import (
    RuntimeLimits,
    create_default_agent_runtime,
)
from mini_deerflow.schemas import Plan
from mini_deerflow.state import AgentState


def positive_integer(value: str) -> int:
    """Parse one strictly positive command-line integer."""

    try:
        parsed_value = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "value must be an integer",
        ) from error

    if parsed_value <= 0:
        raise argparse.ArgumentTypeError(
            "value must be greater than zero",
        )

    return parsed_value


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""

    parser = argparse.ArgumentParser(
        prog="mini-deerflow",
        description="Plan or run a bounded deep research agent.",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    plan_parser = subparsers.add_parser(
        "plan",
        help="Create a validated research plan.",
    )
    plan_parser.add_argument(
        "goal",
        help="Technical research goal to convert into a plan.",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Run the bounded research workflow.",
    )
    run_parser.add_argument(
        "goal",
        help="Technical research goal for the agent.",
    )
    run_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".mini-deerflow/workspace"),
        help=(
            "Workspace directory available to file tools. "
            "Default: .mini-deerflow/workspace"
        ),
    )
    run_parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Allow the agent to use the write_file tool.",
    )
    run_parser.add_argument(
        "--max-tool-calls-per-step",
        type=positive_integer,
        default=5,
        help="Maximum tool calls allowed in one plan step.",
    )
    run_parser.add_argument(
        "--max-total-tool-calls",
        type=positive_integer,
        default=20,
        help="Maximum tool calls allowed in the entire run.",
    )
    run_parser.add_argument(
        "--recursion-limit",
        type=positive_integer,
        default=100,
        help="Maximum LangGraph execution steps.",
    )

    return parser


def run_research_planner(goal: str) -> Plan:
    """Build planner dependencies and create a research plan."""

    settings = Settings()
    model = create_chat_model(settings)

    return create_research_plan(model, goal)


async def run_research_agent(
    goal: str,
    *,
    workspace_root: str | Path,
    allow_write: bool,
    limits: RuntimeLimits,
) -> AgentState:
    """Build the default runtime and execute one research goal."""

    settings = Settings()

    runtime = create_default_agent_runtime(
        settings,
        workspace_root,
        allow_write=allow_write,
        limits=limits,
    )

    return await runtime.run(goal)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Mini DeerFlow command-line interface."""

    parser = build_parser()
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "plan":
            plan = run_research_planner(arguments.goal)
            print(plan.model_dump_json(indent=2))
            return 0

        limits = RuntimeLimits(
            max_tool_calls_per_step=(arguments.max_tool_calls_per_step),
            max_total_tool_calls=arguments.max_total_tool_calls,
            recursion_limit=arguments.recursion_limit,
        )

        state = asyncio.run(
            run_research_agent(
                arguments.goal,
                workspace_root=arguments.workspace,
                allow_write=arguments.allow_write,
                limits=limits,
            )
        )

        final_answer = state["final_answer"]

        if final_answer is None:
            raise RuntimeError(
                "agent completed without a final answer",
            )

        print(final_answer)
        return 0

    except (
        ValidationError,
        ValueError,
        TypeError,
        RuntimeError,
        TimeoutError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
