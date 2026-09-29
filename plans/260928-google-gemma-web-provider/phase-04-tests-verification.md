# Phase 4 - Contract, Security, and Regression Verification

## Overview

- Priority: P1
- Status: pending
- Effort: 3h
- Goal: prove provider normalization, security, runtime compatibility, and unchanged synthesis/evidence behavior without consuming live quota in the default suite.

## Test matrix

| Area | Files | Required assertions |
| --- | --- | --- |
| Settings/model | `tests/test_config.py`, `tests/test_model.py`, `tests/test_structured_output.py` | One redacted Google key; exact model IDs; prompt-JSON; invalid/missing config. |
| Google provider | `tests/test_web_provider.py` | Tool config, grounding/URL metadata parsing, dedupe/caps, malformed response, error categories, cancellation, no leakage. |
| Web safety/tools | `tests/test_web_tools.py`, `tests/test_web_safety.py`, `tests/test_web_contracts.py` | Pre-call SSRF denial, returned-URL denial, typed failures, truncation, interface compatibility. |
| Runtime | `tests/test_runtime_composition.py`, `tests/test_runtime_resume.py` | Google defaults, injected seams, tool order, prompt mode, checkpoint/resume no replay. |
| CLI/demo | `tests/test_cli.py`, `tests/test_live_demo_backend.py`, `tests/test_demo_app.py`, `tests/test_demo_offline.py` | All entrypoints, honest labels, offline no credential/network/provider creation. |
| Evidence/tracing | `tests/test_evidence.py`, `tests/test_tracing.py`, `tests/test_answer_synthesis.py` | Metadata URLs become evidence; invented URLs rejected; provider categories redacted; Vietnamese/table/citation behavior unchanged. |
| End-to-end | `tests/test_final_acceptance.py` | Deterministic complete run and follow-up still succeed using injected model/provider. |

## High-value scenarios

1. Search response contains two valid chunks, duplicate URL, invalid URL, unlinked generated URL -> only ordered unique metadata URLs accepted.
2. Search support points to multiple chunks -> snippet association deterministic and bounded.
3. Fetch target is localhost/private -> Google fake has zero calls.
4. Fetch metadata returns localhost/private final URL -> content rejected before evidence and omitted from trace.
5. Provider throws exception containing key/body/URL -> tool result/trace expose only static category/type.
6. 429 on search/fetch -> failed call consumes workflow budget; run may synthesize partial result; no automatic retry storm.
7. Injected offline provider -> no `google.genai.Client` or `ChatGoogleGenerativeAI` construction.
8. Non-OpenAI fake model + `prompt_json` -> planner/selector/reviewer/replanner/synthesis all bind through prompt parser.
9. Existing Vietnamese comparison request -> evidence-backed table/citations still render; provider swap does not relax synthesis rules.

## Verification sequence

Run narrowest first:

```powershell
uv run pytest -q -p no:cacheprovider tests/test_config.py tests/test_model.py tests/test_structured_output.py
uv run pytest -q -p no:cacheprovider tests/test_web_provider.py tests/test_web_tools.py tests/test_web_safety.py tests/test_web_contracts.py tests/test_tracing.py
uv run pytest -q -p no:cacheprovider tests/test_runtime_composition.py tests/test_cli.py tests/test_live_demo_backend.py tests/test_demo_app.py tests/test_demo_offline.py tests/test_final_acceptance.py
```

Then full quality gates:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest -q -p no:cacheprovider
git diff --check
node .gitnexus/run.cjs detect-changes --scope all --repo .
```

If Windows temp ACLs fail, set `TEMP`/`TMP` and `--basetemp` to a verified directory inside `.mini-deerflow`; do not weaken tests. If GitNexus returns `partial: true` or `truncated: true`, paginate/rerun until all changes are accounted for.

## Live smoke (opt-in, after offline gates)

Use a fresh thread ID and one bounded query. Do not add this to default pytest.

```powershell
uv run mini-deerflow run "Ronaldo và Messi ai mạnh hơn? Hãy so sánh bằng bảng và dẫn nguồn." `
  --thread-id google-live-smoke-001 `
  --trace-json
```

Accept only when trace/evidence shows:

- successful `web_search` and `web_fetch` outcomes;
- no `authentication`, `rate_limit`, or `malformed_response` provider failure;
- at least one accepted evidence record;
- every rendered citation belongs to accepted evidence;
- no credential/provider payload in stdout/stderr.

Repeat once in Streamlit live mode with a new chat. Offline walkthrough must also pass with network disabled.

## Success criteria

- Default suite makes no live Google calls and consumes no quota.
- Full suite and all quality gates pass.
- HIGH/CRITICAL GitNexus changes are reviewed, not waived by shared-axis or UNKNOWN results.
- One controlled live smoke validates the real external contract.

## Todo

- [ ] Add/update focused tests.
- [ ] Run targeted suites.
- [ ] Run full quality gates.
- [ ] Run and review GitNexus detect-changes.
- [ ] Run opt-in CLI and Streamlit live smokes.

