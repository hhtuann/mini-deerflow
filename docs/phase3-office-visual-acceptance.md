# Phase 3 · Task 3.2 — Office visual acceptance

Issue [#19](https://github.com/hhtuann/mini-deerflow/issues/19) / roadmap [#16](https://github.com/hhtuann/mini-deerflow/issues/16)

## Gates and evidence status

**Automated source/structure gate:** the generator creates six independent native editable PPTX decks from real `DeckSpec` and `render_deck`, with `validate_pptx(min_layouts=8)` run on every file. Each contains all eight layouts (title, bullets, comparison, metric, steps, chart, table, conclusion) in a specific combination of Vietnamese/English and light/dark/academic.

**Real viewer gate:** the dedicated GitHub Actions workflow [Phase 3 Office Visual Evidence](../.github/workflows/phase3-office-visual.yml) installs **LibreOffice Impress**, actual system fonts and Poppler tools on Linux. It opens/converts all six PPTX using **LibreOffice headless** to 6 PDFs, then rasterizes the 48 PDF pages to 48 PNG screenshots. A machine-readable manifest includes each PDF's page count, PPTX SHA, screenshot paths/dimensions, PDF text-extraction availability and `pdffonts` outputs. A successful render reports **RENDERED_AWAITING_HUMAN_QA**, NOT visual approval. All source files, real PDF previews, PNGs, font reports and manifest are uploaded as GitHub Actions artifacts (14-day retention).

**Manual visual gate: UNVERIFIED until a reviewer actually inspects the images/files.** Rendering and slide shape geometry do NOT prove text fits, fonts were not substituted, chart labels are readable, contrast is acceptable or editing is retained in PowerPoint. The workflow never marks manual checks as PASS. Native editability is structurally tested; a programmatic python-pptx table cell edit/reopen test is not a human Office round trip.

No LLM calls, API keys or provider secrets are used. All sample captions and data are explicitly **illustrative**, not evidence of factual research.

## Matrix

| Case | Theme | Locale | Native layouts | Additional checks |
| --- | --- | --- | --- | --- |
| `vi-light` | light | vi | all 8 | accented letters, long source label, table and chart |
| `vi-dark` | dark | vi | all 8 | foreground/background legibility and chart labels |
| `vi-academic` | academic | vi | all 8 | muted colors, Vietnamese shaping and wrapping |
| `en-light` | light | en | all 8 | English captions, source-label wrapping |
| `en-dark` | dark | en | all 8 | negative-space contrast and native chart |
| `en-academic` | academic | en | all 8 | typography/table/metric/source text |

The 8-slide shape contract is exercised for **all 3 themes × both languages**, not just one "golden" PPTX. Fixtures are generated from `mini_deerflow.demo.phase2_pptx_offline.demo_spec` with explicit language variants; NOT scripted slide agents masquerading as a live LLM.

## Reproduction

At the repository root, with locked development dependencies:

```sh
uv sync --locked --dev
uv run --no-sync python scripts/visual_acceptance.py --output-dir .mini-deerflow/office-visual-evidence
```

This creates native editable PPTX files and reports `BLOCKED_VIEWER_NOT_RUN` because no viewer was invoked. It is a safe way to create sample decks on Windows.

For genuine viewer screenshots install LibreOffice Impress (`soffice` in PATH), Poppler (`pdfinfo`, `pdftoppm`, `pdffonts`, `pdftotext`) and suitable Unicode fonts (Liberation/Noto). Then run:

```sh
uv run --no-sync python scripts/visual_acceptance.py --render --output-dir .mini-deerflow/office-visual-evidence
```

To pass a non-default LibreOffice executable, use `--soffice /path/to/soffice`. Missing tools produce **BLOCKED_VIEWER_UNAVAILABLE**, never fabricated screenshots; failed conversion, wrong PDF page counts, insufficient PNGs or corrupt PNG headers produce **FAILED_VIEWER_PIPELINE**. Generated PPTX still remain available for inspection.

The real-viewer GitHub workflow runs on relevant PRs, main changes, and manual `workflow_dispatch`; it is separate from the Phase 3 offline regression gate, keeping credentials and paid calls outside CI.

## Reviewer procedure — record actual evidence

Download **office-visual-evidence-<run>-<attempt>** from GitHub Actions; do not judge screenshots solely from the CI green check. Keep the artifact's `manifest.json` next to the report. For each of the 6 cases, inspect screenshots `screenshots/{case}-1.png` … `-8.png`, compare the PDF, and open the corresponding `pptx/{case}.pptx` in Microsoft PowerPoint and/or LibreOffice Impress. For each slide, check the following and record **PASS / FAIL / BLOCKED** with slide index and screenshot/Office-version evidence:

- [ ] **Clipping/wrapping:** no cut-off diacritics, clipping, clipped title/footer, weird lines or text leaving its text box.
- [ ] **Overlap/alignment:** each editable object is aligned, legible and not covering another object.
- [ ] **Font coverage/substitution:** VI uppercase/lowercase `ă â ê ô ơ ư đ`, accents, en dashes and punctuation display correctly. Inspect `fonts/{case}.txt`, installed fonts and target viewer; PDF font listing is not enough to prove zero substitutions.
- [ ] **Theme contrast:** body text, muted citation text, chart axes and chart legends remain readable on every theme.
- [ ] **Native table/chart:** headers, cell contents, chart values/categories/data labels/legend do not overlap; edit values and verify their persistence after **Save → Close → Reopen**.
- [ ] **Long source labels:** source text is distinguishable, wraps acceptably and retains full meaning; screenshots alone cannot prove source semantic validity.
- [ ] **Edit preservation:** edit at least one text box, native table cell, and native chart's workbook/series data in the actual Office viewer; save PPTX, close and reopen, then confirm edits and native chart/table still work.
- [ ] **Cross viewer consistency:** identify Microsoft PowerPoint/LibreOffice version, OS, fonts used and any divergence. If Microsoft PowerPoint is unavailable, mark its gate **BLOCKED**, not PASS.

Recommended manual record (one per case):

| Field | Record |
| --- | --- |
| Case and artifact SHA | `vi-light` / SHA from manifest |
| Reviewer, date/time, viewer version/OS | … |
| Slide numbers and screenshot refs | … |
| Clipping / wrapping / overlap | PASS / FAIL / BLOCKED with notes |
| Fonts and Vietnamese accents | PASS / FAIL / BLOCKED with notes |
| Contrast and table/chart labels | PASS / FAIL / BLOCKED with notes |
| Long source labels | PASS / FAIL / BLOCKED with notes |
| Text/table/chart edit-save-reopen | PASS / FAIL / BLOCKED with notes |
| Viewer-specific issues | … |
| Final manual verdict | PASS / NEEDS FIX / BLOCKED |

**Task 3.2 complete** only when 48 rendered pages have been evidenced, manual visual review is recorded, and failures (if any) are fixed/tested on the Task 3.2 branch. The separate PowerPoint viewer gate can be explicitly documented BLOCKED if not available; never replace it with an unviewed screenshot or structural OOXML check. Actual live-provider slides are variable and not part of the deterministic sample matrix; a live sample should be added to manual review when available.

## Limits and honesty

- LibreOffice headless PDF/PNG proves a genuine conversion, not pixel-perfect PowerPoint parity.
- Automatic PNG header/size/page counts are smoke checks, not clipping/overlap detection or a visual quality score.
- PDF text extraction indicates text accessibility only; it cannot prove rendered glyph shaping.
- `pdffonts` lists actual PDF font names; substitute/no substitute requires a human comparison to expected Arial and viewer installed fonts.
- No screenshot comparison, semantic fact-check, OCR, or simulated human review is used; all human gates remain **UNVERIFIED** until someone checks.
- Font packages vary across platforms; record the renderer OS and font environment in the reviewer notes.
