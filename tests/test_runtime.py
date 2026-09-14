import asyncio
import sqlite3

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from mini_deerflow.actions import CompleteStepAction
from mini_deerflow.decision import ActionContext
from mini_deerflow.persistence import (
    CheckpointStorageError,
    CheckpointUnavailableError,
    InvalidThreadIdError,
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
)
from mini_deerflow.runtime import (
    AgentRuntime,
    RuntimeLimits,
    build_agent_runtime,
)
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import AgentState, create_initial_state
from mini_deerflow.tools import ToolRegistry

_USE_INPUT_STATE = object()


class FakeGraph:
    def __init__(
        self,
        response: object = _USE_INPUT_STATE,
    ) -> None:
        self.call_count = 0
        self.response = response
        self.received_state: AgentState | None = None
        self.received_config: dict[str, object] | None = None

    async def ainvoke(
        self,
        state: AgentState | None,
        *,
        config: dict[str, object],
    ) -> object:
        self.call_count += 1
        self.received_state = state
        self.received_config = config

        if self.response is _USE_INPUT_STATE:
            return state

        return self.response


class FakeCheckpointReader:
    def __init__(
        self,
        checkpoint: object | None,
    ) -> None:
        self.checkpoint = checkpoint
        self.call_count = 0
        self.received_config: RunnableConfig | None = None

    async def aget_tuple(
        self,
        config: RunnableConfig,
    ) -> object | None:
        self.call_count += 1
        self.received_config = config
        return self.checkpoint


class FailingCheckpointReader:
    def __init__(self, error: sqlite3.Error) -> None:
        self.error = error
        self.call_count = 0

    async def aget_tuple(
        self,
        config: RunnableConfig,
    ) -> object | None:
        del config
        self.call_count += 1
        raise self.error


class CompletingSelector:
    def __init__(self) -> None:
        self.contexts: list[ActionContext] = []

    async def select_action(
        self,
        context: ActionContext,
    ) -> CompleteStepAction:
        self.contexts.append(context)

        return CompleteStepAction(
            type="complete_step",
            summary=(f"Completed runtime step {context.step.step_number}."),
            sources=[],
        )


def create_plan(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=1,
                title="Prepare research",
                objective="Prepare the controlled research process.",
                success_criteria="Research preparation is complete.",
            ),
            PlanStep(
                step_number=2,
                title="Review evidence",
                objective="Review the available controlled evidence.",
                success_criteria="Available evidence is reviewed.",
            ),
            PlanStep(
                step_number=3,
                title="Finish research",
                objective="Finish the controlled research workflow.",
                success_criteria="The research workflow is complete.",
            ),
        ],
    )


def test_runtime_limits_have_safe_defaults() -> None:
    limits = RuntimeLimits()

    assert limits.max_tool_calls_per_step == 5
    assert limits.max_total_tool_calls == 20
    assert limits.recursion_limit == 100
    assert limits.max_delegation_concurrency == 2


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("max_tool_calls_per_step", True),
        ("max_total_tool_calls", 1.5),
        ("recursion_limit", "100"),
        ("max_delegation_concurrency", True),
    ],
)
def test_runtime_limits_reject_invalid_types(
    field_name: str,
    invalid_value: object,
) -> None:
    values = {
        "max_tool_calls_per_step": 5,
        "max_total_tool_calls": 20,
        "recursion_limit": 100,
        "max_delegation_concurrency": 2,
    }
    values[field_name] = invalid_value

    with pytest.raises(TypeError, match="must be an integer"):
        RuntimeLimits(**values)


@pytest.mark.parametrize(
    "field_name",
    [
        "max_tool_calls_per_step",
        "max_total_tool_calls",
        "recursion_limit",
    ],
)
def test_runtime_limits_reject_non_positive_values(
    field_name: str,
) -> None:
    values = {
        "max_tool_calls_per_step": 5,
        "max_total_tool_calls": 20,
        "recursion_limit": 100,
        "max_delegation_concurrency": 2,
    }
    values[field_name] = 0

    with pytest.raises(ValueError, match="greater than zero"):
        RuntimeLimits(**values)


@pytest.mark.parametrize("value", [0, 4])
def test_runtime_limits_reject_invalid_delegation_concurrency(value: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 3"):
        RuntimeLimits(max_delegation_concurrency=value)


def test_agent_runtime_invokes_graph_with_initial_state() -> None:
    graph = FakeGraph()
    runtime = AgentRuntime(
        graph=graph,
        limits=RuntimeLimits(recursion_limit=37),
    )

    result = asyncio.run(
        runtime.run(
            "  Research runtime composition  ",
            thread_id="runtime-test",
        )
    )

    assert result["goal"] == "Research runtime composition"
    assert graph.received_state is not None
    assert graph.received_state["plan"] is None
    assert graph.received_config == {
        "configurable": {
            "thread_id": "runtime-test",
        },
        "recursion_limit": 37,
    }


def test_agent_runtime_checks_new_thread_before_graph_call() -> None:
    graph = FakeGraph()
    checkpointer = FakeCheckpointReader(checkpoint=None)
    runtime = AgentRuntime(
        graph=graph,
        limits=RuntimeLimits(recursion_limit=37),
        checkpointer=checkpointer,
    )

    result = asyncio.run(
        runtime.run(
            "  Research runtime composition  ",
            thread_id="runtime-test",
        )
    )

    assert result["goal"] == "Research runtime composition"
    assert checkpointer.call_count == 1
    assert checkpointer.received_config == {
        "configurable": {
            "thread_id": "runtime-test",
        },
        "recursion_limit": 37,
    }
    assert graph.call_count == 1
    assert graph.received_config == checkpointer.received_config


def test_agent_runtime_rejects_existing_thread_before_graph_call() -> None:
    graph = FakeGraph()
    checkpointer = FakeCheckpointReader(checkpoint=object())
    runtime = AgentRuntime(
        graph=graph,
        checkpointer=checkpointer,
    )

    with pytest.raises(
        ThreadAlreadyExistsError,
        match=r"thread 'existing-thread' already has a checkpoint",
    ):
        asyncio.run(
            runtime.run(
                "Research an existing persistent thread",
                thread_id="existing-thread",
            )
        )

    assert checkpointer.call_count == 1
    assert graph.call_count == 0


def test_agent_runtime_run_normalizes_checkpoint_lookup_error() -> None:
    graph = FakeGraph()
    original_error = sqlite3.OperationalError("deterministic lookup failure")
    checkpointer = FailingCheckpointReader(original_error)
    runtime = AgentRuntime(
        graph=graph,
        checkpointer=checkpointer,
    )

    with pytest.raises(
        CheckpointStorageError,
        match="read checkpoint data",
    ) as raised_error:
        asyncio.run(
            runtime.run(
                "Research checkpoint lookup failures",
                thread_id="lookup-failure",
            )
        )

    assert raised_error.value.__cause__ is original_error
    assert checkpointer.call_count == 1
    assert graph.call_count == 0


def test_agent_runtime_rejects_empty_goal_before_graph_call() -> None:
    graph = FakeGraph()
    checkpointer = FakeCheckpointReader(checkpoint=object())
    runtime = AgentRuntime(
        graph=graph,
        checkpointer=checkpointer,
    )

    with pytest.raises(ValueError, match="goal must not be empty"):
        asyncio.run(
            runtime.run(
                "   ",
                thread_id="runtime-test",
            )
        )

    assert graph.received_state is None
    assert graph.call_count == 0
    assert checkpointer.call_count == 0


def test_agent_runtime_rejects_invalid_graph_result() -> None:
    runtime = AgentRuntime(
        graph=FakeGraph(response=["not", "a", "state"]),
    )

    with pytest.raises(
        TypeError,
        match="state dictionary",
    ):
        asyncio.run(
            runtime.run(
                "Research invalid graph output",
                thread_id="runtime-test",
            )
        )


def test_agent_runtime_resumes_graph_without_new_input() -> None:
    expected_state = create_initial_state(
        "Research checkpoint resume",
    )
    expected_state["current_step"] = 2
    expected_state["notes"].append(
        "Completed two research steps.",
    )

    checkpointer = FakeCheckpointReader(
        checkpoint=object(),
    )

    graph = FakeGraph(response=expected_state)
    runtime = AgentRuntime(
        graph=graph,
        limits=RuntimeLimits(
            recursion_limit=45,
        ),
        checkpointer=checkpointer,
    )

    result = asyncio.run(
        runtime.resume(
            thread_id="resume-test",
        )
    )

    assert result is expected_state
    assert graph.call_count == 1
    assert graph.received_state is None
    assert graph.received_config == {
        "configurable": {
            "thread_id": "resume-test",
        },
        "recursion_limit": 45,
    }
    assert checkpointer.call_count == 1
    assert checkpointer.received_config == {
        "configurable": {
            "thread_id": "resume-test",
        },
        "recursion_limit": 45,
    }


def test_agent_runtime_resume_requires_checkpointer() -> None:
    graph = FakeGraph(
        create_initial_state(
            "Research missing checkpoint configuration",
        )
    )
    runtime = AgentRuntime(graph=graph)

    with pytest.raises(
        CheckpointUnavailableError,
        match="requires a configured checkpointer",
    ):
        asyncio.run(
            runtime.resume(
                thread_id="missing-checkpointer",
            )
        )

    assert graph.call_count == 0


def test_agent_runtime_rejects_unknown_resume_thread() -> None:
    graph = FakeGraph(
        create_initial_state(
            "Research an unknown persisted thread",
        )
    )
    checkpointer = FakeCheckpointReader(
        checkpoint=None,
    )
    runtime = AgentRuntime(
        graph=graph,
        checkpointer=checkpointer,
    )

    with pytest.raises(
        ThreadNotFoundError,
        match="unknown-thread",
    ):
        asyncio.run(
            runtime.resume(
                thread_id="unknown-thread",
            )
        )

    assert checkpointer.call_count == 1
    assert graph.call_count == 0


def test_agent_runtime_resume_normalizes_checkpoint_lookup_error() -> None:
    graph = FakeGraph()
    original_error = sqlite3.OperationalError("deterministic lookup failure")
    checkpointer = FailingCheckpointReader(original_error)
    runtime = AgentRuntime(
        graph=graph,
        checkpointer=checkpointer,
    )

    with pytest.raises(
        CheckpointStorageError,
        match="read checkpoint data",
    ) as raised_error:
        asyncio.run(
            runtime.resume(
                thread_id="lookup-failure",
            )
        )

    assert raised_error.value.__cause__ is original_error
    assert checkpointer.call_count == 1
    assert graph.call_count == 0


def test_agent_runtime_rejects_invalid_checkpoint_reader() -> None:
    graph = FakeGraph(
        create_initial_state(
            "Research invalid checkpoint dependency",
        )
    )

    with pytest.raises(
        TypeError,
        match="async checkpoint lookup",
    ):
        AgentRuntime(
            graph=graph,
            checkpointer=object(),
        )


def test_build_agent_runtime_composes_bounded_workflow() -> None:
    selector = CompletingSelector()
    checkpointer = InMemorySaver()
    limits = RuntimeLimits(
        max_tool_calls_per_step=2,
        max_total_tool_calls=3,
        recursion_limit=30,
    )

    runtime = build_agent_runtime(
        create_plan,
        selector,
        ToolRegistry(),
        checkpointer=checkpointer,
        limits=limits,
    )

    result = asyncio.run(
        runtime.run(
            "Research runtime composition",
            thread_id="runtime-test",
        )
    )

    assert result["current_step"] == 3
    assert result["total_tool_calls"] == 0
    assert len(selector.contexts) == 3
    assert selector.contexts[0].remaining_step_tool_calls == 2
    assert selector.contexts[0].remaining_total_tool_calls == 3
    assert runtime.limits is limits
    assert runtime.checkpointer is checkpointer
    assert result["final_answer"] is not None


def test_agent_runtime_rejects_invalid_thread_id_before_graph_call() -> None:
    graph = FakeGraph()
    runtime = AgentRuntime(graph=graph)

    with pytest.raises(
        InvalidThreadIdError,
        match="thread_id",
    ):
        asyncio.run(
            runtime.run(
                "Research persistent agent execution",
                thread_id="../another-thread",
            )
        )

    assert graph.received_state is None
    assert graph.call_count == 0


def test_agent_runtime_rejects_invalid_resume_thread_before_graph_call() -> None:
    graph = FakeGraph()
    runtime = AgentRuntime(graph=graph)

    with pytest.raises(
        InvalidThreadIdError,
        match="thread_id",
    ):
        asyncio.run(
            runtime.resume(
                thread_id="../another-thread",
            )
        )

    assert graph.call_count == 0
