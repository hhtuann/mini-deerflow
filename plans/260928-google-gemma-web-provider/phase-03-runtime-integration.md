# Phase 3 - Runtime and Entrypoint Integration

## Overview

- Priority: P1
- Status: pending
- Effort: 3h
- Goal: wire Gemma and Google web provider into all live entrypoints without breaking dependency injection, offline mode, or structured synthesis.

## Critical warning

`create_default_agent_runtime` is GitNexus **CRITICAL** (17 impacted symbols, 11 flows). Re-run impact immediately before editing. PDG at model construction shows the returned model feeds structured-output selection and downstream action/research composition; a PDG `risk: UNKNOWN` is unresolved and does not override the CRITICAL callgraph result.

## Exact file changes

| File | Action | Change |
| --- | --- | --- |
| `src/mini_deerflow/runtime.py` | modify | Use provider-neutral model factory; default to Google content client/provider; pass same settings key; always honor configured structured-output mode; preserve injected provider seam. |
| `src/mini_deerflow/cli.py` | modify | Pass `settings.structured_output_mode` to standalone `plan`; run/resume continue through default runtime. |
| `src/mini_deerflow/demo/offline_scenario.py` | modify | Use new settings/model protocol; remove OpenAI casts/Jina placeholders; retain injected fake model/provider/resolver. |
| `src/mini_deerflow/demo/app.py` | modify | Replace Jina readiness caption with honest Google model/Search/URL Context wording. |
| `tests/test_runtime_composition.py` | modify | Assert Google default composition and prompt-JSON propagation; injected provider must bypass Google client construction. |
| `tests/test_cli.py` | modify | Update settings fixtures and standalone plan structured-mode expectation. |
| `tests/test_demo_offline.py` | modify | Patch/deny Google model and SDK creation; prove offline mode does not read key/network. |
| `tests/test_live_demo_backend.py` | modify | Update settings fixtures; preserve run/continue/resume/load sandbox/runtime assertions. |
| `tests/test_demo_app.py` | modify | Update environment and live readiness caption assertion. |
| `tests/test_final_acceptance.py` | modify | Generalize model factory typing/settings while preserving full deterministic runtime acceptance. |

## Implementation steps

1. Capture current diffs for every overlapping file; do not revert budget/synthesis/sandbox work.
2. Re-run fresh impact on `create_default_agent_runtime`; report CRITICAL risk in the implementation log.
3. Change `ModelFactory` to the provider-neutral model protocol.
4. In `create_default_agent_runtime`:
   - construct core model once via injected/default factory;
   - set `structured_output_mode = settings.structured_output_mode` without provider-class checks;
   - if `web_provider` is supplied, use it and do not construct Google SDK/client;
   - otherwise create Google client/provider from `settings.google_api_key`, `web_model_name`, and web timeout;
   - leave registries, delegation, reviewer, replanner, synthesis, budgets, evidence, and checkpointer composition unchanged.
5. Keep `open_default_agent_runtime` signature/provider injection stable.
6. Forward prompt-JSON mode in `run_research_planner`; otherwise CLI `plan` would take the provider-native path accidentally.
7. Update offline scenario to build inert settings with `_env_file=None` and inject all external seams before any Google constructor can run.
8. Update live UI wording only; do not display key/model raw configuration.

## Invariants

- Tool names/order unchanged: `list_files`, `read_file`, `web_search`, `web_fetch`, `delegate_research` (+ optional `write_file` execution-only).
- Same core model instance serves planner/selector/reviewer/replanner/synthesizer.
- Same credential value configures core model and web client, but is never persisted in `AgentState`, checkpoint, trace, or UI.
- Offline provider/model injection remains authoritative; no hidden fallback to live services.
- Existing `WebFetchTool` remains the pre/post URL safety boundary.

## Success criteria

- CLI `plan`, `run`, `resume`, Streamlit live run/continue/resume/load, and offline run/continue/resume/load compose successfully.
- Prompt-JSON is used for Gemma across every structured seam.
- Injected provider tests instantiate no Google SDK client.
- No registry, budget, evidence, citation, persistence, or sandbox contract changes.

## Risks and mitigations

- Eager Google construction breaks offline tests: ensure provider branch is lazy and covered by constructor-denial tests.
- Provider-class conditional reappears: assert configured mode with a non-OpenAI fake model.
- Live configuration fails late: instantiate/validate settings before job execution and preserve static user-facing errors.

## Todo

- [ ] Capture overlapping dirty diffs.
- [ ] Re-run CRITICAL impact and inspect direct caller/processes.
- [ ] Integrate model/provider in runtime.
- [ ] Update CLI plan and offline/live demo seams.
- [ ] Run composition, CLI, demo, resume, and acceptance tests.

