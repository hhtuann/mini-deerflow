import asyncio
from collections import deque

import pytest
from langchain_core.exceptions import OutputParserException
from langgraph.errors import NodeCancelledError

from mini_deerflow.actions import (
    AgentAction,
    CompleteStepAction,
    ToolCallAction,
)
from mini_deerflow.agent_workflow import build_agent_workflow
from mini_deerflow.decision import ActionContext
from mini_deerflow.llm_selector import LLMActionSelector
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import (
    AgentState,
    create_initial_state,
)
from mini_deerflow.tools import (
    ToolInput,
    ToolRegistry,
    ToolResult,
)


class EchoInput(ToolInput):
    text: str


class EchoTool:
    name = "echo"
    description = "Return the supplied text."
    input_model = EchoInput
    timeout_seconds = 1.0
    idempotent = True

    def __init__(self) -> None:
        self.call_count = 0

    async def run(self, tool_input: ToolInput) -> ToolResult:
        self.call_count += 1
        return ToolResult.ok(
            data=tool_input.model_dump(),
        )


class FailureTool:
    name = "failure"
    description = "Return a structured tool failure."
    input_model = EchoInput
    timeout_seconds = 1.0
    idempotent = True

    async def run(self, tool_input: ToolInput) -> ToolResult:
        return ToolResult.fail(
            error="Simulated tool failure.",
            metadata={
                "error_type": "SimulatedError",
            },
        )


class CancellingTool:
    name = "cancelling"
    description = "Simulate cancellation."
    input_model = EchoInput
    timeout_seconds = 1.0
    idempotent = True

    async def run(self, tool_input: ToolInput) -> ToolResult:
        raise asyncio.CancelledError


class WaitingTool:
    name = "waiting"
    description = "Wait until the surrounding run is cancelled."
    input_model = EchoInput
    timeout_seconds = 60.0
    idempotent = True

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, tool_input: ToolInput) -> ToolResult:
        self.started.set()

        wait_forever = asyncio.Event()
        await wait_forever.wait()

        raise AssertionError("Waiting tool should be cancelled")


class QueuedActionSelector:
    def __init__(
        self,
        actions: list[AgentAction],
    ) -> None:
        self._actions = deque(actions)
        self.contexts: list[ActionContext] = []

    async def select_action(
        self,
        context: ActionContext,
    ) -> AgentAction:
        self.contexts.append(context)

        if not self._actions:
            raise AssertionError("QueuedActionSelector has no action left")

        return self._actions.popleft()


def create_step(number: int) -> PlanStep:
    return PlanStep(
        step_number=number,
        title=f"Research step {number}",
        objective=f"Collect evidence for research step {number}.",
        success_criteria=f"Evidence for step {number} is collected.",
    )


def planner(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            create_step(1),
            create_step(2),
            create_step(3),
        ],
    )


def complete_action(
    number: int,
    *,
    sources: list[str] | None = None,
) -> CompleteStepAction:
    return CompleteStepAction(
        type="complete_step",
        summary=f"Completed research step number {number}.",
        sources=sources or [],
    )


def run_workflow(
    selector: QueuedActionSelector,
    registry: ToolRegistry,
    *,
    max_tool_calls_per_step: int = 5,
    max_total_tool_calls: int = 20,
) -> AgentState:
    graph = build_agent_workflow(
        planner,
        selector,
        registry,
        max_tool_calls_per_step=max_tool_calls_per_step,
        max_total_tool_calls=max_total_tool_calls,
    )

    return asyncio.run(
        graph.ainvoke(
            create_initial_state("Compare LangGraph and CrewAI"),
            config={
                "recursion_limit": 100,
            },
        )
    )


def test_workflow_executes_tools_across_plan_steps() -> None:
    echo_tool = EchoTool()
    selector = QueuedActionSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="echo",
                arguments={"text": "step 1 evidence A"},
            ),
            ToolCallAction(
                type="tool_call",
                tool_name="echo",
                arguments={"text": "step 1 evidence B"},
            ),
            complete_action(
                1,
                sources=["https://example.com/source"],
            ),
            ToolCallAction(
                type="tool_call",
                tool_name="echo",
                arguments={"text": "step 2 evidence"},
            ),
            complete_action(
                2,
                sources=["https://example.com/source"],
            ),
            complete_action(3),
        ]
    )

    result = run_workflow(
        selector,
        ToolRegistry([echo_tool]),
    )

    assert result["current_step"] == 3
    assert result["pending_action"] is None
    assert result["tool_calls_in_current_step"] == 0
    assert result["total_tool_calls"] == 3
    assert echo_tool.call_count == 3

    observations = result["tool_observations"]

    assert [observation.step_number for observation in observations] == [1, 1, 2]
    assert [observation.step_tool_call_number for observation in observations] == [
        1,
        2,
        1,
    ]
    assert [observation.total_tool_call_number for observation in observations] == [
        1,
        2,
        3,
    ]
    assert all(observation.result.success for observation in observations)

    assert len(selector.contexts) == 6
    assert [len(context.observations) for context in selector.contexts] == [
        0,
        1,
        2,
        0,
        1,
        0,
    ]

    assert result["notes"] == [
        "Completed research step number 1.",
        "Completed research step number 2.",
        "Completed research step number 3.",
    ]

    assert result["sources"] == [
        "https://example.com/source",
        "https://example.com/source",
    ]

    final_answer = result["final_answer"]

    assert final_answer is not None
    assert "- Tool calls: 3" in final_answer
    assert "- Successful tool calls: 3" in final_answer
    assert "- Failed tool calls: 0" in final_answer

    # Duplicate sources remain in audit state but are deduplicated
    # when rendering the final answer.
    assert final_answer.count("- https://example.com/source") == 1


def test_workflow_can_complete_without_tool_calls() -> None:
    selector = QueuedActionSelector(
        [
            complete_action(1),
            complete_action(2),
            complete_action(3),
        ]
    )

    result = run_workflow(
        selector,
        ToolRegistry(),
    )

    assert result["current_step"] == 3
    assert result["total_tool_calls"] == 0
    assert result["tool_observations"] == []
    assert result["errors"] == []
    assert "- Tool calls: 0" in result["final_answer"]


def test_structured_tool_failure_becomes_observation() -> None:
    selector = QueuedActionSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="failure",
                arguments={"text": "test"},
            ),
            complete_action(1),
            complete_action(2),
            complete_action(3),
        ]
    )

    result = run_workflow(
        selector,
        ToolRegistry([FailureTool()]),
    )

    assert result["current_step"] == 3
    assert result["total_tool_calls"] == 1
    assert len(result["tool_observations"]) == 1

    observation = result["tool_observations"][0]

    assert observation.result.success is False
    assert observation.result.data is None
    assert observation.result.error == "Simulated tool failure."
    assert "- Failed tool calls: 1" in result["final_answer"]


def test_unknown_tool_becomes_observation_and_loop_continues() -> None:
    selector = QueuedActionSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="delete_database",
                arguments={},
            ),
            complete_action(1),
            complete_action(2),
            complete_action(3),
        ]
    )

    result = run_workflow(
        selector,
        ToolRegistry(),
    )

    assert result["current_step"] == 3
    assert result["total_tool_calls"] == 1

    observation = result["tool_observations"][0]

    assert observation.result.success is False
    assert observation.result.error == "Tool is not registered."
    assert observation.result.metadata["error_type"] == "UnknownToolError"


def test_per_step_budget_stops_second_tool_execution() -> None:
    echo_tool = EchoTool()
    selector = QueuedActionSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="echo",
                arguments={"text": "first"},
            ),
            ToolCallAction(
                type="tool_call",
                tool_name="echo",
                arguments={"text": "second"},
            ),
        ]
    )

    result = run_workflow(
        selector,
        ToolRegistry([echo_tool]),
        max_tool_calls_per_step=1,
        max_total_tool_calls=10,
    )

    assert echo_tool.call_count == 1
    assert result["current_step"] == 0
    assert result["total_tool_calls"] == 1
    assert len(result["tool_observations"]) == 1
    assert result["pending_action"] is None
    assert len(result["errors"]) == 1
    assert "per-step limit" in result["errors"][0]
    assert "No plan step was completed" in result["final_answer"]

    assert len(selector.contexts) == 2
    assert selector.contexts[1].remaining_step_tool_calls == 0


def test_total_budget_stops_tool_in_next_step() -> None:
    echo_tool = EchoTool()
    selector = QueuedActionSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="echo",
                arguments={"text": "step one"},
            ),
            complete_action(1),
            ToolCallAction(
                type="tool_call",
                tool_name="echo",
                arguments={"text": "step two"},
            ),
        ]
    )

    result = run_workflow(
        selector,
        ToolRegistry([echo_tool]),
        max_tool_calls_per_step=5,
        max_total_tool_calls=1,
    )

    assert echo_tool.call_count == 1
    assert result["current_step"] == 1
    assert result["total_tool_calls"] == 1
    assert result["notes"] == ["Completed research step number 1."]
    assert len(result["errors"]) == 1
    assert "total-run limit" in result["errors"][0]

    assert selector.contexts[-1].remaining_total_tool_calls == 0


def test_invalid_selector_output_is_rejected() -> None:
    class InvalidSelector:
        async def select_action(
            self,
            context: ActionContext,
        ) -> object:
            return {
                "type": "tool_call",
                "tool_name": "echo",
                "arguments": {},
            }

    graph = build_agent_workflow(
        planner,
        InvalidSelector(),
        ToolRegistry([EchoTool()]),
    )

    with pytest.raises(
        TypeError,
        match="invalid action",
    ):
        asyncio.run(
            graph.ainvoke(
                create_initial_state("Compare LangGraph and CrewAI"),
                config={
                    "recursion_limit": 100,
                },
            )
        )


def test_langgraph_wraps_tool_raised_cancellation() -> None:
    selector = QueuedActionSelector(
        [
            ToolCallAction(
                type="tool_call",
                tool_name="cancelling",
                arguments={"text": "cancel"},
            ),
        ]
    )
    graph = build_agent_workflow(
        planner,
        selector,
        ToolRegistry([CancellingTool()]),
    )

    with pytest.raises(
        NodeCancelledError,
        match="execute_tool",
    ) as exc_info:
        asyncio.run(
            graph.ainvoke(
                create_initial_state("Compare LangGraph and CrewAI"),
                config={
                    "recursion_limit": 100,
                },
            )
        )

    assert isinstance(
        exc_info.value.__cause__,
        asyncio.CancelledError,
    )


@pytest.mark.parametrize(
    (
        "limit_name",
        "invalid_value",
    ),
    [
        ("max_tool_calls_per_step", 0),
        ("max_tool_calls_per_step", True),
        ("max_total_tool_calls", 0),
        ("max_total_tool_calls", True),
    ],
)
def test_workflow_rejects_invalid_limits(
    limit_name: str,
    invalid_value: object,
) -> None:
    selector = QueuedActionSelector(
        [
            complete_action(1),
        ]
    )
    limits = {
        "max_tool_calls_per_step": 5,
        "max_total_tool_calls": 20,
    }
    limits[limit_name] = invalid_value

    with pytest.raises(
        ValueError,
        match=limit_name,
    ):
        build_agent_workflow(
            planner,
            selector,
            ToolRegistry(),
            **limits,
        )


def test_workflow_rejects_invalid_selector() -> None:
    with pytest.raises(
        TypeError,
        match="ActionSelector",
    ):
        build_agent_workflow(
            planner,
            object(),
            ToolRegistry(),
        )


def test_external_graph_cancellation_propagates() -> None:
    async def run_and_cancel() -> None:
        waiting_tool = WaitingTool()
        selector = QueuedActionSelector(
            [
                ToolCallAction(
                    type="tool_call",
                    tool_name="waiting",
                    arguments={"text": "wait"},
                ),
            ]
        )
        graph = build_agent_workflow(
            planner,
            selector,
            ToolRegistry([waiting_tool]),
        )

        graph_task = asyncio.create_task(
            graph.ainvoke(
                create_initial_state("Compare LangGraph and CrewAI"),
                config={
                    "recursion_limit": 100,
                },
            )
        )

        await asyncio.wait_for(
            waiting_tool.started.wait(),
            timeout=1,
        )

        graph_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await graph_task

    asyncio.run(run_and_cancel())


class SequencedStructuredRunnable:
    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = deque(outcomes)
        self.calls: list[object] = []

    async def ainvoke(self, messages: object) -> object:
        self.calls.append(messages)

        if not self._outcomes:
            raise AssertionError(
                "SequencedStructuredRunnable has no outcome left",
            )

        outcome = self._outcomes.popleft()

        if isinstance(outcome, BaseException):
            raise outcome

        return outcome


class StructuredOutputFakeModel:
    def __init__(self, runnable: SequencedStructuredRunnable) -> None:
        self._runnable = runnable

    def with_structured_output(
        self,
        schema: object,
        *,
        method: str,
    ) -> SequencedStructuredRunnable:
        return self._runnable


def test_workflow_completes_after_recoverable_action_format_failure() -> None:
    runnable = SequencedStructuredRunnable(
        [
            OutputParserException("Failed to parse ActionDecision"),
            ToolCallAction(
                type="tool_call",
                tool_name="echo",
                arguments={"text": "workspace evidence"},
            ),
            complete_action(1),
            complete_action(2),
            complete_action(3),
        ],
    )
    selector = LLMActionSelector(StructuredOutputFakeModel(runnable))
    echo_tool = EchoTool()

    graph = build_agent_workflow(
        planner,
        selector,
        ToolRegistry([echo_tool]),
    )

    result = asyncio.run(
        graph.ainvoke(
            create_initial_state("Compare LangGraph and CrewAI"),
            config={
                "recursion_limit": 100,
            },
        )
    )

    # The first decision consumed two model attempts: one format failure
    # followed by one corrective retry that returned the tool call.
    assert len(runnable.calls) == 5

    # Action-format retries never count as tool calls.
    assert echo_tool.call_count == 1
    assert result["total_tool_calls"] == 1
    assert result["tool_calls_in_current_step"] == 0
    assert len(result["notes"]) == 3

    final_answer = result["final_answer"]

    assert isinstance(final_answer, str)
    assert "Tool calls: 1" in final_answer
    assert "Successful tool calls: 1" in final_answer
    assert "Failed tool calls: 0" in final_answer
