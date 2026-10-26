# Phase 3 — Acceptance Ledger & Release Go/No-Go

**Recorded:** 2026-10-26 (Asia/Bangkok); evidence snapshot, not a recurring live-status service.  
**Parent:** [Roadmap #16](https://github.com/hhtuann/mini-deerflow/issues/16) · [Closeout #27](https://github.com/hhtuann/mini-deerflow/issues/27).  
**Merged baseline:** `main` at [`7a2b2081e5910d7330a0e3ce18d7beb9ed1c160d`](https://github.com/hhtuann/mini-deerflow/commit/7a2b2081e5910d7330a0e3ce18d7beb9ed1c160d), merging [PR #26](https://github.com/hhtuann/mini-deerflow/pull/26) from `b4cde897a0ec9eeab5c34d42673df3d9e92676f6`.  
**Overall decision: IMPLEMENTATIONS MERGED; RELEASE ACCEPTANCE NOT COMPLETE.** This ledger does not approve production, a real-data pilot, public ingress, or paid provider requests.

## Evidence classifications

- **VERIFIED_GITHUB:** GitHub PR, commit, workflow status, steps and artifact metadata checked on the cited revision. It proves only what that GitHub record actually tests.
- **OPERATOR_REPORTED:** operator/Codex output from a Windows machine; not independently re-executed by this ledger author. Store detailed sanitized evidence separately; do not commit personal files, credentials or the `.mini-deerflow/` folder.
- **HISTORICAL_DIFFERENT_HEAD:** real test or operator result from an older implementation SHA. Not acceptance of later corrections or dependencies.
- **UNVERIFIED/BLOCKED:** not executed, lacking reviewer sign-off or outside approved scope. A merged PR does not upgrade these states.

## Workstream ledger — implementation versus acceptance

| Scope | Implementation evidence | What is verified | Outstanding acceptance |
| --- | --- | --- | --- |
| **3.0 CI** · [#17](https://github.com/hhtuann/mini-deerflow/issues/17) | [PR #22 merged](https://github.com/hhtuann/mini-deerflow/pull/22) | Cross-platform offline CI exists; exact Task 3.4 head's [4/4 successful run](https://github.com/hhtuann/mini-deerflow/actions/runs/40288268263) | CI cannot certify paid live quality, Office layout or operating security |
| **3.1 live E2E** · [#18](https://github.com/hhtuann/mini-deerflow/issues/18) | [PR #23 merged](https://github.com/hhtuann/mini-deerflow/pull/23); [runner/limitations](phase3-live-e2e.md) | Consent guard, non-paid tests and safe diagnostic/reporting contract are part of tested code | **UNVERIFIED on corrected HEAD:** operator-reported VI/EN successes on earlier clipping-based implementation are historical; current-correction real-provider E2E, citation factual correctness, quality rubric and cost/latency review require explicit paid-run approval and manual review. Issue #18 remains open |
| **3.2 Office visual** · [#19](https://github.com/hhtuann/mini-deerflow/issues/19) | [PR #24 merged](https://github.com/hhtuann/mini-deerflow/pull/24); [viewer rubric](phase3-office-visual-acceptance.md) | [LibreOffice run #40285132675](https://github.com/hhtuann/mini-deerflow/actions/runs/40285132675): genuine machine conversion succeeded; artifact `office-visual-evidence-40285132675-1` holds six PPTX/PDF, 48 PNG, six font reports | **UNVERIFIED:** human inspection for clipping/diacritics/contrast/font substitutions; real native table/chart/text edit–save–close–reopen and cross-viewer approval; issue #19 remains open |
| **3.3 ops/recovery** · [#20](https://github.com/hhtuann/mini-deerflow/issues/20) | [PR #25 merged](https://github.com/hhtuann/mini-deerflow/pull/25); [failure matrix](phase3-operations-recovery.md) | Offline ledger/FS fault tests, safe dry-run default, no automatic delete; exact-head GitHub CI passes | **OPERATOR_REPORTED:** Windows stopped-service live-root backup/restore (178 MB, 12 conversations/16 turns, SQLite quick_check OK, FK 0, originals SHA unchanged) and restart/rollback rehearsal. Power-loss durability, remote provider exactly-once, automatic recovery and multi-worker support **not claimed**. Issue #20 remains open |
| **3.4 security** · [#21](https://github.com/hhtuann/mini-deerflow/issues/21) | [PR #26 merged](https://github.com/hhtuann/mini-deerflow/pull/26) at [`7a2b2081`](https://github.com/hhtuann/mini-deerflow/commit/7a2b2081e5910d7330a0e3ce18d7beb9ed1c160d); [boundary matrix](phase3-deployment-security-readiness.md) | Credential-free loopback guard + attack regression tests and remediation commit `b4cde897`; CI four jobs PASS on the remediated PR head | **OPERATOR_REPORTED:** `pip-audit 2.10.1` 42 advisory entries before → 0 after, Windows listener/Origin tests and secret/history sweep. Independent security sign-off absent; key rotation, provider spending limits, ACL/backup cadence remain owner actions. Issue #21 remains open |

All five implementation PRs were merged. The four unresolved scope issues (#18–#21) and roadmap #16 are deliberately **not** marked accepted/closed because remaining gates are real and distinct from merge authorization.

## Exact automated acceptance snapshot

**GitHub Actions (VERIFIED_GITHUB):** [Phase 3 Offline Acceptance #40288268263](https://github.com/hhtuann/mini-deerflow/actions/runs/40288268263), PR head `b4cde897a0ec9eeab5c34d42673df3d9e92676f6`, status `completed/success` on 2026-10-26:

| Job / platform | Actual recorded result |
| --- | --- |
| Python 3.12 · Ubuntu | **1,330 passed / 28 skipped / 122 subtests passed**; Ruff, deterministic evaluator 9 cases/36 invariants, offline demos passed |
| Python 3.12 · Windows | **1,353 passed / 5 skipped / 223 subtests passed**; Ruff, evaluator passed |
| React / Node on Linux | Typecheck, lint, build passed; **38/38 Vitest** tests passed |
| Chromium / real offline FastAPI | **4/4 Playwright** browser tests passed |

Commands defined in [CI workflow](../.github/workflows/phase3-offline-acceptance.yml): `uv sync --locked --dev`; `uv run --no-sync python -m pytest -q`; Ruff check/format; `uv run --no-sync python evals/run_evals.py --dataset evals/dataset.json`; frontend `npm ci`, `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`, `npx playwright test`. Refer to workflow for exact flags and CI environment; this table does not assert locally re-running these commands.

**Windows operator snapshot (OPERATOR_REPORTED):** Windows 10 build 26200, PowerShell 5.1, Python 3.12.14, uv 0.12.5, Node 24.19, npm 11.17, Git 2.55; full local tests **1,343 passed / 15 explained skips / 0 failed / 223 subtests**. Skip reasons involve Windows symlink privileges, POSIX semantics, Python-version restrictions and existing platform conditions. Local `pip-audit` scan on updated lockfile reported **0 current known advisories**, no suppressions; no claim of continuous CVE monitoring or a GitHub-side vulnerability audit. Reports were retained in the operator's private temp evidence directory; do not publish them without redaction.

**Office machine snapshot (VERIFIED_GITHUB):** [Office Visual run #40285132675](https://github.com/hhtuann/mini-deerflow/actions/runs/40285132675) succeeded on **earlier `9a33d89`**, not the final security-remediation SHA; six editable source decks, six PDFs and 48 PNG screenshots validated structurally, with Liberation Sans font substitution noted by operator. Machine conversion reports `RENDERED_AWAITING_HUMAN_QA`, **not** human approval. The artifact retention policy is 14 days; preserve evidence before expiration if needed.

## Release target decisions (as of this snapshot)

| Target | Decision | Reason |
| --- | --- | --- |
| Trusted single-user workstation, FastAPI/Vite only on 127.0.0.1, offline/development | **READY — bounded scope** | Source guard, representative live local listener check reported, same-head CI green |
| Pilot with actual user/private data or billable model | **CONDITIONAL / NOT YET OWNER-APPROVED** | Keys/budget/rotation, protected local storage, backup cadence, incident ownership, real data and cost risk sign-off remain operator decisions |
| Unauthenticated LAN, tunnel, public host, internet preview or shared tenant server | **PROHIBITED** | No authentication, per-user ACL or safe external boundary |
| Authenticated multiuser public production | **BLOCKED / SEPARATE PROJECT** | Requires authN/authZ, tenant isolation, TLS, rate limits, secret manager, monitoring, security review and different architecture |
| Multi-worker / distributed deployment | **NOT_IN_SCOPE** | In-memory job slots/IDs; no cross-worker journal or exactly-once remote effect |

**Phase 3 Go/No-Go: NO-GO for declaring full live release candidate.** Offline localhost developer scope is usable; manual live, human Office, independent security and real-data-operator sign-offs remain distinct. Do not equate this with production readiness.

## Owner/reviewer action register (evidence needed to clear each item)

| ID | Owner/action | Required evidence and decision | Current state |
| --- | --- | --- | --- |
| A1 · live | Operator must separately authorize each provider-paid VI/EN corrected-HEAD E2E run with budgets; reviewer checks final output against cited source claims | Exact SHA, consent, provider/model version, bounded case report, measured timings/tool calls, safe error categories; **manual source/claim rubric** | **BLOCKED — no paid run authorized by closeout** |
| A2 · Office | Human reviewer downloads six decks, PDFs/48 screenshots, checks Vietnamese glyphs, clipping, font substitutions, charts/tables, and performs actual edit–save–close–reopen | For all six cases: viewer/version/OS, issues by slide, native text/table/chart edit persistence, signed verdict; mark unavailable viewer explicitly blocked | **UNVERIFIED** |
| A3 · independent security | A reviewer independent of implementation reviews PR #26's merged diff, transport/storage/privacy/secret/lock gates and residual risks | Review identity/date, PASS/NEEDS FIX/BLOCKED, link or signed note; GitHub PR #26 had no submitted approval at closeout snapshot | **UNVERIFIED** |
| A4 · provider operations | Owner checks provider console, rotates key if required, defines key recovery + spending/billing alerts | Do not reveal token; dated attestation with key identifier safely redacted, provider budget/procedure and approved spend ceiling | **USER_ACTION_REQUIRED** |
| A5 · backups + permissions | Operator schedules backup, tests periodic restore on real representative data, checks NTFS ACL and retention | Backup cadence, last-restored date, quick_check/FK+PPTX SHA result, sanitized ACL review and owner residual-risk approval | **CONDITIONAL — one reported drill only** |
| A6 · CI hardening | Maintainer decides on immutable Action SHA pins and Node 20 deprecation warnings; optionally consolidates incident-response runbook | Separate reviewed diff or documented deferral, no unvetted updates or claims of hardening | **FOLLOW-UP / LOW** |

When an item clears, update its status **with the referenced evidence** and rerun the relevant exact-HEAD checks after any code/lock change. Do not change release matrix merely because a new workflow was dispatched.

## Governance and limitations

- This is a **documentation-only synthesis of verifiable and clearly attributed evidence**, not a penetration test, host inspection or actual provider request.
- No owner-approved public exposure or paid provider call is implied by merger or this ledger.
- Evidence locations under the developer's local temporary directory are not linked as downloadable repository artifacts and may disappear.
- The 2026-10-26 snapshot is not an ongoing security feed; repeat SCA on future changes.
- GitNexus impact/detect-changes could not be executed by this GitHub-only coordinator (no indexed MCP/CLI access to checkout). This change introduces a Markdown file and a README link and modifies **no code symbols**; graph result is **UNVERIFIED**, not “risk LOW.”
- Issue #27 and its PR require CI and independent review. **Do not merge without new explicit owner approval.**