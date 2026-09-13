import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from mini_deerflow.config import Settings
from mini_deerflow.model import create_chat_model
from mini_deerflow.persistence import (
    list_thread_ids,
    normalize_thread_id,
    open_sqlite_checkpointer,
)
from mini_deerflow.planner import create_research_plan
from mini_deerflow.runtime import (
    RuntimeLimits,
    open_default_agent_runtime,
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


def _ensure_utf8_output() -> None:
    """Reconfigure CLI output streams to UTF-8 when the locale default cannot."""

    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue

        encoding = (getattr(stream, "encoding", None) or "").lower()
        if encoding.replace("-", "") == "utf8":
            continue

        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            continue


def _add_checkpoint_argument(
    parser: argparse.ArgumentParser,
) -> None:
    """Add the shared checkpoint database argument."""

    parser.add_argument(
        "--checkpoint-db",
        type=Path,
        default=Path(".mini-deerflow/checkpoints.sqlite"),
        help=(
            "SQLite database used to persist agent threads. "
            "Default: .mini-deerflow/checkpoints.sqlite"
        ),
    )


def _add_runtime_arguments(
    parser: argparse.ArgumentParser,
) -> None:
    """Add arguments shared by the persistent runtime commands."""

    parser.add_argument(
        "--thread-id",
        type=normalize_thread_id,
        required=True,
        help="Stable identifier used to persist and resume one agent thread.",
    )
    _add_checkpoint_argument(parser)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".mini-deerflow/workspace"),
        help=(
            "Workspace directory available to file tools. "
            "Default: .mini-deerflow/workspace"
        ),
    )
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Allow the agent to use the write_file tool.",
    )
    parser.add_argument(
        "--max-tool-calls-per-step",
        type=positive_integer,
        default=5,
        help="Maximum tool calls allowed in one plan step.",
    )
    parser.add_argument(
        "--max-total-tool-calls",
        type=positive_integer,
        default=20,
        help="Maximum tool calls allowed in the entire run.",
    )
    parser.add_argument(
        "--max-replan-cycles",
        type=positive_integer,
        default=2,
        help=(
            "Maximum evidence-review replan cycles allowed in one run. "
            "This budget is independent of the tool-call budget."
        ),
    )
    parser.add_argument(
        "--recursion-limit",
        type=positive_integer,
        default=100,
        help="Maximum LangGraph execution steps.",
    )


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
    _add_runtime_arguments(run_parser)

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume an existing persisted research thread.",
    )
    _add_runtime_arguments(resume_parser)

    threads_parser = subparsers.add_parser(
        "threads",
        help="List persisted research thread identifiers.",
        description="List persisted research thread identifiers.",
    )
    _add_checkpoint_argument(threads_parser)

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
    checkpoint_path: str | Path,
    thread_id: str,
    allow_write: bool,
    limits: RuntimeLimits,
) -> AgentState:
    """Open the persistent runtime and execute one research goal."""

    settings = Settings()

    async with open_default_agent_runtime(
        settings,
        workspace_root,
        checkpoint_path,
        allow_write=allow_write,
        limits=limits,
    ) as runtime:
        return await runtime.run(
            goal,
            thread_id=thread_id,
        )


async def resume_research_agent(
    *,
    workspace_root: str | Path,
    checkpoint_path: str | Path,
    thread_id: str,
    allow_write: bool,
    limits: RuntimeLimits,
) -> AgentState:
    """Open the persistent runtime and resume one existing thread."""

    settings = Settings()

    async with open_default_agent_runtime(
        settings,
        workspace_root,
        checkpoint_path,
        allow_write=allow_write,
        limits=limits,
    ) as runtime:
        return await runtime.resume(
            thread_id=thread_id,
        )


async def list_research_threads(
    checkpoint_path: str | Path,
) -> tuple[str, ...]:
    """Open the SQLite checkpointer and list its persisted threads."""

    async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
        return await list_thread_ids(checkpointer)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Mini DeerFlow command-line interface."""

    _ensure_utf8_output()

    parser = build_parser()
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "plan":
            plan = run_research_planner(arguments.goal)
            print(plan.model_dump_json(indent=2))
            return 0

        if arguments.command == "threads":
            thread_ids = asyncio.run(
                list_research_threads(arguments.checkpoint_db),
            )
            print(
                json.dumps(
                    {
                        "threads": list(thread_ids),
                        "count": len(thread_ids),
                    },
                    indent=2,
                )
            )
            return 0

        limits = RuntimeLimits(
            max_tool_calls_per_step=(arguments.max_tool_calls_per_step),
            max_total_tool_calls=arguments.max_total_tool_calls,
            max_replan_cycles=arguments.max_replan_cycles,
            recursion_limit=arguments.recursion_limit,
        )

        if arguments.command == "run":
            state = asyncio.run(
                run_research_agent(
                    arguments.goal,
                    workspace_root=arguments.workspace,
                    checkpoint_path=arguments.checkpoint_db,
                    thread_id=arguments.thread_id,
                    allow_write=arguments.allow_write,
                    limits=limits,
                )
            )
        else:
            state = asyncio.run(
                resume_research_agent(
                    workspace_root=arguments.workspace,
                    checkpoint_path=arguments.checkpoint_db,
                    thread_id=arguments.thread_id,
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
