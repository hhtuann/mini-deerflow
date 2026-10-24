# Phase 3 · Task 3.1 — Opt-in Live Provider E2E

Issue [#18](https://github.com/hhtuann/mini-deerflow/issues/18) · Roadmap [#16](https://github.com/hhtuann/mini-deerflow/issues/16)

## Purpose and status

This task adds a **manual-only, double-opt-in** executable which exercises the actual live provider, production `LiveDemoBackend`, FastAPI, `SlideAgentService`, editable PPTX renderer and validator, and `PptxArtifactStore` — **without any scripted planner or mocked provider response**.

**Important:** The implementation has NOT made a real provider call. Live quality and factual correctness are **UNVERIFIED** until the owner authorizes using local credentials and provider charges and executes both language cases. GitHub CI remains credential-free.

## How to execute locally

1. Run `uv sync` at the repository root.
2. Load your **real** `MINI_DEERFLOW_API_KEY` securely into the process environment (not just `.env`). Do not put it in a PR, GitHub Actions secret for PRs, source file, terminal history or chat.
3. Explicitly opt in with `MINI_DEERFLOW_LIVE_E2E_CONSENT=YES` and run:

```sh
export MINI_DEERFLOW_LIVE_E2E_CONSENT=YES
uv run python scripts/live_e2e.py --allow-paid-live --case both --timeout-seconds 480 --output .mini-deerflow/live-e2e/report.json
```

For PowerShell, set `$env:MINI_DEERFLOW_LIVE_E2E_CONSENT = 'YES'` after loading `$env:MINI_DEERFLOW_API_KEY` from a trusted local credential manager. Then run the same `uv run python...` command. A single `--case vi` or `--case en` is diagnostic only; full acceptance requires **both**.

The report contains safe, allowlisted metrics only. Keep it local and uncommitted. By default `python scripts/live_e2e.py` exits with BLOCKED **before importing any provider-capable module**. The opt-in requires all three: flag, exact YES consent and a non-fixture key. The dummy CI key is rejected.

**Real monetary cost warning:** This does call paid/network providers when enabled. Runtime bounds (8 research tool calls, 2 per step, 1 replan, recursion 60, 30-second model timeout, 10-second Wikipedia timeout, zero retries, and 30–900 second per-case wall limit) **are not** a hard limit on model requests/tokens, total dollars or remote billing. The controller can terminate the child process but cannot guarantee cancellation of already-sent provider requests. Obtain explicit account-level budget approval first.

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

## Current limitations

- Real model/provider success and human review: **BLOCKED pending owner-run opt-in**.
- No hard monetary ceiling, token or model-request tally.
- HTTP integration via actual FastAPI `TestClient`, not browser/production deployment. Offline Playwright already covers browser interactions separately.
- Single-user local runtime only; cannot claim deployment readiness or Office visual acceptance.
