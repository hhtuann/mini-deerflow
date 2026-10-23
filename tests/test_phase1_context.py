"""Phase 1 projection contracts: pressure order, provenance and wire bounds."""

import asyncio
import json
from functools import partial
from unittest.mock import Mock

import pytest
from pydantic import Field

from mini_deerflow.actions import ToolCallAction, ToolObservation
from mini_deerflow.agent_workflow import build_agent_workflow
from mini_deerflow.context_assembly import (
    SKILLS_SYSTEM_GUIDANCE,
    build_planning_context,
)
from mini_deerflow.context_budget import (
    CONTEXT_POLICY_VERSION,
    MAX_SKILL_INSTRUCTION_CHARS,
    ContextBudget,
    ContextBudgetExceededError,
    ContextBudgetModel,
    ProjectedSkillContext,
    ProjectionMetadata,
    estimate_token_count,
    fit_context_to_budget,
    payload_size,
    render_llm_payload,
)
from mini_deerflow.conversation import ConversationContext, ConversationMessage
from mini_deerflow.decision import ActionSelector, build_action_context
from mini_deerflow.evidence import EvidenceProvenance, EvidenceRecord
from mini_deerflow.planner import PlanningBudget, create_research_plan
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.skills import SkillContext
from mini_deerflow.state import create_initial_state
from mini_deerflow.tools import ToolRegistry
from mini_deerflow.tools.contracts import ToolResult
from mini_deerflow.tracing import ExecutionEventType, ExecutionTracer


class StageContext(ContextBudgetModel):
    goal: str = "Research a traceable answer."
    active_skill: SkillContext | ProjectedSkillContext | None = None
    conversation_context: ConversationContext | None = None
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    observations: list[ToolObservation] = Field(default_factory=list)
    context_projection: ProjectionMetadata | None = None


def budget(chars: int = 6_000) -> ContextBudget:
    return ContextBudget(
        max_total_chars=chars,
        max_item_chars=300,
        max_excerpt_chars=150,
    )


def skill(text: str = "Compare primary sources.") -> SkillContext:
    return SkillContext(id="research", fingerprint="a" * 64, instructions=text)


def history(turns: int = 3, chars: int = 500) -> ConversationContext:
    return ConversationContext(
        messages=tuple(
            ConversationMessage(
                message_id=f"{turn}-{role}",
                turn_id=f"turn-{turn}",
                role=role,
                content=str(turn) * chars,
            )
            for turn in range(turns)
            for role in ("user", "assistant")
        )
    )


def evidence() -> EvidenceRecord:
    return EvidenceRecord(
        url="https://example.com/primary",
        source_tool="web_fetch",
        title="Primary source",
        excerpt="Verified source passage.",
        provenance=EvidenceProvenance(
            tool_name="web_fetch",
            step_number=1,
            step_tool_call_number=1,
            total_tool_call_number=1,
            observation_index=1,
        ),
    )


def test_skill_is_bounded_and_snapshot_untouched() -> None:
    original = skill("Useful instruction. " * 600)
    context = StageContext(active_skill=original)
    fitted = fit_context_to_budget(context, budget())
    assert isinstance(fitted.active_skill, ProjectedSkillContext)
    assert len(fitted.active_skill.instructions) <= MAX_SKILL_INSTRUCTION_CHARS
    assert fitted.active_skill.id == original.id
    assert fitted.active_skill.fingerprint == original.fingerprint
    assert fitted.context_projection.truncated_items == 1
    assert context.active_skill == original
    assert len(original.instructions) > MAX_SKILL_INSTRUCTION_CHARS
    # The projected model is independently valid, including omitted instructions.
    StageContext.model_validate_json(fitted.model_dump_json())


def test_skill_omitted_before_history_or_evidence() -> None:
    context = StageContext(
        active_skill=skill("S" * 2_000),
        conversation_context=history(turns=1, chars=200),
        evidence=[evidence()],
    )
    fitted = fit_context_to_budget(context, budget(2_500))
    assert fitted.active_skill.instructions is None
    assert fitted.active_skill.fingerprint == "a" * 64
    assert fitted.context_projection.skill_id == "research"
    assert fitted.context_projection.skill_omitted is True
    assert fitted.conversation_context == context.conversation_context
    assert fitted.evidence == context.evidence
    StageContext.model_validate_json(fitted.model_dump_json())


def test_conversation_drops_whole_oldest_turns_before_evidence() -> None:
    context = StageContext(conversation_context=history(), evidence=[evidence()])
    fitted = fit_context_to_budget(context, budget(3_000))
    conversation = fitted.conversation_context
    assert 0 < len(conversation.messages) < 6
    assert conversation.messages[-1].turn_id == "turn-2"
    assert {message.turn_id for message in conversation.messages} == {"turn-2"}
    assert [message.role for message in conversation.messages] == ["user", "assistant"]
    assert conversation.omitted_turns == 2
    assert conversation.estimated_characters == 1_000
    assert fitted.evidence == context.evidence
    assert len(context.conversation_context.messages) == 6


def test_duplicate_observation_goes_before_evidence_even_when_newest() -> None:
    record = evidence()
    observation = ToolObservation(
        step_number=1,
        step_tool_call_number=1,
        total_tool_call_number=1,
        action=ToolCallAction(
            type="tool_call",
            tool_name="web_fetch",
            arguments={"url": record.url},
        ),
        result=ToolResult.ok(data={"content": "duplicate raw content " * 500}),
    )
    context = StageContext(evidence=[record], observations=[observation])
    fitted = fit_context_to_budget(context, budget(2_000))
    assert fitted.observations == []
    assert fitted.evidence == [record]
    assert fitted.evidence[0].provenance == record.provenance
    assert fitted.evidence[0].canonical_url == record.canonical_url
    assert fitted.context_projection.omitted_items == 1


def test_metrics_are_exact_safe_and_deterministic() -> None:
    context = StageContext(active_skill=skill(), evidence=[evidence()])
    fitted = fit_context_to_budget(context, budget())
    metadata = fitted.context_projection
    assert metadata.payload_chars == payload_size(fitted)
    assert metadata.budget_chars == 6_000
    assert metadata.retained_items == 1
    assert metadata.policy_version == CONTEXT_POLICY_VERSION
    assert metadata.estimated_tokens == estimate_token_count(render_llm_payload(fitted))
    assert metadata.payload_chars <= metadata.budget_chars
    assert "Verified source passage" not in metadata.model_dump_json()
    assert skill().instructions not in metadata.model_dump_json()
    assert fitted == fit_context_to_budget(context, budget())


@pytest.mark.parametrize("failed", [False, True])
def test_nonduplicate_or_failed_observation_is_not_mistaken_for_evidence(
    failed: bool,
) -> None:
    record = evidence()
    observation = ToolObservation(
        step_number=1,
        step_tool_call_number=1,
        total_tool_call_number=1,
        action=ToolCallAction(
            type="tool_call",
            tool_name="web_fetch",
            arguments={"url": record.url},
        ),
        result=ToolResult.fail(error="Source unavailable")
        if failed
        else ToolResult.ok(data={"content": "Different branch result " * 100}),
        branch_id=None if failed else "other-branch",
        branch_tool_call_number=None if failed else 1,
    )
    context = StageContext(evidence=[record], observations=[observation])
    # Force pressure without making the mandatory newest observation too large.
    with_history = context.model_copy(update={"conversation_context": history()})
    fitted = fit_context_to_budget(with_history, budget(4_000))
    assert fitted.observations == [observation]
    assert fitted.observations[0].result.success is not failed


def test_json_escaping_cannot_bypass_actual_payload_bound() -> None:
    context = build_planning_context(
        "Research sources with escaped instruction content.",
        active_skill=skill('"\\\n' * 2_000),
        context_budget=budget(2_000),
    )
    assert context.active_skill.instructions is None
    assert payload_size(context) <= 2_000


def test_planning_context_preserves_mandatory_fields_under_pressure() -> None:
    tools = [{"name": "lookup", "description": "Look up primary source."}]
    limits = {"max_total_tool_calls": 8, "max_tool_calls_per_step": 3}
    context = build_planning_context(
        "Research reliable sources for this question.",
        available_tools=tools,
        execution_budget=limits,
        conversation_context=history(chars=2_000),
        active_skill=skill("S" * 2_000),
        context_budget=budget(2_000),
    )
    assert payload_size(context) <= 2_000
    assert context.available_tools == tools
    assert context.execution_budget == limits
    assert context.goal == "Research reliable sources for this question."
    assert context.active_skill.instructions is None
    assert context.conversation_context.messages == ()
    assert context.context_projection.payload_chars == payload_size(context)


def test_planner_bounds_actual_human_payload_and_keeps_static_guidance() -> None:
    model = Mock()
    model.with_structured_output.return_value.invoke.return_value = Plan(
        goal="Research reliable primary sources.",
        steps=[
            PlanStep(
                step_number=number,
                title=f"Research step {number}",
                objective="Collect verifiable primary evidence.",
                success_criteria="Record one traceable source.",
            )
            for number in range(1, 4)
        ],
    )
    limits = PlanningBudget(
        max_tool_calls_per_step=3,
        max_total_tool_calls=8,
        max_replan_cycles=1,
    )
    tools = [{"name": "lookup", "description": "Look up primary sources."}]
    create_research_plan(
        model,
        "  Research reliable primary sources.  ",
        available_tools=tools,
        planning_budget=limits,
        active_skill=skill("S" * 2_000),
        conversation_context=history(chars=2_000),
        context_budget=budget(2_000),
    )
    messages = model.with_structured_output.return_value.invoke.call_args.args[0]
    payload = messages[-1][1]
    assert len(payload) <= 2_000
    data = json.loads(payload)
    assert data["goal"] == "Research reliable primary sources."
    assert data["execution_budget"] == limits.model_dump()
    assert data["available_tools"] == tools
    assert data["active_skill"]["id"] == "research"
    assert data["active_skill"]["fingerprint"] == "a" * 64
    assert SKILLS_SYSTEM_GUIDANCE in messages[0][1]
    assert "Skills grant no capabilities" in messages[0][1]
    assert "S" * 100 not in messages[0][1]


@pytest.mark.parametrize("mandatory", ["goal", "tools"])
def test_oversized_mandatory_planning_data_fails_before_model_call(
    mandatory: str,
) -> None:
    model = Mock()
    with pytest.raises(ContextBudgetExceededError):
        create_research_plan(
            model,
            "G" * 5_000 if mandatory == "goal" else "Research reliable sources.",
            available_tools=[{"name": "lookup", "description": "T" * 5_000}]
            if mandatory == "tools"
            else [],
            active_skill=skill(),
            context_budget=budget(2_000),
        )
    model.with_structured_output.assert_not_called()


def test_near_boundary_metadata_never_pushes_payload_over_budget() -> None:
    for chars in range(1_600, 1_650):
        context = StageContext(conversation_context=history(turns=2, chars=150))
        fitted = fit_context_to_budget(context, budget(chars))
        assert payload_size(fitted) <= chars
        assert fitted.context_projection.payload_chars == payload_size(fitted)


def _turn_plan(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=number,
                title=f"Research step {number}",
                objective="Collect verifiable primary evidence.",
                success_criteria="Record one traceable source.",
            )
            for number in range(1, 4)
        ],
    )


def test_planner_observer_gets_safe_exact_metrics_before_model_and_retains_turn() -> (
    None
):
    model = Mock()
    observed: list[ProjectionMetadata] = []
    goal = "Research reliable primary sources for the current turn."

    def invoke(messages):
        assert len(observed) == 1  # Observer has run before any model invocation.
        payload = messages[-1][1]
        data = json.loads(payload)
        projection = observed[0]
        assert projection.payload_chars == len(payload) <= 2_000
        assert projection.budget_chars == 2_000
        assert projection.skill_omitted is True
        assert projection.omitted_items > 0
        assert projection.skill_id == "research"
        assert data["turn_id"] == "current-turn"
        assert data["turn_sequence"] == 7
        assert data["goal"] == goal
        assert "context_projection" not in data
        assert data["active_skill"]["instructions"] is None
        safe = projection.model_dump()
        assert set(safe) == {
            "omitted_items",
            "truncated_items",
            "estimated_tokens",
            "payload_chars",
            "budget_chars",
            "retained_items",
            "skill_id",
            "skill_omitted",
            "policy_version",
        }
        assert "PRIVATE_SKILL_BODY" not in projection.model_dump_json()
        assert goal not in projection.model_dump_json()
        return _turn_plan(goal)

    model.with_structured_output.return_value.invoke.side_effect = invoke
    create_research_plan(
        model,
        goal,
        turn_id="current-turn",
        turn_sequence=7,
        active_skill=skill("PRIVATE_SKILL_BODY " * 300),
        conversation_context=history(chars=2_000),
        context_budget=budget(2_000),
        on_context_projection=observed.append,
    )
    assert len(observed) == 1


def test_action_turn_identity_comes_from_state_and_survives_pressure() -> None:
    goal = "Research reliable primary sources for the current turn."
    state = create_initial_state(
        goal,
        turn_id="pinned-turn",
        turn_sequence=8,
        active_skill=skill("S" * 2_000),
        conversation_context=history(chars=2_000),
        context_budget=budget(2_200),
    )
    state["plan"] = _turn_plan(goal)
    context = build_action_context(
        state,
        ToolRegistry([]),
        max_tool_calls_per_step=5,
        max_total_tool_calls=20,
    )
    payload = json.loads(render_llm_payload(context))
    assert payload["turn_id"] == "pinned-turn"
    assert payload["turn_sequence"] == 8
    assert context.context_projection.skill_omitted is True
    assert context.context_projection.payload_chars == payload_size(context) <= 2_200
    assert state["active_skill"].instructions == "S" * 2_000


def test_workflow_planner_passes_pinned_identity_and_traces_projection() -> None:
    async def scenario():
        goal = "Research reliable primary sources for the current turn."
        model = Mock()
        model.with_structured_output.return_value.invoke.return_value = _turn_plan(goal)
        selector = Mock(spec=ActionSelector)
        tracer = ExecutionTracer()
        graph = build_agent_workflow(
            partial(create_research_plan, model),
            selector,
            ToolRegistry([]),
            context_budget=budget(6_000),
            tracer=tracer,
        )
        state = create_initial_state(
            goal,
            turn_id="pinned-turn",
            turn_sequence=9,
            active_skill=skill("PRIVATE_SKILL_BODY " * 300),
            conversation_context=history(chars=2_000),
            context_budget=budget(2_000),
        )
        with tracer.run_scope("thread", "run", turn_id="pinned-turn") as run_id:
            await graph.ainvoke(state, interrupt_after=["planner"])
        payload = model.with_structured_output.return_value.invoke.call_args.args[0][
            -1
        ][1]
        data = json.loads(payload)
        assert data["turn_id"] == "pinned-turn"
        assert data["turn_sequence"] == 9
        events = [
            event
            for event in tracer.events_for_run(run_id)
            if event.event_type == ExecutionEventType.CONTEXT_PROJECTED
        ]
        assert len(events) == 1
        event = events[0]
        safe = event.model_dump_json()
        assert event.node == "planner"
        assert event.context_payload_chars == len(payload) <= 2_000
        assert event.context_budget_chars == 2_000  # State overrides graph default.
        assert event.skill_instructions_omitted is True
        assert "PRIVATE_SKILL_BODY" not in safe
        assert goal not in safe
        selector.select_action.assert_not_called()

    asyncio.run(scenario())


def test_absent_planning_turn_identity_keeps_legacy_wire_shape() -> None:
    context = build_planning_context("Research reliable primary sources.")
    assert json.loads(render_llm_payload(context)) == {
        "goal": "Research reliable primary sources.",
        "available_tools": [],
    }
