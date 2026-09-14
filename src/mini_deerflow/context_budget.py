"""Deterministic, provenance-preserving context budgeting.

Day 11 architectural rule: the complete validated state — evidence,
citations, findings, completed summaries, errors, review verdicts, and
replan history — stays untouched for checkpointing, audit, rendering, and
resume. Truncation and compaction are applied only when projecting that
state into the LLM-facing contexts (action selector, reviewer, replanner),
and every projection is deterministic so behavior stays testable and can
never introduce new claims, citations, or evidence.

``ContextBudget.max_total_chars`` is a hard upper bound on the serialized
payload actually handed to a model — measured with the exact renderer the
LLM seams use (:func:`render_llm_payload`). A projection that cannot be
brought under the bound by deterministic compaction raises
:class:`ContextBudgetExceededError`; it is never sent oversized.

All compaction is character-based and deterministic. Token counts produced
by :func:`estimate_token_count` are a documented conservative heuristic,
not exact model-token accounting.
"""

import json
import math
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from mini_deerflow.actions import ToolObservation
from mini_deerflow.evidence import EvidenceRecord, StepFinding
from mini_deerflow.schemas import PlanStep

# Conservative heuristic: roughly four characters per token for the mixed
# English/Vietnamese technical text this agent produces. This is an
# approximation for budget reporting only; it is NOT exact GLM tokenizer
# accounting, and nothing in the system treats it as authoritative.
TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4

DEFAULT_MAX_TOTAL_CONTEXT_CHARS = 60_000
DEFAULT_MAX_ITEM_CHARS = 4_000
DEFAULT_RETAINED_RECENT_ITEMS = 30
DEFAULT_MAX_EXCERPT_CHARS = 1_500

# Fixed fields every context always carries (goal, step, remaining budgets,
# provenance) reserve this much of the total budget before droppable
# collections are considered.
FIXED_CONTEXT_RESERVE_CHARS = 1_024

# Headroom subtracted from the configured total so the final projection
# metadata (which is itself serialized) can never push the payload over
# the hard bound after the pressure pass finishes.
METADATA_SLACK_CHARS = 48

# Ceiling for one retained limitation string once pressure reaches that
# tier; failure visibility survives, verbose failure text does not.
LIMITATION_PRESSURE_CHARS = 200

_TRUNCATED_MARKER = "truncated at"
_OMITTED_MARKER = "omitted:"


class ContextBudgetModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )


class ContextBudgetExceededError(RuntimeError):
    """Raised when no deterministic compaction can satisfy the budget."""


class ContextBudget(ContextBudgetModel):
    """Validated limits for one deterministic LLM context projection.

    This budget is independent of the execution budgets: tool-call limits
    bound side effects, the replan-cycle limit bounds plan revisions, the
    recursion limit bounds graph steps, and this budget bounds only the
    size of prompts projected from validated state.
    """

    max_total_chars: int = Field(
        default=DEFAULT_MAX_TOTAL_CONTEXT_CHARS,
        strict=True,
        ge=1,
    )
    max_item_chars: int = Field(
        default=DEFAULT_MAX_ITEM_CHARS,
        strict=True,
        ge=1,
    )
    retained_recent_items: int = Field(
        default=DEFAULT_RETAINED_RECENT_ITEMS,
        strict=True,
        ge=1,
    )
    max_excerpt_chars: int = Field(
        default=DEFAULT_MAX_EXCERPT_CHARS,
        strict=True,
        ge=1,
    )

    @model_validator(mode="after")
    def validate_limit_relationships(self) -> Self:
        if self.max_excerpt_chars > self.max_item_chars:
            raise ValueError(
                "max_excerpt_chars cannot exceed max_item_chars",
            )

        minimum_total = (
            self.max_item_chars + self.max_excerpt_chars + FIXED_CONTEXT_RESERVE_CHARS
        )

        if self.max_total_chars < minimum_total:
            raise ValueError(
                "max_total_chars must leave room for one bounded item, one "
                f"bounded excerpt, and fixed context fields ({minimum_total})"
            )

        return self


class ProjectionMetadata(ContextBudgetModel):
    """Honest bookkeeping for one projected context.

    This metadata travels inside the untrusted context block so the model
    can see that content was omitted or truncated; it never alters trusted
    system instructions, which live outside the compactable context.
    """

    omitted_items: int = Field(
        default=0,
        ge=0,
    )
    truncated_items: int = Field(
        default=0,
        ge=0,
    )
    estimated_tokens: int = Field(
        default=0,
        ge=0,
    )


def render_llm_payload(model: BaseModel) -> str:
    """Serialize one context exactly as the LLM seams serialize it.

    This is the single wire-format renderer shared by the selector,
    reviewer, and replanner prompts, so the budget is enforced against the
    bytes a model actually receives rather than a compact approximation.
    """

    return json.dumps(
        model.model_dump(
            mode="json",
            by_alias=True,
        ),
        ensure_ascii=False,
        indent=2,
    )


def payload_size(model: BaseModel) -> int:
    """Return the exact serialized size of one LLM-facing context."""

    return len(render_llm_payload(model))


def estimate_token_count(text: str) -> int:
    """Return a conservative token estimate for one text.

    Uses ceil(characters / 4): a documented heuristic approximation for
    mixed English/Vietnamese technical prose. It is not exact model-token
    accounting and is used only for reporting and budget enforcement in
    character space.
    """

    return math.ceil(len(text) / TOKEN_ESTIMATE_CHARS_PER_TOKEN)


def truncate_text(
    text: str,
    limit: int,
) -> tuple[str, bool]:
    """Head-truncate one untrusted text with an explicit marker.

    The head is kept because openings carry provenance and framing; the
    marker records both the cut point and the original length so nothing is
    silently shortened. Returns the bounded text and whether truncation
    happened.
    """

    if len(text) <= limit:
        return text, False

    marker = f" [{_TRUNCATED_MARKER} {limit} of {len(text)} characters]"
    keep = max(1, limit - len(marker))

    return text[:keep] + marker, True


def project_evidence_records(
    evidence: list[EvidenceRecord],
    budget: ContextBudget,
) -> tuple[list[EvidenceRecord], ProjectionMetadata]:
    """Project evidence for an LLM context, preserving provenance.

    Keeps the most recent records (the list tail — evidence merge order
    appends newer observations last), truncates each excerpt to the excerpt
    budget, and never touches URLs or provenance, which are small and are
    the identity of the record. Oldest records beyond the retention limit
    are omitted and counted, never removed from caller-owned state.
    """

    keep = min(len(evidence), budget.retained_recent_items)
    omitted = len(evidence) - keep
    truncated = 0

    projected: list[EvidenceRecord] = []

    for record in evidence[len(evidence) - keep :]:
        excerpt, excerpt_truncated = truncate_text(
            record.excerpt,
            budget.max_excerpt_chars,
        )
        title, _ = truncate_text(
            record.title or "",
            budget.max_item_chars,
        )
        truncated += int(excerpt_truncated)
        projected.append(
            record.model_copy(
                update={
                    "excerpt": excerpt,
                    "title": title,
                },
            ),
        )

    return projected, ProjectionMetadata(
        omitted_items=omitted,
        truncated_items=truncated,
    )


def project_tool_result_data(
    data: JsonValue,
    limit: int,
) -> tuple[JsonValue, bool]:
    """Bound every string inside one tool result payload."""

    if isinstance(data, str):
        bounded, was_truncated = truncate_text(data, limit)

        return bounded, was_truncated

    if isinstance(data, dict):
        any_truncated = False
        projected: dict[str, JsonValue] = {}

        for key, value in data.items():
            bounded, was_truncated = project_tool_result_data(value, limit)
            projected[str(key)] = bounded
            any_truncated = any_truncated or was_truncated

        return projected, any_truncated

    if isinstance(data, list):
        any_truncated = False
        projected_values: list[JsonValue] = []

        for value in data:
            bounded, was_truncated = project_tool_result_data(value, limit)
            projected_values.append(bounded)
            any_truncated = any_truncated or was_truncated

        return projected_values, any_truncated

    return data, False


def project_observations(
    observations: list[ToolObservation],
    budget: ContextBudget,
) -> tuple[list[ToolObservation], ProjectionMetadata]:
    """Project tool observations for an LLM context.

    Keeps the most recent observations (highest call numbers), bounds every
    string inside each result payload, and always preserves the observation
    identity fields: tool name, arguments, step/call/observation numbers,
    success flag, structured error, and failure metadata. Failed
    observations stay visible in bounded form.
    """

    keep = min(len(observations), budget.retained_recent_items)
    omitted = len(observations) - keep
    truncated = 0

    projected: list[ToolObservation] = []

    for observation in observations[len(observations) - keep :]:
        data, was_truncated = project_tool_result_data(
            observation.result.data,
            budget.max_item_chars,
        )
        error, _ = truncate_text(
            observation.result.error or "",
            budget.max_item_chars,
        )
        truncated += int(was_truncated)
        projected.append(
            observation.model_copy(
                update={
                    "result": observation.result.model_copy(
                        update={
                            "data": data,
                            "error": error,
                        },
                    ),
                },
            ),
        )

    return projected, ProjectionMetadata(
        omitted_items=omitted,
        truncated_items=truncated,
    )


def project_summaries(
    summaries: list[str],
    budget: ContextBudget,
) -> tuple[list[str], ProjectionMetadata]:
    """Bound completed step summaries without dropping them.

    Summaries carry cross-step continuity and provenance, so they are
    truncated per item but never omitted.
    """

    truncated = 0
    projected: list[str] = []

    for summary in summaries:
        bounded, was_truncated = truncate_text(summary, budget.max_item_chars)
        truncated += int(was_truncated)
        projected.append(bounded)

    return projected, ProjectionMetadata(
        truncated_items=truncated,
    )


def merge_metadata(
    *metadata: ProjectionMetadata,
) -> ProjectionMetadata:
    """Combine collection-level projection bookkeeping."""

    return ProjectionMetadata(
        omitted_items=sum(item.omitted_items for item in metadata),
        truncated_items=sum(item.truncated_items for item in metadata),
    )


def _summary_marker(position: int, summary: str) -> str:
    return (
        f"[summary for completed step {position} "
        f"{_OMITTED_MARKER} {len(summary)} characters]"
    )


def _finding_marker(finding: StepFinding) -> StepFinding:
    return StepFinding(
        step_number=finding.step_number,
        summary=f"[finding {_OMITTED_MARKER} {len(finding.summary)} characters]",
        citations=[],
        branch_id=finding.branch_id,
    )


def _step_marker(step: PlanStep) -> PlanStep:
    return step.model_copy(
        update={
            "objective": (
                f"[objective {_OMITTED_MARKER} {len(step.objective)} characters]"
            ),
            "success_criteria": (
                f"[criteria {_OMITTED_MARKER} {len(step.success_criteria)} characters]"
            ),
        },
    )


def _strip_tool_schema(
    definition: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    return {
        "name": definition.get("name"),
        "description": definition.get("description"),
    }


def _apply_pressure(
    working: ContextBudgetModel,
    target: int,
    metadata: ProjectionMetadata,
) -> tuple[ContextBudgetModel, ProjectionMetadata]:
    """Deterministically compact one context until it fits ``target``.

    Pressure tiers, applied in order and only as far as needed:

    1. drop the oldest evidence items (all of them may go);
    2. drop the oldest observations, always keeping the most recent one;
    3. replace older summaries with position markers, newest last;
    4. replace older findings with step-number markers, newest last;
    5. drop older limitations, then bound the newest one;
    6. strip verbose tool schemas, oldest tools first, keeping the tool
       name and description (tool identity);
    7. replace older replaced-plan step objectives/criteria with markers,
       keeping step number and title.

    Every tier preserves minimal identity (step numbers, tool names, call
    identifiers on retained items) and records honest omission/truncation
    counts. If all tiers are exhausted and the context still exceeds the
    bound, the caller raises instead of returning an oversized projection.
    """

    def size(model: ContextBudgetModel) -> int:
        return payload_size(model)

    def commit(
        model: ContextBudgetModel,
        meta: ProjectionMetadata,
    ) -> tuple[ContextBudgetModel, ProjectionMetadata]:
        return (
            model.model_copy(update={"context_projection": meta}),
            meta,
        )

    # Tier 1: oldest evidence first.
    while size(working) > target and len(getattr(working, "evidence", [])) > 0:
        metadata = metadata.model_copy(
            update={"omitted_items": metadata.omitted_items + 1},
        )
        working, metadata = commit(
            working.model_copy(
                update={"evidence": working.evidence[1:]},
            ),
            metadata,
        )

    # Tier 2: oldest observations, keeping the most recent one.
    while size(working) > target and len(getattr(working, "observations", [])) > 1:
        metadata = metadata.model_copy(
            update={"omitted_items": metadata.omitted_items + 1},
        )
        working, metadata = commit(
            working.model_copy(update={"observations": working.observations[1:]}),
            metadata,
        )

    # Tier 3: summaries become position markers, oldest first.
    summaries = list(getattr(working, "completed_step_summaries", []))

    while size(working) > target and len(summaries) > 0:
        marker_index = next(
            (
                index
                for index, summary in enumerate(summaries)
                if not summary.startswith("[summary for completed step")
            ),
            None,
        )

        if marker_index is None:
            break

        summaries[marker_index] = _summary_marker(
            marker_index + 1,
            summaries[marker_index],
        )
        metadata = metadata.model_copy(
            update={"truncated_items": metadata.truncated_items + 1},
        )
        working, metadata = commit(
            working.model_copy(
                update={"completed_step_summaries": list(summaries)},
            ),
            metadata,
        )

    while size(working) > target and len(summaries) > 1:
        metadata = metadata.model_copy(
            update={"omitted_items": metadata.omitted_items + 1},
        )
        summaries = summaries[1:]
        working, metadata = commit(
            working.model_copy(
                update={"completed_step_summaries": list(summaries)},
            ),
            metadata,
        )

    # Tier 4: findings become step-number markers, oldest first.
    findings = list(getattr(working, "findings", []))

    while size(working) > target and len(findings) > 0:
        marker_index = next(
            (
                index
                for index, finding in enumerate(findings)
                if not finding.summary.startswith("[finding")
            ),
            None,
        )

        if marker_index is None:
            break

        findings[marker_index] = _finding_marker(findings[marker_index])
        metadata = metadata.model_copy(
            update={"truncated_items": metadata.truncated_items + 1},
        )
        working, metadata = commit(
            working.model_copy(update={"findings": list(findings)}),
            metadata,
        )

    while size(working) > target and len(findings) > 1:
        metadata = metadata.model_copy(
            update={"omitted_items": metadata.omitted_items + 1},
        )
        findings = findings[1:]
        working, metadata = commit(
            working.model_copy(update={"findings": list(findings)}),
            metadata,
        )

    # Tier 5: limitations — drop oldest, then bound the newest.
    limitations = list(getattr(working, "limitations", []))

    while size(working) > target and len(limitations) > 1:
        metadata = metadata.model_copy(
            update={"omitted_items": metadata.omitted_items + 1},
        )
        limitations = limitations[1:]
        working, metadata = commit(
            working.model_copy(update={"limitations": list(limitations)}),
            metadata,
        )

    if size(working) > target and len(limitations) == 1:
        bounded, was_truncated = truncate_text(
            limitations[0],
            LIMITATION_PRESSURE_CHARS,
        )

        if was_truncated:
            metadata = metadata.model_copy(
                update={"truncated_items": metadata.truncated_items + 1},
            )
            working, metadata = commit(
                working.model_copy(update={"limitations": [bounded]}),
                metadata,
            )

    # Tier 6: strip verbose tool schemas, oldest tools first.
    tools = list(getattr(working, "available_tools", []))

    while size(working) > target:
        schema_index = next(
            (
                index
                for index, definition in enumerate(tools)
                if "input_schema" in definition
            ),
            None,
        )

        if schema_index is None:
            break

        tools[schema_index] = _strip_tool_schema(tools[schema_index])
        metadata = metadata.model_copy(
            update={"truncated_items": metadata.truncated_items + 1},
        )
        working, metadata = commit(
            working.model_copy(update={"available_tools": list(tools)}),
            metadata,
        )

    # Tier 7: replaced plan steps become step markers, oldest first.
    steps = list(getattr(working, "replaced_steps", []))

    while size(working) > target and len(steps) > 0:
        marker_index = next(
            (
                index
                for index, step in enumerate(steps)
                if not step.objective.startswith("[objective")
            ),
            None,
        )

        if marker_index is None:
            break

        steps[marker_index] = _step_marker(steps[marker_index])
        metadata = metadata.model_copy(
            update={"truncated_items": metadata.truncated_items + 1},
        )
        working, metadata = commit(
            working.model_copy(update={"replaced_steps": list(steps)}),
            metadata,
        )

    return working, metadata


def fit_context_to_budget(
    context: ContextBudgetModel,
    budget: ContextBudget,
    prior: ProjectionMetadata | None = None,
) -> ContextBudgetModel:
    """Enforce the hard total budget on one assembled context model.

    The bound is measured with :func:`render_llm_payload` — the exact
    serialization handed to the model — minus a small metadata slack so the
    final metadata cannot push the payload over the limit. Deterministic
    tiered compaction (:func:`_apply_pressure`) removes or markers older,
    lower-priority content while preserving minimal identity. If the bound
    still cannot be met, :class:`ContextBudgetExceededError` is raised; an
    oversized projection is never returned.
    """

    if "context_projection" not in type(context).model_fields:
        return context

    metadata = merge_metadata(prior) if prior else ProjectionMetadata()
    target = max(
        1,
        budget.max_total_chars - METADATA_SLACK_CHARS,
    )

    working = context.model_copy(
        update={"context_projection": metadata},
    )

    if payload_size(working) > budget.max_total_chars:
        working, metadata = _apply_pressure(working, target, metadata)

    if payload_size(working) > budget.max_total_chars:
        raise ContextBudgetExceededError(
            "context projection cannot fit the configured "
            f"max_total_chars={budget.max_total_chars} even after full "
            "deterministic compaction"
        )

    metadata = metadata.model_copy(
        update={
            "estimated_tokens": estimate_token_count(
                render_llm_payload(working),
            ),
        },
    )

    return working.model_copy(
        update={"context_projection": metadata},
    )
