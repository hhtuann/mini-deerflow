# Structured Execution Events

## Scope

Mini DeerFlow exposes one typed observability stream through `ExecutionEvent` and
`ExecutionTracer`. `ExecutionTrace` remains a compatibility alias for existing CLI and
demo consumers; it is not a second event pipeline. The runtime, canonical LangGraph
workflow, and bounded delegation implementation emit into the same best-effort sink.

This layer records execution topology only. It does not contain a graph projection,
visualization, or UI dependency.

## Execution hierarchy

```text
Main run
├── node executions
├── parent tool executions
└── delegation
    ├── branch run A
    │   ├── child tool execution
    │   └── evidence produced
    ├── branch run B
    │   ├── child tool execution
    │   └── evidence produced
    └── deterministic fan-in
```

`run_started` and `run_completed`/`run_failed` bound one runtime invocation. Canonical
workflow nodes emit start and terminal events. Conditional edges emit `route_selected`.
Parent tool calls emit one start and one terminal event around the existing invocation.

An admitted delegation emits `delegation_started`, concurrent branch lifecycles, child
tool lifecycles, `fan_in_completed`, and `delegation_completed`. Branch event order is
defined only within each branch; events from independent branches may interleave.

## Identity model

- `event_id` uniquely identifies one event.
- `root_run_id` identifies the main runtime invocation and groups its complete event
  stream.
- `run_id` is the main run identity for parent events and a deterministic child-run
  identity for branch events.
- `parent_run_id` is `None` for the main run and the parent run identity for a branch.
- `thread_id` and optional `turn_id` retain the public runtime correlation.
- `node_id` identifies the canonical workflow node or delegated researcher boundary.
- `delegation_id` reuses the validated delegation request identity.
- `branch_id` reuses the validated scoped-task identity.
- `tool_call_id` is derived once from the owning run and existing call number.

The event stream therefore reconstructs `delegation -> branch run -> child tool` without
duplicating delegation, branch, or tool-call identities. `EvidenceProvenance` continues
to carry `delegation_id`, `branch_id`, and `branch_tool_call_number`, linking promoted
evidence to the same execution hierarchy.

## Event and metadata safety

Events use a closed Pydantic schema. Metadata is projected through a shared scalar
allowlist, is capped at 24 fields, and truncates text at 240 characters. Sensitive or
unknown keys are omitted, and secret-like text is redacted. Events never project full
prompts, conversations, provider requests or responses, raw tool output, uploaded file
content, authorization values, API credentials, exception messages, tracebacks, complete
agent state, or evidence collections.

Counts, stable identifiers, closed decision categories, durations, and bounded budget
figures are permitted. Wall-clock timestamps are timezone-aware UTC. Durations use a
monotonic clock at the workflow boundary.

## Failure policy

Execution-event delivery is best-effort. `ExecutionTracer` catches only sink exceptions,
keeps the already-built safe event in its in-memory run history, logs the sink category,
and lets business execution continue. Business exceptions are not swallowed: node and
run failure events are emitted, then the original exception is re-raised.

Controlled tool and branch failures remain controlled business outcomes. Partial branch
failure produces `branch_failed` plus a partial `delegation_completed`; it does not turn
the parent run into `run_failed` when deterministic fan-in can continue. Timeout and
cancellation remain distinct branch statuses.

## Checkpoint and resume semantics

Execution events do not invoke tools and are not part of budget accounting. LangGraph
checkpoints remain the authority for completed business work, so resume does not replay
checkpointed tools, delegations, evidence promotion, or charges. A resume operation has
its own run identity and emits events only for lifecycle boundaries it actually re-enters.

The sink is not a durable exactly-once event store. A process failure can lose emitted
events, and a re-entered boundary can emit another event even when external delivery of a
prior event is unknown. The guarantee is non-interference with the existing checkpointed
business execution contract, not exactly-once observability.

WAVE 2 will build ExecutionGraphProjection from ExecutionEvents.
