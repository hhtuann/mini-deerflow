# Safety, Observability, and Deterministic Evaluation - Day 13

## Outcome

Day 13 adds a learning-MVP control layer around the Day 09–12 runtime without
changing its evidence, citation, artifact, context-budget, delegation, or
checkpoint ownership rules. The new layer consists of:

1. a typed public HTTP(S) target validator before `web_fetch` providers;
2. closed-schema execution traces at workflow and runtime composition seams;
3. an opt-in JSON Lines CLI trace stream; and
4. an eight-case deterministic invariant evaluator.

This is bounded contract verification, not production security certification or
an answer-quality benchmark.

## Safety architecture

`web_safety.py` separates name resolution from policy. `HostResolver` and
`WebTargetValidator` are injectable protocols. Production composition uses
`SystemHostResolver`; tests use fixed resolvers. A successful validation returns
`SafeWebTarget`, containing only the normalized URL, hostname, port, and the
validated address set.

The policy rejects malformed URLs, non-HTTP(S) schemes, userinfo, local names,
scoped IPv6 hosts, and all non-global IP categories. Hostnames are admitted only
when DNS returns a non-empty set and every answer is a public address. The fetch
provider is not called on an initial denial. Final and declared redirect targets
are revalidated before page content enters a successful `ToolResult`.

Safety denials return `safety_denial` plus a controlled `WebSafetyErrorCode`.
Provider and transport failures retain their existing safe normalization, so
operators can distinguish policy enforcement from upstream availability without
seeing targets, bodies, credentials, or raw exception chains.

## Trace architecture and schema

`ExecutionTracer` is injected through `build_agent_workflow`,
`build_agent_runtime`, `create_default_agent_runtime`, and
`open_default_agent_runtime`. Runtime `run`/`resume` methods bind a fresh
`run_id` to the stable `thread_id` with `contextvars`; workflow nodes use the
same context without adding trace data to checkpointed `AgentState`.

Every node emits `start` and `end`. Domain outcomes add focused records:

| Kind | Safe fields beyond identity/phase/outcome |
| --- | --- |
| `checkpoint` | operation and ready/resumed/skipped/failure state |
| `node` | node name, counters, duration |
| `tool` | tool name, budgets, evidence count, controlled failure category/code |
| `context_budget` | omitted/truncated item counts and estimated tokens |
| `delegation` | branch status counts and reserved/used/charged calls |
| `review` | `continue`/`replan`/`finish`, budgets, evidence/citation counts |
| `replan` | route, cycle budget, step/tool counters |
| `citation_validation` | accepted count and rejected count |
| `artifact` | zero/one artifact outcome and evidence/citation counts |

The Pydantic model forbids extra fields. There is intentionally no generic
metadata map and no field for goal text, prompt text, query text, URL, headers,
body, excerpt, finding, artifact content, exception message, or traceback.
Durations use an injectable monotonic clock; deterministic tests inject a
ticking clock where exact duration behavior matters.

Normal CLI stdout is unchanged. `--trace-json` on `run` or `resume` writes
redacted JSON Lines to stderr. No trace file is opened or written by default.
A resumed invocation receives a new `run_id` under the same `thread_id`.

## Preserved Day 08–12 distinctions

- Resume emits a `checkpoint: resumed` outcome and continues from durable graph
  state; completed work is not replanned or re-executed.
- Context projection emits `within_budget` or `compacted`; irreducible pressure
  emits a controlled `refused` outcome before the model seam.
- Delegation records separate success, controlled failure, and cancellation as
  counts, while preserving partial sibling evidence through deterministic
  fan-in.
- Reviewer `continue`, `replan`, and `finish` routes remain distinct; replans
  receive their own outcome record.
- Citation validation reports rejected counts without recording rejected URLs.
- Artifact tracing distinguishes written, failed, and not requested without
  recording a path or report content.

## Evaluation harness and baseline

`evals/dataset.json` maps eight stable cases to executable invariant tests.
`evals/run_evals.py` executes each case independently with the current Python
environment and emits one JSON report. It records no test stdout/stderr or raw
failure payload. A failing case receives only `invariant_test_failed`.

The transparent metrics are:

```text
case_pass_rate = passed cases / total cases
invariant_pass_rate = invariants in passing cases / total declared invariants
```

A failed case conservatively receives zero passed invariants; partial credit is
not inferred from test output. The checked-in `evals/baseline.json` records 8/8
cases and 26/26 invariants passing. It covers safe evidence/citation/artifact,
invented-citation rejection, unsafe pre-provider URL denial, redacted provider
failure, context refusal, reviewer replan/finish, delegation partial failure,
and SQLite checkpoint/resume without duplicate completed work.

Run it with:

```bash
uv run python evals/run_evals.py
```

These results say only that the deterministic contracts held. They do not
measure factual correctness, claim-evidence entailment, benchmark superiority,
model calibration, real provider behavior, live DNS, wall-clock service
latency, or model/provider cost.

## Deferred production hardening

The residual risks and deferred controls are listed in
[the Day 13 threat model](threat-model.md). In particular, the MVP does not bind
validated DNS answers to a direct socket, cannot independently police every
redirect followed inside the remote Jina Reader, has no production telemetry
backend or retention policy, and still uses local SQLite rather than a
multi-process or multi-tenant persistence service.
