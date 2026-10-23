"""Reproducible Phase 1 production-boundary demonstration, entirely offline.

Run ``python -m mini_deerflow.demo.phase1_offline`` from the checkout. Each CLI
invocation uses fresh temporary SQLite storage and prints scalar diagnostics.
The production factory, graph, context compactor, skill activation, conversation
ledger and checkpoints run normally. Only external model/web/researcher seams
are scripted using Phase 0 fixtures. This demonstrates request assembly and
continuity, not model intelligence or factual research quality. No prompts,
skill instruction bodies, fixture answers or raw tool results are printed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from pydantic import ValidationError

from mini_deerflow.config import Settings
from mini_deerflow.conversation import SQLiteConversationRepository
from mini_deerflow.demo.offline_scenario import (
    DEMO_CONTEXT_BUDGET,
    DEMO_LIMITS,
    _DeterministicClock,
    _RecordingTraceSink,
    _ScenarioModel,
    _ScenarioResources,
)
from mini_deerflow.demo.view_models import TraceEventView, project_demo_run
from mini_deerflow.evidence import EvidenceRecord
from mini_deerflow.persistence import open_sqlite_checkpointer
from mini_deerflow.runtime import create_default_agent_runtime
from mini_deerflow.skills import SkillsRegistry
from mini_deerflow.structured_output import StructuredChatModel
from mini_deerflow.tracing import ExecutionTrace, ExecutionTracer
from mini_deerflow.web_safety import PublicWebTargetValidator

_GOAL = "Demonstrate production skill context with bounded offline fixture evidence."
_FOLLOW_UP = "Compare the previous fixture findings and preserve their limitations."
_THREAD_ID = "phase1-offline"
# Tighten only this demonstration so requests containing collected evidence
# actually reach the production compactor's pressure path as well.
_CONTEXT_BUDGET = DEMO_CONTEXT_BUDGET.model_copy(update={"max_total_chars": 7_000})


@dataclass(frozen=True, slots=True)
class ModelRequestCapture:
    """Measurements taken at the actual structured-model invocation boundary."""

    operation: str
    schema: str
    payload_chars: int
    payload_sha256: str
    active_skill_id: str | None
    skill_fingerprint: str | None
    skill_instruction_chars: int
    skill_instructions_omitted: bool
    context_omitted_items: int
    context_truncated_items: int
    context_retained_items: int | None
    context_estimated_tokens: int | None
    context_policy_version: str | None
    conversation_message_count: int
    conversation_omitted_turns: int
    current_goal_preserved: bool
    has_execution_budget: bool
    execution_budget_within_limits: bool | None
    retained_evidence_count: int
    retained_evidence_provenance_count: int


class _CapturingScenarioModel(_ScenarioModel):
    """Observe production requests before handing them to the scripted fixture."""

    def __init__(
        self,
        goal: str,
        operation: str,
        captures: list[ModelRequestCapture],
        *,
        interrupt: bool = False,
    ) -> None:
        super().__init__(goal, interrupt_after_delegation=interrupt)
        self._operation = operation
        self._captures = captures

    def respond(self, schema: type[object], messages: object) -> object:
        payload = _request_payload(schema, messages)
        data = json.loads(payload)
        skill = data.get("active_skill") or {}
        projection = data.get("context_projection") or {}
        conversation = data.get("conversation_context") or {}
        instructions = skill.get("instructions")
        # Validate received records without retaining or printing source content.
        evidence = data.get("evidence", [])
        provenance_count = 0
        for record in evidence:
            try:
                EvidenceRecord.model_validate(record)
            except ValidationError:
                continue
            provenance_count += 1
        budget_fields = {
            "Plan": ("execution_budget",),
            "ActionDecision": (
                "remaining_step_tool_calls",
                "remaining_total_tool_calls",
                "allocated_step_tool_calls",
                "remaining_allocated_step_tool_calls",
            ),
            "ReviewDecision": ("remaining_total_tool_calls", "remaining_replan_cycles"),
            "ReplacementWork": (
                "remaining_total_tool_calls",
                "remaining_replan_cycles",
            ),
        }.get(schema.__name__, ())
        has_budget = bool(budget_fields) and all(key in data for key in budget_fields)
        budget_valid = None
        if budget_fields:
            if schema.__name__ == "Plan":
                budget_valid = data.get("execution_budget") == {
                    "max_tool_calls_per_step": DEMO_LIMITS.max_tool_calls_per_step,
                    "max_total_tool_calls": DEMO_LIMITS.max_total_tool_calls,
                    "max_replan_cycles": DEMO_LIMITS.max_replan_cycles,
                }
            else:
                budget_valid = has_budget and all(
                    type(data[key]) is int
                    and 0
                    <= data[key]
                    <= (
                        DEMO_LIMITS.max_total_tool_calls
                        if key == "remaining_total_tool_calls"
                        else DEMO_LIMITS.max_replan_cycles
                        if key == "remaining_replan_cycles"
                        else DEMO_LIMITS.max_tool_calls_per_step
                    )
                    for key in budget_fields
                )
        self._captures.append(
            ModelRequestCapture(
                operation=self._operation,
                schema=schema.__name__,
                payload_chars=len(payload),
                payload_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                active_skill_id=skill.get("id"),
                skill_fingerprint=skill.get("fingerprint"),
                skill_instruction_chars=len(instructions) if instructions else 0,
                skill_instructions_omitted=bool(skill) and instructions is None,
                context_omitted_items=projection.get("omitted_items", 0),
                context_truncated_items=projection.get("truncated_items", 0),
                context_retained_items=projection.get("retained_items"),
                context_estimated_tokens=projection.get("estimated_tokens"),
                context_policy_version=projection.get("policy_version"),
                conversation_message_count=len(conversation.get("messages", ())),
                conversation_omitted_turns=conversation.get("omitted_turns", 0),
                current_goal_preserved=data.get("goal") == self._goal,
                has_execution_budget=has_budget,
                execution_budget_within_limits=budget_valid,
                retained_evidence_count=len(evidence),
                retained_evidence_provenance_count=provenance_count,
            )
        )
        return super().respond(schema, messages)


def _request_payload(schema: type[object], messages: object) -> str:
    """Extract the exact JSON substring received, rather than reserializing it."""
    if not isinstance(messages, list):
        raise TypeError("Expected production message list")
    for message in reversed(messages):
        content = message[1] if isinstance(message, tuple) else message.content
        if not isinstance(content, str):
            continue
        if (
            isinstance(message, tuple)
            and message[0] == "human"
            and content.lstrip().startswith("{")
        ):
            return content
        for tag in (
            "action_context",
            "review_context",
            "replan_context",
            "answer_context",
        ):
            opening, closing = f"<{tag}>\n", f"\n</{tag}>"
            if opening in content and closing in content:
                return content.split(opening, 1)[1].split(closing, 1)[0]
    raise AssertionError(
        f"Production {schema.__name__} request omitted its JSON context"
    )


@dataclass(frozen=True, slots=True)
class Phase1OfflineReport:
    label: str
    discovered_skill_ids: tuple[str, ...]
    baseline_has_no_skill: bool
    requests: tuple[ModelRequestCapture, ...]
    context_metrics: tuple[TraceEventView, ...]
    historical_skill_ids: tuple[str | None, ...]
    turn_statuses: tuple[str, ...]
    selected_skill_reached_model: bool
    pinned_skill_survived_resume: bool
    interrupted_then_resumed: bool
    resumed_without_replaying_tools: bool
    completed_resume_without_model_calls: bool
    follow_up_has_history: bool
    follow_up_has_no_implicit_skill: bool
    all_payloads_within_budget: bool
    compaction_observed: bool
    current_goal_preserved: bool
    execution_budgets_valid: bool
    evidence_provenance_preserved_under_pressure: bool
    skill_identity_preserved_under_pressure: bool


async def run_phase1_demo(
    storage_root: str | Path,
    *,
    skill_id: str = "deep-research",
) -> Phase1OfflineReport:
    """Compare an unskilled baseline, skilled resume, and unskilled follow-up.

    ``storage_root`` must be fresh. All observations in the returned report come
    from actual model requests, redacted runtime traces or sanitized state views.
    """
    root = Path(storage_root)
    database = root / "checkpoints.sqlite"
    registry = SkillsRegistry()
    discovered_skill_ids = tuple(item.id for item in registry.discover())
    settings = Settings(
        api_key="offline-demo-placeholder",
        base_url="https://api.example.invalid/v1",
        model_name="offline-scripted-model",
        temperature=0.0,
        request_timeout=1.0,
        max_retries=0,
        wiki_request_timeout=1.0,
        structured_output_mode="native",
        _env_file=None,
    )
    captures: list[ModelRequestCapture] = []
    events: list[ExecutionTrace] = []
    repository = SQLiteConversationRepository(database)

    def resources(goal: str, operation: str, *, interrupt: bool = False):
        seams = _ScenarioResources.create(goal)
        seams.model = _CapturingScenarioModel(
            goal,
            operation,
            captures,
            interrupt=interrupt,
        )
        return seams

    def compose(checkpointer, seams: _ScenarioResources):
        return create_default_agent_runtime(
            settings,
            root
            / (
                "baseline-workspace"
                if seams.model._operation == "baseline"
                else "workspace"
            ),
            allow_write=True,
            checkpointer=checkpointer,
            conversation_repository=repository,
            limits=DEMO_LIMITS,
            context_budget=_CONTEXT_BUDGET,
            skills_registry=registry,
            model_factory=lambda _: cast(StructuredChatModel, seams.model),
            web_provider=seams.provider,
            web_target_validator=PublicWebTargetValidator(seams.resolver),
            researcher_subagent=seams.researcher,
            tracer=ExecutionTracer(
                _RecordingTraceSink(events, None),
                clock=_DeterministicClock(),
                run_id_factory=lambda: f"phase1-{seams.model._operation}",
            ),
        )

    # Same goal, fresh thread, no selection: metadata discovery is not activation.
    async with open_sqlite_checkpointer(database) as saver:
        runtime = compose(saver, resources(_GOAL, "baseline"))
        baseline = await runtime.run(
            _GOAL,
            thread_id=f"{_THREAD_ID}-baseline",
            turn_id="turn-baseline",
        )
        baseline_view = project_demo_run(
            baseline,
            events,
            thread_id=f"{_THREAD_ID}-baseline",
            limits=DEMO_LIMITS,
        )
    skilled_event_offset = len(events)

    # Close and reopen SQLite to demonstrate persisted resume with fresh seams.
    interrupted = False
    async with open_sqlite_checkpointer(database) as saver:
        runtime = compose(saver, resources(_GOAL, "run", interrupt=True))
        try:
            await runtime.run(
                _GOAL,
                thread_id=_THREAD_ID,
                turn_id="turn-first",
                skill_id=skill_id,
            )
        except RuntimeError as error:
            if str(error) != "deterministic interruption after delegation checkpoint":
                raise
            interrupted = True
    if not interrupted:
        raise AssertionError("Scripted interruption did not occur")

    resumed_seams = resources(_GOAL, "resume")
    async with open_sqlite_checkpointer(database) as saver:
        runtime = compose(saver, resumed_seams)
        first = await runtime.resume(thread_id=_THREAD_ID)
        await runtime.record_turn_traces(
            _THREAD_ID,
            "turn-first",
            tuple(events[skilled_event_offset:]),
        )
        first_view = project_demo_run(
            first,
            events[skilled_event_offset:],
            thread_id=_THREAD_ID,
            limits=DEMO_LIMITS,
        )
        before_resume = len(captures)
        await runtime.resume(thread_id=_THREAD_ID)
        no_replay = len(captures) == before_resume

    event_offset = len(events)
    async with open_sqlite_checkpointer(database) as saver:
        runtime = compose(saver, resources(_FOLLOW_UP, "continue"))
        # Deliberately omit skill_id: each turn needs an explicit selection.
        await runtime.continue_thread(
            _THREAD_ID,
            _FOLLOW_UP,
            turn_id="turn-second",
        )
        await runtime.record_turn_traces(
            _THREAD_ID,
            "turn-second",
            tuple(events[event_offset:]),
        )
        snapshot = await runtime.load_conversation(_THREAD_ID)
        views = tuple(
            project_demo_run(
                turn.state, turn.traces, thread_id=_THREAD_ID, limits=DEMO_LIMITS
            )
            for turn in snapshot.turns
        )

    follow_up = tuple(item for item in captures if item.operation == "continue")
    baseline_requests = tuple(item for item in captures if item.operation == "baseline")
    metrics = tuple(
        item
        for view in (baseline_view, *views)
        for item in view.traces
        if item.kind == "context_budget"
    )
    selected_requests = tuple(
        item
        for item in captures
        if item.operation == "run" and item.active_skill_id == skill_id
    )
    resumed_requests = tuple(item for item in captures if item.operation == "resume")
    fingerprints = {item.skill_fingerprint for item in selected_requests}
    pressured_requests = tuple(
        item
        for item in captures
        if item.context_omitted_items
        or item.context_truncated_items
        or item.skill_instructions_omitted
    )
    pressured_evidence = tuple(
        item for item in pressured_requests if item.retained_evidence_count
    )
    pressured_skill = tuple(
        item for item in pressured_requests if item.operation in {"run", "resume"}
    )
    return Phase1OfflineReport(
        label="Phase 1: production context assembly with scripted offline seams",
        discovered_skill_ids=discovered_skill_ids,
        baseline_has_no_skill=(
            bool(baseline_requests)
            and all(item.active_skill_id is None for item in baseline_requests)
            and baseline_view.active_skill_id is None
        ),
        requests=tuple(captures),
        context_metrics=metrics,
        historical_skill_ids=tuple(view.active_skill_id for view in views),
        turn_statuses=tuple(turn.record.status for turn in snapshot.turns),
        selected_skill_reached_model=bool(selected_requests),
        pinned_skill_survived_resume=(
            bool(resumed_requests)
            and len(fingerprints) == 1
            and None not in fingerprints
            and all(
                item.active_skill_id == skill_id
                and item.skill_fingerprint in fingerprints
                for item in resumed_requests
            )
            and views[0].active_skill_id == skill_id
        ),
        interrupted_then_resumed=(
            interrupted and first_view.workflow_status == "completed"
        ),
        resumed_without_replaying_tools=(
            resumed_seams.provider.search_call_count == 0
            and resumed_seams.provider.fetch_call_count == 0
            and not resumed_seams.researcher.calls
        ),
        completed_resume_without_model_calls=no_replay,
        follow_up_has_history=any(
            item.conversation_message_count for item in follow_up
        ),
        follow_up_has_no_implicit_skill=(
            bool(follow_up)
            and all(item.active_skill_id is None for item in follow_up)
            and views[-1].active_skill_id is None
        ),
        all_payloads_within_budget=all(
            item.payload_chars <= _CONTEXT_BUDGET.max_total_chars for item in captures
        ),
        compaction_observed=bool(pressured_requests),
        current_goal_preserved=bool(captures)
        and all(item.current_goal_preserved for item in captures),
        # Planner limits must exactly match the configured limits; stage quotas
        # must be present and in range. Synthesis has no execution-budget input.
        execution_budgets_valid=(
            any(item.has_execution_budget for item in pressured_requests)
            and all(
                item.execution_budget_within_limits is not False for item in captures
            )
        ),
        evidence_provenance_preserved_under_pressure=bool(pressured_evidence)
        and all(
            item.retained_evidence_provenance_count == item.retained_evidence_count
            for item in pressured_evidence
        ),
        skill_identity_preserved_under_pressure=(
            bool(pressured_skill)
            and len(fingerprints) == 1
            and None not in fingerprints
            and all(
                item.active_skill_id == skill_id
                and item.skill_fingerprint in fingerprints
                for item in pressured_skill
            )
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-id", default="deep-research")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="mini-deerflow-phase1-") as directory:
        report = asyncio.run(run_phase1_demo(directory, skill_id=args.skill_id))
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
