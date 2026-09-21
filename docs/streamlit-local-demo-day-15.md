# Persistent Streamlit Research Chat

## Outcome and scope

The Streamlit application is a persistent multi-turn chat over the canonical
Mini DeerFlow runtime. It is a local learning and mentor-demo interface, not a
production service or an upstream DeerFlow compatibility claim.

Two modes are explicit:

- **Live web** uses the model endpoint and Jina provider configured in `.env`.
  A model API key is required for inference and a Jina key is required for
  `web_search`. The badge reports configuration mode, not provider readiness;
  model and web readiness is confirmed only when the trace/evidence shows a
  successful web-tool outcome.
- **Offline walkthrough** makes no network calls. It replaces only external
  model/provider/resolver/researcher seams with deterministic fixtures while
  exercising the real conversation ledger, runtime, workflow, checkpoints,
  evidence, reviewer/replanner, delegation, tracing, and artifact path.

## Install and launch

```powershell
uv sync
uv run streamlit run src/mini_deerflow/demo/app.py `
  --server.address 127.0.0.1 `
  --server.headless true `
  --browser.gatherUsageStats false
```

Open the localhost URL printed by Streamlit. The checked-in
`.streamlit/config.toml` also disables usage-stat gathering.

For live mode, verify these values before presenting:

```dotenv
MINI_DEERFLOW_API_KEY=...
MINI_DEERFLOW_BASE_URL=...
MINI_DEERFLOW_MODEL_NAME=...
MINI_DEERFLOW_JINA_API_KEY=...
```

Never show or commit `.env`.

## Mentor runbook

1. Keep **Live web** selected for a real provider demonstration, or explicitly
   select **Offline walkthrough** for a deterministic rehearsal.
2. Click **New chat**.
3. Submit a specific, publicly verifiable research question, for example:
   `Tìm nguồn chính thức mô tả các thay đổi nổi bật của Python 3.13; chỉ kết
   luận từ nguồn tìm thấy và nêu rõ giới hạn.`
4. Confirm the user bubble appears immediately and the assistant shows a
   bounded research status while the worker runs.
5. Inspect the completed sanitized Markdown answer. Open the collapsed
   **🔎 Agent details** panel to show only the detail tabs relevant to that turn.
6. Ask a follow-up such as `Nguồn nào là nguồn trực tiếp và giới hạn của kết
   luận là gì?` without creating a new chat.
7. Confirm the same public `thread_id` remains in the header and a new turn is
   added. Evidence, trace, budgets, and artifact remain separate by turn.
8. Reload Streamlit and reopen the conversation from the sidebar to prove that
   SQLite, rather than session memory, owns the transcript.

Expected success signals in live mode are a completed assistant answer,
evidence/citation entries, and tool outcomes in the trace. Authentication,
rate-limit, transport, or configuration failures are shown as controlled
messages; switch to Offline only if you intend to demonstrate deterministic
behavior rather than live connectivity.

## Turn and resume semantics

- The first message creates a conversation and turn.
- Each follow-up calls `continue_thread` with the same public `thread_id` and
  a new `turn_id`.
- Duplicate submission of the same turn ID and same message is idempotent.
  Reusing a turn ID for different content is rejected.
- `resume` never means “new message.” It continues the active interrupted turn
  (including a `running` ledger row left by a hard process stop).
  A completed conversation has no Resume button.
- A committed turn that has no first LangGraph checkpoint can be rebuilt from
  its durable ledger input during recovery.

Each turn owns a fresh `AgentState`, isolated root checkpoint key, evidence,
citations, trace, budget counters, and artifact. Only bounded completed
user/assistant pairs are projected into the next turn; prior evidence is not
promoted into current-turn citation membership.

## Chat and per-turn details

The main page uses native `st.chat_message` and `st.chat_input`. The assistant
bubble renders only the durable, user-facing `final_answer`. The deterministic
`research_report` stays in the collapsed **🔎 Agent details** panel. That panel
creates only the tabs for which the current turn has data:

1. **Execution** — goal, plan, progress, budgets, and limitations.
2. **Evidence & Citations** — bounded evidence, provenance, accepted citations,
   and rejected count.
3. **Delegation** — branch status, budget accounting, fan-in, and limitations.
4. **Review & Replan** — structured verdict/finding and replacement-step
   metadata, without raw reviewer rationale.
5. **Trace** — a filterable timeline from the closed redacted trace schema.
6. **Research report** — a safe structured preview and exact internal Markdown
   source in a code block.

The background worker never calls `st.*`. It owns its event loop, runtime,
SQLite connection, and workspace lifetime. Only one job may run per UI session.

## Security boundary

The renderer receives frozen allowlisted projections, never raw `AgentState`.
It excludes prompts, model payloads, raw tool observations, arbitrary arguments,
provider exception bodies, credentials, paths, checkpoint internals, rejected
URLs, failed-branch findings, and reviewer rationale. User messages remain plain
text. Assistant answers use sanitized Markdown: raw HTML and remote image syntax
are removed, and only validated citation URLs remain active. The research report
is never used as the primary chat answer or follow-up context; it appears only as
inert source plus download inside the **Research report** tab.

## Remaining limitations

- no authentication, tenancy, public deployment, archive/delete, or retention;
- no distributed job queue, cancellation contract, or exactly-once external
  effects;
- no automatic provider readiness probe, latency guarantee, or live fallback;
- deterministic evaluation measures contract compliance, not factual quality.
