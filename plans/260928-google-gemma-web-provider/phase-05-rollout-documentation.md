# Phase 5 - Documentation, Rollout, and Rollback

## Overview

- Priority: P2
- Status: pending
- Effort: 2h
- Goal: make the provider cutover explicit, reproducible, safe, and reversible.

## Exact file changes

| File | Action | Change |
| --- | --- | --- |
| `.env.example` | modify | Show one Google key, Gemma core model, Gemini web model, timeouts/retries, prompt-JSON; remove Z.AI/Jina values. |
| `README.md` | modify | Update architecture, setup, Streamlit live instructions, technology stack, readiness checks, quota/error guidance. |
| `docs/project-overview-pdr.md` | modify | State the Google-backed live boundary and model-mediated URL Context evidence limitation. |
| `docs/system-architecture.md` | modify | Update composition/data flow while preserving workflow nodes and evidence contracts. |
| `docs/codebase-summary.md` | modify | Replace OpenAI/Jina adapter summary and list the new provider module. |
| `docs/code-standards.md` | modify | Document Google provider normalization/error/metadata rules and one-key secret policy. |
| `docs/threat-model.md` | modify | Replace Jina remote-fetch assumptions with Google Search/URL Context data disclosure, redirect visibility, SDK logging, and quota risks. |

Do not rewrite historical `report-ngay-*`, `*-day-*`, or roadmap milestone documents; they describe prior architecture. No documentation-validator script exists in this repository, so manually verify relative links/symbols and use code search plus Markdown review.

## Migration steps for users

1. Stop Streamlit/CLI processes so they do not retain old settings.
2. Back up the local `.env` outside version control.
3. Replace old model/Jina variables with:

   ```dotenv
   MINI_DEERFLOW_GOOGLE_API_KEY=<Google AI Studio key>
   MINI_DEERFLOW_MODEL_NAME=gemma-4-26b-a4b-it
   MINI_DEERFLOW_WEB_MODEL_NAME=gemini-2.5-flash
   MINI_DEERFLOW_TEMPERATURE=0
   MINI_DEERFLOW_REQUEST_TIMEOUT=120
   MINI_DEERFLOW_MAX_RETRIES=2
   MINI_DEERFLOW_WEB_REQUEST_TIMEOUT=20
   MINI_DEERFLOW_STRUCTURED_OUTPUT_MODE=prompt_json
   ```

4. Remove `MINI_DEERFLOW_API_KEY`, `MINI_DEERFLOW_BASE_URL`, `MINI_DEERFLOW_JINA_API_KEY`, and Jina-only response settings.
5. Run `uv sync --locked`, targeted tests, then full gates.
6. Start Streamlit and create a **new chat/thread** for the smoke; old completed fallback turns remain historical checkpoint data.

## Rollout order

1. Merge dependencies/config/model.
2. Merge provider and tests.
3. Merge runtime integration only after provider contracts pass.
4. Run complete regression and GitNexus change analysis.
5. Configure the Google key locally; never commit `.env`.
6. Run one low-budget live smoke, then Streamlit live smoke.
7. Monitor trace categories and Google AI Studio usage/quota. Treat the documented 500 free grounded searches/day as current external pricing, not an SLA.

## Rollback

Rollback trigger: repeated auth/model incompatibility, malformed grounding metadata, citation ownership failure, SSRF regression, or unacceptable quota behavior.

1. Stop live processes.
2. Revert the migration commit(s), including `uv.lock`; do not selectively restore runtime without matching dependencies/config/tests.
3. Restore the backed-up old `.env` locally.
4. Run old targeted/full tests and one provider smoke before reopening Streamlit.
5. Preserve SQLite/checkpoints; schemas are unchanged. Start a new thread rather than replaying a provider-failed completed turn.

No runtime dual-provider fallback is planned. It would double configuration/test surface and hide provider failures; Git rollback is the explicit recovery mechanism.

## Operational guidance

- `rate_limit`: wait for quota reset or enable billing; do not rotate/retry keys automatically.
- `authentication`: verify Google AI Studio key/project access; do not print the key.
- `endpoint`: verify both model IDs are available to the project/region.
- `malformed_response`: retain redacted trace/category and stop; do not accept model-authored fallback URLs.
- Partial answers are expected when web calls fail; UI must not mislabel these as model-service-only failures.

## Success criteria

- Fresh setup requires one clearly named key and no Jina/Z.AI values.
- Current docs match source and UI wording.
- Threat model states Google-specific trust/residual-risk boundaries.
- Rollout and rollback preserve checkpoint compatibility and user changes.

## Todo

- [ ] Update example env and current-state docs.
- [ ] Verify links, symbols, and commands manually.
- [ ] Execute migration/runbook on a clean local shell.
- [ ] Complete live smoke and record only redacted outcomes.
- [ ] Confirm rollback steps remain valid after final diff.

