# Task 2.0 frontend verification

## Automated checks

Executed from `frontend/` on Windows with the current Python API:

| Exact command | Result |
| --- | --- |
| `npm install` | PASS; GFM dependency installed and lockfile updated; 393 packages audited, 0 vulnerabilities |
| `npm run typecheck` | PASS; TypeScript strict checks, no diagnostics |
| `npm run lint` | PASS; ESLint, zero warnings |
| `npm test` | PASS; 20 tests in 4 files |
| `npx playwright install chromium` | PASS; Chromium/headless shell installed |
| `npm run test:e2e` | PASS; 2 real-API Chromium tests |
| `npm run build` | PASS; TypeScript and Vite production build |

Executed from the repository root for the frontend's Python browser-test launcher:

| Exact command | Result |
| --- | --- |
| `uv run ruff format --check frontend/e2e/serve_api.py` | PASS; 1 file already formatted |
| `uv run ruff check frontend/e2e/serve_api.py` | PASS; all checks passed |

The production build splits the graph into a lazy chunk (~226 kB raw / 76 kB gzip). The application entry including GFM is ~451 kB raw / 140 kB gzip. Fonts are bundled, including Vietnamese glyphs.

## Verified behaviors

Component/integration tests cover submission locking, new and follow-up endpoint selection, skill metadata and reset, server history navigation, mode-scoped requests, late-POST navigation isolation, safe read-error retry, interrupted-turn recovery controls, active job discovery, inspector panels, trace filters, exact inert artifact source, and Markdown URL/HTML/image safety. Additional regressions cover terminal persisted-run precedence versus queued/running snapshots, terminal fallback without a persisted run, and semantic GFM comparison tables with the same URL/media restrictions.

Parity regressions also assert all seven authoritative graph metric values (including zero), every textual status/kind legend entry, and the Plan goal without an artifact. Trace tests combine multiple runs, kinds and outcomes, verify exact included/excluded events and no-match states, clear individual filters, deselect all options, and reset every filter. Empty selections mean All; values within a list are ORed, and the three lists are ANDed.

Cache/freshness regressions exercise the actual application with the production five-minute query-cache lifetime and one-second stale time (automatic retries disabled only for deterministic fault assertions):

- Distinct completed turn/job 1 and active follow-up turn/job 2; retain the cached one-turn session through New chat and reopen; default to job 2, switch to historical turn 1, and return to the active graph.
- Running-to-completed resume with delayed session refresh, stale 61-node cached projection versus latest 92-node snapshot, refresh failure, explicit retry and success. Old Plan/artifact content is not silently shown as current; the segment is labeled, download/submission/recovery wait, and full 92-event history plus historical failed child return after reconciliation.
- A successful read that started before terminal status does not clear the refreshing state. A subsequent read capturing the terminal identity is required. This uses request-start identities rather than wall-clock timestamp comparisons.

Real browser tests start the actual API from the repository root with a new, unseeded temporary storage directory and start Vite on its required origin. The first test verifies empty history/jobs before the initial browser submission. Only after that cold workflow finishes does the second test seed an interrupted conversation via the real offline runtime. There are no API mocks. They verify:

- Offline create with `research-brief`, follow-up without a skill, reload and historical turn identity.
- Backend-projected successful parent, failed child, successful child, fan-in and parent continuation; Workflow versus Current run.
- An actual React Flow canvas node click (not the fallback selector), followed by assertions that the selected node ID and safe details change to that node.
- Every graph metric card and both textual legends; phone graph drawer has no horizontal overflow.
- Evidence, trace-kind filtering, approximate context diagnostics, exact artifact response and real browser download.
- Combined run/kind/outcome subset filtering and reset through native multiple listboxes; single-item filtering still works. Plan displays the actual backend goal.
- Desktop 1440×900 and phone 375×812; no document-level horizontal overflow.
- At 375×812: focus the composer, type with Playwright keyboard events, send a third turn with Enter, await actual completion, and assert the composer/send button are fully within the viewport.
- Native navigation/inspector modal dialogs, Escape and return focus, arrow-key tab selection **and focus**.
- A genuinely interrupted persisted turn, page reload, explicit resume into the same turn, and persistence after completion. After resume, Current run still exposes the original failed child, successful child, delegation and fan-in; Tools & Delegation and Trace retain pre-resume tool history. The regression confirms the terminal resume job itself has no tool events, so assertions exercise persisted history rather than a coincidentally complete snapshot.
- Live/offline navigation isolation and browser disconnection/reconnection recovery.
- No uncaught browser errors in the primary workflow.

## Browser-inspected evidence

Real screenshots are generated under `frontend/test-results/` (ignored generated outputs):

- `workspace-real-offline-res-0c45b-nspector-and-phone-keyboard/desktop-chat.png`
- `workspace-real-offline-res-0c45b-nspector-and-phone-keyboard/desktop-failed-child-graph.png`
- `workspace-real-offline-res-0c45b-nspector-and-phone-keyboard/desktop-context.png`
- `workspace-real-offline-res-0c45b-nspector-and-phone-keyboard/phone-workspace.png`
- `workspace-real-offline-res-0c45b-nspector-and-phone-keyboard/phone-graph-inspector.png`
- `workspace-restored-interru-d5ba4-ity-and-read-errors-recover/resumed-persisted-turn.png`

Desktop chat/graph and phone screenshots were inspected during implementation. Scroll containment was corrected after inspection so the composer and transcript occupy separate regions.

## Integration finding

An earlier cold-storage browser run reproduced a SQLite checkpoint initialization lock when conversation reads overlapped the first active write. The current API serializes durable reads and rejects them with HTTP 409 during active operations. The frontend retains job-first discovery, pauses durable reads while a job is active, and uses job DTOs for progress. Once terminal, persisted turn data is authoritative for graph and trace history. The latest browser run passed from genuinely unseeded storage against the updated API. Conflict/read errors remain actionable through the existing query retry and Refresh behavior.

## Final overall checks — 2026-10-24

**PASS — automated local React migration acceptance.** Independent final review:
**APPROVE**, no important defects remaining. Final corrected-tree results:

| Directory | Exact command | Result |
| --- | --- | --- |
| Root | `uv run --offline --no-sync pytest -q -rs -p no:cacheprovider --basetemp C:\Users\GIGABYTE\AppData\Local\Temp\opencode\react-final-pytest` | 816 passed, 6 skipped, 223 subtests in 47.05s; includes 38 API tests |
| Root | `uv run --offline --no-sync python evals/run_evals.py --dataset evals/dataset.json` | 9 cases / 36 invariants, all passed |
| Root | `uv run --offline --no-sync python -m mini_deerflow.demo.phase1_offline` | All 14 top-level booleans true; unskilled baseline, deep-research interruption/resume, unskilled follow-up |
| Root | `uv run --offline --no-sync ruff check . --no-cache` | PASS |
| Root | `uv run --offline --no-sync ruff format --check . --no-cache` | PASS; 124 files already formatted |
| Root | `uv lock --check --offline` | PASS; 95 packages |
| Root | `git diff --check` | PASS |
| `frontend/` | `npm ci` | PASS; 393 packages audited, 0 vulnerabilities |
| `frontend/` | `npm run typecheck` | PASS |
| `frontend/` | `npm run lint` | PASS; zero warnings |
| `frontend/` | `npm test` | 20 tests passed in 4 files |
| `frontend/` | `npm run build` | PASS; 2,101 modules transformed |
| `frontend/` | `npm run test:e2e` | 2 passed in 21.4s; real API, cold storage followed by seeded interruption, no mocked backend |

The six Python skips are four symlink cases without Windows creation privileges,
one native Windows API case requiring Python 3.13+, and one POSIX-semantics case.
Desktop chat, failed-child graph, nonempty Context and the 375px phone screenshots
were visually inspected. Context shows 5,592 payload chars / 8,000 budget chars
with `research-brief`. See [the migration record](../docs/react-ui-migration.md)
for the HTTP inventory, parity matrix and operating limits.

### Acceptance still unverified

- Live model/tool execution with configured credentials.
- Real mobile software-keyboard behavior: the 375×812 desktop Chromium viewport test verifies focus, keyboard-event typing/submission and viewport containment; it does **not** emulate an on-screen keyboard or validate visual-viewport resizing on physical iOS/Android devices.
- GFM table semantics and safety are unit-tested and wide tables have a focusable horizontal-scroll wrapper; browser inspection of a runtime-produced wide comparison table remains pending.
- Manual mentor acceptance and assistive-technology/contrast audit beyond the implemented semantic tokens and browser keyboard checks.

For reproduction and local run commands, see `README.md` in this directory. No commits were created.
