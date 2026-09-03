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

Not yet available:

- Persistent checkpoint/resume
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
CLI (plan | run)
 └── Settings → model factory (OpenAI-compatible)
      └── Planner (json_mode structured output → validated Plan)
           └── Runtime composition
                ├── ToolRegistry allowlist + ToolRunner
                ├── Workspace boundary + file tools
                ├── LLMActionSelector (structured action selection)
                └── RuntimeLimits (step/total budgets, recursion limit)
                     └── LangGraph bounded agent workflow
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
uv run mini-deerflow run "Inspect the local workspace and summarize verified evidence."
```

On Windows PowerShell:

```powershell
uv run mini-deerflow run "Inspect the local workspace and summarize verified evidence."
```

`run` executes the full bounded agent: planning, tool selection, tool execution, step completion, and final synthesis. The run is **read-only by default**; `--allow-write` is opt-in and adds the `write_file` tool.

Runtime options for `run`:

| Option | Default | Meaning |
| --- | --- | --- |
| `--workspace` | `.mini-deerflow/workspace` | Workspace directory available to file tools. Created if missing. |
| `--allow-write` | off | Opt in to the `write_file` tool. |
| `--max-tool-calls-per-step` | 5 | Maximum tool calls allowed in one plan step. |
| `--max-total-tool-calls` | 20 | Maximum tool calls allowed in the entire run. |
| `--recursion-limit` | 100 | Maximum LangGraph execution steps. |

Example with options:

```bash
uv run mini-deerflow run \
  "Inspect the local workspace and summarize verified evidence." \
  --workspace ".mini-deerflow/workspace" \
  --max-tool-calls-per-step 2 \
  --max-total-tool-calls 6 \
  --recursion-limit 60
```

On Windows PowerShell:

```powershell
uv run mini-deerflow run `
  "Inspect the local workspace and summarize verified evidence." `
  --workspace ".mini-deerflow/workspace" `
  --max-tool-calls-per-step 2 `
  --max-total-tool-calls 6 `
  --recursion-limit 60
```

### Output and exit codes

- `plan` prints the validated Plan JSON to stdout.
- `run` prints the final research answer to stdout.
- Domain and validation errors are printed to stderr and the process exits with code 1.
- Argument parsing errors print argparse usage to stderr and exit with code 2.
- Some model API infrastructure errors are not yet converted into a clean error message and may appear as a traceback; this is a known deferred gap.

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
3. Checkpoint and resume
4. Context management and source provenance
5. A bounded research sub-agent
6. Safety, tracing, and evaluation
