---
title: "Google Gemma model and grounded web provider"
description: "Migrate the live agent from OpenAI-compatible GLM plus Jina to Gemma 4 plus Gemini grounded search and URL context using one Google AI Studio key."
status: pending
priority: P1
effort: 16h
branch: feat/budget-aware-answer-synthesis
tags: [feature, backend, api, security, critical]
created: 2026-09-28
---

# Google Gemma and Google Web Provider Plan

## Outcome

Live CLI and Streamlit use `gemma-4-26b-a4b-it` for planning/action/review/synthesis and `gemini-2.5-flash` for Google Search plus URL Context. One `MINI_DEERFLOW_GOOGLE_API_KEY` authenticates both. Offline mode remains deterministic and network-free.

## Critical blast-radius warning

Fresh GitNexus analysis reports `create_default_agent_runtime` as **CRITICAL**: 17 impacted symbols, 11 execution flows, and 3 modules. Its direct caller is `open_default_agent_runtime`; affected paths include CLI run/resume and Streamlit live/offline run/continue/resume/load. Do not edit this composition root before re-running impact, and do not merge without all entrypoint regressions.

Additional graph findings: `Settings` is MEDIUM (20 impacts, 5 direct), `JinaWebProvider`/`WebProvider` are MEDIUM (30 impacts, 5 direct), while `WebSearchTool` and `WebFetchTool` are LOW. The design therefore preserves tool/domain interfaces and changes adapters behind them.

## Chosen architecture

```text
Settings.google_api_key
  +-> ChatGoogleGenerativeAI(gemma-4-26b-a4b-it)
  |     -> existing planner / selector / reviewer / replanner / synthesizer
  +-> GoogleGenAIContentClient
        -> GoogleWebProvider(gemini-2.5-flash)
             +-> Google Search grounding metadata -> SearchResult[]
             +-> URL Context metadata/text -> FetchedPage
                    -> existing WebSearchTool / WebFetchTool
                    -> existing evidence and citation pipeline
```

Decisions:

- Use `langchain-google-genai` for the LangChain chat-model surface; it uses Google's native Gen AI stack and avoids an unverified OpenAI-compatibility path for Gemma.
- Import `google-genai` directly for Search and URL Context; declare it directly even if transitive.
- Keep `WebProvider`, `SearchResult`, `FetchedPage`, tool names, budgets, evidence schemas, and checkpoint schemas unchanged.
- Keep `prompt_json` as the default structured-output mode and pass it explicitly at every composition path; do not assume Gemma provider-native structured output.
- Hard-cut live configuration to one Google key. Do not retain dual Jina/OpenAI provider selection or feature flags.
- Never edit or print the real `.env`; update only `.env.example` and migration docs.

## Phases

| # | Phase | Effort | Dependency | File |
| --- | --- | --- | --- | --- |
| 1 | Google model/config foundation | 3h | None | [phase-01](./phase-01-google-model-config.md) |
| 2 | Grounded Google web provider | 5h | Phase 1 | [phase-02](./phase-02-google-web-provider.md) |
| 3 | Runtime and entrypoint integration | 3h | Phases 1-2 | [phase-03](./phase-03-runtime-integration.md) |
| 4 | Contract, security, and regression tests | 3h | Phases 1-3 | [phase-04](./phase-04-tests-verification.md) |
| 5 | Documentation, rollout, and rollback | 2h | Phase 4 | [phase-05](./phase-05-rollout-documentation.md) |

## Migration contract

New live `.env` values:

```dotenv
MINI_DEERFLOW_GOOGLE_API_KEY=<Google AI Studio key>
MINI_DEERFLOW_MODEL_NAME=gemma-4-26b-a4b-it
MINI_DEERFLOW_WEB_MODEL_NAME=gemini-2.5-flash
MINI_DEERFLOW_STRUCTURED_OUTPUT_MODE=prompt_json
```

Remove obsolete `MINI_DEERFLOW_API_KEY`, `MINI_DEERFLOW_BASE_URL`, and `MINI_DEERFLOW_JINA_API_KEY`. Missing Google key must fail at settings validation before network activity. Historical day reports stay unchanged; only current-state docs are migrated.

## Verification gates

1. Targeted unit/provider/composition/demo tests pass without network.
2. Full `pytest`, Ruff check, Ruff format check, and `git diff --check` pass.
3. Fresh GitNexus `detect-changes --scope all` has no partial/truncated result; HIGH/CRITICAL flows are explicitly reviewed.
4. Opt-in live smoke produces successful `web_search` and `web_fetch`, non-empty accepted evidence, and citations owned by normalized provider metadata.
5. Streamlit live mode answers a new-thread query; offline walkthrough still works with credentials/network disabled.

## Global risks

- Google free-tier/search quotas can change; HTTP 429/provider quota failures must become `rate_limit`, consume the admitted tool call, and lead to a visible partial result rather than retries or secret leakage.
- Google Search grounding URIs may be provider redirect URLs. Preserve metadata URIs exactly; never invent/parse source URLs from generated prose.
- URL Context content is model-mediated, not a byte-exact page fetch. Label this in docs and retain final-URL validation; direct transport fidelity is deferred.
- Google may not expose every redirect hop. Revalidate every URL it does expose and document residual remote-redirect risk.
- Existing dirty worktree overlaps runtime/tests/docs. Implementation must inspect and preserve current diffs; no reset, checkout, or broad rewrite.

## Open questions

No blocking product questions. If future requirements demand byte-exact page capture or provider fallback, plan them separately; neither belongs in this migration.

