"""Bounded, single-depth researcher delegation.

The parent workflow remains the owner of durable state, global budgets,
citations, and artifacts.  A delegation tool receives only narrow tasks,
projects each task through the Day 11 context budget, runs a fixed-size
fan-out, and returns a typed record for deterministic parent-side fan-in.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from typing import Annotated, Literal, Protocol, Self, cast, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    StringConstraints,
    ValidationError,
    field_serializer,
    model_validator,
)

from mini_deerflow.actions import (
    AgentAction,
    CompleteStepAction,
    ToolCallAction,
    ToolObservation,
)
from mini_deerflow.context_budget import (
    ContextBudget,
    ContextBudgetModel,
    ProjectionMetadata,
    fit_context_to_budget,
    merge_metadata,
    project_evidence_records,
    project_observations,
)
from mini_deerflow.evidence import (
    EvidenceRecord,
    StepFinding,
    extract_evidence_records,
    merge_evidence_records,
    sanitize_finding_summary,
    validate_citations,
)
from mini_deerflow.schemas import PlanStep
from mini_deerflow.tools import ToolInput, ToolRegistry, ToolResult, ToolRunner

MAX_DELEGATION_BRANCHES = 3
MIN_DELEGATION_BRANCHES = 2
MAX_BRANCH_TOOL_CALLS = 5
DEFAULT_DELEGATION_CONCURRENCY = 2
DEFAULT_BRANCH_TIMEOUT_SECONDS = 30.0

BranchId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^[a-z][a-z0-9_-]{0,31}$",
    ),
]


class DelegationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScopedResearchTask(DelegationModel):
    """One narrow, non-delegating unit of researcher work."""

    branch_id: BranchId
    objective: str = Field(min_length=10, max_length=4_000)
    success_criteria: str = Field(min_length=10, max_length=4_000)
    tool_call_budget: int = Field(
        default=2, strict=True, ge=1, le=MAX_BRANCH_TOOL_CALLS
    )
    delegation_depth: Literal[1] = 1


class DelegationInput(ToolInput):
    """Validated parent request for one bounded fan-out wave."""

    delegation_id: BranchId
    tasks: list[ScopedResearchTask] = Field(
        min_length=MIN_DELEGATION_BRANCHES,
        max_length=MAX_DELEGATION_BRANCHES,
    )

    @model_validator(mode="after")
    def validate_unique_branches(self) -> Self:
        branch_ids = [task.branch_id for task in self.tasks]

        if len(branch_ids) != len(set(branch_ids)):
            raise ValueError("delegation branch identifiers must be unique")

        return self


class BranchFinding(DelegationModel):
    summary: str = Field(min_length=1, max_length=4_000)
    citations: list[HttpUrl] = Field(default_factory=list, max_length=20)

    @field_serializer("citations")
    def serialize_citations(self, citations: list[HttpUrl]) -> list[str]:
        return [str(citation) for citation in citations]


BranchStatus = Literal["success", "controlled_failure", "cancelled"]


class BranchResult(DelegationModel):
    """Auditable outcome from one researcher branch."""

    branch_id: BranchId
    status: BranchStatus
    observations: list[ToolObservation] = Field(default_factory=list)
    finding: BranchFinding | None = None
    error: str | None = Field(default=None, max_length=500)
    tool_calls_used: int = Field(default=0, strict=True, ge=0, le=MAX_BRANCH_TOOL_CALLS)

    @model_validator(mode="after")
    def validate_result_shape(self) -> Self:
        if self.tool_calls_used != len(self.observations):
            raise ValueError("tool_calls_used must equal the observation count")

        if self.status == "success":
            if self.finding is None or self.error is not None:
                raise ValueError("successful branch requires a finding and no error")
        elif self.error is None or not self.error.strip():
            raise ValueError("unsuccessful branch requires a bounded error")

        return self


class FanInSummary(DelegationModel):
    """Deterministic merged view consumed by the parent workflow."""

    successful_branches: list[BranchId] = Field(default_factory=list)
    failed_branches: list[BranchId] = Field(default_factory=list)
    cancelled_branches: list[BranchId] = Field(default_factory=list)
    findings: list[StepFinding] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class DelegationRecord(DelegationModel):
    """Checkpoint-safe record of one complete delegation wave."""

    delegation_id: BranchId
    parent_step_number: int = Field(ge=1, le=7)
    tasks: list[ScopedResearchTask]
    results: list[BranchResult]
    fan_in: FanInSummary
    reserved_tool_calls: int = Field(ge=0)
    used_tool_calls: int = Field(ge=0)
    charged_tool_calls: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_resource_accounting(self) -> Self:
        if self.used_tool_calls > self.charged_tool_calls:
            raise ValueError("used_tool_calls cannot exceed charged_tool_calls")
        if self.charged_tool_calls > self.reserved_tool_calls:
            raise ValueError("charged_tool_calls cannot exceed reserved_tool_calls")
        return self


class ResearchTaskContext(ContextBudgetModel):
    """The only parent-derived context exposed to a researcher branch."""

    task: ScopedResearchTask
    available_tools: list[dict[str, JsonValue]] = Field(max_length=2)
    remaining_branch_tool_calls: int = Field(ge=0)
    context_projection: ProjectionMetadata | None = None


@runtime_checkable
class ResearcherSubagent(Protocol):
    async def research(self, context: ResearchTaskContext) -> BranchResult:
        """Run one already-bounded branch context."""


@runtime_checkable
class BranchActionSelector(Protocol):
    async def select_action(self, context: object) -> AgentAction:
        """Select one action inside a bounded researcher branch."""


def build_research_task_context(
    task: ScopedResearchTask,
    registry: ToolRegistry,
    context_budget: ContextBudget,
) -> ResearchTaskContext:
    """Build a narrow Day 11 projection without copying parent raw state."""

    context = ResearchTaskContext(
        task=task,
        available_tools=cast(list[dict[str, JsonValue]], registry.definitions()),
        remaining_branch_tool_calls=task.tool_call_budget,
    )
    return cast(
        ResearchTaskContext,
        fit_context_to_budget(context, context_budget, ProjectionMetadata()),
    )


class BoundedResearcherSubagent:
    """Single-depth researcher limited to the supplied web-tool registry."""

    def __init__(
        self,
        action_selector: BranchActionSelector,
        registry: ToolRegistry,
        *,
        context_budget: ContextBudget | None = None,
    ) -> None:
        if not isinstance(action_selector, BranchActionSelector):
            raise TypeError("action_selector must satisfy BranchActionSelector")

        invalid_tools = set(registry.names()) - {"web_search", "web_fetch"}
        if invalid_tools or not registry.names():
            raise ValueError("researcher registry may contain only web tools")

        self._action_selector = action_selector
        self._registry = registry
        self._runner = ToolRunner(registry)
        self._context_budget = context_budget or ContextBudget()

    async def research(self, context: ResearchTaskContext) -> BranchResult:
        from mini_deerflow.decision import ActionContext

        task = context.task
        observations: list[ToolObservation] = []
        evidence: list[EvidenceRecord] = []

        for call_number in range(1, task.tool_call_budget + 1):
            projected_observations, observation_metadata = project_observations(
                observations,
                self._context_budget,
            )
            projected_evidence, evidence_metadata = project_evidence_records(
                evidence,
                self._context_budget,
            )
            selector_context = ActionContext(
                goal=task.objective,
                step=PlanStep(
                    step_number=1,
                    title=f"Delegated branch {task.branch_id}",
                    objective=task.objective,
                    success_criteria=task.success_criteria,
                ),
                available_tools=cast(
                    list[dict[str, JsonValue]], self._registry.definitions()
                ),
                observations=projected_observations,
                evidence=projected_evidence,
                remaining_step_tool_calls=task.tool_call_budget - len(observations),
                remaining_total_tool_calls=task.tool_call_budget - len(observations),
            )
            selector_context = cast(
                ActionContext,
                fit_context_to_budget(
                    selector_context,
                    self._context_budget,
                    merge_metadata(observation_metadata, evidence_metadata),
                ),
            )
            action = await self._action_selector.select_action(selector_context)

            if isinstance(action, CompleteStepAction):
                citations, rejected = validate_citations(action.sources, evidence)
                if rejected:
                    return BranchResult(
                        branch_id=task.branch_id,
                        status="controlled_failure",
                        observations=observations,
                        error="Branch proposed citations absent from successful evidence.",
                        tool_calls_used=len(observations),
                    )
                return BranchResult(
                    branch_id=task.branch_id,
                    status="success",
                    observations=observations,
                    finding=BranchFinding(
                        summary=sanitize_finding_summary(action.summary),
                        citations=citations,
                    ),
                    tool_calls_used=len(observations),
                )

            if not isinstance(action, ToolCallAction):
                raise TypeError("researcher selector returned an invalid action")

            if action.tool_name == "delegate_research":
                return BranchResult(
                    branch_id=task.branch_id,
                    status="controlled_failure",
                    observations=observations,
                    error="Nested delegation is not allowed.",
                    tool_calls_used=len(observations),
                )

            result = await self._runner.run(action.tool_name, action.arguments)
            observation = ToolObservation(
                step_number=1,
                step_tool_call_number=call_number,
                total_tool_call_number=call_number,
                action=action,
                result=result,
                branch_id=task.branch_id,
                branch_tool_call_number=call_number,
            )
            observations.append(observation)
            evidence = merge_evidence_records(
                evidence,
                extract_evidence_records(observation),
            )

        return BranchResult(
            branch_id=task.branch_id,
            status="controlled_failure",
            observations=observations,
            error="Branch tool-call budget was exhausted before completion.",
            tool_calls_used=len(observations),
        )


def deterministic_fan_in(
    results: Sequence[BranchResult],
    *,
    parent_step_number: int,
) -> FanInSummary:
    """Merge branch data by branch id and revalidate every citation."""

    ordered = sorted(results, key=lambda result: result.branch_id)
    evidence: list[EvidenceRecord] = []

    for result in ordered:
        for observation in result.observations:
            evidence = merge_evidence_records(
                evidence,
                extract_evidence_records(observation),
            )

    findings: list[StepFinding] = []
    citations: list[str] = []
    limitations: list[str] = []

    for result in ordered:
        if result.status == "success" and result.finding is not None:
            accepted, rejected = validate_citations(result.finding.citations, evidence)
            findings.append(
                StepFinding(
                    step_number=parent_step_number,
                    summary=sanitize_finding_summary(result.finding.summary),
                    citations=accepted,
                    branch_id=result.branch_id,
                )
            )
            citations.extend(source for source in accepted if source not in citations)
            if rejected:
                limitations.append(
                    f"Branch {result.branch_id} had {rejected} unsupported citation(s)."
                )
        else:
            limitations.append(f"Branch {result.branch_id}: {result.error}")

    return FanInSummary(
        successful_branches=[r.branch_id for r in ordered if r.status == "success"],
        failed_branches=[
            r.branch_id for r in ordered if r.status == "controlled_failure"
        ],
        cancelled_branches=[r.branch_id for r in ordered if r.status == "cancelled"],
        findings=findings,
        evidence=evidence,
        citations=citations,
        limitations=limitations,
    )


class DelegateResearchTool:
    """Parent-only orchestration tool for one bounded delegation wave.

    The returned record becomes durable with the parent tool-node checkpoint,
    so a completed node is not dispatched again on resume. This is deliberately
    not an exactly-once claim for an interruption after an external side effect
    but before that node checkpoint is written.
    """

    name = "delegate_research"
    description = (
        "Delegate 2-3 independent, narrowly scoped web-research tasks. "
        "Branches cannot delegate, write files, or create artifacts."
    )
    input_model = DelegationInput
    idempotent = False

    def __init__(
        self,
        researcher: ResearcherSubagent,
        branch_registry: ToolRegistry,
        *,
        max_concurrency: int = DEFAULT_DELEGATION_CONCURRENCY,
        branch_timeout_seconds: float = DEFAULT_BRANCH_TIMEOUT_SECONDS,
        context_budget: ContextBudget | None = None,
    ) -> None:
        if not isinstance(researcher, ResearcherSubagent):
            raise TypeError("researcher must satisfy ResearcherSubagent")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int):
            raise TypeError("max_concurrency must be an integer")
        if max_concurrency < 1 or max_concurrency > MAX_DELEGATION_BRANCHES:
            raise ValueError("max_concurrency must be between 1 and 3")
        if (
            isinstance(branch_timeout_seconds, bool)
            or not isinstance(branch_timeout_seconds, int | float)
            or not math.isfinite(branch_timeout_seconds)
            or branch_timeout_seconds <= 0
        ):
            raise ValueError("branch_timeout_seconds must be positive")

        invalid_tools = set(branch_registry.names()) - {"web_search", "web_fetch"}
        if invalid_tools or not branch_registry.names():
            raise ValueError("branch_registry may contain only web tools")

        self._researcher = researcher
        self._branch_registry = branch_registry
        self._max_concurrency = max_concurrency
        self._branch_timeout_seconds = float(branch_timeout_seconds)
        self._context_budget = context_budget or ContextBudget()
        self.timeout_seconds = self._branch_timeout_seconds * MAX_DELEGATION_BRANCHES

    async def run(self, tool_input: ToolInput) -> ToolResult:
        del tool_input
        return ToolResult.fail(
            error="Delegation requires parent budget admission.",
            metadata={"error_type": "DelegationAdmissionError"},
        )

    async def run_with_parent_budget(
        self,
        arguments: dict[str, JsonValue],
        *,
        parent_step_number: int,
        remaining_tool_calls: int,
    ) -> ToolResult:
        """Validate, reserve, dispatch, and deterministically merge one wave."""

        try:
            request = self.input_model.model_validate(arguments)
        except ValidationError as error:
            return ToolResult.fail(
                error="Delegation input is invalid.",
                metadata={
                    "error_type": "ValidationError",
                    "validation_error_count": error.error_count(),
                    "delegated_tool_calls": 0,
                },
            )

        ordered_tasks = sorted(request.tasks, key=lambda task: task.branch_id)
        reserved = sum(task.tool_call_budget for task in ordered_tasks)

        if reserved > max(0, remaining_tool_calls - 1):
            return ToolResult.fail(
                error="Delegation cannot reserve branch calls within parent budgets.",
                metadata={
                    "error_type": "DelegationBudgetExceededError",
                    "delegated_tool_calls": 0,
                    "reserved_tool_calls": reserved,
                },
            )

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def dispatch(task: ScopedResearchTask) -> BranchResult:
            try:
                context = build_research_task_context(
                    task,
                    self._branch_registry,
                    self._context_budget,
                )
                async with semaphore:
                    result = await asyncio.wait_for(
                        self._researcher.research(context),
                        timeout=self._branch_timeout_seconds,
                    )
                if (
                    not isinstance(result, BranchResult)
                    or result.branch_id != task.branch_id
                    or result.tool_calls_used > task.tool_call_budget
                ):
                    return BranchResult(
                        branch_id=task.branch_id,
                        status="cancelled",
                        error="Branch returned an invalid or over-budget result.",
                    )
                return result
            except TimeoutError:
                return BranchResult(
                    branch_id=task.branch_id,
                    status="cancelled",
                    error="Branch timed out before producing a checkpointable result.",
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - normalize the subagent seam
                return BranchResult(
                    branch_id=task.branch_id,
                    status="controlled_failure",
                    error="Branch failed before producing a valid result.",
                )

        results = list(
            await asyncio.gather(*(dispatch(task) for task in ordered_tasks))
        )

        used = sum(result.tool_calls_used for result in results)
        charged = sum(
            task.tool_call_budget
            if result.status == "cancelled"
            else result.tool_calls_used
            for task, result in zip(ordered_tasks, results, strict=True)
        )
        fan_in = deterministic_fan_in(results, parent_step_number=parent_step_number)
        record = DelegationRecord(
            delegation_id=request.delegation_id,
            parent_step_number=parent_step_number,
            tasks=ordered_tasks,
            results=results,
            fan_in=fan_in,
            reserved_tool_calls=reserved,
            used_tool_calls=used,
            charged_tool_calls=charged,
        )
        return ToolResult.ok(
            data={"delegation": record.model_dump(mode="json")},
            metadata={
                "delegated_tool_calls": charged,
                "reserved_tool_calls": reserved,
                "branch_count": len(results),
            },
        )


def parse_delegation_record(result: ToolResult) -> DelegationRecord | None:
    """Parse only the typed record shape emitted by the delegation tool."""

    if not result.success or not isinstance(result.data, dict):
        return None
    payload = result.data.get("delegation")
    if not isinstance(payload, dict):
        return None
    try:
        return DelegationRecord.model_validate(payload)
    except ValidationError:
        return None
