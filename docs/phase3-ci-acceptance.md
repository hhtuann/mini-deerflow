# Phase 3 · Task 3.0 — Automated Offline CI Acceptance

Issue: [#17](https://github.com/hhtuann/mini-deerflow/issues/17); roadmap: [#16](https://github.com/hhtuann/mini-deerflow/issues/16).

## Why this task exists

Phase 2 was delivered with extensive **local** acceptance (last reported: 1,260 Python passed / 11 skipped / 223 subtests, 38 Vitest, 4 Playwright E2E). At the Phase 3 baseline (`main` at `38a6a8873123ce251194a2e3e3dd2b79e23b146d`), no `.github/workflows` existed, and the PRs had no GitHub CI checks. Local output does not prove a fresh PR or a clean OS image will pass.

This task introduces automated, **credential-free** gates; **CI configuration does not mean the tests have passed**. Collect a successful GitHub Actions run before calling this task verified.

## Workflow architecture

File: `.github/workflows/phase3-offline-acceptance.yml`.

```text
PR / push main / manual dispatch
             |
   +---------+-----------+
   |         |           |
 Python     React     Browser E2E
 3.12       Node 22   Ubuntu + Chromium
 Ubuntu +   npm ci    FastAPI real offline
 Windows    tsc       test harness
 uv locked  ESLint    Playwright (4 E2E)
 pytest     Vitest    Verified PPTX download
 Ruff       build     + restart isolation
 evaluator
 offline demos (Ubuntu only)
   |         |           |
   +------ GitHub status checks -----+
```

Workflow permissions are restricted to `contents: read`. Checkout disables persisted credentials. Jobs do not reference secrets or `.env`; they never execute real LLM/provider/network-research integrations. Python and frontend dependencies, GitHub Actions, and Chromium **are downloaded** from external package/action/browser registries during installation, so this is not an air-gapped build.

### Python job: Linux and Windows

Uses Python 3.12 and `uv sync --locked --dev`. Runs:

```sh
uv lock --check --offline
uv run --no-sync ruff check --no-cache .
uv run --no-sync ruff format --check .
uv run --no-sync python -m pytest -q -rs
uv run --no-sync python evals/run_evals.py --dataset evals/dataset.json
```

Linux additionally runs the Phase 1, PPTX Core 2.1, Artifact 2.2 and Slide Agent 2.3 credential-free demos with fresh CI-owned directories. Windows symlink/OS-specific skips are expected *when supported by pytest conditions*, not preemptively excluded; all failures remain blocking.

### Frontend job

Node 22, `npm ci`, `npm run typecheck`, `npm run lint`, `npm test`, and `npm run build`. Each command is a hard gate; `continue-on-error` is not used.

### Browser job

Installs Python 3.12, Node 22, locked dependencies and `npx playwright install --with-deps chromium`, then runs `npm run test:e2e`.

`frontend/playwright.config.ts` starts two short-lived servers:

1. Real FastAPI on `127.0.0.1:8000` using a unique temporary persistence directory.
2. Vite on `127.0.0.1:5173` proxying `/api` to that API.

The E2E harness injects the deterministic `ScriptedSlidePlanner` through the **production `SlideAgentService`** to test generation/download end to end. Default production offline mode still returns 503 without injection. No HTTP success responses are mocked for this browser acceptance. E2E compares actual downloaded PPTX bytes with the server response, checks reload/recovery, artifact ownership, and mobile 375×812 browser states.

Traces, screenshots and browser reports are uploaded **on failure only**, retained for 7 days. These may contain fixture research text but must never contain real provider secrets; the workflow does not supply any.

## Branch and PR governance

- One task = one branch and one PR, branched from the latest merged `main`.
- PR workflow must complete with all check jobs green before coordinator PASS; a GitHub Actions run absent/disabled is **BLOCKED** for CI acceptance, not PASS.
- Explicit user authorization is required to merge.
- Branch protection required-status-check settings remain a **repository administrator control**; adding YAML does not configure GitHub branch protection automatically.
- `AGENTS.md` asks for GitNexus impact and detect-changes checks. This change only adds workflow/docs (no production symbols); if the GitNexus CLI/MCP is unavailable, record that graph verification was **not run** rather than claiming a clean graph.

## Exclusions / non-goals

- No automatic live-LLM, external researcher, Jina, or paid-provider acceptance.
- No authentication, tenancy, public network exposure or multi-worker deployment.
- No simulated Office viewer acceptance. Browser screenshots do not establish PowerPoint or LibreOffice text fit.
- No change to the research runtime, artifact safety contracts or React UI.

## Evidence to record from the first GitHub workflow run

| Gate | Required evidence |
| --- | --- |
| Python Linux | Actual pytest summary including skips and subtests; evaluator result; demos |
| Python Windows | Actual pytest summary including platform-specific skips; evaluator result |
| React | Typecheck, lint, Vitest count and build result |
| Browser | Playwright count, real offline API startup, exact PPTX download tests |
| Workflow | Run URL, commit SHA, timestamps, status conclusion, skipped/failing steps |
| Scope | PR diff only workflow/docs; no secrets or live calls |

A code-reviewed workflow is a **CI implementation**, not a successful CI run. Phase 3 Task 3.0 is complete only once those checks have executed successfully against the precise PR HEAD and independent review has passed.
