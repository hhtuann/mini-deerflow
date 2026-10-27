# Phase 2 Observability — Stage A Implementation & Stage B Privacy ADR

Issue [#31](https://github.com/hhtuann/mini-deerflow/issues/31) · Parent gate [#41](https://github.com/hhtuann/mini-deerflow/issues/41)

## Stage A — Measured provider usage (implemented; safe default)

### Contract

A new typed `MODEL_INVOCATION` trace event is emitted **once per actual provider invocation** (including each failed attempt) with:

| Field | Type | Meaning |
|---|---|---|
| `schema_name` | bounded str | Prompt category (Pydantic schema class name; e.g. `Plan`, `AnswerSynthesisDraft`) — never the prompt body |
| `model` | bounded str | Provider model identity (e.g. `glm-5.3`) from the model config |
| `attempt` | 1–100 | Invocation number within the executing node (retries get separate attempts) |
| `duration_ms` | int ≥ 0 | Wall latency of this one invocation |
| `input_tokens` / `output_tokens` / `total_tokens` / `cached_tokens` | int ≥ 0 or **null** | **Only** values actually reported by the provider SDK (`AIMessage.usage_metadata`); null when absent — never 0, never estimated |
| `usage_reported` | bool | Distinguishes an explicit provider zero from a missing report |

Correlation: every event inherits the tracer's active run/turn/node/delegation/branch identity, so resumed runs get new run ids (no cross-run attribution), branch calls carry `delegation_id`/`branch_id`, and each retry is a separate attempt under the correct node.

### Honesty rules (tested)

- `context_estimated_tokens` (chars/4) is **never** shown near billed tokens without its "NOT billed tokens" label.
- Unknown usage → `usage_reported: false` + all token fields `null`; the UI renders "Not reported".
- Provider cost is always "Not reported" — no price table exists; inference is forbidden.
- Offline scripted doubles fabricate nothing: no `include_raw` support → no usage events at all.

### Capture mechanics (no return-shape changes)

| Mode | Mechanism | Provider call count |
|---|---|---|
| `prompt_json` | `UsageRecordingModel` delegate reads `AIMessage.usage_metadata` on each `invoke`/`ainvoke` | unchanged |
| `native` | `with_structured_output(..., include_raw=True)` wrapped by `NativeStructuredRawRunnable`; usage read from `raw.usage_metadata`, parsed value returned unchanged | unchanged |
| `native` (no `include_raw` support) | Graceful fallback to the exact legacy shape; no capture | unchanged |

The tracer's `wrap_node` opens a per-node collection scope, drains it after the node handler completes (success **or** failure), and emits one event per observation. No prompts, provider payloads, secrets, private reasoning, tool responses or credentials are ever stored — verified by a canary test that injects `api_key`/`system_prompt` into `additional_kwargs` and asserts their absence from the serialized event.

### UI projection

`TraceEventView` gains scalar `model_schema`, `model_name`, `model_attempt`, `model_duration_ms`, `model_input_tokens`, `model_output_tokens`, `model_total_tokens`, `model_cached_tokens`, `usage_reported` (all nullable; legacy events project `null`). The Inspector's **Current run** node details render a "Provider usage" block with the latest invocation's metrics, an all-attempts expandable list, and "Not reported" fallbacks. The static **Workflow** topology view renders none of these (unchanged).

### Regression coverage (`tests/test_model_usage.py`, 22 tests; frontend 2 new)

Value hygiene (bool/negative/float/overflow/non-int rejection) · model-name sanitization · per-attempt capture with node correlation · failed attempts → `PROVIDER_FAILED` events without usage · missing usage → null (never 0) · overflow dropped while valid siblings survive · branch correlation (`delegation_id`/`branch_id`) · resume isolation (distinct run ids, correct operation labels) · closed bounded schema (negative/zero attempt/oversized/newline names rejected) · secret-canary non-leak · offline doubles fabricate nothing · native parsed-output preservation + parsing-error re-raise + `include_raw`-less legacy fallback · JSON round-trip persistence · legacy-event view defaults · UI: exact node usage display with null→"Not reported", cross-node isolation, absent-usage fallback.

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
