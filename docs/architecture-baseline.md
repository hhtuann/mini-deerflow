# Mini DeerFlow Architecture Baseline

## Purpose and scope

This document freezes the current architecture of `mini-deerflow` before the next architecture wave. It describes behavior that is present in the repository at baseline commit `8dc8f3a90b7ef750e7035bba8664a0e773f22763` on 2026-10-06. It is intentionally descriptive: it records current contracts, boundaries, and regression coverage without introducing a WAVE 1+ design.

The canonical production orchestration path is `src/mini_deerflow/agent_workflow.py`, built through `src/mini_deerflow/runtime.py`. The older `src/mini_deerflow/workflow.py` remains a smaller scaffold used by focused tests and is not the production graph described here.

## Runtime composition

`build_agent_runtime(...)` is the dependency-injected assembly seam. It receives a planner, action selector, execution `ToolRegistry`, optional action allowlist, reviewer, replanner, answer synthesizer, checkpointer, runtime limits, context budget, artifact path, tracer, and conversation repository. It builds the canonical graph and returns an `AgentRuntime` that owns run/continue/resume behavior.

`create_default_agent_runtime(...)` supplies the production defaults:

- an LLM-backed research planner, action selector, reviewer, replanner, and answer synthesizer;
- Wikipedia search/lookup as the normal research tools, or injected web search/fetch providers for explicit dependency-injected scenarios;
- a bounded researcher sub-agent plus `DelegateResearchTool`;
- workspace list/read tools, with `write_file` added to the execution registry only when `allow_write=True`;
- a separate action registry exposed to the model, so an execution-only write capability is never automatically model-selectable;
- explicit `RuntimeLimits` and `ContextBudget` objects.

`open_default_agent_runtime(...)` layers the durable SQLite checkpointer around that composition.

## Canonical graph and control flow

The graph is a LangGraph `StateGraph` over `AgentState`. Its current nodes are:

1. `planner`
2. `decide_action`
3. `execute_tool`
4. `complete_step`
5. `budget_exhausted`
6. optional `review`
7. optional `replan`
8. `synthesize`

The fixed entry path is `START -> planner -> decide_action`. `decide_action` routes to tool execution, step completion, or controlled budget exhaustion. Tool execution returns to `decide_action`. With a reviewer configured, completed steps pass through review and may continue, replan, or finish. Without a reviewer, completion routes directly to the next decision, budget handling, or synthesis. `synthesize` is the only graph edge to `END`.

The compiled graph receives the configured checkpointer, so node-boundary state is the durable resume substrate.

## Shared state contract

`AgentState` separates durable execution state from public answer material. Important fields include:

- goal, plan, current step, and pending action;
- tool observations and per-step/total tool-call counters;
- review verdicts, replans, and delegation records;
- notes and errors;
- structured findings, validated evidence, and citation sources;
- public/final answer, research report, completion/finalization status, and optional artifact path;
- optional turn metadata and bounded conversation context.

Reducers are part of the contract. Messages, observations, reviews, replans, delegations, findings, notes, and errors append. Sources are merged as citation sources. Evidence is merged by canonical URL through `merge_evidence_records(...)`; when the same canonical URL is observed again, the newer record replaces the older record at the same position. This means deduplication preserves the latest provenance for that URL.

Every fresh run starts from `create_initial_state(...)`; evidence, observations, counters, replans, and delegations start empty. Conversation context, when supplied, becomes bounded prior user/assistant messages plus the current user message rather than inherited execution state.

## Tool and capability boundaries

`ToolRegistry` is an explicit allowlist. A registered tool must expose a valid name, description, Pydantic input model, positive finite timeout, boolean `idempotent` flag, and async `run(...)` contract. Registry definitions are serialized for the model in deterministic order.

`ToolRunner` performs the runtime boundary checks:

- unknown tool names become controlled `ToolResult.fail(...)` results;
- arguments are validated through the tool input model before execution;
- execution is bounded by the tool's declared timeout;
- timeouts and provider/tool exceptions are normalized into controlled failures rather than escaping as arbitrary model-visible exceptions.

The canonical graph may also receive an `action_registry`. When present, a model-selected action outside that registry is denied before execution. The default runtime uses this to keep `write_file` out of model-selectable research actions even when writing is enabled for the final artifact path.

## Workspace boundary

`Workspace` is a filesystem boundary rooted at one resolved directory. Absolute paths are rejected. Relative paths are resolved and checked to remain inside the root. Reads and writes have byte limits. File enumeration does not follow symlinks or junctions, and write resolution is repeated after parent-directory creation to reduce link/junction escape opportunities.

Default runtime writes are disabled. When `allow_write=False`, no `write_file` tool is registered for execution and no synthesis artifact path is passed into the graph. When writes are enabled, the final report still targets the configured workspace-relative artifact path.

## Delegation contract

Delegation is one bounded fan-out/fan-in wave, not arbitrary recursive multi-agent execution.

`ScopedResearchTask` requires a branch identifier, narrow objective, success criteria, a bounded branch tool-call budget, and `delegation_depth=1`. `DelegationInput` requires unique branch identifiers. `ResearchTaskContext` exposes only the scoped task, branch-safe tool definitions, branch budget, projected observations/evidence, and bounded context metadata; it does not receive the full parent state.

`DelegateResearchTool` sorts tasks by `branch_id`, reserves the aggregate branch budget before dispatch, caps concurrency, and runs each branch with a timeout. Nested `delegate_research` is rejected by the bounded researcher. Invalid, over-budget, timed-out, or failed branch results become controlled branch outcomes.

Fan-in is deterministic by branch id. Successful branch evidence is re-extracted from structured tool observations, citations are revalidated against merged evidence, and failed/cancelled branches become limitations/errors instead of silently contaminating findings.

When fan-in is integrated into the parent graph, branch observations receive parent/global call numbers plus `delegation_id`, `branch_id`, and `branch_tool_call_number`. Evidence extraction copies those fields into `EvidenceProvenance`. The parent delegation action and all charged child calls count toward the parent's budgets.

## Execution budgets and fairness

`RuntimeLimits` currently bounds:

- tool calls per plan step;
- total tool calls per run;
- replan cycles;
- graph recursion;
- delegation concurrency;
- session tool calls across conversation turns;
- retained conversation-context characters.

The graph computes an allocated step budget as well as the raw remaining total. Delegation receives the parent's remaining allocated step budget, so a fan-out wave cannot consume capacity reserved for later plan steps. An over-budget delegation is rejected before child dispatch. Per-step, total-run, replan, and session exhaustion are represented as controlled finalization/partial-result paths rather than unbounded retries.

`ContextBudget` is a separate prompt-size control. It bounds serialized LLM-facing context by characters, truncates individual untrusted items with explicit markers, records omitted/truncated counts, and preserves evidence URLs/provenance. The token estimate is only a reporting heuristic (`ceil(characters / 4)`); execution enforcement remains character-based.

## Evidence, citations, and synthesis

Evidence is created only from successful structured observations recognized by the evidence extraction layer. Each `EvidenceRecord` includes a canonical URL, source tool, bounded source text, and `EvidenceProvenance` with step/global call attribution and optional delegation/branch attribution.

The answer-synthesis seam does not accept model-authored URLs as citations. `AnswerSynthesisDraft` uses 1-based positive `evidence_indices`; `validate_answer_draft(...)` maps only indices that exist in the provided synthesis context to runtime-owned evidence URLs. Unknown indices, nonpositive indices at the schema boundary, model-authored URLs in prose, language mismatches, and other invalid structured output are rejected.

If structured synthesis fails after its bounded retry behavior, the graph renders a deterministic safe fallback from already validated findings/evidence and records the synthesis limitation. The fallback does not promote unsupported model output into citations.

## Checkpoint and resume semantics

The SQLite LangGraph checkpointer is the authoritative execution-state resume mechanism. A normal `run(...)` refuses to overwrite an existing public checkpoint. `resume(...)` loads the persisted graph state and continues from the durable node boundary rather than rebuilding the plan or replaying already-checkpointed tool work.

Regression coverage proves that, after a crash following checkpointed work, the planner is not called again, completed steps are not repeated, previously checkpointed tool calls are not re-executed, and existing observations/counters remain intact.

This is deliberately a checkpoint-boundary guarantee. The current baseline does not claim exactly-once semantics for an external side effect if a process terminates during an in-flight tool invocation before the next durable checkpoint.

## Conversation persistence and turn isolation

`SQLiteConversationRepository` persists public conversations separately from graph checkpoints. A conversation owns an ordered set of turns. Each turn has a unique `(thread_id, turn_id)`, sequence number, checkpoint namespace, status, user message, optional assistant response, tool-call reservation/charge, and trace JSON.

The repository enforces one active turn per conversation, idempotency for repeated turn IDs, conflict detection when the same turn ID is reused with a different payload, and a durable session-level tool-call limit. A follow-up turn receives only bounded completed-turn conversational context; its research evidence/tool observations are fresh state. Internal research reports are replaced by a safe summary before they are reused as conversational assistant text.

An interrupted/running reserved turn can be recovered after restart. Resume honors the durable tool-call reservation and refuses a runtime limit that exceeds that reservation.

## Regression coverage frozen by WAVE 0

The baseline suite covers the architecture invariants across focused and end-to-end tests. Representative locks include:

| Invariant | Representative coverage |
| --- | --- |
| Canonical delegation fan-out/fan-in, deterministic ordering, child work, branch provenance, resume without replay | `tests/test_delegation_workflow.py` |
| Concurrency cap, partial branch failure, timeout charging, aggregate and allocated delegation budgets | `tests/test_delegation.py` |
| Planner/tool/step non-replay across SQLite restart | `tests/test_runtime_resume.py` |
| Evidence extraction, dedupe, provenance, citation rejection | `tests/test_evidence.py`, `tests/test_research_pipeline.py` |
| Positive-only evidence indices, unknown index rejection, model-authored URL rejection | `tests/test_answer_synthesis.py` |
| Per-step/total-run budgets and deterministic synthesis fallback | `tests/test_agent_workflow.py` |
| Replan/budget finalization and context pressure | `tests/test_review_loop.py`, `tests/test_context_pressure.py` |
| Multi-turn idempotency, bounded history, fresh per-turn evidence, reserved-turn recovery | `tests/test_conversation.py`, `tests/test_multiturn_runtime.py` |
| Partial delegated research, citations, budgets, sandbox/output behavior, interruption/resume | `tests/test_final_acceptance.py` |

The WAVE 0 additions are intentionally test-only: they make child branch execution/provenance explicit in the canonical delegation integration test and add a direct regression for zero/negative evidence indices. No production behavior is changed.

## Repository-level validation contract

At this baseline there is no checked-in GitHub Actions workflow under `.github/workflows`. The repository's documented local validation contract is therefore the authoritative gate:

```text
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv run python evals/run_evals.py --dataset evals/dataset.json
```

In restricted environments, pytest may need `--basetemp` pointed inside the repository when the host temporary directory is unreadable. That changes only the test temp location, not the application behavior under test.

## Known boundaries and future-facing areas

The following are boundaries of the current implementation, not defects claimed by WAVE 0:

- delegation is single-depth, in-process bounded fan-out/fan-in; nested or distributed delegation is outside the current contract;
- resume guarantees non-replay for work that reached a durable checkpoint, not arbitrary exactly-once external side effects during an in-flight tool call;
- citation validation establishes that a citation came from runtime-owned successful evidence; it does not independently prove that the external source is factually correct;
- context budgeting uses exact serialized character limits with an approximate token estimate, not model-tokenizer accounting;
- filesystem access is scoped to the configured workspace; it is not a general host filesystem capability;
- production-default research uses the configured default research providers; dependency-injected providers used by deterministic tests do not expand the default capability set;
- any WAVE 1+ execution-event, proof, scheduling, or orchestration changes are outside this baseline and must preserve or deliberately revise the contracts above with new regression evidence.

This document is the WAVE 0 reference point. Future architecture work should cite the specific invariant it changes, add or update regression coverage first, and preserve checkpoint, budget, provenance, citation, workspace, and turn-isolation guarantees unless an intentional design change is documented.
