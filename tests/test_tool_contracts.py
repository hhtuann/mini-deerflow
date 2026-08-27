import pytest
from pydantic import ValidationError

from mini_deerflow.tools import Tool, ToolInput, ToolResult


class SearchInput(ToolInput):
    query: str


class EchoTool:
    name = "echo"
    description = "Return the provided text."
    input_model = SearchInput
    timeout_seconds = 5.0
    idempotent = True

    async def run(
        self,
        tool_input: ToolInput,
    ) -> ToolResult:
        return ToolResult.ok(
            {"query": tool_input.model_dump()},
        )


def test_tool_input_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SearchInput(
            query="LangGraph",
            unexpected=True,
        )


def test_tool_input_is_frozen() -> None:
    tool_input = SearchInput(query="LangGraph")

    with pytest.raises(ValidationError):
        tool_input.query = "CrewAI"


def test_tool_result_factories_create_valid_results() -> None:
    success = ToolResult.ok(
        {"items": ["result"]},
        metadata={"count": 1},
    )
    failure = ToolResult.fail(
        "  Search timed out  ",
        metadata={"error_type": "TimeoutError"},
    )

    assert success.success is True
    assert success.data == {"items": ["result"]}
    assert success.error is None
    assert success.metadata == {"count": 1}

    assert failure.success is False
    assert failure.data is None
    assert failure.error == "Search timed out"
    assert failure.metadata == {
        "error_type": "TimeoutError",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {
            "success": True,
            "data": None,
            "error": None,
        },
        {
            "success": True,
            "data": {},
            "error": "unexpected error",
        },
        {
            "success": False,
            "data": {},
            "error": "failed",
        },
        {
            "success": False,
            "data": None,
            "error": None,
        },
        {
            "success": False,
            "data": None,
            "error": "   ",
        },
    ],
)
def test_tool_result_rejects_inconsistent_states(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ToolResult(**payload)


def test_tool_result_rejects_non_json_data() -> None:
    with pytest.raises(ValidationError):
        ToolResult.ok(object())


def test_successful_empty_collection_is_valid() -> None:
    result = ToolResult.ok(
        {
            "items": [],
        },
        metadata={"count": 0},
    )

    assert result.success is True
    assert result.data == {"items": []}
    assert result.metadata["count"] == 0


def test_tool_results_do_not_share_metadata() -> None:
    first = ToolResult.ok("first")
    second = ToolResult.ok("second")

    first.metadata["source"] = "test"

    assert second.metadata == {}


def test_structural_tool_protocol_accepts_matching_object() -> None:
    tool = EchoTool()

    assert isinstance(tool, Tool)
