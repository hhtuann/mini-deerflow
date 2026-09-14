# Mini DeerFlow

A minimal deep research agent built with Python, LangGraph, and OpenAI-compatible language models.

This project is inspired by the architecture of [ByteDance DeerFlow](https://github.com/bytedance/deer-flow), but it is an independent implementation and does not depend on DeerFlow source code.

## Project status

The project is under active development. It is a **bounded tool-using research agent prototype**, not a production-ready system.

Mini DeerFlow plans a research goal into a strict multi-step schema, selects one structured action at a time, executes allowlisted workspace tools under explicit budgets, and synthesizes a final answer from the recorded evidence.

Implemented:

- Environment-based validated configuration (Pydantic Settings) and an OpenAI-compatible model factory
- Strict structured `Plan` schema (unknown fields rejected, consecutive step numbers enforced)
- Planner using GLM-compatible `json_mode` structured output
- Bounded planner structured-output attempts
- LangGraph stateful agent workflow
- LLM structured action selection with `ToolCallAction` and `CompleteStepAction`
- Per-step and total-run tool-call budgets
- LangGraph recursion limit
- `ToolRegistry` allowlist
- `ToolRunner` input validation, timeout, and structured failure results
- Workspace boundary enforcement
- `list_files` and `read_file` in the default read-only runtime
- Jina Search and Reader providers behind an injectable, size-bounded HTTP boundary
- Multi-source evidence records with canonical URL deduplication and provenance
- Citation validation against successful web tool observations
- `write_file` only when `--allow-write` is enabled
- Markdown research artifact generation when `--allow-write` is enabled
- Completed-step summaries for cross-step continuity
- Strict HTTP/HTTPS source contract for step completions
- Bounded action-format retry with a static corrective message
- Local SQLite checkpoints with stable thread identifiers
- Persistent thread listing and resume support
- Evidence-quality reviewer returning a structured `continue | replan | finish` verdict
- Bounded replanner that replaces only the remaining plan steps and preserves completed work
- Independent replan-cycle budget, distinct from the tool-call and recursion budgets
- Review verdicts, findings, and replan history persisted in checkpoints
- Final report includes review conclusions, evidence gaps, and limitations
- Deterministic hard-bounded context projections for the action selector, reviewer, and replanner
- Depth-one bounded researcher delegation with parent-budget admission and deterministic fan-in

Not yet available:

- Streaming progress
- Human-in-the-loop review
- Heterogeneous researcher roles and nested or unbounded delegation
- Exact tokenizer-aware context accounting
- LLM summarization of older context
- Production-grade SSRF protection and redirect policy
- Production-grade API error presentation

### Capability distinction

The default CLI runtime composes the read-only `list_files`, `read_file`, `web_search`, `web_fetch`, and parent-only `delegate_research` tools. Web access uses Jina Search/Reader through an injectable HTTP client. Jina Search requires `MINI_DEERFLOW_JINA_API_KEY`; Reader can use Jina's anonymous quota. `write_file` is added only with `--allow-write`, which also writes the deterministic final report to `reports/research-report.md` through the workspace boundary.

## Current architecture

```text
CLI (plan | run | resume | threads)
 └── Settings → model factory (OpenAI-compatible)
      └── Planner (json_mode structured output → validated Plan)
           └── Runtime composition
                ├── ToolRegistry allowlist + ToolRunner
                ├── Workspace boundary + file tools
                ├── Jina provider + web search/fetch tools
                ├── Depth-one bounded researcher delegation (web-only branches)
                ├── LLMActionSelector (structured action selection)
                ├── LLMReviewer + replanner (evidence-quality loop)
                ├── ContextBudget (bounded LLM-facing projections)
                └── RuntimeLimits (step/total/replan budgets, recursion and delegation limits)
                     └── LangGraph bounded agent workflow
                          ├── SQLite checkpoint per stable thread ID
                          ├── decide_action (select one action)
                          ├── execute_tool (observation + evidence extraction)
                          ├── complete_step (validate citations against evidence)
                          ├── review (continue | replan | finish verdict)
                          ├── replan (replace only remaining steps)
                          └── synthesize → Markdown answer/artifact
```

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- An OpenAI-compatible model endpoint

The development configuration currently uses GLM-5.3 through the Z.AI OpenAI-compatible API.

## Setup

Clone the repository:

```bash
git clone https://github.com/hhtuann/mini-deerflow.git
cd mini-deerflow
```

Install dependencies:

```bash
uv sync
```

Create the local environment file:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Open `.env` and provide your API key:

```dotenv
MINI_DEERFLOW_API_KEY=replace-with-your-api-key
MINI_DEERFLOW_JINA_API_KEY=replace-with-your-jina-api-key
```

Never commit `.env`.

## Usage

The CLI exposes four commands:

```text
mini-deerflow plan
mini-deerflow run
mini-deerflow resume
mini-deerflow threads
```

### Create a validated research plan

```bash
uv run mini-deerflow plan "Compare LangGraph and CrewAI for a research agent."
```

`plan` only builds and validates a `Plan`; it does not execute tools.

On Windows PowerShell:

```powershell
uv run mini-deerflow plan "Compare LangGraph and CrewAI for a research agent."
```

### Run the bounded research agent

```bash
uv run mini-deerflow run \
  "Inspect the local workspace and summarize verified evidence." \
  --thread-id "workspace-audit-001" \
  --checkpoint-db ".mini-deerflow/checkpoints.sqlite" \
  --workspace ".mini-deerflow/workspace"
```

On Windows PowerShell:

```powershell
uv run mini-deerflow run `
  "Inspect the local workspace and summarize verified evidence." `
  --thread-id "workspace-audit-001" `
  --checkpoint-db ".mini-deerflow/checkpoints.sqlite" `
  --workspace ".mini-deerflow/workspace"
```

`run` creates a new persisted thread and executes the full bounded agent: planning, tool selection, tool execution, evidence extraction, citation validation, step completion, evidence-quality review, bounded replanning, and final synthesis. The default tool registry is **read-only**; `--allow-write` remains an explicit opt-in that adds the `write_file` tool and writes `reports/research-report.md`.

After every completed step, the reviewer judges the accumulated evidence against the goal and returns one structured verdict: `continue` (keep the current plan), `replan` (replace only the remaining steps with better-targeted work), or `finish` (the evidence is sufficient, or the remaining budgets make further collection useless). The replanner never discards completed steps or validated evidence, and deterministic runtime guards force `finish` when the replan-cycle budget, the tool-call budget, or the seven-step plan limit leaves no useful work.

### Bounded researcher delegation (Day 12)

The parent may select `delegate_research` for one depth-one fan-out wave containing exactly 2–3 tasks with unique branch IDs. Delegation concurrency defaults to `2`, is validated in the inclusive range `1–3`, and can be configured for `run` or `resume` with `--max-delegation-concurrency`.

The parent workflow retains ownership of the original goal, per-step and total tool-call budgets, SQLite thread/checkpoint state, final citation validation, and final answer/artifact rendering. Each branch receives only its narrow task and a Day 11 hard-bounded projected context. Branch registries may contain only the read-only `web_search` and `web_fetch` tools: branches cannot write to the workspace, create artifacts, mutate parent state, or delegate again.

Before dispatch, the parent reserves the aggregate branch tool-call budgets plus the parent delegation call against both remaining tool-call limits. Fan-in sorts results by branch ID, canonical-deduplicates successful evidence, and revalidates citations against that merged evidence. A controlled failure, invalid result, or timeout cancellation becomes a bounded limitation; evidence and findings from successful sibling branches remain available.

The complete delegation record is persisted with the parent tool-node checkpoint. Resuming from SQLite reuses a completed record rather than dispatching those branches again. This is not an exactly-once guarantee for a crash during an external effect before that checkpoint is written.

Delegation preserves the Day 09 evidence/citation and parent-only artifact boundaries and the Day 11 rule that compaction affects only LLM-facing projections, not durable state. The deterministic three-branch smoke with concurrency `2` passes, and the full suite reports **473 passed, 2 skipped**. This MVP makes no production-readiness claim: live model/network benchmarking, heterogeneous branch roles, nested or unbounded scheduling, and exactly-once external side effects remain out of scope.

### Bounded LLM context projections

Mini DeerFlow separates complete checkpointed state from the context sent to
LLM-facing seams. Raw evidence, validated citations, findings, completed-step
summaries, review history, and replan history remain intact for audit, SQLite
resume, and deterministic artifact rendering. Before each action-selector,
reviewer, or replanner call, the runtime derives a separate, deterministic
projection and compacts only that projection; it does not truncate the durable
state.

`ContextBudget` has four character-based limits:

| Limit | Default | Meaning |
| --- | ---: | --- |
| `max_total_chars` | `60000` | Hard ceiling on the exact serialized LLM-facing payload. |
| `max_item_chars` | `4000` | Per-item text limit during projection. |
| `retained_recent_items` | `30` | Number of recent evidence records and observations retained before total-size pressure. |
| `max_excerpt_chars` | `1500` | Per-evidence-excerpt limit in projected context. |

Compaction is deterministic and priority-based. Current-step identity,
remaining budgets, required tool identity, recent material, and relevant
finding/replan identity are preserved first. Older lower-priority evidence,
observations, summaries, findings, limitations, verbose tool schemas, and
replaced-step detail are compacted or omitted as pressure increases. Shortened
or replaced text carries explicit markers, while projection metadata records
truncation and omission counts.

Projection preserves provenance: compacted context cannot create a citation or
promote arbitrary text into evidence, and citation validation still accepts
only URLs backed by successful evidence. If mandatory context still cannot fit
after all deterministic compaction tiers, the runtime raises
`ContextBudgetExceededError` instead of sending an oversized prompt.

This context budget is independent of per-step and total tool-call limits, the
replan-cycle limit, and the LangGraph recursion limit. A custom
`ContextBudget` can currently be injected programmatically through runtime
composition; there is no CLI flag for it yet. The reported token figure is
only a conservative `characters / 4` estimate, not exact GLM token accounting.

The deterministic hard-bound/context-pressure smoke passes, and the current
test suite result is **473 passed, 2 skipped**. This does not establish
production readiness. Exact tokenizer integration, LLM summarization of old
context, and real-model context-pressure behavior remain untested or
unimplemented.

Runtime options for `run` and `resume`:

| Option | Default | Meaning |
| --- | --- | --- |
| `--thread-id` | required | Stable identifier for one thread in the selected checkpoint database. |
| `--checkpoint-db` | `.mini-deerflow/checkpoints.sqlite` | Local SQLite database used for persisted checkpoints. |
| `--workspace` | `.mini-deerflow/workspace` | Workspace directory available to file tools. Created if missing. |
| `--allow-write` | off | Opt in to the `write_file` tool. |
| `--max-tool-calls-per-step` | 5 | Maximum tool calls allowed in one plan step. |
| `--max-total-tool-calls` | 20 | Maximum tool calls allowed in the entire run. |
| `--max-replan-cycles` | 2 | Maximum evidence-review replan cycles per run. Independent of the tool-call budget. |
| `--max-delegation-concurrency` | 2 | Maximum concurrent researcher branches; validated from 1 through 3. |
| `--recursion-limit` | 100 | Maximum LangGraph execution steps. |

### Resume a persisted thread

```bash
uv run mini-deerflow resume \
  --thread-id "workspace-audit-001" \
  --checkpoint-db ".mini-deerflow/checkpoints.sqlite" \
  --workspace ".mini-deerflow/workspace"
```

On Windows PowerShell:

```powershell
uv run mini-deerflow resume `
  --thread-id "workspace-audit-001" `
  --checkpoint-db ".mini-deerflow/checkpoints.sqlite" `
  --workspace ".mini-deerflow/workspace"
```

`resume` does not accept a goal. It loads the goal and workflow state from the selected thread's checkpoint, then continues an interrupted thread or reads the final result of a completed thread.

### List persisted threads

```bash
uv run mini-deerflow threads \
  --checkpoint-db ".mini-deerflow/checkpoints.sqlite"
```

On Windows PowerShell:

```powershell
uv run mini-deerflow threads `
  --checkpoint-db ".mini-deerflow/checkpoints.sqlite"
```

Example output:

```json
{
  "threads": [
    "research-001",
    "workspace-audit-001"
  ],
  "count": 2
}
```

### Thread lifecycle and identity

- `run` creates a new thread; it rejects a duplicate `thread_id` already present in the selected checkpoint database.
- `threads` lists the persisted thread identifiers in that database.
- `resume` continues an interrupted thread or reads the saved result of a completed thread.
- A thread identity belongs to one checkpoint database. The same `thread_id` in another database is a separate namespace.
- The default tool registry stays read-only. Use `--allow-write` only when the `write_file` tool is intentionally required.

### Migration from the Day 07 runtime

Requiring a stable thread identity is a breaking CLI and runtime contract change:

```text
Before: mini-deerflow run "<goal>"
Now:    mini-deerflow run "<goal>" --thread-id "<stable-id>"
```

The Python runtime changed from `AgentRuntime.run(goal)` to `AgentRuntime.run(goal, *, thread_id=...)`; callers must now supply `thread_id` explicitly.

### Example with custom limits

```bash
uv run mini-deerflow run \
  "Inspect the local workspace and summarize verified evidence." \
  --thread-id "bounded-audit-001" \
  --checkpoint-db ".mini-deerflow/checkpoints.sqlite" \
  --workspace ".mini-deerflow/workspace" \
  --max-tool-calls-per-step 2 \
  --max-total-tool-calls 6 \
  --recursion-limit 60
```

On Windows PowerShell:

```powershell
uv run mini-deerflow run `
  "Inspect the local workspace and summarize verified evidence." `
  --thread-id "bounded-audit-001" `
  --checkpoint-db ".mini-deerflow/checkpoints.sqlite" `
  --workspace ".mini-deerflow/workspace" `
  --max-tool-calls-per-step 2 `
  --max-total-tool-calls 6 `
  --recursion-limit 60
```

### Output and exit codes

- `plan` prints the validated Plan JSON to stdout.
- `run` prints the final research answer to stdout.
- `resume` prints the resumed or previously completed research answer to stdout.
- `threads` prints sorted thread identifiers and their count as JSON.
- Domain and validation errors are printed to stderr and the process exits with code 1.
- Argument parsing errors print argparse usage to stderr and exit with code 2.
- Some model API infrastructure errors are not yet converted into a clean error message and may appear as a traceback; this is a known deferred gap.

### Persistence limitations

Persistence uses local SQLite and is intended for the MVP. It does not yet provide lifecycle status, delete/prune operations, or a multi-process atomic duplicate-thread guarantee. Checkpointing also does not guarantee exactly-once external side effects. Production deployment and multi-tenant storage are not implemented.

## Security

- Secrets are loaded from `.env`, which is ignored by Git; API keys use Pydantic `SecretStr`.
- `ToolRegistry` is an allowlist; tools outside it cannot be executed.
- Write access is opt-in through `--allow-write`.
- The workspace rejects absolute paths and path traversal, and filters symlinks and junctions.
- Tool inputs are validated with Pydantic before execution; tool failures are normalized into structured results.
- Per-step and total-run budgets plus the recursion limit prevent unbounded loops.
- Tool output, fetched content, and local files are treated as untrusted evidence; the agent is instructed not to follow instructions found inside them.
- Reviewer and replanner prompts wrap their context in explicit untrusted-data framing; verdicts and findings cannot introduce citations — the report renders citations only from validated evidence records, and model-authored URLs in review text are sanitized.
- The replan-cycle budget is enforced by deterministic runtime guards, not by model cooperation, and is independent of the tool-call and recursion budgets.
- `HttpUrl` validates source structure, while the workflow separately requires every citation URL to occur in a successful `web_search` or `web_fetch` observation.
- Evidence and citations are bounded, canonicalized, deduplicated, and checkpointed with tool-call and step provenance.
- Full SSRF and redirect hardening is deferred; web URLs and fetched content remain untrusted input.

## Development checks

Run unit tests:

```bash
uv run pytest -q
```

Run lint checks:

```bash
uv run ruff check .
```

Check formatting:

```bash
uv run ruff format --check .
```

## Documentation

- [DeerFlow request lifecycle](docs/deerflow-request-lifecycle.md)
- [LangGraph workflow (day 04)](docs/langgraph-workflow-day-04.md)
- [Tool execution layer (day 05)](docs/tool-execution-layer-day-05.md)
- [Bounded agent loop (day 06)](docs/bounded-agent-loop-day-06.md)

## Learning objective

The goal is to learn Deep Agent architecture by implementing a small vertical slice containing:

```text
plan → act → observe → review → re-plan → artifact
```

DeerFlow is used only as a reference implementation and behavioral baseline.

## Roadmap

The remaining roadmap progressively adds:

1. Real web search/fetch provider composition and richer citations
2. Re-planning and review
3. Production checkpoint lifecycle management and storage
4. Context management and source provenance
5. A bounded research sub-agent
6. Safety, tracing, and evaluation
