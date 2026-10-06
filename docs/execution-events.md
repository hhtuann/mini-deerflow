# Structured Execution Events

## Purpose and scope

WAVE 1 provides a typed execution-event contract for reconstructing Mini DeerFlow's
runtime topology without parsing log messages. The stream makes parent runs, canonical
workflow nodes, parent tools, delegated branch/sub-agent runs, child tools, evidence,
failures, and deterministic fan-in structurally observable.

This contract is the data foundation for **WAVE 2 — Agent Graph UI**. WAVE 1 contains
no graph projection, visualization, node inspector, or UI dependency.

## Architecture

WAVE 1 evolved the existing tracing pipeline instead of introducing a second event bus.
There is one event model and one sink path:

```text
runtime / canonical workflow / bounded delegation
                    |
             ExecutionTracer
                    |
                TraceSink
                    |
       Null / in-memory / JSON Lines sink
```

The concrete types in `src/mini_deerflow/tracing.py` are:

- `ExecutionEvent`: the immutable, closed Pydantic event model and source of truth.
- `ExecutionEventType`: the closed event taxonomy.
- `ExecutionStatus`: normalized lifecycle status.
- `ExecutionTrace`: a compatibility alias to `ExecutionEvent`, not a separate model or
  pipeline.
- `ExecutionTracer`: binds run context, creates identities, assigns sequence numbers,
  maps legacy trace fields to typed events, and delivers events to a sink.
- `TraceSink`: synchronous protocol accepting an already-safe `ExecutionEvent`.
- `NullTraceSink`: intentionally discards events.
- `InMemoryTraceSink`: retains events in order for tests and in-process consumers.
- `JsonLinesTraceSink`: writes one serialized event per line to a caller-owned stream.

Existing `TraceKind`, `TracePhase`, and `TraceOutcome` fields remain on the model as a
compatibility projection for current CLI and demo consumers. New code can use
`event_type` and `status` directly.

Event delivery is best-effort. `ExecutionTracer` first retains the safe event in its
in-memory run history, then invokes the configured sink. If that sink raises,
`ExecutionTracer` logs only the exception class and lets business execution continue.
This suppression applies only at the sink boundary; application exceptions are not
silently swallowed.

## Execution hierarchy

```text
Main Run
|-- Node executions
|-- Parent tool executions
`-- Delegation
    |-- Branch / Sub-agent A
    |   |-- Child tool execution
    |   `-- Evidence
    |-- Branch / Sub-agent B
    |   |-- Child tool execution
    |   `-- Evidence
    `-- Deterministic fan-in
        `-- Delegation completion
            `-- Parent workflow continues
```

The branch runs are the real bounded researcher sub-agents implemented by
`BoundedResearcherSubagent`. They select bounded actions and invoke tools through the
branch `ToolRegistry`; they are not simulated delegation messages.

`run_started` and `run_completed` or `run_failed` bound one public runtime invocation.
Canonical LangGraph nodes produce node lifecycle events. Parent tools are wrapped once.
An admitted delegation produces concurrent branch lifecycles, child-tool events,
evidence events, deterministic fan-in, and delegation completion before the parent
continues.

## Event taxonomy

Every current `ExecutionEventType` is listed below.

| Event | Emitted by | Meaning and important fields |
| --- | --- | --- |
| `run_started` | `ExecutionTracer.run_scope` | A public `run`, `continue`, or `resume` invocation began. Carries run/thread/turn correlation and `operation`. |
| `run_completed` | `ExecutionTracer.run_scope` | The parent invocation completed successfully. Carries `duration_ms`. |
| `run_failed` | `ExecutionTracer.run_scope` | The parent invocation raised. Carries bounded `error_category`, `error_code`, and duration. |
| `node_started` | `ExecutionTracer.wrap_node` | A canonical workflow node entered. Carries `node_id` and safe state counters when available. |
| `node_completed` | `ExecutionTracer.wrap_node` | A canonical node returned successfully. Carries duration and updated counters. |
| `node_failed` | `ExecutionTracer.wrap_node` | A canonical node raised. The original exception is re-raised after the event. |
| `route_selected` | `ExecutionTracer.wrap_route` | A conditional graph edge was selected. Metadata contains safe `from_node`, `to_node`, `decision_type`, and `route_reason` values. |
| `tool_started` | `agent_workflow.py` and `BoundedResearcherSubagent` | An existing parent or child tool invocation began. Carries tool, call, node, and owning-run identities. |
| `tool_completed` | Parent and child tool wrappers | A tool returned a successful `ToolResult`. May carry duration and evidence count. |
| `tool_failed` | Parent and child tool wrappers | A tool returned a controlled failure or its invocation was cancelled/raised. Carries safe failure classification only. |
| `delegation_started` | `DelegateResearchTool.run_with_parent_budget` | A validated delegation passed budget admission. Carries delegation identity, branch count, and reservation metadata. |
| `delegation_completed` | `DelegateResearchTool.run_with_parent_budget` | Fan-in finished and the delegation produced its final aggregate outcome. Carries success/failure/cancellation counts and budget/evidence totals. |
| `branch_started` | Delegation `dispatch` | A bounded child run entered its branch context. Carries parent, delegation, branch, and child-run correlation. |
| `branch_completed` | Delegation `dispatch` | A branch returned a successful `BranchResult`. Carries used calls and evidence count. |
| `branch_failed` | Delegation `dispatch` | A branch had controlled failure, timeout, cancellation, invalid output, or normalized unexpected failure. |
| `evidence_produced` | `BoundedResearcherSubagent.research` | A child tool observation yielded one or more evidence records. Carries count and child-tool correlation, not evidence payloads. |
| `fan_in_completed` | Immediately after `deterministic_fan_in` | Ordered branch results were merged and citations revalidated. Carries aggregate branch, budget, evidence, citation, and limitation counts. |
| `checkpoint_observed` | Runtime checkpoint trace sites | A checkpoint lifecycle outcome such as ready or resumed was observed. It does not persist the event itself. |
| `context_projected` | Context projection in `agent_workflow.py` | Model-facing context was within budget, compacted, or refused. Carries bounded counts, never full context. |
| `review_completed` | Review node in `agent_workflow.py` | A reviewer returned continue, replan, or finish. Carries closed verdict/outcome values. |
| `replan_completed` | Replan node in `agent_workflow.py` | Replacement planning completed with bounded plan/budget counters. |
| `citation_validated` | Citation processing in `agent_workflow.py` | Citation acceptance or rejection was recorded using counts only. |
| `artifact_completed` | Synthesis/artifact handling in `agent_workflow.py` | Artifact writing completed or was not requested. Carries count and safe outcome values. |

## Correlation and identity model

The principal correlation fields are top-level `ExecutionEvent` fields rather than
free-form metadata:

- `event_id`: unique event identity generated once per event.
- `root_run_id`: identity of the public parent invocation; groups its parent and branch
  events.
- `run_id`: current execution identity. It is the parent run for parent events and a
  deterministic child identity for branch events.
- `parent_run_id`: `None` for the main run; the owning parent run for a branch.
- `thread_id`: public conversation/thread correlation.
- `turn_id`: optional turn correlation inherited by branch events.
- `node_id`: canonical workflow node or `delegated_researcher` boundary.
- `delegation_id`: the existing validated delegation-request identity.
- `branch_id`: the existing validated scoped-task identity.
- `tool_call_id`: deterministic identity derived from the owning run and existing call
  number.
- `sequence`: a root-run-wide increasing sequence shared by the parent and its branch
  contexts.

Parent events use:

```text
root_run_id = parent-run
run_id = parent-run
parent_run_id = None
```

Branch/sub-agent events use the same root but a child run:

```text
root_run_id = parent-run
run_id = branch:parent-run:wave-events:alpha
parent_run_id = parent-run
delegation_id = wave-events
branch_id = alpha
```

A child tool within that branch uses:

```text
tool_call_id = tool:branch:parent-run:wave-events:alpha:1
run_id = branch:parent-run:wave-events:alpha
delegation_id = wave-events
branch_id = alpha
```

Derived identifiers are bounded to the model's 128-character identifier limit. When the
readable composition would exceed that limit, the tracer uses a deterministic hash-based
identifier. WAVE 2 can reconstruct parent/child, delegation/branch, and branch/tool
relationships directly from these fields without parsing log text.

## Canonical workflow instrumentation

The production graph in `src/mini_deerflow/agent_workflow.py` wraps these meaningful
nodes:

- `planner`
- `decide_action`
- `execute_tool`
- `complete_step`
- `budget_exhausted`
- `synthesize`
- `review`, when a reviewer is configured
- `replan`, when replanning is configured

Each wrapped node emits `node_started`, followed by exactly one `node_completed` or
`node_failed`. The wrapper records duration with a monotonic clock and preserves the
handler's existing state update and exception behavior.

Structured `route_selected` events cover conditional transitions from `decide_action`,
`review`, `complete_step`, and `budget_exhausted`. A safe review route can be represented
as:

```text
from_node=review
to_node=replan
decision_type=review_verdict
route_reason=reviewer_verdict
review_verdict=replan
```

Only closed decision categories and destinations are emitted. Raw model reasoning and
chain-of-thought are never included.

## Tool lifecycle

Parent and child tools use the same lifecycle:

```text
tool_started
    -> existing tool invocation
    -> tool_completed | tool_failed
```

Depending on the boundary, useful fields include `tool_name`, `tool_call_id`,
`current_step`, step and total call numbers, `duration_ms`, `success`,
`evidence_count`, `error_category`, and `error_code`. Child events inherit the branch
run, delegation, and branch identities from the active branch context.

Instrumentation never invokes a tool a second time. The events wrap the existing call
and observe its existing `ToolResult` or exception.

## Delegation and real sub-agent proof

The production delegation seam is:

```text
Parent
  -> DelegateResearchTool
  -> BoundedResearcherSubagent
  -> ToolRegistry
  -> child research tool
  -> ToolObservation
  -> BranchResult
  -> deterministic_fan_in
  -> Parent
```

The integration test uses the synthetic IDs below and observes this representative
sequence:

```text
run_started(run_id=parent-run)
node_started(node_id=execute_tool)
tool_started(tool_name=delegate_research)

delegation_started(delegation_id=wave-events)

branch_started(branch_id=alpha,
               run_id=branch:parent-run:wave-events:alpha,
               parent_run_id=parent-run)
branch_started(branch_id=beta,
               run_id=branch:parent-run:wave-events:beta,
               parent_run_id=parent-run)

tool_started(tool_name=web_search, branch_id=alpha)
tool_started(tool_name=web_search, branch_id=beta)

tool_completed(...)
evidence_produced(...)
branch_completed(...)

fan_in_completed(delegation_id=wave-events, evidence_promoted=2)
delegation_completed(delegation_id=wave-events, successful_branches=2)

tool_completed(tool_name=delegate_research)
node_completed(node_id=execute_tool)

node_started(node_id=decide_action)
```

This proves that delegation creates distinct child run contexts, each child selects and
executes a registered research tool, evidence is extracted from real tool observations,
fan-in occurs after branch terminal events, and the parent resumes afterward. A simulated
delegation message could not satisfy the child-tool, evidence-provenance, barrier, and
parent-continuation assertions in `tests/test_execution_events.py`.

## Evidence correlation

The existing evidence architecture remains authoritative. `EvidenceRecord` provenance
continues to carry:

```text
delegation_id
branch_id
branch_tool_call_number
```

Child tool and `evidence_produced` events expose compatible delegation, branch, and tool
identities. The event contains an evidence count, not a copied evidence record or evidence
array. WAVE 1 introduced no new evidence store, evidence model, or citation subsystem.

## Safe metadata model

`project_execution_metadata` applies one shared bounded projection:

- At most 24 metadata entries are retained.
- Values must be scalar: string, integer, float, boolean, or `None`.
- String values are stripped and truncated to 240 characters.
- Unknown keys are dropped.
- Keys matching `authorization`, `bearer`, `api_key`, `token`, `password`, `secret`, or
  `credential` patterns are dropped.
- String values containing those secret-like patterns become `[redacted]`.

The current metadata allowlist is:

```text
from_node                 to_node
decision_type             route_reason
review_verdict            step_number
tool_call_number          success
failure_category          requested_branches
successful_branches       failed_branches
cancelled_branches        budget_reserved
budget_used               budget_charged
budget_remaining          evidence_count
citation_count            source_count
evidence_promoted         limitations_produced
```

The closed event model separately validates identifiers, node/tool names, counters,
durations, counts, and error enums.

Events do not contain full prompts, full conversations, provider requests or responses,
raw tool output, uploaded file contents, API keys, authorization headers, full
`AgentState`, full evidence arrays, unsafe exception messages, or secret-bearing
tracebacks.

## Failure semantics

### Tool failure

An unsuccessful `ToolResult` produces `tool_failed` with `status=failed` and safe
category/code fields. Existing workflow behavior decides whether the failure is
recoverable. An unhandled parent tool exception also emits `tool_failed` and is re-raised.

### Controlled branch failure

A controlled unsuccessful branch produces `branch_failed` with `status=failed` and
`failure_category=controlled_failure`. Successful sibling branches remain eligible for
fan-in.

### Timeout

`asyncio.wait_for` cancellation causes an active child tool to emit
`tool_failed` with `status=cancelled`. The enclosing branch then emits `branch_failed`
with `status=cancelled` and `failure_category=timeout`.

### Cancellation

Direct branch cancellation emits `branch_failed` with `status=cancelled` and
`failure_category=cancelled`, then re-raises the cancellation. This remains distinct from
a timeout. Invalid/over-budget branch output uses `invalid_result`; a normalized
unexpected researcher failure uses `unexpected_failure`.

### Partial delegation failure

Successful branches still participate in deterministic fan-in. If any branch failed or
was cancelled, `delegation_completed` has `status=partial` and aggregate branch counts.
The parent may continue according to the existing business contract; partial branch
failure alone does not become `run_failed`.

### Parent failure

A business exception in a canonical node produces:

```text
node_failed
    -> run_failed
```

The original exception is re-raised and is not hidden by observability.

### Event sink failure

Sink failure is best-effort and does not affect business execution. The safe event remains
available through the tracer's in-memory run history, and only the sink failure class is
logged.

## Concurrency

Delegation retains the existing `asyncio.gather` fan-out and `asyncio.Semaphore`
concurrency cap. Instrumentation adds branch-local context but no sequential execution
loop.

Global event order across concurrent branches intentionally follows scheduling and is not
fixed. This is valid:

```text
branch_started A
branch_started B
tool_started B
tool_started A
tool_completed A
tool_completed B
```

Ordering within a branch remains meaningful, and every event receives a unique increasing
root-run sequence number. The concurrency test uses a synchronization barrier: both child
tools must reach the barrier before either is released. That test would fail if branch
instrumentation serialized execution.

## Budget semantics

Execution events are observational only. They do not allocate, reserve, transfer, refund,
or charge tool calls. Existing WAVE 0 behavior remains authoritative for:

- Per-branch tool-call budgets.
- Parent admission and remaining-call checks.
- Delegation reservation.
- Fair-share allocation.
- Actual and conservative cancellation charging.
- Unused-budget behavior.

Events expose only bounded snapshots such as reserved, used, charged, and remaining
counts after the existing business logic computes them.

## Resume and checkpoint semantics

LangGraph checkpoints and the runtime persistence layer remain authoritative for business
execution. Existing resume behavior ensures:

- Completed tools are not re-invoked.
- Completed delegations are not replayed.
- Evidence is not promoted twice.
- Budgets are not charged twice.
- Lifecycle events are emitted only for execution boundaries actually re-entered.

A public resume operation receives its own root run identity. Branch events, when any are
actually re-entered, group under that invocation's `root_run_id`.

**Execution-event delivery is not guaranteed to be durable or exactly-once.** A process
failure can lose sink delivery, and a re-entered boundary can emit another event when the
delivery state of a prior event is unknown. The guarantee is non-interference with
checkpointed business execution, not exactly-once observability.

## Verification

Accepted WAVE 1 verification results:

| Check | Result |
| --- | --- |
| Full test suite | 633 passed, 2 skipped |
| Evaluations | 9/9 cases passed |
| Evaluation invariants | 36/36 passed |
| Ruff lint | Passed |
| Ruff format check | Passed |
| `compileall` | Passed |
| Lockfile check | Passed |
| Targeted changed-module typing | Passed |
| GitNexus change/security checks | Passed |

The repository currently has no official full-project type-check command or CI type-check
gate. Targeted checks for the changed production modules passed. An ad hoc full-project
mypy audit still reports unrelated pre-existing typing debt; that debt is not part of the
WAVE 1 event contract.

Key proof lives in:

- `tests/test_execution_events.py`: schema/safety, sink isolation, hierarchy, real child
  tools, evidence, concurrency, partial failure, timeout/cancellation, and parent failure.
- `tests/test_tracing.py`: typed redacted trace compatibility and existing lifecycle
  outcomes.
- `tests/test_demo_offline.py`: durable run identity and resume behavior.
- `tests/test_final_acceptance.py`: interruption/resume, no replay, event ordering, and
  sensitive-data canaries.

## Scope boundary

This implementation does not contain:

- Graph visualization or Agent Graph UI.
- React Flow, Cytoscape, Mermaid live graph rendering, or node inspector UI.
- No WAVE 1 Workspace feature and no ContextSnapshot or ArtifactVersion implementation.
- SkillRegistry, SlideSkill, source ingestion, or Notebook UI.
- SandboxProvider changes or multi-user authentication.
- A replacement evidence or citation architecture.

WAVE 2 will build ExecutionGraphProjection from ExecutionEvents.
