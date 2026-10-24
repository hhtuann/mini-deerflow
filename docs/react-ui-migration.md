# React UI migration — architecture and acceptance record

Final acceptance: **2026-10-24**, working tree relative to base `b5fa5ec`.
**PASS — automated local React migration acceptance.** Independent final review:
**APPROVE**, no important defects remaining. This record covers the local
React/FastAPI workspace; live external-provider execution, manual mentor acceptance
and physical mobile software-keyboard behavior remain unverified.

## Architecture and ownership

```text
Browser: React 19 / TypeScript / Vite (127.0.0.1:5173)
  → /api proxy → FastAPI (127.0.0.1:8000, one process)
  → ApiJobs → shared DemoJobManager (one worker)
  → DemoRuntimeService → OfflineDemoBackend | LiveDemoBackend
  → AgentRuntime → canonical agent_workflow.py / LangGraph
  → SQLite conversation ledger + turn-isolated checkpoints
  → confined session workspace / per-turn research report

Runtime events/state → safe demo view models + graph projection
  → explicit HTTP DTOs → query cache → chat / execution inspector

CLI → AgentRuntime                     (retained)
Streamlit → DemoRuntimeService          (retained reference/fallback)
```

The [`HTTP adapter`](../src/mini_deerflow/api/) reuses
[`service`](../src/mini_deerflow/demo/service.py),
[`view models`](../src/mini_deerflow/demo/view_models.py),
[`graph projection`](../src/mini_deerflow/demo/graph_projection.py) and
[`jobs`](../src/mini_deerflow/demo/jobs.py) without duplicating runtime policy.
`agent_workflow.py` is canonical; `workflow.py` remains a test scaffold.

Python owns conversations, turns, status, evidence, budgets, checkpoints, skill
identity, traces and artifacts. React owns drafts, drawers/tabs, selection and
filters; URL `?mode=offline|live&thread=<id>` supports reload and Back/Forward.
TanStack Query polls jobs every 1.5s, saved sessions every 2.5s while idle and
health every 30s. Client storage reads wait for job discovery and pause while its
mode is busy. Independently, the server's list/detail/artifact reads hold the
submission lock and return 409 during **any** active worker, including the
opposite mode. This fixes cold-SQLite initialization races in the single-process API.

The picker includes unpersisted job turns, defaults to the active turn after
navigation and preserves explicit historical selection. A session read reconciles
only terminal job identities known when it **started**; a late pre-terminal read
cannot mark history fresh. Until a qualifying read succeeds, the inspector labels
the latest segment with refreshing/error/retry states and withholds downloads,
submission and recovery. Fresh history restores the full graph/trace, including
pre-resume failed children. Tool events are real; there is no token streaming.

## Startup and configuration

Requirements: Python 3.12+, uv, and a Node version supported by Vite 7
(Node 20.19+ or 22.12+; use a supported current release), with npm.
Run Python commands from the repository root:

```sh
uv sync
uv run python -m mini_deerflow.api
```

Equivalent explicit server invocation:

```sh
uv run python -m uvicorn mini_deerflow.api:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

In another terminal, from the repository root:

```sh
cd frontend
npm ci
npm run dev
```

Open <http://127.0.0.1:5173>. Vite strictly binds that port and proxies `/api` to
`http://localhost:8000`. Keep both ports free; use one API worker and do not run
Streamlit concurrently against its store. Port 8000 exposes `/docs` and
`/openapi.json`; health confirms transport availability, not live credentials.
`npm run build` writes `frontend/dist`, which the API does not serve.
`npm run preview` previews the frontend; integrated local use requires the dev proxy.

| Setting / location | Owner and behavior |
| --- | --- |
| `.env` / process environment | Python server only; copy `.env.example` for live mode |
| `MINI_DEERFLOW_API_KEY` | Live model credential, never a browser/Vite setting |
| `MINI_DEERFLOW_BASE_URL` | Default `https://api.z.ai/api/coding/paas/v4` |
| `MINI_DEERFLOW_MODEL_NAME` | Default `glm-5.3`; further model/timeouts/retries use existing `MINI_DEERFLOW_` settings |
| Frontend environment | No credential or API-base environment variable is required; relative `/api` requests |
| Default offline store | `.mini-deerflow/demo` |
| Default live store | `.mini-deerflow/live` |
| `create_app(storage_root=...)` | Python factory option, not an HTTP/environment setting; offline uses that root, live uses its `live/` child |

Never place secrets in public `VITE_*` inputs, frontend source or browser storage.
Offline uses scripted seams with `_env_file=None`, no key or research network
calls; dependency/browser installation may need network access. React defaults to
**Offline walkthrough**, Streamlit to Live. Actual successful GLM-compatible model
and Wikipedia `wiki_search`/`wiki_lookup` outcomes establish live readiness.

## Exact HTTP contract

Source of truth: [`api/dto.py`](../src/mini_deerflow/api/dto.py) and
[`api/__init__.py`](../src/mini_deerflow/api/__init__.py); nested frozen projections
are defined in the demo view models/graph projection. JSON tuples serialize as
arrays. RootModel responses below are **unwrapped**, with no `data`/`root` envelope.
Frontend mirrors them in [`types.ts`](../frontend/src/types.ts).

Common parameters and bodies:

- `mode` is exactly `live` or `offline`.
- `thread_id`/`turn_id` path parameters match
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, maximum 128 characters.
- `job_id` must parse as a UUID. New jobs use UUID4; new conversations/turns
  receive server-generated `chat-<uuid hex>` / `turn-<uuid hex>` IDs.
- `MessageRequest = {message: string, skill_id?: string | null}`. Message is strict
  text, stripped before enforcing 10–1,000 characters. Default skill is null.
  Skill IDs are at most 64 characters and match
  `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`; selected ID must be in the available catalog.
- `ResumeRequest = {}`; body may be omitted or null. Extra request fields are
  forbidden, including caller-supplied thread/turn IDs or runtime settings.
- Request body buffering is capped at 16,384 bytes, including chunked bodies
  and bodies with false/missing Content-Length.

| Method and route | Success | Body / exact response type |
| --- | --- | --- |
| `GET /api/health` | 200 | `HealthDTO = {status:"ok", modes:["live","offline"]}` |
| `GET /api/skills` | 200 | `SkillsDTO = {skills:SkillDTO[], available:boolean}`; unavailable catalog returns empty array + false |
| `GET /api/workflow` | 200 | `ExecutionGraphProjection`, possible topology only |
| `GET /api/{mode}/conversations` | 200 | `ConversationSummaryView[]`, not job history |
| `GET /api/{mode}/conversations/{thread_id}` | 200 | `ChatSessionView` |
| `POST /api/{mode}/conversations` | 202 | `MessageRequest` → `JobDTO`, operation `run` |
| `POST /api/{mode}/conversations/{thread_id}/turns` | 202 | `MessageRequest` → `JobDTO`, operation `continue` |
| `POST /api/{mode}/conversations/{thread_id}/resume` | 202 | optional `ResumeRequest` → `JobDTO`, operation `resume` |
| `GET /api/{mode}/jobs` | 200 | `JobDTO[]`, retained jobs of this mode in insertion order |
| `GET /api/{mode}/jobs/{job_id}` | 200 | `JobDTO`; repeatable safe snapshot, not a consumable event queue |
| `GET /api/{mode}/conversations/{thread_id}/turns/{turn_id}/artifact` | 200 | exact projected report source; `text/markdown`, attachment `report.md`, `X-Content-Type-Options: nosniff` |

There are no cancellation, deletion, arbitrary file-download, credential-setting,
budget-setting, presentation/PPTX or streaming endpoints.

### Response field inventory

Notation: `?` after a type means nullable, not an omitted JSON property. `[]`
means array. All properties below are named literally; the nested definitions
cover the full public response surface. Numbers here are integers.

```text
SkillDTO:
  id:string, description:string, applicability:string, version:string
ConversationSummaryView:
  thread_id:string, title:string, status:string, updated_at:string,
  session_tool_calls:int, session_tool_call_limit:int
ChatSessionView:
  conversation:ConversationSummaryView, turns:ChatTurnView[]
ChatTurnView:
  turn_id:string, sequence:int, user_message:string, assistant_message:string?,
  status:"running"|"completed"|"interrupted"|"failed", safe_error:string?,
  run:DemoRunView?
JobDTO:
  job_id:string, mode:"live"|"offline", thread_id:string, turn_id:string?,
  operation:"run"|"continue"|"resume",
  status:"queued"|"running"|"completed"|"failed", error_message:string?,
  traces:TraceEventView[], execution_graph:ExecutionGraphProjection,
  view:DemoRunView?
DemoRunView:
  thread_id:string, turn_id:string?, operation:string?,
  workflow_status:"completed"|"incomplete", goal:string,
  plan:PlanStepView[], budget:BudgetView, evidence:EvidenceView[],
  accepted_citations:string[], rejected_citation_count:int,
  reviews:ReviewCycleView[], replans:ReplanView[], delegations:DelegationWaveView[],
  traces:TraceEventView[], artifact:ArtifactView, limitations:string[],
  execution_graph:ExecutionGraphProjection, active_skill_id:string?
PlanStepView:
  step_number:int, title:string, objective:string, success_criteria:string,
  status:"completed"|"current"|"pending"
BudgetView (all int):
  current_step, total_steps, step_tool_calls, total_tool_calls,
  max_step_tool_calls, max_total_tool_calls, replan_cycles,
  max_replan_cycles, max_delegation_concurrency
EvidenceView:
  canonical_url:string, source_tool:string, title:string, excerpt:string,
  provenance:EvidenceProvenanceView, is_cited:boolean
EvidenceProvenanceView:
  tool_name:string, step_number:int, step_tool_call_number:int,
  total_tool_call_number:int, observation_index:int,
  delegation_id:string?, branch_id:string?, branch_tool_call_number:int?
ReviewCycleView:
  review_number:int, route:string, findings:ReviewFindingView[]
ReviewFindingView:
  category:string, description:string, related_step_numbers:int[]
ReplanView:
  replan_number:int, replaced_step_numbers:int[], replacement_step_titles:string[]
DelegationWaveView:
  delegation_id:string, parent_step_number:int, branches:DelegationBranchView[],
  reserved_tool_calls:int, used_tool_calls:int, charged_tool_calls:int,
  successful_branch_count:int, failed_branch_count:int,
  cancelled_branch_count:int, limitations:string[]
DelegationBranchView:
  branch_id:string, status:string, tool_calls_used:int, finding_summary:string?
ArtifactView:
  label:string, available:boolean, finding_summaries:string[],
  citation_urls:string[], limitation_notices:string[], markdown_source:string
ExecutionGraphProjection:
  nodes:ExecutionGraphNode[], edges:ExecutionGraphEdge[], metrics:ExecutionGraphMetrics
ExecutionGraphNode:
  id:string, kind:"workflow_node"|"delegation"|"branch"|"tool"|"run"|"fan_in",
  label:string, status:"pending"|"running"|"completed"|"failed"|"cancelled"|"skipped",
  parent_id:string?, started_at:string?, completed_at:string?, duration_ms:int?,
  safe_summary:string?, details:GraphField[], technical_details:GraphField[]
GraphField: label:string, value:string
ExecutionGraphEdge:
  source:string, target:string, relation:string, label:string?, selected:boolean
ExecutionGraphMetrics (all int):
  nodes, running, completed, failed, tool_calls, sub_agents, evidence
TraceEventView:
  run_id:string, thread_id:string, sequence:int, kind:string, phase:string, outcome:string
  nullable strings: operation, node, tool_name, route, error_category, error_code,
    turn_id, active_skill_id, context_policy_version
  nullable ints: duration_ms, current_step, step_tool_calls, total_tool_calls,
    max_step_tool_calls, max_total_tool_calls, max_replan_cycles, evidence_count,
    citation_count, rejected_citation_count, artifact_count, branch_count,
    successful_branch_count, failed_branch_count, cancelled_branch_count,
    reserved_tool_calls, used_tool_calls, charged_tool_calls,
    context_omitted_items, context_truncated_items, context_estimated_tokens,
    context_payload_chars, context_budget_chars, context_retained_items
  nullable boolean: skill_instructions_omitted
```

Top-level DTO models are frozen and forbid extra fields. Nested projections are
explicit frozen dataclasses, not unrestricted `AgentState` serialization.
Absence of a metric is null, not zero; timestamps/durations are reported values,
not frontend-generated timing. Context tokens are approximately `ceil(chars/4)`;
the character budget excludes static instructions, wrappers and provider overhead.

### Errors and submission semantics

Errors normally use `{"detail":"controlled message"}` without echoing inputs:

| Status | Meaning |
| --- | --- |
| 400 | Unapproved Host; TrustedHost middleware text response |
| 403 / 413 | Disallowed write Origin / invalid or oversized body |
| 404 / 422 | Missing resource / invalid request or unknown skill |
| 409 | `An operation is already active.`, `Resume the active turn first.` or `No resumable turn exists.` |
| 500 / 503 | `Operation failed.` / shutdown or unavailable requested skill catalog |

Asynchronous runtime failures become **failed jobs** with controlled
`error_message`; they do not retroactively change the accepted POST status.

Storage GETs (conversation list/detail and artifact) also return 409 while any
worker is active, even for the other mode. Job, health, skill and workflow reads
remain available. Idle storage reads are serialized with one another and with
submission, so a worker cannot start during an in-flight storage read.

A 202 acknowledges submission, not completed work. The returned job can already
be running/completed because the worker progresses immediately; clients must not
require seeing every transient state. New POSTs generate new IDs: the HTTP API
does not accept client idempotency keys. A double-click lock prevents duplicate
submissions in this client, but after an uncertain POST timeout inspect job/history
before resending. Runtime completed-turn replay semantics are not HTTP retry safety.

## Server job lifecycle, persistence and restart

`ApiJobs` combines the async submission lock with a thread lock and one shared
`DemoJobManager` / `ThreadPoolExecutor(max_workers=1)`. One active operation spans
**both modes and all conversations**; a second submission gets 409. This is
separate from runtime delegation concurrency and tool-call/replan limits.

```text
POST → queued → running → completed | failed
restart → empty in-memory job history; persistent turns/checkpoints retained
load unresolved running/interrupted turn → explicit POST resume → new job, same turn
```

At most 32 snapshots are retained across modes; eviction leaves durable
conversations/artifacts intact. Snapshots expose repeatable safe traces and graph;
internal `succeeded` maps to HTTP `completed`. `view` is null before success and
on failure. A failed job can leave an interrupted, resumable turn. Follow-up is
refused while a turn is running/interrupted; resume requires exactly one such
turn, accepts no new goal/skill and preserves the original skill/context pins.

Graceful shutdown closes submission and waits for the worker (`wait=True`, no
cancellation). Abrupt termination can leave a `running` ledger turn; restart
neither replays it nor reconstructs job IDs. Reload history and explicitly resume.
If no reservation was persisted, there may be no turn to recover. An external
effect can repeat if interruption precedes its checkpoint. API tests cover app
re-creation/recovery; separate OS-process kill/restart acceptance is unverified.

## Local browser/security boundary

- The supplied launcher binds `127.0.0.1:8000`. Use a single process/worker; there
  are no cross-process leases or distributed locks.
- Trusted hosts: `localhost`, `127.0.0.1`, and `testserver` (test support).
- CORS origins are exactly `http://localhost:5173` and `http://127.0.0.1:5173`;
  allowed methods GET/POST and header Content-Type; credentials are not enabled.
  The local boundary also rejects non-safe-method requests carrying an unapproved
  or duplicate Origin. Requests without Origin remain allowed for local clients.
- This is **not authentication**. No user identity, ownership authorization,
  tenant isolation, TLS termination or protection from another local process is
  supplied. CORS/Host checks do not make it an Internet-facing service.
- Only safe projections cross HTTP: no raw state, prompts, chain of thought,
  observations, provider exceptions, unrestricted tool arguments, skill instruction
  bodies, credentials or absolute server filesystem paths.
- `SafeMarkdown` supports semantic GFM tables in a focusable horizontal-scroll
  region and disables HTML/images/active embedded content; links require exact
  membership in the turn's accepted URLs plus an independent HTTP(S) check.
  Evidence that is not cited remains readable but its URL is inert. Citation
  membership is not factual verification. Artifacts render as inert source text.
- Artifact download resolves selected mode/thread/turn through the loaded session;
  it accepts no path. Existing workspace traversal/link/junction protections remain
  in Python. Filesystem sandboxing is not OS/container isolation.

## Frontend structure and design

| File | Responsibility |
| --- | --- |
| `frontend/src/main.tsx` | Query provider, strict React root, locally bundled font weights 400–700 |
| `frontend/src/App.tsx` | Navigation, mode isolation, transcript, composer, submission lock, polling, recovery and responsive shell |
| `frontend/src/session.ts` | Request-start terminal-job identities for fresh persisted-session reconciliation |
| `frontend/src/api.ts`, `types.ts`, `links.ts` | Relative HTTP client (15-second default timeout or supplied abort signal), safe status messages, DTO mirrors and exact URL checks |
| `frontend/src/components/Inspector.tsx` | Per-turn Graph, Plan, Tools & Delegation, Evidence, Review, Trace, Context, Artifacts tabs |
| `frontend/src/components/ExecutionGraph.tsx` | Lazy React Flow/Dagre graph, pan/zoom/fit, node click and equivalent native selector |
| `frontend/src/components/SafeMarkdown.tsx`, `UI.tsx` | Safe answer rendering, statuses, fields, empty states and modal drawers |
| `frontend/src/styles.css` | Shared tokens, component styles, responsive layout and reduced motion |
| `frontend/e2e/` | Real API browser journey and isolated interrupted-turn seed server |

[`DESIGN.md`](../DESIGN.md), especially its product addendum, is normative.
The implemented workspace applies Corporate Trust to an application rather than
marketing sections: light surfaces, restrained primary gradient, dense inspector,
conversation-first layout, Lucide icons and no decorative moving backgrounds.

| Token group | Implemented values |
| --- | --- |
| Surface / text / border | `#f8fafc`, `#fff`, main `#0f172a`, brand muted `#64748b`, contrast text variant `#526079`, border `#e2e8f0` |
| Brand | Indigo `#4f46e5`, violet `#7c3aed`, accent surface `#eef2ff`, accent-success `#10b981` |
| Semantic foreground/background | Success `#166534/#f0fdf4`, error `#991b1b/#fef2f2`, warning `#854d0e/#fefce8`, info `#1e40af/#eff6ff` |
| Geometry / rhythm | Card 12px, input 8px, spacing 4/8/12/16/24/32px, indigo-tinted shadow |
| Typography | Bundled Plus Jakarta Sans + system fallback; 14px base / 1.65 line height; monospace IDs and tabular numeric fields |

Brand tokens retain the specified values; centralized contrast variants provide
readable muted/semantic text, borders, shadows and interaction states.
At ≥1280px the optional inspector is 400px alongside a 250px navigation column;
1024–1279px uses sidebar/chat with inspector drawer. Below 1024px navigation also
becomes a drawer; below 640px controls/spacing compact and trace filters stack.
The 375×812 target uses a single chat column and full-width inspector sheet.
`min-width:0`, wrapping and contained code/graph overflow prevent page-wide
scrolling; the composer uses `100dvh` layout and bottom safe-area padding, with
16px textarea text on phones to avoid focus-triggered input zoom.

Accessibility implementation includes native buttons/labels/selects, a skip link,
visible focus, 44px control targets, textual status labels/icons, polite job live
region, alert errors, dialog focus containment/Escape/return focus, and tab
ArrowLeft/ArrowRight/Home/End navigation. Canvas interaction has an equivalent
keyboard node selector. CSS honors reduced motion. These are implemented measures
and selected browser checks, not a complete WCAG, contrast or assistive-technology
audit; software-keyboard behavior on a physical phone also needs acceptance.

## Streamlit capability parity matrix

Reference UI: [`demo/app.py`](../src/mini_deerflow/demo/app.py) and
[`demo/components.py`](../src/mini_deerflow/demo/components.py), plus README's
retained Streamlit sections. `API` = `tests/test_api.py`, `UI` =
`frontend/src/App.test.tsx`, `E2E` = `frontend/e2e/workspace.spec.ts`.

| Existing capability | React / HTTP location | Verification reference and parity qualification |
| --- | --- | --- |
| Live/offline selection and truthful readiness badge | Navigation mode selector, header, mode note; mode-scoped routes | UI mode navigation; E2E mode isolation; API injected live transport. Actual live model run pending; default now offline |
| New chat, bounded message and pending submitted message | App welcome/composer, POST conversations | UI submission/reset and duplicate lock; E2E real offline create; API length/validation boundaries |
| Saved conversation list, selected chat, refresh, empty/error states | Navigation, header Refresh; GET conversations/session | UI history/read retry; E2E reload; API persistent history |
| Same-thread follow-up with isolated new turn | Composer → POST turns; transcript articles | API offline follow-up; UI endpoint/cache navigation tests; E2E desktop follow-up and phone third turn |
| Reconstructed transcript after reload | URL thread + server session, no browser-owned ledger | E2E reload and API app re-creation |
| Resume running/interrupted turn without a new goal | Recovery strip → POST resume | API persisted running/interrupted and restart tests; UI freshness/error/retry transitions; E2E same-turn resume retains earlier failed child, delegation, fan-in and tool history |
| Running-job feedback and duplicate-work prevention | Job strip, disabled composer, GET jobs; graph/trace during work | API atomic concurrency/progress; UI active discovery; E2E polling. Server-wide slot is stronger than old per-session worker; other mode may still receive 409 |
| Safe runtime and failure messages / failed trace | Alert banner, turn safe error, inspector Trace | API failure-redaction tests; UI safe retry; E2E disconnect. Failed-job traces survive while job retained, not guaranteed after restart |
| Metadata skill discovery, None, description/applicability | Composer selector and selected metadata; GET skills | API skills/catalog fail-closed tests; UI metadata/reset; E2E research-brief |
| Per-turn skill reset and historical identity | Composer reset; each turn/inspector Historical skill | UI submit/follow-up; E2E history. Fixed Phase 0 offline answer is not evidence of skill quality |
| Concise safe Markdown answer separate from details | SafeMarkdown + per-turn Inspect action; semantic GFM comparison tables in a scroll region | Markdown regressions verify tables retain HTML/image denial and citation allowlist; E2E transcript |
| Session tool budget, turn status, thread/turn identity | Job strip, navigation budget, transcript, inspector IDs | API session DTO assertions; UI panels; source review (thread also in URL, not old metric strip) |
| Goal, plan, objectives, success criteria, step states | Explicit Goal in Inspector Plan, independent of artifacts | Inspector regression and E2E Plan goal assertion; source review of all fields |
| Current step, step/total calls, replan and delegation limits | Inspector Plan | Source review; API RunView contract; no separate per-field browser assertion |
| Public execution limitations | Plan/Review and delegation/artifact notices | Source review of Inspector rendering; API safe projection tests |
| Workflow possible topology vs Current run observed graph | Graph tab view selector → GET workflow or projected run/job graph | API workflow; E2E topology distinction and parent/children/fan-in/continuation |
| Active live graph and ordered safe trace | Graph/Trace consume current JobDTO, then freshly reconciled saved RunView | UI retained-cache navigation, unpersisted-turn picker, delayed/failed refresh and pre-terminal read regressions; API progress; E2E terminal graph |
| Graph parent, branches, tools, fan-in and partial failure | ExecutionGraph backend IDs, textual statuses and safe node inspector | E2E completed parent/child, failed child, fan-in, continuation |
| Inspect node, safe summary/timing/details and technical IDs | Canvas click or native Inspect node selector | E2E actual canvas click updates selected ID/details; selector and keyboard access; drag disabled for stable read-only layout |
| Graph pan/zoom, category/status and metrics | React Flow controls, all seven metrics, textual status/kind legends | Component and E2E assertions for nodes/running/completed/failed/tool calls/sub-agents/evidence, including zero, and six entries in each legend |
| Evidence title/source URL/tool/excerpt/provenance/cited state | Evidence tab, provenance disclosure | E2E evidence title; source review of complete fields; API projection safety |
| Accepted citations and rejected count | Evidence tab; validated answer links | SafeMarkdown tests; E2E evidence; source review. Membership, not fact checking |
| Delegation wave/branches, findings, reserved/used/charged counts and outcomes | Tools & Delegation plus graph | E2E branch/fan-in checks; source review of budget/limitations fields |
| Review verdict/findings/related steps and replacement plan | Review tab | API projected data and source review; not individually asserted in browser tests |
| Filtered trace run/kind/outcome, sequence/stage/tool/route/timing/counts/errors | Multi-select subsets + per-list All / global Reset; expandable scalar fields | Component/E2E combined subset filtering: OR within a list, AND across lists, empty means All; no-match and reset coverage matches Streamlit semantics |
| Context chars/budget/retained/omitted/truncated, approximate tokens, skill omission/policy | Context tab plus full trace scalar fields | UI metrics; E2E approximate label; visually inspected nonempty payload 5,592 / budget 8,000 with research-brief; separate Phase 1 demo also verified |
| Structured report preview, exact inert Markdown, download per turn | Artifacts tab + scoped artifact GET | API exact bytes/ownership and restart; UI inert source/link; E2E real download and exact source |
| Conditional detail sections and empty states | Stable eight-tab inspector with explicit no-data messages | UI panels and source review; presentation differs from Streamlit's conditional tabs |
| Local fixture determinism and Phase 1 context demo | Offline backend shared; `python -m mini_deerflow.demo.phase1_offline` retained | API offline journey; E2E real runtime. Phase 1 separate command is retained, not a new browser mode |

**Automated local functional parity passed.** The graph metrics/legends, trace
subset filters, Plan goal, safe GFM tables, active-job selection and resumed
history/freshness regressions are resolved. Live-provider and manual acceptance
remain separate from this result.

## Offline browser procedure and screenshots

1. Open `/?mode=offline` at 1440×900. Choose `research-brief`, read its metadata
   and submit 10–1,000 characters. Answers remain the fixed mentor scenario.
2. Confirm completion and skill reset to None. Follow up, reload and inspect
   each turn's saved title, historical skill and separate artifact.
3. In Current run, inspect Parent, successful alpha, failed beta, tools, fan-in
   and continuation. Check all metrics/legends, canvas click, keyboard selector
   and pan/zoom/fit; Workflow must be labeled possible topology.
4. Inspect Plan goal, delegation, evidence, review, trace subsets/reset and Context.
   Missing diagnostics are shown honestly. Compare the downloaded Markdown with
   exact per-turn source, including after reload.
5. At 375×812, check drawers, Escape/return focus, tab arrows/Home/End, composer
   Enter/Shift+Enter, contained overflow, reduced motion and touch targets.
6. Disconnect/reconnect and Refresh. E2E seeds `browser-interrupted` through the
   real runtime after the cold journey, then verifies same-turn resume and prior
   failed-child history. Normal API startup does not create this fixture.

Reproduce automated browser evidence with `npx playwright install chromium`, then
`npm run test:e2e` from `frontend/`, with ports 8000/5173 free. Playwright starts
its own temporary-store API and Vite, runs the cold-storage journey first, then
seeds a real interrupted checkpoint; it does not reuse existing servers.
Screenshots are explicit test captures under
ignored `frontend/test-results/`, not checked-in design mockups:

| Test output directory | Captures |
| --- | --- |
| `workspace-real-offline-res-0c45b-nspector-and-phone-keyboard/` | `desktop-chat.png`, `desktop-failed-child-graph.png`, `desktop-context.png`, `phone-graph-inspector.png`, `phone-workspace.png` |
| `workspace-restored-interru-d5ba4-ity-and-read-errors-recover/` | `resumed-persisted-turn.png` |

Real desktop chat, failed-child graph, Context and 375px phone screenshots were
visually inspected. Context was nonempty: **5,592 payload chars / 8,000 budget
chars**, active skill **research-brief**. Names may change with Playwright titles;
preserve captures before rerunning. See
[`frontend/VERIFICATION.md`](../frontend/VERIFICATION.md) for scenario details.
Physical software keyboards and exhaustive accessibility review remain unverified.

## Final verification (2026-10-24)

Corrected-tree results: **PASS — automated local migration acceptance**.
Independent final code review: **APPROVE**, no important defects remaining.

| Working directory | Exact command | Final result |
| --- | --- | --- |
| Root | `uv run --offline --no-sync pytest -q -rs -p no:cacheprovider --basetemp C:\Users\GIGABYTE\AppData\Local\Temp\opencode\react-final-pytest` | 816 passed, 6 skipped, 223 subtests in 47.05s; includes all 38 API tests |
| Root | `uv run --offline --no-sync python evals/run_evals.py --dataset evals/dataset.json` | 9 cases / 36 invariants, all passed |
| Root | `uv run --offline --no-sync python -m mini_deerflow.demo.phase1_offline` | All 14 top-level booleans true: unskilled baseline, deep-research interrupted/resumed run, unskilled follow-up |
| Root | `uv run --offline --no-sync ruff check . --no-cache` | PASS |
| Root | `uv run --offline --no-sync ruff format --check . --no-cache` | PASS; 124 files already formatted |
| Root | `uv lock --check --offline` | PASS; 95 packages |
| Root | `git diff --check` | PASS |
| `frontend/` | `npm ci` | PASS; 393 packages audited, 0 vulnerabilities |
| `frontend/` | `npm run typecheck` | PASS |
| `frontend/` | `npm run lint` | PASS; zero warnings |
| `frontend/` | `npm test` | 20 passed in 4 files |
| `frontend/` | `npm run build` | PASS; 2,101 modules transformed |
| `frontend/` | `npm run test:e2e` | 2 real-API Chromium tests passed in 21.4s; cold storage, then seeded interruption; no mocked backend |

The six pytest skips are four symlink cases without Windows creation privileges,
one native Windows API case requiring Python 3.13+, and one POSIX-semantics case.
Provision dependencies with `uv sync`/`npm ci` first. Python `--offline --no-sync`
uses the installed environment. Install Chromium with
`npx playwright install chromium` before browser tests. Pytest recreates its
basetemp; do not share it between concurrent runs. Evaluator subprocesses manage
their own per-case temporary directories. Offline checks validate contracts and
continuity, not live-provider research quality.

## Limitations and Streamlit retirement plan

The local single-process app has no auth/tenancy, durable queue, cancellation,
job retention on restart, workspace cleanup, generic artifact server, production
hosting recipe or PPTX generation. Live defaults exclude general web tools;
offline fixtures do not establish live research/skill quality. Separate-process
crash acceptance, exhaustive accessibility and security certification are outside
verified coverage. Storage locks do not coordinate another API process, CLI or
Streamlit instance. DTO mirrors are maintained manually.

Retirement is staged, with Streamlit still runnable as reference/fallback:

1. Complete live-provider, physical software-keyboard, accessibility and manual
   mentor acceptance; automated local regression and independent review passed.
2. Confirm React as the accepted default with backup-based history/resume/artifact
   checks and documented fallback; never run competing workers over one store.
3. In a separate cleanup, review consumers before retiring `demo/app.py`,
   `demo/components.py` and Streamlit-only configuration/dependency. Migrate AppTest
   coverage; retain shared services/backends/jobs/projections, offline scenarios,
   `phase1_offline.py`, runtime fixtures and Phase 0/1 contract tests.
4. Any later move of shared `demo/` modules requires graph-aware impact/rename and
   compatibility planning, not duplicated runtime logic.
