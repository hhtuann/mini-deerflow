from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from langchain_openai import ChatOpenAI

from mini_deerflow.agent_workflow import build_agent_workflow
from mini_deerflow.config import Settings
from mini_deerflow.decision import ActionSelector
from mini_deerflow.llm_selector import LLMActionSelector
from mini_deerflow.model import create_chat_model
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
        state: AgentState,
        *,
        config: dict[str, object],
    ) -> object:
        """Execute the graph from an initial state."""


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Execute a compiled research graph with bounded runtime settings."""

    graph: AgentGraph
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)

    def __post_init__(self) -> None:
        if not isinstance(self.graph, AgentGraph):
            raise TypeError("graph must support async invocation")

        if not isinstance(self.limits, RuntimeLimits):
            raise TypeError("limits must be RuntimeLimits")

    async def run(self, goal: str) -> AgentState:
        """Run one research goal and return its final state."""

        initial_state = create_initial_state(goal)

        result = await self.graph.ainvoke(
            initial_state,
            config={
                "recursion_limit": self.limits.recursion_limit,
            },
        )

        if not isinstance(result, dict):
            raise TypeError("graph must return a state dictionary")

        return cast(AgentState, result)


def build_agent_runtime(
    planner: Planner,
    action_selector: ActionSelector,
    registry: ToolRegistry,
    *,
    limits: RuntimeLimits | None = None,
) -> AgentRuntime:
    """Build a testable runtime from explicitly supplied dependencies."""

    resolved_limits = limits or RuntimeLimits()

    graph = build_agent_workflow(
        planner,
        action_selector,
        registry,
        max_tool_calls_per_step=(resolved_limits.max_tool_calls_per_step),
        max_total_tool_calls=resolved_limits.max_total_tool_calls,
    )

    return AgentRuntime(
        graph=graph,
        limits=resolved_limits,
    )


def create_default_agent_runtime(
    settings: Settings,
    workspace_root: str | Path,
    *,
    allow_write: bool = False,
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
        limits=limits,
    )
