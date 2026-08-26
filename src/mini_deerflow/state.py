from operator import add
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from mini_deerflow.schemas import Plan


class AgentState(TypedDict):
    """Shared state passed between nodes in the research workflow."""

    goal: str
    messages: Annotated[list[AnyMessage], add_messages]
    plan: Plan | None
    current_step: int
    notes: Annotated[list[str], add]
    sources: Annotated[list[str], add]
    final_answer: str | None
    errors: Annotated[list[str], add]


def create_initial_state(goal: str) -> AgentState:
    """Create a complete and independent state for a new research run."""

    normalized_goal = goal.strip()

    if not normalized_goal:
        raise ValueError("goal must not be empty")

    return AgentState(
        goal=normalized_goal,
        messages=[],
        plan=None,
        current_step=0,
        notes=[],
        sources=[],
        final_answer=None,
        errors=[],
    )
