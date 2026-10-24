# Phase 2 Slide Agent — Issue #12, task 2.3

Task 2.3 adds turn-owned, evidence-grounded presentation generation: a
model-driven slide planner over the existing research turn, a deterministic
fail-closed conversion into the [task 2.1 PPTX core](phase2-pptx-core.md), and
publication through the [task 2.2 artifact store](phase2-pptx-artifacts.md).
Implementation and offline demo verification are complete; the final acceptance
run has also completed — actual results are in the
[final verification table](#final-verification) below.

## Architecture

No new agent framework: `SlideAgentService` is a plain orchestrator over the
existing runtime, structured-output seam, renderer, validator and store. There
are no new graph nodes, tools or runtime services.

```text
PresentationRequestDTO (HTTP, task 2.4 boundary)
        |
POST .../turns/{turn_id}/presentation  -> 202 JobDTO (operation "presentation")
        |
SlideAgentService.generate
        |
DemoRuntimeService.load_conversation  (existing AgentRuntime ledger view)
        |  turn must be status="completed"
        |
Sanitized evidence projection: EvidenceView -> SlideEvidenceItem
        |  newest 10, omitting unrepresentable URLs (>300 chars); keys stay stable
        |  title<=120, excerpt<=400 chars
        |
Pinned skill snapshot from ledger run_context_json (fail closed)
        |
SlidePlanningContext -> fit_context_to_budget (shared ContextBudget)
        |
create_structured_output_runnable(model, DeckPlan)  <- 2-attempt contract
        |  native structured output or prompt_json (same seam as research planner)
        |
DeckPlan -> build_deck_spec (deterministic, fail-closed conversion)
        |
render_deck (task 2.1) -> validate_pptx(min_layouts=4)
        |
PptxArtifactStore.publish (task 2.2, immutable, turn-owned)
        |
GET .../turns/{turn_id}/presentation  -> verified bytes, ETag = sha256
```

| Source | Responsibility |
| --- | --- |
| `src/mini_deerflow/pptx/slide_agent.py` | `PresentationRequest`, evidence projection, `DeckPlan` schema, planner, `build_deck_spec`, `SlideAgentService` |
| `src/mini_deerflow/context_budget.py` | Shared compactor used for the planning payload; one compatibility fix (below) |
| `src/mini_deerflow/api/__init__.py` | Presentation POST (job submission) and verified GET download |
| `src/mini_deerflow/api/dto.py` | `PresentationRequestDTO` public boundary |
| `skills/slide-creation/SKILL.md` | Explicitly selected deck-planning guidance skill |
| `src/mini_deerflow/demo/phase2_slide_agent_offline.py` | Credential-free end-to-end offline demo |

The planner runs through `asyncio.to_thread` with a callable
`planner_model_factory`, so the live default can resolve model credentials
lazily at generation time. Planning failures, conversion failures, renderer
overflow and validation failures each map to controlled `SlideAgentError`
subclasses (`SlideAgentValidationError`, `SlideAgentConflictError`) whose
messages never leak paths, prompts or payloads.

## Model-driven planning: typed DeckPlan

`plan_deck` reuses the production structured-output seam: the model is wrapped
by `create_structured_output_runnable(model, DeckPlan, method="json_mode",
mode=...)` — the **same two-attempt contract as the research planner** (native
structured output or `prompt_json`). A `ValidationError` or
`OutputParserException` on the first attempt is retried once; two failures
raise `SlideAgentValidationError`.

The model sees **only** `render_llm_payload(SlidePlanningContext)`: a bounded
`PresentationBrief` (topic ≤200, audience ≤120, instructions ≤500 chars,
language, slide count 5–12, theme), the allowlisted evidence tuple, and the
optional projected skill. Never raw state, tool output or the ledger.
`DeckPlan` itself is frozen, rejects unknown fields, and constrains each slide
to `layout` (one of 8 kinds), `title`, `purpose` (≤240), `evidence_keys`
(0–6 integers) and a layout-specific `payload` validated against a per-layout
model with `extra="forbid"`.

## Anti-fabrication invariants

Invented sourcing is structurally impossible rather than merely discouraged:

1. **Citation only by integer key.** Slides reference evidence exclusively
   through `evidence_keys` (and conclusion `source_keys`, ≤3) referencing the
   **actual post-compaction planner evidence** by its stable `key` field.
   Compaction may remove old entries without renumbering survivors. The
   converter rejects any omitted/unknown key instead of consulting evidence
   that was not included in the model-facing payload. For URLs ≤180
   characters, conclusion sources retain the canonical URL exactly.
   Longer URLs (181–300) appear as an explicitly labeled title/reference with
   the SHA-256 digest of the **unchanged canonical URL**, allowing lookup in
   the original turn evidence without creating a false truncated URL.
   URLs longer than 300 are omitted, never silently truncated.
2. **No URL-like plan text.** Any `http(s)://` or `www.` match in the deck
   title, a slide title, its `purpose`, or its serialized payload raises
   `SlideAgentValidationError` before rendering.
3. **Numeric data is always labeled illustrative, in the slide itself.** Chart
   captions always, table captions containing any numeric cell, and metric
   insights whose value looks numeric get the language-specific marker
   `"(số liệu minh họa)"` (vi) / `"(illustrative data)"` (en) appended by the
   deterministic converter, truncating beforehand to stay within limits.
4. **Empty or uncited evidence yields a deterministic uncertainty line** in the
   conclusion sources: *"Bằng chứng còn hạn chế: nội dung này chưa có nguồn đã
   xác thực."* / *"Limited evidence: no validated source supports this content
   yet."* The same line appears when no evidence item is cited anywhere.
5. **Language and slide-count agreement is enforced**: the plan's language must
   equal the request's, and the plan must contain exactly `slide_count` slides
   (5–12).
6. **Theme comes from the request**, never the plan — `DeckPlan` has no theme
   field; `build_deck_spec` injects `request.theme` into the `DeckSpec`.

## Skills: explicit selection, pinned snapshot, shared compactor

`skills/slide-creation/SKILL.md` (id `slide-creation`, version 1.0.0) guides
evidence-grounded, audience-focused deck planning: ground claims in the turn's
validated evidence, state limitations plainly when evidence is thin, never
invent citations/quotations/numbers, and treat chart/table numbers as clearly
illustrative.

- **Explicitly selected per research turn** through the existing message
  endpoint `skill_id` field (`POST /api/{mode}/conversations` or
  `POST .../turns`); unknown IDs are rejected with 422. The skill is **never
  auto-selected**: the default is `None`, and follow-up turns revert to `None`.
- The **pinned `SkillContext` snapshot** stored in the turn ledger's
  `run_context_json` at reservation time is revalidated and passed into
  planning as `active_skill`, reaching the model-facing payload under the
  **same `ContextBudget` compactor** as every other projection: under
  pressure, `ProjectedSkillContext` caps instructions at **2,000 characters**
  (`MAX_SKILL_INSTRUCTION_CHARS`) with honest truncation/omission markers.
- A **malformed or unparsable pinned snapshot fails closed**: generation
  raises `SlideAgentError("Pinned skill snapshot failed validation.")` rather
  than silently dropping the skill.

Compatibility fix: `context_budget._apply_pressure`'s provenance-based
observation dedup now skips evidence items without `EvidenceProvenance`.
Full evidence records carry call identity (step/tool-call/delegation keys) used
to drop observations duplicated in evidence; the bounded slide-planning
projection carries none, so those items are now skipped unchanged instead of
failing the projection. The full Phase 0/1 regression suite — the no-behavior-
change gate for the existing research-turn compaction — passed unchanged.

## HTTP API contract (for the task 2.4 React UI)

1. **Create/continue the research turn** (existing endpoints):
   `POST /api/{mode}/conversations` with `{"message": ..., "skill_id?": ...}`
   creates a conversation (`operation: "run"`); `POST
   /api/{mode}/conversations/{thread_id}/turns` continues one
   (`operation: "continue"`). Both return **202 `JobDTO`**.
2. **Poll the job**: `GET /api/{mode}/jobs/{job_id}` until
   `status: "completed"`; the job's `view` carries the completed turn.
3. **Request the deck**: `POST /api/{mode}/conversations/{thread_id}/turns/
   {turn_id}/presentation` with `PresentationRequestDTO`:

   | Field | Contract |
   | --- | --- |
   | `topic` | 10–1000 characters (strict message constraint) |
   | `audience` | 3–120 characters |
   | `language` | `"vi" \| "en"` |
   | `slide_count` | 5–12, strict integer, default **8** |
   | `theme` | `"light" \| "dark" \| "academic"`, default `"light"` |
   | `instructions` | ≤500 characters, default `""` |

   Returns **202 `JobDTO`** with `operation: "presentation"`; poll
   `GET /api/{mode}/jobs/{job_id}` for completion.
4. **Download**: `GET /api/{mode}/conversations/{thread_id}/turns/{turn_id}/
   presentation` returns the verified PPTX bytes with
   `Content-Type: application/vnd.openxmlformats-officedocument.presentationml.presentation`,
   `Content-Disposition: attachment; filename="presentation.pptx"`,
   `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` and
   `ETag: "<sha256>"`.

Semantics: generation is offered **only for completed turns** — otherwise
**409** "Complete or resume the turn before generating slides."; an unknown
thread/turn is **404**. The offline mode without an injected scripted planner
returns **503** ("Presentation generation is unavailable in this mode."); the
live default resolves model credentials lazily at generation time, so app
creation stays offline-safe. Publication is **immutable by validated DeckSpec**,
not by HTTP request fingerprint. Retrying the same request with a planner that
returns a semantically identical DeckSpec reuses the first verified artifact
and its ETag. If a nondeterministic planner returns a different valid DeckSpec
(even for the same HTTP request), the job safely fails with
**"A different presentation already belongs to this turn."** The published
file is not overwritten. For reliable recovery, clients should first
`GET .../presentation` and download an existing verified artifact instead of
rerunning the planner. New content requires a new completed research turn.
Results survive restart through the ledger + artifact store; process-local job
snapshots are **not replayed**.

## Credential-free offline demo

From a provisioned checkout, run exactly (a **fresh directory is required** —
the demo refuses an existing `checkpoints.sqlite`):

```powershell
uv run --offline python -m mini_deerflow.demo.phase2_slide_agent_offline --storage-root .mini-deerflow/phase23-slide-final
```

Flow: a **real offline research turn** runs first with the `slide-creation`
skill explicitly pinned (verified via `view.active_skill_id`). Generation then
runs through the **SAME production `SlideAgentService`** with a scripted
structured planner injected via `planner_model_factory` (native mode): the demo
drives the API POST/poll/download cycle with FastAPI `TestClient`, submits the
identical request twice (the scripted planner produces identical specifications),
submits a conflicting request
(`slide_count: 6`) and asserts the job fails with the safe conflict message,
and compares downloaded bytes against the store. A **fresh child interpreter**
re-downloads through a newly constructed app, revalidates the deck against the
persisted spec (`min_layouts=4`), checks conversation isolation (404) and turn
status, and its digest is compared with the parent's. Output is
machine-readable JSON; any failed assertion exits nonzero.

**Label:** the scripted offline planner demonstrates the **production code
path** (projection, planning seam, conversion, rendering, validation,
publication, HTTP contract), **NOT live model quality**; live provider
execution is unverified.

## Actual demo evidence

From the FINAL acceptance run at `.mini-deerflow/phase23-slide-final` (the
final demo command above):

| Measurement | Value |
| --- | --- |
| Artifact SHA-256 | `09f448b11b0c1f886647e80c0757205f58ed0ac2d6cfce1fc980587d2d1fa583` |
| Byte size | 46,157 |
| Slides / layouts | 8 / 8 (title, bullets, comparison, metric, steps, chart, table, conclusion) |
| Native objects | 33 text boxes, 14 shapes, 1 chart, 1 table; 49 geometry checks |
| Planning context | `payload_chars` 1,369; topic/audience echoed; `language` vi; `slide_count` 8; `theme` academic |
| Evidence reach | url `https://evidence.example/delegated` in the planning payload |
| Skill reach | `skill_id` `slide-creation`; 666 instruction chars in payload |
| Illustrative markers | 7 |
| Conflict / restart / isolation | `conflict_rejected` true; `fresh_process` true; `turn_status` completed; `download_matches_storage` true; `conversation_isolated` true |

Post-review hardening (from the independent security review; both findings
fixed with regression coverage):

- The evidence projection retains the **newest 10** records and clips
  original URLs ≤300 characters without rewriting them and omits longer URLs.
  Titles and excerpts are bounded at 120/400 characters. Source references
  longer than the PPTX Core's 180-character text bound are displayed as an
  explicit title plus a SHA-256 fingerprint of the original URL.
- The illustrative marker triggers on **any digit** in metric values and table
  cells — including quoted numbers, units and currency such as `"42"`,
  `"91%"`, `"1.2M"`, `"$3.5B"`.

ZIP timestamps mean new runs produce different digests and byte sizes; compare
measurements within one run (parent vs. child), not across runs.

## Limitations

- Deck language/audience adaptation relies on the model; the scripted offline
  planner echoes request inputs, so live planning quality is unverified.
- No image generation.
- No React UI yet — that is task 2.4; this task exposes the HTTP contract only.
- Single-user local trust boundary, inherited from the existing API and store:
  no authentication, tenancy or production serving.
- Manual PowerPoint/LibreOffice visual rendering remains **unverified**
  (inherited task 2.1/2.2 limitation); structural validation does not
  establish visual text fit.
- Chart/table numbers are illustrative by policy and are never
  evidence-derived statistics; post-review, the marker triggers on any digit
  in metric values and table cells (quoted numbers/units/currency included).

## Corrective review verification

This revision adds regression cases for long cited URLs, source labels with
full SHA-256 identity, stable evidence keys after context pressure (including
omitted-key rejection), and immutable retry behavior when the model returns
different plans for the same HTTP request. Test commands and results for the
corrective revision must be recorded separately from the historical acceptance
table below; **prior PASS numbers are not a rerun of this revision**.

## Final verification

All checks below were actually executed.

| Check | Command | Final result |
| --- | --- | --- |
| Full Python suite | `uv run --offline pytest -q --basetemp .test-tmp/phase23-final3` | PASS — 1250 passed, 11 skipped, 223 subtests passed (skips: 9 Windows symlink-privilege, 1 Python 3.13+ native API, 1 POSIX-only) |
| Slide-agent focused | `uv run --offline pytest tests/test_slide_agent.py -q --basetemp .test-tmp/fix5` | PASS — 96 passed |
| Ruff lint / format | `uv run --offline ruff check --no-cache .`; `uv run --offline ruff format --check .` | PASS / PASS (140 files) |
| Offline lock validation | `uv lock --check --offline` | PASS — 99 packages |
| Deterministic evaluator | `uv run --offline python evals/run_evals.py --dataset evals/dataset.json` | PASS — 9/9 cases, 36/36 invariants |
| Phase 1 offline demo | `uv run --offline python -m mini_deerflow.demo.phase1_offline` | PASS — all flags true |
| Task 2.1 demo | `uv run --offline python -m mini_deerflow.demo.phase2_pptx_offline --output-dir .mini-deerflow/phase23-core-evidence` | PASS — 8 slides |
| Task 2.2 demo | `uv run --offline python -m mini_deerflow.demo.phase2_artifacts_offline --storage-root .mini-deerflow/phase23-artifacts-evidence` | PASS — download matches storage |
| Task 2.3 demo (final) | final demo command above (`--storage-root .mini-deerflow/phase23-slide-final`) | PASS — evidence above |
| Frontend (`frontend/`) | `npm run typecheck`; `npm run build`; `npm test`; `npm run lint` | PASS — typecheck; build; 20 tests / 4 files; lint 0 warnings |
| Independent security review | — | REQUEST CHANGES → both findings (evidence clipping HIGH, numeric-string illustrative marker MEDIUM) fixed with regressions (96 tests) → resolved; verdict on fixes: closed |
| Manual PowerPoint/LibreOffice visual inspection | — | Unverified |
| External live-provider execution | — | Unverified (offline scripted planner exercises the production path only) |

PM sync-back: `docs/plans` contains the earlier budget-aware synthesis and
session-scoped sandbox plans; no applicable task 2.3 plan was found. No
unrelated plan status is changed by this guide.
