from operator import add
from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph.message import add_messages

from mini_deerflow.actions import AgentAction, ToolObservation
from mini_deerflow.conversation import ConversationContext
from mini_deerflow.delegation import DelegationRecord
from mini_deerflow.evidence import (
    AnswerLanguage,
    EvidenceRecord,
    RunCompletionStatus,
    StepFinding,
    UserFacingAnswer,
    merge_citation_sources,
    merge_evidence_records,
)
from mini_deerflow.review import (
    FinalizationReason,
    ReplanRecord,
    ReviewRoute,
    ReviewVerdict,
)
from mini_deerflow.schemas import Plan


class AgentState(TypedDict):
    """Shared state passed between nodes in the research workflow."""

    goal: str
    turn_id: NotRequired[str | None]
    turn_sequence: NotRequired[int | None]
    conversation_context: NotRequired[ConversationContext | None]
    messages: Annotated[list[AnyMessage], add_messages]
    plan: Plan | None
    current_step: int

    pending_action: AgentAction | None
    tool_observations: Annotated[list[ToolObservation], add]
    tool_calls_in_current_step: int
    total_tool_calls: int

    pending_review_verdict: ReviewRoute | None
    review_verdicts: Annotated[list[ReviewVerdict], add]
    replans: Annotated[list[ReplanRecord], add]
    delegations: Annotated[list[DelegationRecord], add]

    notes: Annotated[list[str], add]
    findings: Annotated[list[StepFinding], add]
    evidence: Annotated[list[EvidenceRecord], merge_evidence_records]
    sources: Annotated[list[str], merge_citation_sources]
    final_answer: str | None
    public_answer: NotRequired[UserFacingAnswer | None]
    output_language: NotRequired[AnswerLanguage]
    completion_status: NotRequired[RunCompletionStatus]
    finalization_reason: NotRequired[FinalizationReason | None]
    research_report: NotRequired[str | None]
    artifact_path: str | None
    errors: Annotated[list[str], add]


def create_initial_state(
    goal: str,
    *,
    turn_id: str | None = None,
    turn_sequence: int | None = None,
    conversation_context: ConversationContext | None = None,
) -> AgentState:
    """Create a complete and independent state for a new research run."""

    normalized_goal = goal.strip()

    if not normalized_goal:
        raise ValueError("goal must not be empty")

    messages: list[AnyMessage] = []
    if conversation_context is not None:
        for message in conversation_context.messages:
            message_type = HumanMessage if message.role == "user" else AIMessage
            messages.append(
                message_type(content=message.content, id=message.message_id)
            )
        messages.append(
            HumanMessage(
                content=normalized_goal,
                id=f"{turn_id or 'turn'}-user",
            )
        )

    state = AgentState(
        goal=normalized_goal,
        messages=messages,
        plan=None,
        current_step=0,
        pending_action=None,
        tool_observations=[],
        tool_calls_in_current_step=0,
        total_tool_calls=0,
        pending_review_verdict=None,
        review_verdicts=[],
        replans=[],
        delegations=[],
        notes=[],
        findings=[],
        evidence=[],
        sources=[],
        final_answer=None,
        public_answer=None,
        completion_status="running",
        finalization_reason=None,
        research_report=None,
        artifact_path=None,
        errors=[],
    )
    if turn_id is not None:
        state["turn_id"] = turn_id
    if turn_sequence is not None:
        state["turn_sequence"] = turn_sequence
    if conversation_context is not None:
        state["conversation_context"] = conversation_context
    return state
