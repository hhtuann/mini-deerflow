import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver

from mini_deerflow.agent_workflow import build_agent_workflow
from mini_deerflow.config import Settings
from mini_deerflow.decision import ActionSelector
from mini_deerflow.llm_selector import LLMActionSelector
from mini_deerflow.model import create_chat_model
from mini_deerflow.persistence import (
    AsyncCheckpointReader,
    CheckpointStorageError,
    CheckpointUnavailableError,
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
    create_thread_config,
    open_sqlite_checkpointer,
)
from mini_deerflow.planner import create_research_plan
from mini_deerflow.schemas import Plan
from mini_deerflow.state import AgentState, create_initial_state
from mini_deerflow.tools import (
    ListFilesTool,
    ReadFileTool,
    ToolRegistry,
    WriteFileTool,
)
from mini_deerflow.workspace import Workspace

Planner = Callable[[str], Plan]
ModelFactory = Callable[[Settings], ChatOpenAI]


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """Bound execution resources for one agent run."""

    max_tool_calls_per_step: int = 5
    max_total_tool_calls: int = 20
    recursion_limit: int = 100

    def __post_init__(self) -> None:
        for name in (
            "max_tool_calls_per_step",
            "max_total_tool_calls",
            "recursion_limit",
        ):
            value = getattr(self, name)

            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")

            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")


@runtime_checkable
class AgentGraph(Protocol):
    """Minimal compiled-graph interface required by AgentRuntime."""

    async def ainvoke(
        self,
        state: AgentState | None,
        *,
        config: dict[str, object],
    ) -> object:
        """Execute or resume the graph."""


async def _read_checkpoint(
    checkpointer: AsyncCheckpointReader,
    config: dict[str, object],
) -> object | None:
    """Read one checkpoint while preserving a narrow storage boundary."""

    try:
        return await checkpointer.aget_tuple(config)
    except sqlite3.Error as error:
        raise CheckpointStorageError(
            "could not read checkpoint data",
        ) from error


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Execute a compiled research graph with bounded runtime settings."""

    graph: AgentGraph
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)
    checkpointer: AsyncCheckpointReader | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.graph, AgentGraph):
            raise TypeError("graph must support async invocation")

        if not isinstance(self.limits, RuntimeLimits):
            raise TypeError("limits must be RuntimeLimits")

        if self.checkpointer is not None and not isinstance(
            self.checkpointer,
            AsyncCheckpointReader,
        ):
            raise TypeError(
                "checkpointer must support async checkpoint lookup",
            )

    async def run(
        self,
        goal: str,
        *,
        thread_id: str,
    ) -> AgentState:
        """Run one research goal and return its final state."""

        initial_state = create_initial_state(goal)

        config = create_thread_config(
            thread_id,
            recursion_limit=self.limits.recursion_limit,
        )

        if self.checkpointer is not None:
            checkpoint = await _read_checkpoint(
                self.checkpointer,
                config,
            )

            if checkpoint is not None:
                raise ThreadAlreadyExistsError(
                    f"thread {thread_id!r} already has a checkpoint",
                )

        result = await self.graph.ainvoke(
            initial_state,
            config=config,
        )

        return _validate_agent_state_result(result)

    async def resume(
        self,
        *,
        thread_id: str,
    ) -> AgentState:
        """Continue an existing persisted thread."""

        config = create_thread_config(
            thread_id,
            recursion_limit=self.limits.recursion_limit,
        )

        if self.checkpointer is None:
            raise CheckpointUnavailableError(
                "resume requires a configured checkpointer",
            )

        checkpoint = await _read_checkpoint(
            self.checkpointer,
            config,
        )

        if checkpoint is None:
            raise ThreadNotFoundError(
                f"thread {thread_id!r} has no checkpoint",
            )

        result = await self.graph.ainvoke(
            None,
            config=config,
        )

        return _validate_agent_state_result(result)


def _validate_agent_state_result(
    result: object,
) -> AgentState:
    if not isinstance(result, dict):
        raise TypeError("graph must return a state dictionary")

    return cast(AgentState, result)


def build_agent_runtime(
    planner: Planner,
    action_selector: ActionSelector,
    registry: ToolRegistry,
    *,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    limits: RuntimeLimits | None = None,
) -> AgentRuntime:
    """Build a testable runtime from explicitly supplied dependencies."""

    resolved_limits = limits or RuntimeLimits()

    graph = build_agent_workflow(
        planner,
        action_selector,
        registry,
        checkpointer=checkpointer,
        max_tool_calls_per_step=(resolved_limits.max_tool_calls_per_step),
        max_total_tool_calls=resolved_limits.max_total_tool_calls,
    )

    return AgentRuntime(
        graph=graph,
        limits=resolved_limits,
        checkpointer=checkpointer,
    )


def create_default_agent_runtime(
    settings: Settings,
    workspace_root: str | Path,
    *,
    allow_write: bool = False,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    limits: RuntimeLimits | None = None,
    model_factory: ModelFactory = create_chat_model,
) -> AgentRuntime:
    """Create the default local Mini DeerFlow runtime."""

    if not isinstance(allow_write, bool):
        raise TypeError("allow_write must be a boolean")

    model = model_factory(settings)
    workspace = Workspace(workspace_root)

    tools = [
        ListFilesTool(workspace),
        ReadFileTool(workspace),
    ]

    if allow_write:
        tools.append(
            WriteFileTool(workspace),
        )

    registry = ToolRegistry(tools)

    planner = partial(
        create_research_plan,
        model,
        available_tools=registry.definitions(),
    )

    action_selector = LLMActionSelector(model)

    return build_agent_runtime(
        planner,
        action_selector,
        registry,
        checkpointer=checkpointer,
        limits=limits,
    )


@asynccontextmanager
async def open_default_agent_runtime(
    settings: Settings,
    workspace_root: str | Path,
    checkpoint_path: str | Path,
    *,
    allow_write: bool = False,
    limits: RuntimeLimits | None = None,
    model_factory: ModelFactory = create_chat_model,
) -> AsyncIterator[AgentRuntime]:
    """Open a persistent runtime and close its checkpointer on exit."""

    async with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
        yield create_default_agent_runtime(
            settings,
            workspace_root,
            allow_write=allow_write,
            checkpointer=checkpointer,
            limits=limits,
            model_factory=model_factory,
        )
