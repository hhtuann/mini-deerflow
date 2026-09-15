# Day 14: MVP stabilization, deterministic demo, and DeerFlow-style comparison

## Outcome and scope

The 14-day Mini DeerFlow learning MVP is complete as a bounded, local deep-research vertical slice. Day 14 adds final acceptance coverage, reproducible evaluation, and an honest capability comparison. It does not add a UI, API server, deployment layer, authentication, shell tool, browser automation, container sandbox, or production database.

This project is an independent implementation inspired by DeerFlow architecture. The comparison below maps concepts and learning outcomes; it does **not** claim source compatibility, behavioral equivalence, or upstream DeerFlow feature parity.

## Deterministic final acceptance demo

Run the coherent offline acceptance scenario:

```powershell
uv run pytest -q tests/test_final_acceptance.py::test_final_mvp_acceptance_survives_interruption_and_resume
```

Run the complete deterministic evaluation suite:

```powershell
uv run python evals/run_evals.py
```

This advances the Day 13 baseline from 8/8 cases and 26/26 declared invariants to 9/9 cases and 36/36 declared invariants.

The acceptance test uses the real `open_default_agent_runtime` composition, real LangGraph workflow, real tools, real evidence/citation logic, real context projection, real delegation fan-in, real tracing, workspace writes, and a real SQLite checkpointer. Deterministic fakes exist only at the model, web provider, researcher, and hostname-resolver seams. It neither reads `.env` nor calls a real model, Jina, network, or DNS service.

One run performs this trajectory:

1. Deny a loopback fetch before provider transport.
2. Normalize a secret-bearing provider failure without exposing its payload.
3. Collect successful search and fetched-page evidence large enough to force bounded LLM projection.
4. Reject unsafe and model-invented citations while retaining valid evidence provenance.
5. Route through reviewer `replan` and replace only unfinished work.
6. Execute one two-branch delegation wave; alpha succeeds and beta has a controlled failure.
7. Interrupt immediately after the completed delegation node is checkpointed.
8. Reopen the SQLite database, resume under a fresh run ID, and do not repeat completed provider or delegation work.
9. Finish review and render exactly one deterministic Markdown artifact.

The test asserts the terminal state, all execution budgets, citation/evidence membership, complete raw evidence, bounded model projections, deterministic fan-in, explicit limitations, trace redaction, absence of trace data from `AgentState`, artifact uniqueness, and checkpoint replay behavior.

## Safe CLI walkthrough

The deterministic commands above need no real API key. The CLI commands below use configured model/provider services and therefore are not the offline acceptance test. Configure credentials through environment variables, never commit `.env`, use a unique thread ID, and keep the default read-only workspace unless an artifact is explicitly required.

Create a validated plan without executing tools:

```powershell
uv run mini-deerflow plan "Compare two documented approaches to bounded agent context management."
```

Start a new read-only persisted run with explicit limits:

```powershell
uv run mini-deerflow run `
  "Compare two documented approaches to bounded agent context management." `
  --thread-id "day14-demo-001" `
  --checkpoint-db ".mini-deerflow/day14.sqlite" `
  --workspace ".mini-deerflow/workspace" `
  --max-tool-calls-per-step 4 `
  --max-total-tool-calls 12 `
  --max-replan-cycles 1 `
  --max-delegation-concurrency 2 `
  --recursion-limit 100
```

Add `--allow-write` only when the agent should receive `write_file` capability and write the final `reports/research-report.md` artifact inside the selected workspace. This flag is a capability grant, not a production approval workflow.

Resume the same thread with the same storage and operational limits:

```powershell
uv run mini-deerflow resume `
  --thread-id "day14-demo-001" `
  --checkpoint-db ".mini-deerflow/day14.sqlite" `
  --workspace ".mini-deerflow/workspace" `
  --max-tool-calls-per-step 4 `
  --max-total-tool-calls 12 `
  --max-replan-cycles 1 `
  --max-delegation-concurrency 2 `
  --recursion-limit 100
```

List durable thread IDs without invoking the model:

```powershell
uv run mini-deerflow threads --checkpoint-db ".mini-deerflow/day14.sqlite"
```

For `run` or `resume`, add `--trace-json` to emit the closed-schema, redacted JSON Lines trace to stderr. The final answer remains on stdout, so redirect the streams separately if records are captured:

```powershell
uv run mini-deerflow resume `
  --thread-id "day14-demo-001" `
  --checkpoint-db ".mini-deerflow/day14.sqlite" `
  --workspace ".mini-deerflow/workspace" `
  --max-delegation-concurrency 2 `
  --trace-json 1>answer.md 2>trace.jsonl
```

`--max-delegation-concurrency` accepts only `1`, `2`, or `3`. It bounds simultaneous branches, while per-step and total tool-call budgets separately admit the parent delegation call and aggregate branch reservations. Reuse of a completed thread by `run` is rejected; use `resume` instead.

## MVP capability map

| Area | MVP capability | Enforced boundary and evidence |
| --- | --- | --- |
| Planning | Strict 3–7 step plan with bounded structured-output attempts | Pydantic validation; capability-aware plan during `run` |
| Bounded tools | Allowlisted file/web/delegation tools with typed inputs, timeouts, and independent step/total/recursion limits | Default runtime is read-only; workspace traversal is denied; write is opt-in |
| Persistence and resume | Stable `thread_id`, local SQLite checkpointing, `run`/`resume`/`threads` | Resume invokes persisted state and skips already checkpointed nodes |
| Web evidence and citations | Search/fetch observations become canonical, provenance-bearing evidence only after successful schema validation | Final citations are a subset of successful evidence URLs; unsupported URLs are rejected |
| Reviewer and replanner | Structured `continue`, `replan`, or `finish`; unfinished suffix replacement only | Completed work, evidence, counters, and history survive replanning; cycle budget is independent |
| Context management | Deterministic character-bounded projections for selector, reviewer, replanner, and researcher | Raw state stays complete; projection metadata reports omission, truncation, and estimated tokens |
| Delegation | Bounded depth-one waves of 2–3 web-only branches with concurrency 1–3 | Aggregate budget admission, deterministic sorted fan-in, citation revalidation, partial-failure retention |
| Safety, tracing, evals | Public-target checks, safe error normalization, closed-schema external traces, deterministic invariant suite | Trace stores counts/outcomes rather than raw payloads and is not part of `AgentState` |

## DeerFlow-style concept comparison — not an upstream parity claim

| DeerFlow-style concept | Mini DeerFlow learning analogue | Honest difference |
| --- | --- | --- |
| Lead agent | One LangGraph parent owns plan, action loop, review, synthesis, and budgets | Small single-purpose graph; no full upstream harness or channel ecosystem |
| Middleware and tool policy | Typed registry, runner validation, workspace boundary, web target checks, and hard counters | Explicit local composition rather than a broad middleware stack |
| Sandbox | Files are confined to one workspace and shell execution does not exist | No container/process sandbox and no shell tool |
| Persistence | Checkpointed graph state keyed by `thread_id` | Local SQLite only; no production lifecycle, tenancy, or distributed coordination |
| Context management | Deterministic projections with provenance-preserving compaction metadata | Character bound and heuristic token estimate, not an exact model tokenizer or memory platform |
| Sub-agent orchestration | Bounded depth-one web-research fan-out/fan-in waves | No nested agents, heterogeneous roles, dynamic scheduling, or general task delegation |
| Tracing and evaluation | Run/thread-correlated redacted events plus executable invariant cases | Local deterministic contract evaluation, not production telemetry or live quality benchmarking |
| Artifact ownership | Parent-only deterministic Markdown report through the workspace tool | One fixed artifact pattern, not a general artifact service |

## MVP complete versus deferred production work

| MVP complete | Deferred production work |
| --- | --- |
| Strict planning and bounded plan/action/review/replan loop | Authentication, authorization, and multi-tenancy |
| Allowlisted tools, workspace confinement, and write opt-in | UI/API surface and deployment architecture |
| Local SQLite interruption/resume without replay of completed checkpointed work | Production database, lifecycle operations, distributed locking, and retention policy |
| Successful web evidence provenance and citation-subset validation | Production egress controls, DNS pinning/rebinding defenses, and independent remote-reader redirect enforcement |
| Character-bounded LLM projections with complete raw state | Exact tokenizer integration and model-specific context accounting |
| Depth-one bounded delegation with deterministic partial-failure fan-in | Browser automation, general orchestration, and nested/heterogeneous agents |
| Redacted out-of-state traces and deterministic contract evals | Production telemetry, live model-quality benchmarking, latency/cost benchmarking, and alerting |
| Workspace file boundary without shell execution | Container sandbox and shell/process isolation |
| Checkpoint replay guarantees for completed graph nodes | Exactly-once external side effects across a crash before checkpoint commit |

Evidence provenance proves where an observation entered the run and whether a cited URL belongs to successful evidence. It does not prove that a source is true or that every rendered claim is semantically entailed by that source. Those judgments, along with production hardening, remain outside this MVP.
