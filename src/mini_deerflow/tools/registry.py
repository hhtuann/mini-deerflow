import math
import re
from collections.abc import Iterable

from mini_deerflow.tools.contracts import Tool, ToolInput

TOOL_NAME_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]{0,63}$",
)


class ToolRegistryError(ValueError):
    """Base exception raised by the tool registry."""


class InvalidToolError(ToolRegistryError):
    """Raised when a tool violates the registry contract."""


class DuplicateToolError(ToolRegistryError):
    """Raised when a tool name is already registered."""


class UnknownToolError(ToolRegistryError):
    """Raised when a requested tool is not registered."""


class ToolRegistry:
    """Explicit allowlist of tools available to an agent."""

    def __init__(
        self,
        tools: Iterable[Tool] = (),
    ) -> None:
        self._tools: dict[str, Tool] = {}

        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Validate and register one tool."""

        self._validate_tool(tool)

        if tool.name in self._tools:
            raise DuplicateToolError(
                f"tool is already registered: {tool.name}",
            )

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """Return an allowed tool by exact name."""

        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownToolError(
                f"unknown tool: {name}",
            ) from exc

    def names(self) -> tuple[str, ...]:
        """Return registered names in deterministic order."""

        return tuple(self._tools)

    def definitions(self) -> list[dict[str, object]]:
        """Return serializable definitions exposed to an LLM."""

        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": (tool.input_model.model_json_schema()),
                "timeout_seconds": tool.timeout_seconds,
                "idempotent": tool.idempotent,
            }
            for tool in self._tools.values()
        ]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @staticmethod
    def _validate_tool(tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise InvalidToolError(
                "tool does not implement the Tool protocol",
            )

        if not TOOL_NAME_PATTERN.fullmatch(tool.name):
            raise InvalidToolError(
                f"invalid tool name: {tool.name!r}",
            )

        if not tool.description.strip():
            raise InvalidToolError(
                "tool description must not be empty",
            )

        input_model = tool.input_model

        if not isinstance(input_model, type) or not issubclass(input_model, ToolInput):
            raise InvalidToolError(
                "tool input_model must inherit ToolInput",
            )

        timeout = tool.timeout_seconds

        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise InvalidToolError(
                "tool timeout_seconds must be a positive finite number",
            )

        if not isinstance(tool.idempotent, bool):
            raise InvalidToolError(
                "tool idempotent must be a boolean",
            )
