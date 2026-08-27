import asyncio
import logging

from pydantic import JsonValue, ValidationError

from mini_deerflow.tools.contracts import ToolResult
from mini_deerflow.tools.registry import ToolRegistry, UnknownToolError

logger = logging.getLogger(__name__)


class ToolRunner:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def run(
        self,
        tool_name: str,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        try:
            tool = self._registry.get(tool_name)
        except UnknownToolError:
            return ToolResult.fail(
                error="Tool is not registered.",
                metadata={
                    "tool_name": tool_name,
                    "error_type": "UnknownToolError",
                },
            )

        try:
            validated_input = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult.fail(
                error="Tool input is invalid.",
                metadata={
                    "tool_name": tool.name,
                    "error_type": type(exc).__name__,
                    "validation_error_count": exc.error_count(),
                },
            )
        except Exception as exc:
            logger.exception(
                "Unexpected input validation error for tool %s",
                tool.name,
            )
            return ToolResult.fail(
                error="Tool input validation failed.",
                metadata={
                    "tool_name": tool.name,
                    "error_type": type(exc).__name__,
                },
            )

        try:
            result = await asyncio.wait_for(
                tool.run(validated_input),
                timeout=tool.timeout_seconds,
            )
        except TimeoutError:
            return ToolResult.fail(
                error="Tool execution timed out.",
                metadata={
                    "tool_name": tool.name,
                    "error_type": "TimeoutError",
                    "timeout_seconds": tool.timeout_seconds,
                },
            )
        except Exception as exc:
            logger.exception(
                "Tool %s raised an exception",
                tool.name,
            )
            return ToolResult.fail(
                error="Tool execution failed.",
                metadata={
                    "tool_name": tool.name,
                    "error_type": type(exc).__name__,
                },
            )

        if not isinstance(result, ToolResult):
            logger.error(
                "Tool %s returned %s instead of ToolResult",
                tool.name,
                type(result).__name__,
            )
            return ToolResult.fail(
                error="Tool returned an invalid result.",
                metadata={
                    "tool_name": tool.name,
                    "error_type": "InvalidToolResultError",
                    "actual_type": type(result).__name__,
                },
            )

        return result
