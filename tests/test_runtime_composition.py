from functools import partial
from pathlib import Path
from typing import cast

import pytest
from langchain_openai import ChatOpenAI

from mini_deerflow import runtime as runtime_module
from mini_deerflow.actions import ActionDecision
from mini_deerflow.config import Settings
from mini_deerflow.decision import ActionSelector
from mini_deerflow.llm_selector import LLMActionSelector
from mini_deerflow.planner import create_research_plan
from mini_deerflow.runtime import (
    AgentRuntime,
    Planner,
    RuntimeLimits,
    create_default_agent_runtime,
)
from mini_deerflow.tools import ToolRegistry


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
            ),
        ),
        (
            True,
            (
                "list_files",
                "read_file",
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
        limits: RuntimeLimits | None = None,
    ) -> AgentRuntime:
        captured["planner"] = planner
        captured["action_selector"] = action_selector
        captured["registry"] = registry
        captured["limits"] = limits

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
        limits=limits,
        model_factory=fake_model_factory,
    )

    assert actual_runtime is expected_runtime
    assert captured["settings"] is settings
    assert captured["limits"] is limits
    assert workspace_root.is_dir()

    registry = captured["registry"]
    assert isinstance(registry, ToolRegistry)
    assert registry.names() == expected_names

    planner = captured["planner"]
    assert isinstance(planner, partial)
    assert planner.func is create_research_plan
    assert planner.args == (fake_model,)
    assert planner.keywords == {
        "available_tools": registry.definitions(),
    }

    assert isinstance(
        captured["action_selector"],
        LLMActionSelector,
    )
    assert fake_model.structured_output_calls == [
        (
            ActionDecision,
            "json_mode",
        )
    ]


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
