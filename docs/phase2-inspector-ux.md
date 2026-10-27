# Phase 2 follow-up — Inspect panel resizing and node diagnostics

[Issue #29](https://github.com/hhtuann/mini-deerflow/issues/29) · React/FastAPI local MVP.

## What changed

1. On desktop (width >=1280px), drag the Inspector's thin left-edge handle horizontally. The Inspector starts at 400px, has a 340px minimum, and grows until the conversation retains at least 380px (plus the optional 250px conversation navigation). Click the handle and use **Left/Right Arrow** (24px increments), **Home** (minimum) or **End** (maximum) for keyboard access. Resizing the window or toggling left navigation reclamps the width. On narrower viewports the preexisting modal drawer is unchanged.
2. Choose **Graph → Current run** and click a real node, or select it through the keyboard-accessible **Inspect node** picker. The detail panel groups available **Input & context**, **Prompt**, **Execution & outcome**, **Token usage & cost**, and technical IDs. Shows only the existing safe graph and trace fields; an exact workflow-node key combines `run_id` and `node`, avoiding attribution from unrelated resume/sub-agent segments.
3. **Graph → Workflow** shows *possible* topology rather than real run evidence. It explicitly declines to show any execution data for pending illustrative nodes.

## Important observability limitations

The server's current `ExecutionGraphProjection` and `TraceEventView` **intentionally do not expose raw prompts, raw tool/LLM inputs/outputs, provider request/response bodies, secrets, or true provider token-usage/cost metadata**. The UI **must not invent** these. Actual prompt/completion/total tokens and billing cost therefore show **Not reported**. Only when a matching context trace exists do we show `context_estimated_tokens`, labeled **character-based estimate, not actual billed tokens**. The Planner can show the user goal as a **goal (not LLM prompt)**. Safe node details can show status, execution count, evidence count and bounded tool/review error categories.

To deliver the **exact** prompt/output/token feature requested, a further explicitly reviewed, locally opt-in **backend observability contract** is necessary, including trace-scope consent, provider usage adapter, secrets/PII redaction, per-node correlation, bounded preview size/retention and no private model reasoning. That is **not implemented** in this frontend-only change. Any raw information currently absent from the server cannot be recovered for historical runs.

## Safety & acceptance

- No changes to FastAPI, the model SDK, persistence, trace redaction, local networking, secrets or provider calling.
- Frontend tests cover resize constraints and true run-specific context versus static topology; existing tests cover resumed-turn trace selection and inert artifacts.
- Verification must use the **new PR head**: frontend typecheck/lint/Vitest/build and Chromium E2E, plus regular cross-platform CI. GitNexus impact/detect-changes requested by AGENTS.md is **UNVERIFIED** here because the GitHub-only environment does not expose its indexed graph or CLI.
- **No merge without explicit owner permission.**
