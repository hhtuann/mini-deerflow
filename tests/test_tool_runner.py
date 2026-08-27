import asyncio

import pytest
from pydantic import model_validator

from mini_deerflow.tools.contracts import ToolInput, ToolResult
from mini_deerflow.tools.registry import ToolRegistry
from mini_deerflow.tools.runner import ToolRunner


class EchoInput(ToolInput):
    text: str


class EchoTool:
    name = "echo"
    description = "Return the supplied text."
    input_model = EchoInput
    timeout_seconds = 1.0
    idempotent = True

    def __init__(self) -> None:
        self.received_input: EchoInput | None = None

    async def run(self, tool_input: ToolInput) -> ToolResult:
        assert isinstance(tool_input, EchoInput)
        self.received_input = tool_input
        return ToolResult.ok(data={"text": tool_input.text})


class TrackingTool:
    name = "tracking"
    description = "Track whether the tool was executed."
    input_model = EchoInput
    timeout_seconds = 1.0
    idempotent = True

    def __init__(self) -> None:
        self.was_called = False

    async def run(self, tool_input: ToolInput) -> ToolResult:
        self.was_called = True
        return ToolResult.ok(data=tool_input.model_dump())


class SlowTool:
    name = "slow"
    description = "Simulate a slow operation."
    input_model = EchoInput
    timeout_seconds = 0.01
    idempotent = True

    async def run(self, tool_input: ToolInput) -> ToolResult:
        await asyncio.sleep(1)
        return ToolResult.ok(data=tool_input.model_dump())


class CrashingTool:
    name = "crashing"
    description = "Simulate an ordinary execution failure."
    input_model = EchoInput
    timeout_seconds = 1.0
    idempotent = True

    async def run(self, tool_input: ToolInput) -> ToolResult:
        raise ConnectionError("Internal service host is unavailable")


class InvalidResultTool:
    name = "invalid_result"
    description = "Return a value that violates the tool contract."
    input_model = EchoInput
    timeout_seconds = 1.0
    idempotent = True

    async def run(self, tool_input: ToolInput) -> object:
        return {"unexpected": "dictionary"}


class CancellingTool:
    name = "cancelling"
    description = "Simulate task cancellation."
    input_model = EchoInput
    timeout_seconds = 1.0
    idempotent = True

    async def run(self, tool_input: ToolInput) -> ToolResult:
        raise asyncio.CancelledError


class BrokenInput(ToolInput):
    text: str

    @model_validator(mode="before")
    @classmethod
    def fail_validation(cls, value: object) -> object:
        raise RuntimeError("Broken custom validator")


class BrokenValidationTool:
    name = "broken_validation"
    description = "Simulate an unexpected validator failure."
    input_model = BrokenInput
    timeout_seconds = 1.0
    idempotent = True

    async def run(self, tool_input: ToolInput) -> ToolResult:
        return ToolResult.ok(data=tool_input.model_dump())


def test_runner_executes_registered_tool_with_validated_input() -> None:
    tool = EchoTool()
    runner = ToolRunner(ToolRegistry([tool]))

    result = asyncio.run(
        runner.run(
            tool_name="echo",
            arguments={"text": "LangGraph"},
        )
    )

    assert result.success is True
    assert result.data == {"text": "LangGraph"}
    assert result.error is None
    assert tool.received_input == EchoInput(text="LangGraph")


def test_runner_rejects_unknown_tool() -> None:
    runner = ToolRunner(ToolRegistry())

    result = asyncio.run(
        runner.run(
            tool_name="delete_database",
            arguments={},
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Tool is not registered."
    assert result.metadata["error_type"] == "UnknownToolError"


def test_runner_rejects_invalid_input_without_executing_tool() -> None:
    tool = TrackingTool()
    runner = ToolRunner(ToolRegistry([tool]))

    result = asyncio.run(
        runner.run(
            tool_name="tracking",
            arguments={},
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Tool input is invalid."
    assert result.metadata["error_type"] == "ValidationError"
    assert result.metadata["validation_error_count"] == 1
    assert tool.was_called is False


def test_runner_converts_timeout_to_failure() -> None:
    runner = ToolRunner(ToolRegistry([SlowTool()]))

    result = asyncio.run(
        runner.run(
            tool_name="slow",
            arguments={"text": "wait"},
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Tool execution timed out."
    assert result.metadata["error_type"] == "TimeoutError"
    assert result.metadata["timeout_seconds"] == 0.01


def test_runner_converts_execution_exception_to_safe_failure() -> None:
    runner = ToolRunner(ToolRegistry([CrashingTool()]))

    result = asyncio.run(
        runner.run(
            tool_name="crashing",
            arguments={"text": "test"},
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Tool execution failed."
    assert result.metadata["error_type"] == "ConnectionError"

    serialized_result = result.model_dump_json()

    assert "Internal service host" not in serialized_result


def test_runner_rejects_invalid_tool_result() -> None:
    runner = ToolRunner(ToolRegistry([InvalidResultTool()]))

    result = asyncio.run(
        runner.run(
            tool_name="invalid_result",
            arguments={"text": "test"},
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Tool returned an invalid result."
    assert result.metadata["error_type"] == "InvalidToolResultError"
    assert result.metadata["actual_type"] == "dict"


def test_runner_does_not_swallow_cancellation() -> None:
    runner = ToolRunner(ToolRegistry([CancellingTool()]))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runner.run(
                tool_name="cancelling",
                arguments={"text": "cancel"},
            )
        )


def test_runner_converts_unexpected_validation_exception() -> None:
    runner = ToolRunner(ToolRegistry([BrokenValidationTool()]))

    result = asyncio.run(
        runner.run(
            tool_name="broken_validation",
            arguments={"text": "test"},
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Tool input validation failed."
    assert result.metadata["error_type"] == "RuntimeError"
