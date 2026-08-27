import math

import pytest
from pydantic import BaseModel

from mini_deerflow.tools.contracts import ToolInput, ToolResult
from mini_deerflow.tools.registry import (
    DuplicateToolError,
    InvalidToolError,
    ToolRegistry,
    UnknownToolError,
)


class EchoInput(ToolInput):
    text: str


class EchoTool:
    name = "echo"
    description = "Return the supplied text."
    input_model = EchoInput
    timeout_seconds = 5.0
    idempotent = True

    async def run(self, tool_input: ToolInput) -> ToolResult:
        return ToolResult.ok(data=tool_input.model_dump())


class SearchTool:
    name = "web_search"
    description = "Search the web."
    input_model = EchoInput
    timeout_seconds = 10.0
    idempotent = True

    async def run(self, tool_input: ToolInput) -> ToolResult:
        return ToolResult.ok(data={"query": tool_input.model_dump()})


def test_register_and_get_tool() -> None:
    registry = ToolRegistry()
    tool = EchoTool()

    registry.register(tool)

    assert registry.get("echo") is tool
    assert registry.names() == ("echo",)
    assert "echo" in registry
    assert len(registry) == 1


def test_constructor_registers_tools_in_order() -> None:
    echo = EchoTool()
    search = SearchTool()

    registry = ToolRegistry([echo, search])

    assert registry.names() == ("echo", "web_search")
    assert registry.get("echo") is echo
    assert registry.get("web_search") is search


def test_definitions_expose_tool_metadata_and_input_schema() -> None:
    registry = ToolRegistry([EchoTool()])

    definitions = registry.definitions()

    assert len(definitions) == 1

    definition = definitions[0]

    assert definition["name"] == "echo"
    assert definition["description"] == "Return the supplied text."
    assert definition["timeout_seconds"] == 5.0
    assert definition["idempotent"] is True

    input_schema = definition["input_schema"]

    assert input_schema["type"] == "object"
    assert "text" in input_schema["properties"]
    assert input_schema["required"] == ["text"]


def test_duplicate_tool_name_is_rejected() -> None:
    registry = ToolRegistry([EchoTool()])

    with pytest.raises(DuplicateToolError):
        registry.register(EchoTool())


def test_unknown_tool_is_rejected() -> None:
    registry = ToolRegistry([EchoTool()])

    with pytest.raises(UnknownToolError):
        registry.get("delete_database")


@pytest.mark.parametrize(
    "invalid_name",
    [
        "",
        "Echo",
        "web-search",
        "../secret",
        "_hidden",
        "two words",
        "a" * 65,
    ],
)
def test_invalid_tool_name_is_rejected(invalid_name: str) -> None:
    tool = EchoTool()
    tool.name = invalid_name

    with pytest.raises(InvalidToolError):
        ToolRegistry([tool])


def test_blank_description_is_rejected() -> None:
    tool = EchoTool()
    tool.description = "   "

    with pytest.raises(InvalidToolError):
        ToolRegistry([tool])


def test_input_model_must_extend_tool_input() -> None:
    tool = EchoTool()
    tool.input_model = BaseModel

    with pytest.raises(InvalidToolError):
        ToolRegistry([tool])


@pytest.mark.parametrize(
    "invalid_timeout",
    [
        0,
        -1,
        math.inf,
        math.nan,
        True,
    ],
)
def test_invalid_timeout_is_rejected(invalid_timeout: object) -> None:
    tool = EchoTool()
    tool.timeout_seconds = invalid_timeout

    with pytest.raises(InvalidToolError):
        ToolRegistry([tool])


@pytest.mark.parametrize("invalid_value", ["yes", 1])
def test_idempotent_must_be_boolean(invalid_value: object) -> None:
    tool = EchoTool()
    tool.idempotent = invalid_value

    with pytest.raises(InvalidToolError):
        ToolRegistry([tool])


def test_object_missing_tool_contract_is_rejected() -> None:
    class IncompleteTool:
        name = "incomplete"

    with pytest.raises(InvalidToolError):
        ToolRegistry([IncompleteTool()])
