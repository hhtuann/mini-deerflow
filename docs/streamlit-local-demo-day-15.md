# Day 15: Local Deterministic Streamlit Mentor Demo

## Outcome and scope

Day 15 adds a localhost-only Streamlit interface around the completed Mini
DeerFlow learning MVP. The UI is a deterministic mentor walkthrough, not a
production API, hosted service, or claim of parity with upstream DeerFlow.

The default backend uses the real runtime, LangGraph workflow, SQLite
checkpointer, tool registry, evidence and citation rules, reviewer/replanner,
bounded context, bounded delegation, redacted tracing, and workspace artifact
boundary. Only the model, web provider, hostname resolver, and researcher
seams are deterministic local fixtures. The offline backend does not read
`.env` or use model/provider secrets already present in the process environment;
it requires no API key, resolves no DNS, and calls no model or web service.

## Install and launch

Install the locked project dependencies:

```powershell
uv sync
```

Launch Streamlit on the loopback interface without automatically opening a
browser:

```powershell
uv run streamlit run src/mini_deerflow/demo/app.py `
  --server.address 127.0.0.1 `
  --server.headless true `
  --browser.gatherUsageStats false
```

The checked-in `.streamlit/config.toml` also disables Streamlit usage-stat
collection. Keeping the explicit launch flag above makes that telemetry-safe
boundary visible during setup and remains safe if the command is copied into a
different local environment.

Open the localhost URL printed by Streamlit. The app exposes no API-key,
checkpoint, workspace, artifact-path, tool-picker, write-toggle, database, or
cancel control.

## Offline walkthrough

1. Confirm the sidebar says `Offline deterministic — no network` and
   `Mentor walkthrough v1`.
2. Enter a new valid thread identifier, or keep the sample identifier.
3. Keep the sample research goal and select **Run**.
4. Watch the typed trace progress while the worker runs independently of the
   Streamlit render loop.
5. Inspect the plan, budgets, evidence, accepted citations, rejected-citation
   count, review/replan history, delegation fan-in, and final artifact.
6. Refresh the thread list, select the completed thread, and choose
   **Resume selected thread**.
7. Confirm the resumed trace contains the checkpoint/resume lifecycle but no
   repeated provider or delegation work.

The deterministic trajectory visibly covers:

```text
unsafe-target denial
→ redacted provider failure
→ successful evidence
→ invented-citation rejection
→ reviewer replan
→ partial delegation failure with successful sibling fan-in
→ completion and one Markdown artifact
→ completed-thread resume without replay
```

## Run and Resume semantics

**Run** creates a new SQLite-backed thread. Reusing an existing identifier is
rejected before workflow execution and does not overwrite its checkpoint.

**Resume** supplies no replacement goal. LangGraph restores the selected
thread from its checkpoint. Resuming a completed thread returns its persisted
result without repeating already checkpointed model, provider, tool, or
delegation work.

The UI lists persisted thread identifiers through the application service. It
does not inspect raw SQLite records or checkpoint channel values. After a
fresh Streamlit process starts against the same local demo data, the SQLite
thread is listed as persisted and its detailed safe view is available after
Resume. That fresh-process Resume follows the same no-replay guarantee: it does
not repeat completed model, provider, tool, or delegation work.

## One-page interface

The page has a sidebar, a status strip, and exactly five tabs:

1. **Overview** — plain-text goal, plan progress, current step, bounded tool,
   replan, and delegation budgets, plus an explicit local-MVP limitation.
2. **Evidence & Citations** — bounded plain-text evidence cards, canonical URL
   text, provenance, accepted citations, rejected count only, and review/replan
   chronology.
3. **Delegation** — wave and branch status, reserved/used/charged accounting,
   deterministic fan-in, and controlled limitations. Raw branch observations
   and failed-branch findings are not exposed.
4. **Trace** — filters and an ordered timeline projected only from the closed
   `ExecutionTrace` schema. There is no generic JSON, log, prompt, or payload
   viewer.
5. **Artifact** — a structured preview rebuilt from safe view models and the
   exact Markdown source in a code block. The source is never rendered as
   arbitrary HTML or active external content.

## Non-blocking execution

Run and Resume execute in a single-worker background manager. The worker owns
its event loop, runtime, SQLite connection, and workspace lifetime and never
calls Streamlit. The main Streamlit thread polls the future and drains a
thread-safe typed trace queue.

Only one job may be active in a session. Run, Resume, and mutable controls are
disabled while it is active, and the manager independently rejects a racing
double submission. The last completed safe view remains visible until the new
job completes. Cancellation is intentionally absent because the runtime has
no cancellation/checkpoint contract to support it honestly.

## Security and untrusted-content boundaries

The Streamlit layer receives immutable, allowlisted view models rather than
raw `AgentState`. Explicit projection excludes:

- messages, prompts, structured model responses, and pending actions;
- raw tool observations and result payloads;
- checkpoints, SQLite internals, credentials, `.env` data, and machine paths;
- provider exception text, bodies, headers, and tracebacks;
- rejected URLs and failed-branch false findings;
- arbitrary filesystem paths and tool arguments.

Goal text, evidence text, review rationale, and delegation limitations are
untrusted. They are bounded and displayed only with plain-text components.
Canonical evidence/citation URLs are displayed as text rather than embedded or
automatically fetched resources. Errors are mapped to short controlled
messages. Trace rendering accepts only typed redacted fields.

The Markdown artifact source is shown in a code block. The preview is rebuilt
from already projected fields; the app does not enable `unsafe_allow_html`,
iframes, custom browser components, or arbitrary Markdown rendering.

## Five-to-seven-minute mentor script

| Time | Demonstration |
| --- | --- |
| 0:00–0:45 | Establish offline/no-network mode and the non-production scope. |
| 0:45–1:30 | Create a thread and start Run; point out the non-blocking status strip. |
| 1:30–2:30 | Show safe/provider outcomes and bounded context events in Trace. |
| 2:30–3:30 | Walk the plan, current step, and independent budgets in Overview. |
| 3:30–4:30 | Show evidence provenance, accepted citations, and rejected count. |
| 4:30–5:20 | Explain reviewer replanning and partial delegation fan-in. |
| 5:20–6:10 | Show the structured artifact preview and exact Markdown source. |
| 6:10–7:00 | Resume the completed thread and verify that tool/delegation work is absent. |

## Verification and evaluator scope

Day 15 adds focused projector, job-manager, offline integration, and Streamlit
smoke tests. The Day 14 deterministic evaluator remains the system-level
baseline. No evaluator case is added because Streamlit widget structure is a
presentation detail, not a stable runtime invariant.

## Deferred production work

- authentication, authorization, multi-tenancy, and public deployment;
- production databases, lifecycle retention, archive/delete, and distributed
  locking;
- durable job queues, cross-process state, cancellation, and exactly-once
  external effects;
- live credential management, automatic live fallback, and live-mode UI;
- general browser/shell execution or arbitrary tool configuration;
- production telemetry, alerting, cost/latency benchmarks, and a general
  Markdown/HTML sanitizer.

This remains an independent learning implementation inspired by DeerFlow
concepts. It is not production-ready and does not claim source compatibility,
behavioral equivalence, or upstream feature parity.
