# Phase 3 · Task 3.1 — Opt-in Live Provider E2E

Issue [#18](https://github.com/hhtuann/mini-deerflow/issues/18) · Roadmap [#16](https://github.com/hhtuann/mini-deerflow/issues/16)

## Purpose and status

This task adds a **manual-only, double-opt-in** executable which exercises the actual live provider, production `LiveDemoBackend`, FastAPI, `SlideAgentService`, editable PPTX renderer and validator, and `PptxArtifactStore` — **without any scripted planner or mocked provider response**.

**Status:** Earlier VI/EN live successes were implementer-reported for the prior implementation; they were **not independently replayed or audited** by the Coordinator. The safety correction below removes global clipping and changes density handling, so those results do not establish live acceptance of the corrected HEAD. No new paid calls were made for this correction. Live quality, claim/source accuracy and Office visual acceptance remain **UNVERIFIED**. GitHub CI remains credential-free.

## How to execute locally

1. Run `uv sync` at the repository root.
2. Load your **real** `MINI_DEERFLOW_API_KEY` securely into the process environment (not just `.env`). Do not put it in a PR, GitHub Actions secret for PRs, source file, terminal history or chat.
3. Explicitly opt in with `MINI_DEERFLOW_LIVE_E2E_CONSENT=YES` and run:

```sh
export MINI_DEERFLOW_LIVE_E2E_CONSENT=YES
uv run --env-file .env python scripts/live_e2e.py --allow-paid-live --case both --timeout-seconds 1200 --output .mini-deerflow/live-e2e/report.json
```

For PowerShell, set `$env:MINI_DEERFLOW_LIVE_E2E_CONSENT = 'YES'`. The same command securely loads the local `.env` into the child environment; alternatively load `$env:MINI_DEERFLOW_API_KEY` from a trusted local credential manager and omit `--env-file`. Default timeout is **1,200 seconds per case**, accepted bounds are **30–1,800 seconds**. A single `--case vi` or `--case en` is diagnostic only; full acceptance requires **both**.

The report contains safe, allowlisted metrics only. Keep it local and uncommitted. By default `python scripts/live_e2e.py` exits with BLOCKED **before importing any provider-capable module**. The opt-in requires all three: flag, exact YES consent and a non-fixture key. The dummy CI key is rejected.

**Real monetary cost warning:** This does call paid/network providers when enabled. Runtime bounds (8 research tool calls, 2 per step, 1 research replan, recursion 60, 120-second research model request timeout, 240-second slide-planner request timeout, 10-second Wikipedia timeout, zero SDK retries, and 30–1,800 second per-case wall limit) **are not** a hard limit on model requests/tokens, total dollars or remote billing. Slide planning has at most two parse attempts per plan and two plan attempts; density failure may use the second plan attempt. The controller can terminate the child process but cannot guarantee cancellation of already-sent provider requests. Obtain explicit account-level budget approval first.

## Production pipeline and isolation

Each case runs in an independent, killable Python subprocess with its own temporary SQLite/artifact workspace. The parent supervises deadlines and never prints provider stdout/stderr.

```text
consent + credential + CLI flag
    |
    +-- Vietnamese test (fresh local SQLite/workspace)
    |     POST /api/live/conversations -- real model/research, pinned slide-creation skill
    |     GET /api/live/jobs/{job_id} -- bounded polling to completed / failed
    |     GET /api/live/conversations/{thread} -- evidence + accepted citations
    |     POST .../turns/{turn}/presentation -- real DeckPlan, renderer and store
    |     GET .../presentation/metadata and verified PPTX bytes
    |     independently reopen stored DeckSpec, validate OOXML and compare SHA/bytes
    |
    +-- English test (separate fresh storage and worker)
    |
    +-- sanitized JSON summary; temporary files deleted
```

The two predefined public cases are:
- **vi:** benefits/limitations of solar energy for the electricity grid; audience: students/lecturers; 8 academic-theme slides.
- **en:** urban tree planting and summer heat mitigation; audience: students/researchers; 8 academic-theme slides.

The `slide-creation` skill is explicitly selected on the research turn **before** deck generation. There is no regeneration of previously stored turns and no automatic replay of uncertain POST requests.

## Automated validation and truthful failures

After a 202 job submission the runner checks terminal job status and completed source turn; a queued job is never treated as success. It checks accepted-citation membership against the actual safe evidence set, then requests slides with the configured provider, validates the persisted DeckSpec and downloaded PPTX (correct MIME/filename, 8 slides, at least 4 native layouts, identical stored/downloaded bytes and SHA-256).

A failed job, timeout, malformed output, corrupt artifact, HTTP error or unexpected exception is a **FAIL** with a bounded stage/reason code, not an invented successful deck. The child deliberately suppresses provider exception strings, raw URLs, stack traces and untrusted response text.

The JSON report includes UTC timestamps, provider **hostname**, configured model name, case status, research/slide wall times, evidence count, accepted citation count, membership boolean, research tool-call count, native layout count, slide count, PPTX SHA-256 and file size. **Total model calls, tokens and provider cost are NULL** because the API does not expose trustworthy metrics; tool calls cannot be treated as model-call counts.

**A green live structural check is not proof of factual accuracy or presentation aesthetics.** Each case's `manual_fact_check` and `manual_office_visual` remain `UNVERIFIED` regardless of automated outcome.

## Diagnosing a failed research job

If the report returns `research_job_failed`, generation has **not** reached
the slide planner; an API key passing the consent gate only proves the key is
present, **not** that it is accepted by the provider.

New runner reports include `research_failure_family` and safe, allowlisted
`last_failed_node`, `last_trace_error_category` and
`last_trace_error_code` on job failure. The family is classified by exception
**type only** (for example provider authentication, rate limit, connection,
timeout, structured-output validation, context budget, recursion or storage).
If these fields are `null` or `unclassified`, the cause is still unknown:
do **not** label it an authentication or provider outage without further
evidence. No raw exception messages, prompts, stack traces or provider payloads
are retained.

These fields cannot retroactively explain a report generated by an earlier
harness version: its temporary subprocess workspace was deleted. Avoid
repeating the full two-case live run blindly. First validate the provider
model/configuration through a separately owner-approved, minimal one-request
probe (which can still be billable); if it succeeds, rerun one language case
with the updated harness and inspect only the safe diagnostic fields.

## Manual quality rubric

For each of the Vietnamese and English cases, an independent human reviewer must:
1. Compare at least **three important research claims** to original source pages. Mark **SUPPORTED / PARTIAL / UNSUPPORTED / UNVERIFIABLE** with precise source/date/context. Evidence URL membership is not fact-checking.
2. Verify claim-to-citation alignment, any ungrounded inferred conclusions, source reliability and research uncertainty.
3. Check each slide for audience fit, linguistic consistency, narrative coherence, factual alignment with research and honest treatment of numeric/illustrative data.
4. Inspect a genuinely rendered PPTX in PowerPoint/LibreOffice under Task 3.2 (the isolated harness deletes the temporary PPTX; it cannot substitute for Office acceptance).
5. Document provider/model, timestamp, observed latency, failures and externally measured token/cost records when available — without credentials or unredacted model responses.

Manual fact-check and Office decisions must be reported as **separate human gates**, not automated PASS.

## CI regression scope

`tests/test_live_e2e_harness.py` provides credential-free checks for the consent gate, dummy-key rejection, safe default, worker invocation, timeout validation, English/Vietnamese brief boundaries, polling and redacted report structure. Normal CI does **not** run the live provider test; no live GitHub Actions workflow is added.

## Corrective fix (Phase 3.1)

Recommended invocation for this provider (1200 s per case):

```sh
uv run --env-file .env python scripts/live_e2e.py --allow-paid-live --case both --timeout-seconds 1200 --output .mini-deerflow/live-e2e/report.json
```

### Historical diagnosis and prior implementation

1. Original both-case failure: research job failed with family `provider_timeout` — the harness child forced `request_timeout=30.0`; a trivial provider call answers in ~3s but the planner's first structured call exceeds 30s on glm-5.3 (reasoning + strict JSON). Fixed: `WORKER_REQUEST_TIMEOUT_SECONDS = 120.0`.
2. Vanishing-worker bug: after the timeout fix, a deadline breach produced NO child result (parent reported hard_timeout/stage worker) because TestClient/ApiJobs teardown waits for an in-flight bounded request. Fixed: `poll_deadline()` with `WORKER_POLL_GRACE_SECONDS` (250) so polling stops early enough to write the result before the hard kill; wall-clock ceiling raised to 1800s (measured research alone ~682s at 900s ceiling).
3. Deterministic fallback crash (production bug): `build_user_facing_answer` raised pydantic ValidationError when a multi-citation finding summary (legal up to 4,000 chars) exceeded `AnswerClaim.text` max 1,000 — turning recoverable synthesis failures into total turn failures at the synthesize node (live evidence: last_failed_node=synthesize, family structured_validation). Fixed: `_bound_public_text()` truncates at sentence/word boundaries; limitations and sources capped too. Regression: tests/test_evidence.py.
4. Live presentation failures: schema-valid plans exceeded the renderer density preflight. The previous implementation added global prefix clipping; the Coordinator rejected it because shortening can remove qualifiers or negations and alter evidence meaning. **That clipping implementation is now removed entirely.** Slide-planner request timeout remains 240s (`WORKER_SLIDE_PLANNER_TIMEOUT_SECONDS`).
5. Provider SDK exception types are classified in tracing (`model_provider_failure`); node attribution prefers node-bearing failed traces. The previous message pass-through was not a safe-constant boundary; the correction below replaces it with fixed prose and report-side allowlisting.

### Historical live evidence (implementer-reported, not independently audited)

The following results were reported for the previous clipping-based implementation (glm-5.3 via api.z.ai, 2026-10-24 UTC). Local reports/PPTX files were not available to the GitHub reviewer. They are historical evidence only, **not live verification of this corrective HEAD**.

- **vi: PASS** — research 502.89s (7 tool calls, 11 evidence, 7 accepted citations, membership valid), presentation 61.16s, 8 slides / 7 distinct layouts, PPTX 46,452 bytes sha256 `bef2555925d277e034fdab645977a537b0a7f580b611d19ee0bf29819fdc2399`, structure valid. Report: `.mini-deerflow/live-e2e/final6-vi.json`
- **en: PASS** — research 735.66s (7 tool calls, 14 evidence, 4 accepted citations, membership valid), presentation 60.16s, 8 slides / 7 layouts, PPTX 39,511 bytes sha256 `f8d60c5d0fa8c8a80207c1a7a700d633a48c65027959e399513ab38ddee839dc`, structure valid. Report: `.mini-deerflow/live-e2e/final-en.json`
- One minimal provider smoke test executed first (2.9s latency, type-only diagnostics). Bounded diagnostics: 2 isolated slide-planner probes + 1 offline-fixture replay before final paid runs.
- Manual fact-check and Office visual acceptance remain **UNVERIFIED** (report fields).
- Earlier failed-run evidence retained under `.mini-deerflow/live-e2e/` (diag-vi.json, fix*.json, final*.json, diag2/diag3) — do not delete.

### Historical offline verification (c84e2a6)

- pytest: 1283 passed, 11 skipped (9 Windows symlink-privilege, 1 Python 3.13+ native API, 1 POSIX-only), 223 subtests
- ruff check/format: PASS (143 files); `uv lock --check --offline`: PASS (99 packages)
- evaluator: 9/9 cases, 36/36 invariants
- Prior regressions: harness 16 tests; evidence fallback bounds; slide-agent 104 tests. New corrective verification is recorded below.

## Coordinator safety correction (review #6394298547)

- **Error trust boundary:** `SlideAgentValidationError` maps by type to fixed prose: `Presentation planning failed validation.` Neither its text nor its cause is inspected or reflected in `JobDTO.error_message`. The live report separately allows only a small exact set of public constant messages; all arbitrary job text, even short strings, is omitted.
- **Evidence-safe density handling:** the service never shortens accepted slide text to make it fit. It sends the same bounded evidence projection back through one permitted re-plan with explicit instructions to preserve numbers, units, negations, uncertainty, source attribution and critical qualifiers, using fewer complete claims or a less dense layout. This guidance survives a parse retry too. Every candidate must pass the unchanged schema, renderer, independent validator and artifact checks. If it still cannot fit, generation fails closed without publication or overwriting an earlier artifact.
- **No automatic semantic guarantee:** re-planning is model-authored, not a proof of unchanged meaning. Structural validation/citation membership cannot establish semantic equivalence. Claim/source and content QA remain required. This correction performs no automatic clipping; it never publishes a prefix-truncated claim or table cell as a substitute for an overflowing candidate.
- **Negative regressions:** secret/URL-bearing validation exceptions and even an exception whose `__str__` raises must produce fixed public prose; arbitrary report error text is rejected. Long claims ending with uncertainty/negation and long table cells must remain unchanged and fail closed when both plans overflow. Fitting qualified claims/cells are published verbatim; a valid re-plan is independently rendered and verified.
- **Corrective local verification:** `uv sync --locked`, Ruff lint/format (143 files), and lock validation (99 packages) passed. Full `pytest`: **1,290 passed, 11 skipped, 223 subtests passed**; focused slide-agent/harness suites: **127 passed**. Evaluator: **9/9 cases, 36/36 invariants**. Frontend typecheck/lint/build passed, Vitest **38 passed**, real-backend Chromium Playwright **4 passed** (including 375×812 mobile). Skip reasons: nine unavailable Windows symlink-privilege cases, one native API requiring Python 3.13+, and one POSIX-only case. Independent corrective review: **APPROVE**; restoring the old clipping logic in memory made all three density regressions fail, confirming they detect the removed behavior. Exact-HEAD GitHub Actions results are linked in the PR follow-up comment. No new live-provider acceptance is claimed.

## Current limitations

- Corrected-HEAD live-provider acceptance: **not rerun**; earlier live results are implementer-reported historical evidence. Human claim/source and Office visual review remain **UNVERIFIED**.
- No hard monetary ceiling, token or model-request tally.
- HTTP integration via actual FastAPI `TestClient`, not browser/production deployment. Offline Playwright already covers browser interactions separately.
- Single-user local runtime only; cannot claim deployment readiness or Office visual acceptance.
