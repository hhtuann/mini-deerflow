import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from mini_deerflow.state import AgentState, create_initial_state


def test_create_initial_state_returns_complete_state() -> None:
    state = create_initial_state("  Research LangGraph  ")

    assert state == {
        "goal": "Research LangGraph",
        "messages": [],
        "plan": None,
        "current_step": 0,
        "pending_action": None,
        "tool_observations": [],
        "tool_calls_in_current_step": 0,
        "total_tool_calls": 0,
        "pending_review_verdict": None,
        "review_verdicts": [],
        "replans": [],
        "delegations": [],
        "notes": [],
        "findings": [],
        "evidence": [],
        "sources": [],
        "final_answer": None,
        "artifact_path": None,
        "errors": [],
    }


def test_create_initial_state_rejects_empty_goal() -> None:
    with pytest.raises(ValueError, match="goal must not be empty"):
        create_initial_state("   ")


def test_initial_states_do_not_share_mutable_lists() -> None:
    first = create_initial_state("First goal")
    second = create_initial_state("Second goal")

    first["notes"].append("first note")
    first["sources"].append("https://example.com")
    first["errors"].append("first error")
    first["messages"].append(HumanMessage(content="hello"))

    assert second["notes"] == []
    assert second["sources"] == []
    assert second["errors"] == []
    assert second["messages"] == []

    assert first["tool_observations"] is not second["tool_observations"]
    assert second["tool_observations"] == []


def test_graph_accumulates_notes_and_replaces_current_step() -> None:
    def first_node(_: AgentState) -> dict[str, object]:
        return {
            "notes": ["observation 1"],
            "current_step": 1,
        }

    def second_node(_: AgentState) -> dict[str, object]:
        return {
            "notes": ["observation 2"],
            "current_step": 2,
        }

    builder = StateGraph(AgentState)
    builder.add_node("first", first_node)
    builder.add_node("second", second_node)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)

    graph = builder.compile()
    result = graph.invoke(create_initial_state("Research reducers"))

    assert result["notes"] == [
        "observation 1",
        "observation 2",
    ]
    assert result["current_step"] == 2


def test_graph_accumulates_messages_with_message_reducer() -> None:
    def add_human_message(_: AgentState) -> dict[str, object]:
        return {
            "messages": [
                HumanMessage(
                    id="human-1",
                    content="Research LangGraph",
                )
            ]
        }

    def add_ai_message(_: AgentState) -> dict[str, object]:
        return {
            "messages": [
                AIMessage(
                    id="ai-1",
                    content="I will create a plan.",
                )
            ]
        }

    builder = StateGraph(AgentState)
    builder.add_node("human", add_human_message)
    builder.add_node("ai", add_ai_message)
    builder.add_edge(START, "human")
    builder.add_edge("human", "ai")
    builder.add_edge("ai", END)

    graph = builder.compile()
    result = graph.invoke(create_initial_state("Research messages"))

    assert [message.content for message in result["messages"]] == [
        "Research LangGraph",
        "I will create a plan.",
    ]
