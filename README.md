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
- `write_file` only when `--allow-write` is enabled
- Completed-step summaries for cross-step continuity
- Strict HTTP/HTTPS source contract for step completions
- Bounded action-format retry with a static corrective message
- Local SQLite checkpoints with stable thread identifiers
- Persistent thread listing and resume support

Not yet available:

- Streaming progress
- Human-in-the-loop review
- Sub-agents
- Default composition of a real web search/fetch provider
- Rich local/web citation model
- Token-aware truncation for file/tool observations
- Production-grade API error presentation

### Capability distinction

The default CLI runtime composes the workspace file tools: `list_files` and `read_file`, plus `write_file` when `--allow-write` is enabled. Web provider contracts (`src/mini_deerflow/web.py`) and web tool adapters (`web_search` and `web_fetch` in `src/mini_deerflow/tools/web.py`) already exist, but a real web provider is not composed into the default runtime yet, so end-to-end web research is not complete.

## Current architecture

```text
CLI (plan | run | resume | threads)
 └── Settings → model factory (OpenAI-compatible)
      └── Planner (json_mode structured output → validated Plan)
           └── Runtime composition
                ├── ToolRegistry allowlist + ToolRunner
                ├── Workspace boundary + file tools
                ├── LLMActionSelector (structured action selection)
                └── RuntimeLimits (step/total budgets, recursion limit)
                     └── LangGraph bounded agent workflow
                          ├── SQLite checkpoint per stable thread ID
                          ├── decide_action (select one action)
                          ├── execute_tool (run tool, record observation)
                          ├── complete_step (summary + HTTP/HTTPS sources)
                          └── synthesize → final answer
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

`run` creates a new persisted thread and executes the full bounded agent: planning, tool selection, tool execution, step completion, and final synthesis. The default tool registry is **read-only**; `--allow-write` remains an explicit opt-in that adds the `write_file` tool.

Runtime options for `run` and `resume`:

| Option | Default | Meaning |
| --- | --- | --- |
| `--thread-id` | required | Stable identifier for one thread in the selected checkpoint database. |
| `--checkpoint-db` | `.mini-deerflow/checkpoints.sqlite` | Local SQLite database used for persisted checkpoints. |
| `--workspace` | `.mini-deerflow/workspace` | Workspace directory available to file tools. Created if missing. |
| `--allow-write` | off | Opt in to the `write_file` tool. |
| `--max-tool-calls-per-step` | 5 | Maximum tool calls allowed in one plan step. |
| `--max-total-tool-calls` | 20 | Maximum tool calls allowed in the entire run. |
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
- `HttpUrl` on sources guarantees URL structure only; it does not prove that a URL exists or was observed by a tool.

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
