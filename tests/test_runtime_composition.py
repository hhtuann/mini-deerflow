import asyncio
from functools import partial
from pathlib import Path
from typing import cast

import pytest
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from mini_deerflow import runtime as runtime_module
from mini_deerflow.actions import ActionDecision
from mini_deerflow.config import Settings
from mini_deerflow.context_budget import ContextBudget
from mini_deerflow.decision import ActionSelector
from mini_deerflow.llm_reviewer import LLMReviewer
from mini_deerflow.llm_selector import LLMActionSelector
from mini_deerflow.persistence import create_thread_config
from mini_deerflow.planner import create_research_plan
from mini_deerflow.replanner import create_replacement_plan
from mini_deerflow.review import (
    EvidenceReviewer,
    Replanner,
    ReviewDecision,
)
from mini_deerflow.runtime import (
    AgentRuntime,
    Planner,
    RuntimeLimits,
    create_default_agent_runtime,
    open_default_agent_runtime,
)
from mini_deerflow.tools import ToolRegistry
from mini_deerflow.tracing import ExecutionTracer


class FakeStructuredRunnable:
    async def ainvoke(
        self,
        messages: object,
    ) -> object:
        return messages


class FakeModel:
    def __init__(self) -> None:
        self.structured_output_calls: list[tuple[object, str]] = []

    def with_structured_output(
        self,
        schema: type[ActionDecision],
        *,
        method: str,
    ) -> FakeStructuredRunnable:
        self.structured_output_calls.append(
            (schema, method),
        )

        return FakeStructuredRunnable()


class FakeGraph:
    async def ainvoke(
        self,
        state: object,
        *,
        config: dict[str, object],
    ) -> object:
        return state


@pytest.mark.parametrize(
    ("allow_write", "expected_names"),
    [
        (
            False,
            (
                "list_files",
                "read_file",
                "web_search",
                "web_fetch",
                "delegate_research",
            ),
        ),
        (
            True,
            (
                "list_files",
                "read_file",
                "web_search",
                "web_fetch",
                "delegate_research",
                "write_file",
            ),
        ),
    ],
)
def test_default_runtime_composes_expected_file_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allow_write: bool,
    expected_names: tuple[str, ...],
) -> None:
    settings = Settings(
        api_key="test-api-key",
        _env_file=None,
    )
    fake_model = FakeModel()
    expected_runtime = AgentRuntime(
        graph=FakeGraph(),
    )
    limits = RuntimeLimits(
        max_tool_calls_per_step=2,
        max_total_tool_calls=4,
        recursion_limit=30,
    )
    expected_checkpointer = InMemorySaver()
    captured: dict[str, object] = {}

    def fake_model_factory(
        received_settings: Settings,
    ) -> ChatOpenAI:
        captured["settings"] = received_settings
        return cast(ChatOpenAI, fake_model)

    def fake_build_agent_runtime(
        planner: Planner,
        action_selector: ActionSelector,
        registry: ToolRegistry,
        *,
        action_registry: ToolRegistry | None = None,
        reviewer: EvidenceReviewer | None = None,
        replanner: Replanner | None = None,
        checkpointer: BaseCheckpointSaver[str] | None = None,
        limits: RuntimeLimits | None = None,
        context_budget: ContextBudget | None = None,
        artifact_path: str | None = None,
        tracer: ExecutionTracer | None = None,
    ) -> AgentRuntime:
        captured["planner"] = planner
        captured["action_selector"] = action_selector
        captured["registry"] = registry
        captured["action_registry"] = action_registry
        captured["reviewer"] = reviewer
        captured["replanner"] = replanner
        captured["checkpointer"] = checkpointer
        captured["limits"] = limits
        captured["context_budget"] = context_budget
        captured["artifact_path"] = artifact_path
        captured["tracer"] = tracer

        return expected_runtime

    monkeypatch.setattr(
        runtime_module,
        "build_agent_runtime",
        fake_build_agent_runtime,
    )

    workspace_root = tmp_path / "workspace"

    actual_runtime = create_default_agent_runtime(
        settings,
        workspace_root,
        allow_write=allow_write,
        checkpointer=expected_checkpointer,
        limits=limits,
        model_factory=fake_model_factory,
    )

    assert actual_runtime is expected_runtime
    assert captured["settings"] is settings
    assert captured["limits"] is limits
    assert captured["tracer"] is None
    assert workspace_root.is_dir()

    registry = captured["registry"]
    assert isinstance(registry, ToolRegistry)
    assert registry.names() == expected_names

    action_registry = captured["action_registry"]
    assert isinstance(action_registry, ToolRegistry)
    assert action_registry.names() == (
        "list_files",
        "read_file",
        "web_search",
        "web_fetch",
        "delegate_research",
    )

    planner = captured["planner"]
    assert isinstance(planner, partial)
    assert planner.func is create_research_plan
    assert planner.args == (fake_model,)
    assert planner.keywords == {
        "available_tools": action_registry.definitions(),
    }

    assert isinstance(
        captured["action_selector"],
        LLMActionSelector,
    )
    assert isinstance(
        captured["reviewer"],
        LLMReviewer,
    )

    composed_replanner = captured["replanner"]

    assert isinstance(composed_replanner, partial)
    assert composed_replanner.func is create_replacement_plan
    assert composed_replanner.args == (fake_model,)
    assert composed_replanner.keywords == {}

    # The replanner binds lazily. The researcher selector, parent selector,
    # and reviewer bind eagerly at composition time.
    assert fake_model.structured_output_calls == [
        (
            ActionDecision,
            "json_mode",
        ),
        (
            ActionDecision,
            "json_mode",
        ),
        (
            ReviewDecision,
            "json_mode",
        ),
    ]

    assert captured["checkpointer"] is expected_checkpointer
    assert captured["context_budget"] == ContextBudget()
    assert captured["artifact_path"] == (
        "reports/research-report.md" if allow_write else None
    )


def test_default_runtime_rejects_non_boolean_write_permission(
    tmp_path: Path,
) -> None:
    settings = Settings(
        api_key="test-api-key",
        _env_file=None,
    )
    model_factory_called = False

    def fake_model_factory(
        received_settings: Settings,
    ) -> ChatOpenAI:
        nonlocal model_factory_called
        model_factory_called = True

        raise AssertionError(f"Unexpected settings: {received_settings}")

    with pytest.raises(
        TypeError,
        match="allow_write must be a boolean",
    ):
        create_default_agent_runtime(
            settings,
            tmp_path / "workspace",
            allow_write="yes",
            model_factory=fake_model_factory,
        )

    assert model_factory_called is False


def test_open_default_agent_runtime_owns_sqlite_lifecycle(
    tmp_path: Path,
) -> None:
    settings = Settings(
        api_key="test-api-key",
        _env_file=None,
    )
    fake_model = FakeModel()
    checkpoint_path = tmp_path / "checkpoints.sqlite"

    def fake_model_factory(
        received_settings: Settings,
    ) -> ChatOpenAI:
        assert received_settings is settings

        return cast(
            ChatOpenAI,
            fake_model,
        )

    async def run_scenario() -> None:
        async with open_default_agent_runtime(
            settings,
            tmp_path / "workspace",
            checkpoint_path,
            model_factory=fake_model_factory,
        ) as runtime:
            checkpointer = getattr(
                runtime.graph,
                "checkpointer",
                None,
            )

            assert isinstance(
                checkpointer,
                AsyncSqliteSaver,
            )

            config = create_thread_config(
                "runtime-context-test",
                recursion_limit=30,
            )

            assert await checkpointer.aget_tuple(config) is None

        with pytest.raises(
            ValueError,
            match="no active connection",
        ):
            await checkpointer.conn.execute("SELECT 1")

    asyncio.run(run_scenario())

    assert checkpoint_path.is_file()
