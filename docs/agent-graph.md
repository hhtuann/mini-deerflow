# Agent Graph UI

## Purpose

WAVE 2 makes the canonical workflow and observed execution visible in Streamlit.
The **Workflow** view shows where the canonical workflow may route. The **Current run**
view shows what a concrete observed run actually executed, including delegated
sub-agents, their tool calls, partial failure, and evidence fan-in.

The graph is a read-only observability consumer. Runtime events remain the source of
truth; graph projection and rendering do not participate in agent decisions.
The underlying contracts are documented in [Structured Execution Events](execution-events.md)
and the [WAVE 0 architecture baseline](architecture-baseline.md).

## Architecture

```text
Canonical runtime / workflow / bounded research delegation
                         |
                  ExecutionTracer
                         |
                  ExecutionEvent[]
                         |
             project_execution_graph()
                         |
             ExecutionGraphProjection
                         |
       DemoRunView / DemoJobManager live snapshot
                         |
                render_agent_graph()
                         |
                 Streamlit / ECharts
```

[`graph_projection.py`](../src/mini_deerflow/demo/graph_projection.py) converts the
closed event contract into immutable, UI-ready graph semantics. The renderer in
[`components.py`](../src/mini_deerflow/demo/components.py) receives the projection,
so it neither parses console logs nor interprets the complete raw event taxonomy.
`project_static_workflow()` provides the separate possible-topology view.

## Static Workflow Topology

The static graph describes the reviewer-enabled canonical workflow in
[`agent_workflow.py`](../src/mini_deerflow/agent_workflow.py):

```text
__start__ -> planner -> decide_action
decide_action -> execute_tool | complete_step | budget_exhausted
execute_tool -> decide_action
complete_step -> review
review -> decide_action (continue) | replan (replan) | synthesize (finish)
replan -> decide_action
budget_exhausted -> decide_action (continue) | review | synthesize (finish)
synthesize -> __end__
```

These are possible control-flow routes, not an execution trace. Static nodes have
`pending` status and possible edges are unselected. Display labels such as
"Action selection" and "Synthesis" map to the canonical node IDs above.

Production projection keeps explicit `_WORKFLOW_LABELS` and `_STATIC_EDGE_SPECS`.
It does not automatically extract topology on each render. The regression test
`test_static_projection_matches_compiled_canonical_workflow` compiles the real
reviewer/replanner-enabled LangGraph, compares exact node and edge-pair equality,
and checks the three review-route labels. This guards the explicit specification
against topology drift. Optional reviewer-free configurations are not separate
static-view variants.

## Runtime Execution Graph

`project_execution_graph(events)` reconstructs only observed entities. An empty event
stream produces an empty runtime projection; static topology remains available.
Lifecycle events collapse into one logical entity:

- Node start/completion/failure events merge by owning run and canonical node ID.
- Tool start/completion/failure and child evidence events merge by tool-call identity.
- Branch start/completion/failure events merge by child-run identity.
- Run and delegation lifecycle events merge by their correlated identities.
- An explicit `FAN_IN_COMPLETED` event creates the fan-in entity.

Repeated visits to a workflow node within one run share one graph node. Its inspector
reports execution count, earliest start, latest terminal timestamp, and the sum of
available terminal durations. Status follows the latest sequence-ordered event for
the entity. The graph summarizes execution; the Trace tab retains the event timeline.

## Graph Projection Model

All projection records are frozen dataclasses with tuple collections:

| Type | Contents |
| --- | --- |
| `ExecutionGraphProjection` | Nodes, edges, and aggregate metrics |
| `ExecutionGraphNode` | ID, kind, label, status, parent ID, timing, safe summary, inspector fields, technical fields |
| `ExecutionGraphEdge` | Source, target, relation, optional label, and selected flag |
| `ExecutionGraphMetrics` | Node, running, completed, failed, tool-call, sub-agent, and evidence counts |
| `GraphField` | An explicitly selected scalar label/value pair |

The displayed Failed metric includes failed and cancelled nodes. Tool calls and
sub-agents count logical tool/branch entities. Evidence observed sums child
`EVIDENCE_PRODUCED` counts and successful non-delegation parent tool evidence counts;
it is not a separate count of unique final citations or promoted evidence.

## Node Kinds

| Kind | Meaning |
| --- | --- |
| `workflow_node` | A possible static node or an observed canonical node within a run |
| `run` | A public runtime invocation bounded by run lifecycle events |
| `tool` | One correlated parent or branch-owned tool invocation |
| `delegation` | One admitted research delegation wave |
| `branch` | One real bounded researcher sub-agent run |
| `fan_in` | The explicit deterministic aggregation outcome |

Evidence is represented through counts and fan-in relationships, not individual
evidence graph nodes. Branch entities represent child execution even when there is
no separate child `RUN_STARTED` event.

## Status Model

| Graph status | Current source/mapping |
| --- | --- |
| `pending` | Static possible workflow nodes |
| `running` | WAVE 1 `ExecutionStatus.STARTED` |
| `completed` | Remaining event statuses, including `COMPLETED` and `PARTIAL` |
| `failed` | `ExecutionStatus.FAILED` |
| `cancelled` | `ExecutionStatus.CANCELLED` |
| `skipped` | Supported by the view model and renderer; no current WAVE 1 event maps to it |

A partial delegation is displayed as `completed` with a safe summary of successful,
failed, and cancelled branches. Its failed branch remains `failed`; the parent may
continue and complete. There is no separate `partial` graph status.

## Runtime Identity and Correlation

Delivery identity and logical entity identity have different roles:

| Event field | Projection use |
| --- | --- |
| `event_id` | Deduplicate repeated delivery of the same event |
| `root_run_id` | Group the public invocation and namespace delegation, branch, tool, and fan-in graph IDs |
| `run_id` | Identify the current parent or child execution and scope workflow/tool ownership |
| `parent_run_id` | Describe child-to-parent run correlation in technical details |
| `delegation_id` | Join delegation, branches, and fan-in within a root run |
| `branch_id` | Label and expose the branch identity; branch graph identity uses its child `run_id` |
| `tool_call_id` | Join one tool lifecycle and its evidence events |
| `sequence` | Root-run-wide ordering for lifecycle precedence and execution relationships |
| `timestamp` | Timing display and a sorting tie-breaker after sequence |

The implemented graph IDs reuse these runtime identities with namespaces:

```text
run:         run_id
workflow:    workflow:{run_id}:{node_id}
delegation:  delegation:{root_run_id}:{delegation_id}
branch:      branch:{root_run_id}:{child_run_id}
tool:        tool:{root_run_id}:{tool_call_id}
fan-in:      fan-in:{root_run_id}:{delegation_id}
```

These are projection keys, not new runtime identifiers. Thread and turn IDs remain
available for technical correlation. The `run:` text above labels the kind; the run
graph key is the unchanged `run_id`.

## Route Visualization

Static view includes all possible review routes: `continue`, `replan`, and `finish`.
Runtime view adds `selected_route` edges from observed `ROUTE_SELECTED` events using
allowlisted `from_node` and `to_node` metadata. Both workflow endpoints must have
been observed in that run before such an edge appears. Labels use the safe route
field, review verdict, or destination name.

Runtime `next` edges also connect sequence-ordered workflow starts. Repeated visits
are collapsed, and identical edges are deduplicated. Runtime observed edges are
selected/solid; static possible edges are unselected/dashed.
No chain-of-thought or hidden model reasoning is displayed. Route labels do not
expose free-form model rationale.

## Delegation and Sub-agent Visualization

```text
Main run
  -> parent workflow node
    -> delegate_research tool
      -> Research delegation
         |-> Sub-agent alpha -> branch-owned tool calls
         |-> Sub-agent beta  -> branch-owned tool calls
         `-> Evidence fan-in -> next observed parent workflow node
```

The branches are real [`BoundedResearcherSubagent`](../src/mini_deerflow/delegation.py)
executions. Child tools attach to the branch matched by `run_id`, independent of
interleaving. Parent tools attach to the most recent workflow start in their run
whose sequence precedes the tool start, falling back to the run when none exists.
The delegation attaches to the containing `delegate_research` tool sequence interval.
Branches and fan-in join to their delegation by `(root_run_id, delegation_id)`.

Tool names depend on runtime composition. In
[`create_default_agent_runtime`](../src/mini_deerflow/runtime.py), the normal live
runtime registers `wiki_search` and `wiki_lookup` for both parent and branch research.
Explicitly injecting a `web_provider` selects the compatibility tools `web_search`
and `web_fetch`; the deterministic offline walkthrough uses this path. The graph
shows the observed tool names and does not substitute conceptual names.

## Fan-in and Parent Continuation

The fan-in node corresponds to WAVE 1's explicit `FAN_IN_COMPLETED` event, emitted
after `deterministic_fan_in`. Each matching branch connects through a
`returns_evidence` edge, including a failed branch whose result contributes a
limitation rather than promoted findings. The relationship does not imply every
branch supplied evidence. Without observed branches, delegation connects directly
to fan-in.

The inspector exposes successful/failed/cancelled branch counts, evidence promoted,
and limitations produced when available. A `parent_continues` edge connects fan-in
to the next observed parent `NODE_STARTED` after its sequence. Aggregation, evidence
promotion, citation revalidation, and failed-branch exclusion remain runtime policy.

## Node Inspector

The **Inspect node** selector synchronizes chart highlighting and inspector content.
Common fields are label, normalized status, optional safe summary, available start
and completion timestamps, duration in milliseconds, and Type.

| Kind | Additional safe fields when available |
| --- | --- |
| Workflow node | Executions |
| Branch | Tool calls (`actual / reserved` when `budget_reserved` exists), Evidence produced |
| Delegation | Requested, successful, failed, cancelled branches; reserved, used, charged tool calls; evidence |
| Fan-in | Successful, failed, cancelled branches; Evidence promoted; Limitations produced |
| Tool | Tool name, step, Evidence produced, safe failure category/code |
| Run | Operation (`run`, `continue`, or `resume`) |

Branch actual tool usage counts distinct observed tool-call IDs; reserved usage
comes from safe branch metadata. It is separate from the delegation's charged budget.

A collapsed **Technical IDs** expander contains Graph ID, Run ID, and available
Parent graph ID, Root run ID, Parent run ID, Thread ID, Turn ID, Node ID, Delegation ID,
Branch ID, and Tool call ID. Static nodes expose their canonical Node ID there.

## Safety Boundary

The graph consumes the existing WAVE 1 closed `ExecutionEvent` schema and
`project_execution_metadata()` allowlist/sanitizer in
[`tracing.py`](../src/mini_deerflow/tracing.py). Metadata is limited to bounded scalar
values under approved keys; secret-like metadata text is redacted. Projection then
selects specific fields and constructs safe summaries. Generic event metadata is
never blindly rendered, and inspector scalar fields use `st.text`.

The graph boundary must exclude raw prompts, chain-of-thought, model completions,
API keys, Authorization headers, Bearer tokens, cookies, credentials, unsafe tool
arguments, full provider responses, uploaded source contents, and raw exception
strings containing secrets. It displays closed failure classifications and counts
instead of provider error bodies or evidence payloads. This relies on the safe event
contract; the graph is not a general-purpose sanitizer for arbitrary objects.

`test_projection_does_not_expose_unallowlisted_or_secret_metadata` injects secret-like
metadata canaries and checks that the projection excludes them. WAVE 1 schema and
redaction tests cover the upstream boundary. These are local-demo safety checks,
not a claim of production frontend security.

## Live Update Architecture

[`QueueTraceSink`](../src/mini_deerflow/demo/jobs.py) retains safe typed events and a
separate projected trace timeline. Its `execution_graph()` copies the event history
under a lock, releases the lock, and calls `project_execution_graph()`.
`DemoJobManager.execution_graph()` exposes this snapshot to the UI.

In [`app.py`](../src/mini_deerflow/demo/app.py), the active-job Streamlit fragment
polls every 0.5 seconds and renders the **Live agent graph** expander through
`render_agent_graph()`. Runtime work runs in the background worker. Rendering does
not serialize delegated branches or mutate workflow decisions. Snapshotting and
projection have a cost; this architecture does not claim zero overhead.

## Completed and Restored Runs

[`project_demo_run()`](../src/mini_deerflow/demo/view_models.py) builds
`DemoRunView.execution_graph` from the turn's typed trace events. Completed turns show
the **Agent graph** tab inside **Agent details**. Restored conversations follow the
same projection/rendering path using safe trace JSON stored in the existing SQLite
conversation-turn ledger. Offline runs use the same path as live runs.

No new graph persistence subsystem is introduced. Historical runs without structured
WAVE 1 events cannot be reconstructed retroactively. Missing events can leave an
incomplete observed graph; checkpoint state alone is not a replacement event stream.

## Concurrency, Out-of-order Events, and Replay

Branch events may interleave, for example `A start, B start, B tool start, A tool start,
A tool end, B tool end`. Ownership comes from correlated run/delegation/tool identities
and sequence semantics, not adjacency in the incoming list.

The projection keeps the first event for each `event_id`, then sorts by
`(root_run_id, sequence, timestamp, event_id)`. Sequence is authoritative within a
root run; timestamp and event ID break ties. Entity lifecycle processing uses sequence
order. It does not establish a global causal order across independent root runs.

The projection is idempotent for replayed event identities and merges lifecycle events
into logical graph entities. It does not provide transport-level exactly-once delivery.
A conflicting payload reused under the same event ID is not reconciled: first delivery
wins. A new event ID is a new event and may affect execution counts even if its payload
resembles an earlier event. Edge duplicates merge by source, target, relation, and label.

`test_projection_is_order_independent_and_duplicate_delivery_is_idempotent` reverses
a representative concurrent event stream, appends repeated identities, and asserts
exact projection equality.

## Offline Mentor Walkthrough and Partial Failure

Select **Offline walkthrough** in Streamlit. The backend in
[`offline_scenario.py`](../src/mini_deerflow/demo/offline_scenario.py) uses the real
runtime/workflow, SQLite checkpoints, bounded delegation, tool execution, evidence,
citations, and event emission. Model decisions, provider data, resolver behavior,
and trace identity/time are deterministic and require no external network or credentials.

The actual scenario order is:

```text
Goal -> planner -> action selection
  -> unsafe web_fetch denied / controlled parent web_search provider failure
  -> complete step / invented citation rejection
  -> review (replan) -> replacement plan -> delegation
       |-> alpha: web_search succeeds -> evidence -> completed
       `-> beta:  web_search controlled failure -> failed
  -> deterministic fan-in -> parent continuation
  -> review (continue) -> final step -> review (finish) -> synthesize
```

`_PartialResearcher` wraps the production `BoundedResearcherSubagent` with a
deterministic action selector and real registered `WebSearchTool`/`WebFetchTool`
execution. Both child branches call `web_search`; registering `web_fetch` does not
mean a branch executes it in this scenario. Alpha has a reservation of two calls;
beta has one and ends with a controlled failure after its failed search.

The mentor-facing proof is **alpha completed, beta failed, parent completed**.
Successful alpha evidence is preserved and promoted by runtime fan-in; failed beta
remains visible and contributes a limitation. Delegation emits a partial outcome,
displayed as completed with branch counts. The same event projection and graph
renderer visualize this production execution path.

## UI Renderer

Streamlit 1.64+ supplies native `st.echarts_chart`; WAVE 2 needs no extra graph
dependency. `build_graph_chart_spec()` creates a JSON-only Apache ECharts graph-mode
specification with directed arrows, status colors, kind-specific symbols, hover
tooltips, draggable nodes, pan/zoom, force layout, and SVG rendering.

Native `st.echarts_chart` does not currently provide a chart-node click callback to
Python. Inspection therefore uses **Inspect node**, rather than direct click-to-inspect.
The **Workflow / Current run** segmented control defaults to Current run when runtime
nodes exist, otherwise Workflow. The UI displays six graph metrics and an evidence
count caption.

## Testing and Verification

[`tests/test_execution_graph.py`](../tests/test_execution_graph.py) verifies static
topology equality, lifecycle merging, delegation/branch/tool ownership, budgets,
selected routes, partial failure, replay/order independence, secret-metadata
exclusion, JSON-only chart highlighting, real offline child tools, and Streamlit
renderer smoke behavior.

The companion `test_demo_jobs.py`, `test_demo_view_models.py`, `test_demo_app.py`, and
`test_demo_offline.py` cover job snapshots, safe projection, chat/details integration,
network-free execution, persisted conversation loading, and resume without replay.
Repository regression commands are listed in [README](../README.md#testing-and-evaluation).

## Current Limitations and Scope Boundary

Accepted WAVE 2 limitations include selector-based inspection, variable force-layout
positions, collapsed repeated workflow visits, no current `skipped` event mapping,
and incomplete reconstruction when structured events are unavailable. The static
specification represents the reviewer-enabled canonical workflow and requires its
drift test to remain synchronized with workflow changes.

The view is read-only: graph editing, workflow mutation, and cross-run analytics are
outside WAVE 2. WAVE 3+ workspace/context-revision/artifact models and later skill,
ingestion, sandbox, authentication, or tenancy features are not implemented by this
graph layer. The WAVE 0 runtime and WAVE 1 execution-event contracts remain intact.
