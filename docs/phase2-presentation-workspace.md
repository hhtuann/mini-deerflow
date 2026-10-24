# Phase 2 Presentation Workspace — Issue #14, task 2.4

Task 2.4 adds the React presentation workspace: a per-completed-turn dialog for
generating, verifying and downloading an evidence-grounded deck, built on the
[task 2.3 slide agent](phase2-slide-agent.md) HTTP contract and the existing
Drawer/shell with Corporate Trust tokens — no separate marketing UI. Final
automated acceptance is complete; actual results are in the
[final verification table](#final-verification) below.

## User journey and architecture

```text
completed turn in the transcript
        |
"Presentation for turn N" action (completed turns only, disabled while busy)
        |
existing Drawer shell "Presentation workspace"
        |
PresentationWorkspace (frontend/src/components/Presentation.tsx)
        |
form bound exactly to PresentationRequestDTO (server stays authoritative)
        |
POST .../turns/{turn_id}/presentation -> 202 JobDTO (operation "presentation")
        |  shared jobs polling (1.5 s), submission locking, busy gating
        |
GET  .../turns/{turn_id}/presentation/metadata  (NEW, read-only, verified)
GET  .../turns/{turn_id}/presentation           (server-controlled download URL)
```

The form mirrors `PresentationRequestDTO` field for field:

| Field | Behavior |
| --- | --- |
| `topic` | 10–1000 chars, prefilled from the turn's real user question (`deriveTopic`); no other field is silently inferred |
| `audience` | 3–120 chars, user-entered; invalid input blocks submit with an accessible message |
| `language` | `vi \| en` |
| `slides` | 5–12, default 8 |
| `theme` | `light \| dark \| academic` |
| `instructions` | ≤500 chars, optional |

- **Metadata, never job history.** The saved-deck panel is driven only by the
  new `GET .../presentation/metadata` endpoint, verified through
  `PptxArtifactStore.retrieve` (full digest and presentation-structure
  validation). `PresentationMetadataDTO` exposes exactly `artifact_type`,
  `title`, `theme`, `slide_count`, `layouts`,
  `outline[{index, slide_id, layout, title}]`, `byte_size`, `sha256` — no
  paths, no raw spec, no arbitrary URLs. Absent/ineligible turn → **404**;
  corrupt artifact → **409** (download consistently disabled). Availability is
  never inferred from ephemeral job history: the query runs only for the
  selected completed turn while the server is idle (`enabled: eligible &&
  !busy`, `staleTime` 30 s) and is invalidated when the generation job
  completes.
- **Download** uses the server-controlled URL `GET .../presentation`:
  `attachment; filename="presentation.pptx"`, PPTX MIME type, `nosniff`,
  `no-store`, `ETag = "<sha256>"`.
- **Reload/reopen restores** the saved deck from persisted SQLite/filesystem
  with zero regeneration — jobs are process-local and never replayed; the e2e
  asserts the presentation job count stays 1 after reload.

| Source | Responsibility |
| --- | --- |
| `frontend/src/App.tsx` | per-turn action, Drawer wiring, shared jobs/busy state |
| `frontend/src/components/Presentation.tsx` | form, mutation, verified-metadata panel, download link |
| `frontend/src/presentation-form.ts` | shared boundaries, topic derivation, metadata guard, download URL |
| `src/mini_deerflow/api/__init__.py` | NEW verified metadata endpoint (`presentation_metadata`) |
| `src/mini_deerflow/api/dto.py` | `PresentationMetadataDTO`, `PresentationSlideOutlineDTO` |
| `tests/test_presentation_metadata.py` | 7 endpoint tests |
| `frontend/e2e/serve_api.py`, `frontend/e2e/zz-presentation.spec.ts` | real-API browser harness and tests |

## Truthfulness rules

1. **Immutable publication semantics.** Retrying an identical request is
   idempotent (verified artifact and ETag are reused). A **different** request
   on the same turn fails safely with
   *"A different presentation already belongs to this turn."* — the published
   file is not overwritten and the UI keeps showing the saved deck (failed-job
   alert alongside the unchanged verified panel).
2. **Offline mode is never bypassed.** Without an injected planner the
   presentation POST returns **503**. The e2e harness injects the deterministic
   `ScriptedSlidePlanner` through the production `SlideAgentService` via
   `planner_model_factory` — a clearly labeled **test-harness-only injection**;
   the offline 503 default is exercised by Python API tests.
3. **No fake preview or progress.** The read-only slide outline comes from the
   verified persisted `DeckSpec` metadata and is explicitly labeled
   *"not a rendered preview"*; progress is the real job status announced via
   `aria-live`, never a simulated timeline.
4. **Skill selection precedes research.** The `slide-creation` skill is
   explicitly selected in the composer **before** the research turn is created;
   its identity stays pinned per turn (task 2.3 pinned snapshot). The
   presentation workspace never re-picks or reassigns it.

## Accessibility and responsive behavior

- Dialog focus return (Escape returns focus to the opener — e2e-verified),
  labeled fields, `aria-invalid`, `role="alert"` validation messages,
  `aria-live` generation status.
- 44 px minimum targets, full keyboard flow (Tab order e2e-verified),
  reduced-motion respected.
- 375×812 viewport: no horizontal overflow in the dialog or the document
  (e2e-verified).

## Final verification

All checks below were actually executed.

| Check | Command | Final result |
| --- | --- | --- |
| Full Python suite | `uv run --offline pytest -q -rs --basetemp .test-tmp/phase24-final` | PASS — 1260 passed, 11 skipped (9 Windows symlink-privilege, 1 Python 3.13+ native API, 1 POSIX-only), 223 subtests |
| New metadata endpoint tests | `tests/test_presentation_metadata.py` | PASS — 7 passed |
| Ruff lint / format | `uv run --offline ruff check --no-cache .`; `uv run --offline ruff format --check .` | PASS / PASS (141 files) |
| Offline lock validation | `uv lock --check --offline` | PASS — 99 packages |
| Deterministic evaluator | `uv run --offline python evals/run_evals.py --dataset evals/dataset.json` | PASS — 9/9 cases, 36/36 invariants |
| Phase 1 offline demo | `python -m mini_deerflow.demo.phase1_offline` | PASS — compaction/skill identity true |
| Task 2.1 demo | `--output-dir .mini-deerflow/phase24-core` | PASS — 8 slides, 46,136 bytes |
| Task 2.2 demo | `--storage-root .mini-deerflow/phase24-artifacts` | PASS — `fresh_process` + `download_matches_storage` |
| Task 2.3 demo | `--storage-root .mini-deerflow/phase24-slide` | PASS — 8 slides, sha256 `015320aa5f99bfbfe9ccb30982683c6d88b58f54de1fc7eeec01a71c101318f1`, 46,157 bytes, skill pinned, conflict rejected |
| Frontend static/tests | `npm run typecheck`; `npm run lint`; `npm test`; `npm run build` | PASS — typecheck; lint 0 warnings; Vitest 33 passed (5 files); build. 13 Presentation tests: validation boundaries, DTO shape, 404/409 distinction, failed-job message, running-state gating, ineligible turn, topic derivation, plus two new regressions — busy-409 treated as transient *"Checking the saved presentation waits until the active operation finishes."* (never corruption); transport/5xx failures treated as unknown with a *"Retry status check"* button (never absence); verified data stays authoritative over background refetch errors |
| Browser e2e | `npm run test:e2e` | PASS — 4/4 (2 new: desktop end-to-end real generation/download/persistence/isolation/conflict incl. byte-identical UI-vs-server download, `suggestedFilename` `presentation.pptx` and MIME check; mobile 375×812 keyboard/focus-return/overflow) |
| Screenshots | `.mini-deerflow/phase24-evidence/screens/` | Captured (`presentation-form-empty.png`, `presentation-saved.png`, `presentation-restored.png`, `presentation-phone.png`) — programmatic capture only, **not** visually inspected; DOM assertions are the verification |

### Independent review

The reviewer's verdict on task 2.4 was **REQUESTED CHANGES** on one MEDIUM
truthfulness defect (409 conflation: busy-409 rendered as *"failed
verification"*; non-404 errors rendered as absence). Fixed in
`frontend/src/components/Presentation.tsx` with exclusive data/error branches
and three-way absent/corrupt/unavailable semantics; re-verified 33/33 Vitest
and 4/4 e2e. Verdict closed.

## Limitations

- Manual PowerPoint/LibreOffice visual acceptance **UNVERIFIED**.
- Live-provider execution **UNVERIFIED**; the e2e planner injection is
  deterministic offline, not live model quality.
- Screenshots were not human-reviewed.
- The outline is verified metadata, not rendered slides; in-app slide rendering
  is not provided.
- Single-user local app: no authentication or tenancy (inherited boundary).

## Readiness notes

None required beyond task scope — no React changes beyond this workspace;
task 2.4 completes Phase 2.

PM sync-back: `docs/plans` contains the earlier budget-aware synthesis and
session-scoped sandbox plans only; no task 2.4 plan was found needing
sync-back. No unrelated plan status is changed by this guide.
