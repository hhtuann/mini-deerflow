# Phase 1 - Google Model and Configuration Foundation

## Overview

- Priority: P1
- Status: pending
- Effort: 3h
- Goal: establish one Google credential, provider-neutral model typing, and a Gemma-backed LangChain model without touching runtime web composition yet.

## Context

- `src/mini_deerflow/config.py` currently requires generic `api_key`, defaults to Z.AI `base_url`/GLM, and separately carries `jina_api_key`.
- `src/mini_deerflow/model.py`, `runtime.py`, `planner.py`, and offline/test code are typed to `ChatOpenAI`.
- `structured_output.py` already provides the stable prompt-JSON seam used by all structured model adapters.
- The standalone CLI `plan` path currently calls `create_research_plan` without forwarding `settings.structured_output_mode`; this must be corrected for Gemma.

## Requirements

- One required secret: `MINI_DEERFLOW_GOOGLE_API_KEY`.
- Core default model: `gemma-4-26b-a4b-it`.
- Web default model: `gemini-2.5-flash`.
- No OpenAI base URL or Jina credential in current live settings.
- Keep request timeout, model retries, temperature, web timeout, and `prompt_json` bounds.
- Secrets remain `SecretStr` and absent from repr/errors/traces.

## Exact file changes

| File | Action | Change |
| --- | --- | --- |
| `pyproject.toml` | modify | Replace `langchain-openai` with bounded `langchain-google-genai`; add direct `google-genai` dependency. |
| `uv.lock` | regenerate | Lock Google integration/SDK and remove no-longer-reachable OpenAI packages where safe. |
| `src/mini_deerflow/config.py` | modify | Add required `google_api_key`; defaults for `model_name` and `web_model_name`; remove `api_key`, `base_url`, `jina_api_key`, and Jina-only response-byte setting. |
| `src/mini_deerflow/structured_output.py` | modify | Add a small provider-neutral `StructuredChatModel` protocol combining sync/async invocation and native structured-output capability. Do not alter parsing semantics. |
| `src/mini_deerflow/model.py` | modify | Construct `ChatGoogleGenerativeAI` with Gemma model, shared Google key, temperature, timeout, retries; return provider-neutral protocol type. |
| `src/mini_deerflow/planner.py` | modify | Replace `ChatOpenAI` annotation/import with provider-neutral structured model typing; update GLM-specific comment to provider-neutral prompt-JSON rationale. |
| `tests/test_config.py` | modify | Verify env loading, default model names, key redaction, missing-key validation, invalid bounds, and obsolete keys not satisfying the new required key. |
| `tests/test_model.py` | modify | Mock `ChatGoogleGenerativeAI`; assert exact Google constructor arguments and no OpenAI base URL. |
| `tests/test_structured_output.py` | modify if needed | Assert the existing prompt-JSON path works with a provider-neutral fake and remains strict. |

## Implementation steps

1. Before each symbol edit, run GitNexus impact for `Settings`, `create_chat_model`, and `create_research_plan`; retain MEDIUM/LOW results in implementation notes.
2. Change dependencies, then run `uv lock` and `uv sync`. Do not hand-edit the lockfile.
3. Replace settings fields with:
   - `google_api_key: SecretStr`;
   - `model_name="gemma-4-26b-a4b-it"`;
   - `web_model_name="gemini-2.5-flash"`;
   - existing temperature/model timeout/model retries/web timeout;
   - `structured_output_mode="prompt_json"`.
4. Define the minimum shared chat-model protocol in `structured_output.py`; avoid introducing a general provider registry.
5. Build `ChatGoogleGenerativeAI` in `create_chat_model`. Pass the secret object/value through the supported constructor without materializing it in logs.
6. Generalize planner/runtime-facing type hints. Do not weaken runtime validation in selector/reviewer adapters.
7. Add focused tests before proceeding to web integration.

## Success criteria

- `create_chat_model` returns the Google LangChain adapter configured for exact Gemma ID.
- No production import of `langchain_openai.ChatOpenAI` remains.
- Settings need only one external credential and redact it.
- Offline fakes can still satisfy the model protocol without inheriting a provider class.
- Lockfile is reproducible with `uv sync --locked`.

## Risks and mitigations

- Constructor parameter drift: pin compatible minimum versions and assert mocked constructor arguments.
- Gemma structured output variance: keep prompt-JSON parsing and bounded retries; do not select `native` by model class.
- Dirty overlapping files: patch narrow ranges and review `git diff` after every file.

## Todo

- [ ] Run required impacts.
- [ ] Update dependencies and lockfile.
- [ ] Migrate settings and model factory.
- [ ] Generalize model protocol/type hints.
- [ ] Add and run focused config/model tests.

