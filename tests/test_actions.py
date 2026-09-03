import pytest
from pydantic import ValidationError

from mini_deerflow.actions import (
    ActionDecision,
    CompleteStepAction,
    ToolCallAction,
    parse_agent_action,
)


def test_parse_tool_call_action() -> None:
    action = parse_agent_action(
        {
            "type": "tool_call",
            "tool_name": "web_search",
            "arguments": {
                "query": "LangGraph",
                "max_results": 5,
            },
        }
    )

    assert isinstance(action, ToolCallAction)
    assert action.tool_name == "web_search"
    assert action.arguments == {
        "query": "LangGraph",
        "max_results": 5,
    }


def test_tool_name_is_normalized() -> None:
    action = ToolCallAction(
        type="tool_call",
        tool_name="  web_search  ",
    )

    assert action.tool_name == "web_search"


def test_tool_arguments_default_to_independent_dictionaries() -> None:
    first = ToolCallAction(
        type="tool_call",
        tool_name="list_files",
    )
    second = ToolCallAction(
        type="tool_call",
        tool_name="list_files",
    )

    assert first.arguments == {}
    assert second.arguments == {}
    assert first.arguments is not second.arguments


def test_parse_complete_step_action() -> None:
    action = parse_agent_action(
        {
            "type": "complete_step",
            "summary": "  Collected three official sources.  ",
            "sources": [
                "https://example.com",
            ],
        }
    )

    assert isinstance(action, CompleteStepAction)
    assert action.summary == "Collected three official sources."
    assert action.model_dump(mode="json") == {
        "type": "complete_step",
        "summary": "Collected three official sources.",
        "sources": [
            "https://example.com/",
        ],
    }


def test_complete_step_sources_default_to_independent_lists() -> None:
    first = CompleteStepAction(
        type="complete_step",
        summary="Completed the first research step.",
    )
    second = CompleteStepAction(
        type="complete_step",
        summary="Completed the second research step.",
    )

    assert first.sources == []
    assert second.sources == []
    assert first.sources is not second.sources


def test_action_decision_serializes_without_wrapper_field() -> None:
    decision = ActionDecision.model_validate(
        {
            "type": "tool_call",
            "tool_name": "read_file",
            "arguments": {
                "path": "notes/result.md",
            },
        }
    )

    assert decision.model_dump(mode="json") == {
        "type": "tool_call",
        "tool_name": "read_file",
        "arguments": {
            "path": "notes/result.md",
        },
    }


def test_action_schema_uses_discriminator() -> None:
    schema = ActionDecision.model_json_schema()

    assert "oneOf" in schema
    assert schema["discriminator"]["propertyName"] == "action_type"


def test_unknown_action_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(
            {
                "type": "unknown",
            }
        )


def test_tool_call_rejects_complete_step_fields() -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(
            {
                "type": "tool_call",
                "tool_name": "web_search",
                "arguments": {},
                "summary": "This field belongs to complete_step.",
            }
        )


def test_complete_step_rejects_tool_call_fields() -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(
            {
                "type": "complete_step",
                "summary": "Completed the current research step.",
                "tool_name": "web_search",
                "arguments": {},
            }
        )


def test_tool_call_requires_tool_name() -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(
            {
                "type": "tool_call",
                "arguments": {},
            }
        )


@pytest.mark.parametrize(
    "invalid_tool_name",
    [
        "",
        "WebSearch",
        "web-search",
        "../secret",
        "_hidden",
        "a" * 65,
    ],
)
def test_invalid_tool_name_is_rejected(
    invalid_tool_name: str,
) -> None:
    with pytest.raises(ValidationError):
        ToolCallAction(
            type="tool_call",
            tool_name=invalid_tool_name,
        )


def test_non_json_tool_arguments_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolCallAction(
            type="tool_call",
            tool_name="web_search",
            arguments={
                "invalid": object(),
            },
        )


def test_tool_arguments_must_be_dictionary() -> None:
    with pytest.raises(ValidationError):
        ToolCallAction(
            type="tool_call",
            tool_name="web_search",
            arguments=[
                "not",
                "a",
                "dictionary",
            ],
        )


@pytest.mark.parametrize(
    "invalid_summary",
    [
        "",
        "   ",
        "too short",
        "a" * 4_001,
    ],
)
def test_invalid_completion_summary_is_rejected(
    invalid_summary: str,
) -> None:
    with pytest.raises(ValidationError):
        CompleteStepAction(
            type="complete_step",
            summary=invalid_summary,
        )


def test_complete_step_rejects_more_than_twenty_sources() -> None:
    with pytest.raises(ValidationError):
        CompleteStepAction(
            type="complete_step",
            summary="Completed the current research step.",
            sources=[f"https://example.com/{index}" for index in range(21)],
        )


@pytest.mark.parametrize(
    "invalid_source",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "not-a-url",
        "evidence.txt",
        "./evidence.txt",
        "read_file:evidence.txt",
        (
            "workspace file 'evidence.txt' "
            "(read via read_file; no external URLs involved)"
        ),
    ],
)
def test_complete_step_rejects_invalid_source_url(
    invalid_source: str,
) -> None:
    with pytest.raises(ValidationError):
        CompleteStepAction(
            type="complete_step",
            summary="Completed the current research step.",
            sources=[invalid_source],
        )


@pytest.mark.parametrize(
    "valid_source",
    [
        "https://example.com/evidence/report",
        "http://example.org/evidence/report",
    ],
)
def test_complete_step_accepts_http_and_https_source_urls(
    valid_source: str,
) -> None:
    action = CompleteStepAction(
        type="complete_step",
        summary="Completed the current research step.",
        sources=[valid_source],
    )

    assert [str(source) for source in action.sources] == [valid_source]


def test_complete_step_accepts_empty_sources_for_local_only_evidence() -> None:
    action = CompleteStepAction(
        type="complete_step",
        summary=(
            "Verified the requested facts using local workspace "
            "read_file observations only."
        ),
    )

    assert action.sources == []


def test_action_models_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ToolCallAction(
            type="tool_call",
            tool_name="web_search",
            unexpected=True,
        )


def test_action_models_are_frozen() -> None:
    action = ToolCallAction(
        type="tool_call",
        tool_name="web_search",
    )

    with pytest.raises(ValidationError):
        action.tool_name = "web_fetch"


def test_transport_alias_is_normalized_to_domain_field() -> None:
    action = parse_agent_action(
        {
            "action_type": "tool_call",
            "tool_name": "web_search",
            "arguments": {
                "query": "LangGraph",
            },
        }
    )

    assert isinstance(action, ToolCallAction)
    assert action.type == "tool_call"

    assert action.model_dump(mode="json") == {
        "type": "tool_call",
        "tool_name": "web_search",
        "arguments": {
            "query": "LangGraph",
        },
    }

    assert action.model_dump(
        mode="json",
        by_alias=True,
    ) == {
        "action_type": "tool_call",
        "tool_name": "web_search",
        "arguments": {
            "query": "LangGraph",
        },
    }
