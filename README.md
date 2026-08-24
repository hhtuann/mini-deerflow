# Mini DeerFlow

A minimal deep research agent built with Python, LangGraph, and OpenAI-compatible language models.

This project is inspired by the architecture of [ByteDance DeerFlow](https://github.com/bytedance/deer-flow), but it is an independent implementation and does not depend on DeerFlow source code.

## Project status

The project is currently under active development.

Implemented:

- Environment-based validated configuration
- OpenAI-compatible model factory
- Pydantic schemas for bounded research plans
- Structured planner using function calling
- Command-line interface
- Unit tests for configuration, schemas, model factory, planner, and CLI

Not implemented yet:

- LangGraph state workflow
- Web search and page fetching tools
- Agent tool-calling loop
- Per-thread workspace and artifacts
- Re-planning and review
- Checkpoint and resume
- Sub-agents
- Evaluation and tracing

The current version is a validated **planner component**, not yet a complete Deep Agent.

## Current architecture

```text
CLI
 └── Settings
      └── Model factory
           └── Structured planner
                └── Validated Plan JSON
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

Create a validated research plan:

```bash
uv run mini-deerflow \
  "Compare LangGraph and CrewAI for building a deep research agent."
```

On Windows PowerShell:

```powershell
uv run mini-deerflow `
  "Compare LangGraph and CrewAI for building a deep research agent."
```

The CLI prints a JSON object containing:

- The normalized research goal
- Between 3 and 7 steps
- Consecutive step numbers
- An objective for each step
- Verifiable success criteria

## Development checks

Run unit tests:

```bash
uv run pytest -q
```

Run lint checks:

```bash
uv run ruff check src tests
```

Check formatting:

```bash
uv run ruff format --check src tests
```

## Security

- Secrets are loaded from `.env`.
- `.env` and virtual environments are ignored by Git.
- API keys use Pydantic `SecretStr` to reduce accidental exposure in logs.
- Model output is validated before entering application logic.
- Unknown schema fields are rejected.
- No shell or filesystem execution tool is currently enabled.

## Learning objective

The goal is to learn Deep Agent architecture by implementing a small vertical slice containing:

```text
plan → act → observe → review → re-plan → artifact
```

DeerFlow is used only as a reference implementation and behavioral baseline.

## Roadmap

The two-week roadmap progressively adds:

1. LangGraph typed state and workflow
2. Web and workspace tools
3. Tool-calling execution loop
4. Planning, review, and re-planning
5. Checkpoint and resume
6. Context management and source provenance
7. A bounded research sub-agent
8. Safety, tracing, and evaluation