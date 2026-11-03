# Phase 2 Observability — Stage A Implementation & Stage B Privacy ADR

Issue [#31](https://github.com/hhtuann/mini-deerflow/issues/31) · Parent gate [#41](https://github.com/hhtuann/mini-deerflow/issues/41)

## Stage A — Measured provider usage (implemented; safe default)

### Contract

A new typed `MODEL_INVOCATION` trace event is emitted **once per actual provider invocation** (including each failed attempt) with:

| Field | Type | Meaning |
|---|---|---|
| `schema_name` | bounded str | Prompt category (Pydantic schema class name; e.g. `Plan`, `AnswerSynthesisDraft`) — never the prompt body |
| `model` | bounded str or **null** | Provider model identity when it satisfies the closed safe identifier contract; unsupported identities never affect execution |
| `node_execution` / `attempt` / `invocation` | int ≥ 1 | Node execution within run+node, attempt within that execution, and monotonic invocation ordinal within run+node |
| `duration_ms` | int ≥ 0 | Wall latency of this one invocation |
| `input_tokens` / `output_tokens` / `total_tokens` | int ≥ 0 or **null** | Only values actually reported by `AIMessage.usage_metadata`; null when absent — never estimated |
| `cached_tokens` | int ≥ 0 or **null** | SDK `input_token_details.cache_read` only |
| `cache_creation_tokens` | int ≥ 0 or **null** | SDK `input_token_details.cache_creation` only; never combined with cache reads |
| `usage_reported` | bool | Distinguishes an explicit provider zero from a missing report |
| `usage_truncated` / `omitted_invocations` | bool / int ≥ 0 | Honest bounded-telemetry marker when more than 100 invocations occur in one node execution |

Correlation: every event inherits run/turn/node/delegation/branch identity. `node_execution` distinguishes re-entry/retry, `attempt` identifies the in-execution call, and `invocation` is monotonic per run+node. Resumed runs get new run ids; delegated branches keep isolated child-run counters.

### Honesty rules (tested)

- `context_estimated_tokens` (chars/4) is **never** shown near billed tokens without its "NOT billed tokens" label.
- Unknown or partial usage keeps each missing field null; the UI renders every null token row as "Not reported" while preserving SDK-reported zero.
- Provider cost is always "Not reported" — no price table exists; inference is forbidden.
- Unsupported model identities and all telemetry validation/sink failures are fail-open: the handler return or original exception always wins.
- Collection is bounded to 100 observations per node execution; later invocations are counted and disclosed as omitted, never clamped or merged.
- Offline scripted doubles fabricate nothing: no `include_raw` support → no usage events at all.

### Capture mechanics (no return-shape changes)

| Mode | Mechanism | Provider call count |
|---|---|---|
| `prompt_json` | `UsageRecordingModel` delegate reads `AIMessage.usage_metadata` on each `invoke`/`ainvoke` | unchanged |
| `native` | `with_structured_output(..., include_raw=True)` wrapped by `NativeStructuredRawRunnable`; usage read from `raw.usage_metadata`, parsed value returned unchanged | unchanged |
| `native` (no `include_raw` support) | Graceful fallback to the exact legacy shape; no capture | unchanged |

The tracer's `wrap_node` opens a bounded per-node collection scope and drains it after success or failure. Native parse failures preserve true usage from their raw `AIMessage`; provider exceptions without raw usage remain unknown. Telemetry construction/emission is strictly best effort and cannot replace the node result or exception. No prompts, provider payloads, secrets, private reasoning, tool responses or credentials are stored — verified by canary tests.

### UI projection

`TraceEventView` exposes only nullable scalar model fields, including execution/invocation identity, separate cache-read/cache-creation counts and bounded-telemetry indicators. The Inspector's **Current run** node details render the latest invocation, an all-invocations list, honest per-field "Not reported" fallbacks and explicit zero. Static **Workflow** topology renders none of these.

### Regression coverage (`tests/test_model_usage.py`; frontend `ExecutionGraph.test.tsx`)

Coverage includes unusual provider model identities, forced telemetry failure preserving original results/exceptions, real locked-SDK `UsageMetadata` cache detail shapes, native parse-failure usage, 101-invocation bounded collection, repeated node execution identity, resume/branch isolation, partial/zero/missing UI values, secret canaries, storage round-trip and legacy nullable defaults.

## Stage B — Exact prompt/input/output previews (DESIGN ONLY; not implemented)

**Status: BLOCKED on separate owner privacy approval.** This ADR is the required first artifact; no raw capture code exists on this branch.

### Proposed opt-in contract (for review, not authorization)

1. **Disclosure scope:** per-`thread_id`+`turn_id`+`node_id`+`attempt`, bounded to 500 characters per preview, covering only the **user-visible request** (goal/question) and the **structured output summary** already exposed. System/developer prompts, hidden instructions, private chain-of-thought, credentials, API keys, raw tool payloads and untrusted URLs remain excluded unconditionally.
2. **Opt-in mechanism:** an explicit per-process flag (e.g. `MINI_DEERFLOW_DIAGNOSTIC_PREVIEWS=I_CONSENT`) set at server start; default off. No HTTP toggle, no persisted preference, no model/tool control.
3. **Redaction pipeline (before storage):** strip sensitive-key regex canaries (already in `tracing.py`), strip URLs not in the run's accepted-citation set, collapse whitespace, cap size, then store in the trace event's bounded metadata only.
4. **Retention:** diagnostic previews live only in the turn's trace JSON (existing 60k-char ledger bound); no separate storage; cleared with the turn.
5. **Tests required before any implementation:** canary non-leak (api key, bearer, password, PII), size caps, opt-out default produces zero preview fields, negative test that no HTTP endpoint can enable capture.

### Explicitly not authorized by this document

- Any raw system/developer prompt body
- Any private model reasoning/chain-of-thought
- Any credential, secret or untrusted provider field
- Any change to retention, deployment scope, or network exposure
