# Phase 2 PPTX core — Issue #8, task 2.1

## Scope and architecture

Task 2.1 provides a standalone, offline presentation engine: validated contracts,
eight native editable layouts, three static themes, an independent validator,
and an illustrative Vietnamese demo. **PASS — automated task 2.1 acceptance**;
final review verdict: **APPROVE**. Manual viewer verification remains unverified.

The engine is under `src/mini_deerflow/pptx/`:

| Module | Responsibility |
| --- | --- |
| `schemas.py` | Closed, frozen Pydantic contracts and content limits |
| `design.py` | Static canvas, spacing, color and typography tokens |
| `renderer.py` | `DeckSpec` to genuine PPTX bytes using python-pptx; inert embedded chart data using XlsxWriter |
| `validator.py` | Independent OOXML/ZIP inspection and python-pptx reopening against the expected contract |
| `../demo/phase2_pptx_offline.py` | Caller-owned local output and eight-layout fixture |

This core has no runtime integration: no agent tool registration, graph node,
LLM deck generation, research-to-slide mapping, checkpoint changes, API endpoint,
React/Streamlit download flow, or workspace artifact integration. The existing
[architecture baseline](architecture-baseline.md),
[system architecture](system-architecture.md), and
[React/API migration](react-ui-migration.md) describe those separate application
boundaries. A future integration must explicitly arrange ownership and authority.

## Python API and ownership

```python
from mini_deerflow.pptx import DeckSpec, render_deck, validate_pptx

spec = DeckSpec.model_validate({
    "title": "Example deck",
    "slides": [{
        "id": "intro",
        "title": "Example slide",
        "content": {"kind": "title", "subtitle": "Literal editable text"},
    }],
})
data: bytes = render_deck(spec)
report = validate_pptx(data, spec)
assert report.valid
```

- `render_deck(spec: DeckSpec) -> bytes` returns an in-memory PPTX. It has no
  filesystem/path authority. The caller owns the returned bytes and any write,
  storage, download, or cleanup policy.
- `validate_pptx(source: bytes | str | Path, expected: DeckSpec, *,
  min_layouts: int = 1) -> ValidationReport` requires the **complete expected
  `DeckSpec`**, including every slide and all content. It is not a generic
  validate-any-PowerPoint-file API. Local path input is caller authority, never
  a path supplied inside a deck contract.
- Both entry points revalidate serialized model data, including models bypassed
  through `model_construct` or changed with `model_copy`.
- Schema violations raise Pydantic validation errors. Schema-valid content can
  still raise `LayoutOverflowError` (from `pptx.renderer`) during density
  preflight: shorten it or split the slide. Failed package/content checks raise
  public `PptxValidationError`, rather than returning a success-shaped partial
  validation report.
- Public package exports are `DeckSpec`, `SlideSpec`, `render_deck`,
  `validate_pptx`, and `PptxValidationError`. Content model classes live in
  `pptx.schemas`.

## Contract fields and limits

All models reject unknown fields (`extra="forbid"`) and are frozen. Ordered
tuples preserve slide/content order; JSON-style arrays can be validated into
them. Limits below count Python string characters, not UTF-8 bytes or graphemes.

| Shared type | Limit / meaning |
| --- | --- |
| `Title` | 1–80 characters |
| `Text` | 1–180 characters |
| `Label` | 1–36 characters |
| `Cell` | 1–48 characters |
| `Number` | Strict numeric float contract, finite, inclusive range −1,000,000,000 to +1,000,000,000; canonicalized to 15 significant digits |

All text types additionally reject blank/whitespace-only and unsafe text as
described below. Required fields have no default unless explicitly stated.

| Model | Fields |
| --- | --- |
| `DeckSpec` | `title: Title`; `aspect_ratio: "16:9"` (default `"16:9"`); `theme: "light" \| "dark" \| "academic"` (default `"light"`); `slides`: 1–40 `SlideSpec` values |
| `SlideSpec` | `id`: unique within deck, regex `^[a-z][a-z0-9-]{0,47}$` (1–48 characters); `title: Title`; `content: Content` |

`Content` is an eight-way discriminated union using `kind`. Serialized content
must identify its branch with `kind`; each concrete content class defaults its
own `kind` to the corresponding literal. A slide cannot mix unrelated layout
fields or select an arbitrary template.

| `kind` / model | All content fields besides `kind` | Native result |
| --- | --- | --- |
| `title` / `TitleContent` | `subtitle: Text` | Title, subtitle, page number and accent shape |
| `bullets` / `BulletsContent` | `items`: 1–6 `Text` values | Editable bullet-prefixed paragraphs |
| `comparison` / `ComparisonContent` | `left`, `right`: `Column`; each column has `heading: Label`, `items`: 1–4 `Text` values | Two panels, headings and bullet-prefixed paragraphs |
| `metric` / `MetricContent` | `value: Label`, `label: Title`, `insight: Text` | Large literal metric, label and insight |
| `steps` / `StepsContent` | `items`: 2–5 `Title` values | Numbered editable steps on native panels |
| `chart` / `ChartContent` | `chart_type`: `"column" \| "bar" \| "line"` (default `"column"`); `categories`: 1–8 unique `Label` values; `series`: 1–3 `ChartSeries` values; `caption: Text` | Native clustered column, clustered bar or line chart, data labels, legend and caption |
| `table` / `TableContent` | `headers`: 1–5 `Cell` values; `rows`: 1–8 data rows of `Cell \| Number`; `caption: Text` | Native editable table, header row and caption |
| `conclusion` / `ConclusionContent` | `items`: 1–3 `Text` values; `sources`: 1–3 `Text` values | Conclusion bullets and literal source lines |

### Chart and table bounds

`ChartSeries` has `name: Label` and `values`: 1–8 `Number` values. Series names
must be unique, categories must be unique, and each series must have exactly
one value per category. Ordering is retained. Data is embedded in an editable
XLSX `Sheet1`, with categories in column A, series names in row 1 from column B,
and numeric values in the corresponding data columns. This is illustrative
categorical data, not arbitrary workbook generation.

Each table data row must have exactly as many cells as there are headers.
The maximum is five columns and eight data rows plus one header row. Numeric
cells use the same canonical numeric policy as charts; rendered cell text is
`str(value)` after validation, so a numeric integer input can display as `1.0`.
There is no custom number-format, merged-cell or arbitrary style field.

Schema maxima are hard input bounds, not promises that every maximum-length
combination fits a layout. Density preflight may reject a bounded table or text
slide before returning bytes.

## Literal Unicode and numeric policy

Text is preserved literally: Vietnamese diacritics, combining marks, CJK and
other accepted Unicode are not transliterated or normalized. Markup-looking
strings, URLs and spreadsheet-looking strings are text, not HTML, resource
requests, active hyperlinks or executable expressions. `sources` are literal
source lines, not runtime citation-membership validation.

Every text boundary rejects Unicode categories `Cc` (controls) and `Cs`
(surrogates), plus U+FFFE and U+FFFF. This includes tabs, LF/CR, NUL, vertical
tabs and C1 controls; they are rejected rather than silently stripped or turned
into line breaks. Use separate `items`/`sources` entries for separate paragraphs.
Other Unicode categories are not broadly stripped. Length and density checks
are separate from this control-character policy.

`Number` accepts numeric integers/floats under the strict float contract, not
numeric strings or booleans. NaN, infinities and out-of-range values are rejected.
Validation canonicalizes with `float(format(value, ".15g"))`, aligning chart
caches and embedded workbook values with Excel's 15-significant-digit precision.
This is **significant digits**, not 15 decimal places, arbitrary decimal
precision, or preservation of every input binary-float bit. Expected specs used
for validation undergo the same canonicalization.

The renderer writes workbook strings explicitly with formula and URL conversion
disabled (`strings_to_formulas=False`, `strings_to_urls=False`). Formula-looking
text remains inert. Chart OOXML still contains the required internal `c:f`
worksheet range references; these are distinct from executable worksheet cell
formulas, which the validator rejects.

## Static design and determinism

The canvas is 12.8 × 7.2 inches (16:9), with a 0.6-inch margin, 0.4-inch gap,
body top 1.7 inches and body height 4.4 inches. Layout placement is fixed.

| Theme | Background | Foreground | Muted | Accent | Surface |
| --- | --- | --- | --- | --- | --- |
| `light` | `F8FAFC` | `0F172A` | `475569` | `4F46E5` | `E2E8F0` |
| `dark` | `0F172A` | `F8FAFC` | `CBD5E1` | `A5B4FC` | `1E293B` |
| `academic` | `FFFCF5` | `192E28` | `445A50` | `25634C` | `E6EDE6` |

All three themes request **Arial**. Fonts are not embedded; Helvetica/sans-serif
substitution and glyph fallback are viewer-owned, not an engine-enforced fallback
chain. Base title/body/small sizes are 30/22/14 pt. Layout-specific sizes include
28 pt subtitle, 24/20 pt comparison, 54/26/22 pt metric, 20 pt steps, 16 pt table
and sources, and 14 pt captions/chart text.

The determinism contract is **semantic**, not ZIP-byte identity. Stable content,
slide/object order, geometry and static styling are the comparison targets.
Presentation metadata uses a fixed 2000-01-01 UTC timestamp, revision 1 and
author `Mini DeerFlow`; the embedded workbook also has fixed creation metadata.
ZIP timestamps/packaging and dependency or implementation changes can change
bytes and file size. Do not use a fixed size or binary hash as acceptance.

## Independent validation: what is checked

The validator imports contracts, not the renderer or a renderer-produced manifest.
It derives expected text from the complete spec, securely parses OOXML with
defusedxml, and independently reopens the actual presentation with python-pptx.

### Package and nested XLSX checks

- PPTX input must be nonempty and at most **10,000,000 bytes**. Path input is
  size-checked before a bounded read.
- ZIPs allow at most **2,000 members**, no duplicate member names, valid CRCs,
  and at most **40,000,000 bytes** declared total uncompressed size. Unsafe
  member paths (absolute, `..` segments, backslashes, NUL or colon) are rejected.
- Required content-types and presentation/workbook parts must exist. XML is
  parsed securely; relationship IDs must be unique and internal targets must
  exist. External relationships are unsupported and rejected.
- The embedded XLSX receives its own package/relationship/XML checks and must
  expand to **less than 40,000,000 bytes**. These are per-package bounds, not a
  claimed aggregate nested-package budget or general-purpose hostile-file scanner.
- Active-content checks inspect the full archive member list, including binary
  members, not just parsed XML. `.bin` members are rejected except for the
  presentation-only standard inert printer-settings path matching exactly
  `ppt/printerSettings/printerSettings[0-9]+\.bin`. Nested workbooks have no
  binary-member exception. Member names containing `activex` (case-insensitive)
  are rejected.
- Relationship types containing VBA, OLE object or ActiveX tokens are rejected
  case-insensitively in both presentation and workbook packages, along with
  external relationships. Worksheet cell formulas are rejected; the workbook
  XML-part scan additionally rejects names containing `vbaProject` or
  `externalLink`. The generated workbook has no macros, active links or cell
  formulas. This is a validator for the bounded generated-deck contract, not a
  general-purpose Office sanitizer.
- There must be exactly one sheet named `Sheet1`, with the expected worksheet
  relationship. Shared-string indices and duplicate populated-cell addresses
  are checked; all populated workbook values must match the expected series
  names, categories and canonical numbers, with no extra populated data.
- Chart type, categories, series names/numeric caches and data-label presence
  are checked on reopen. The category cache and each series' internal name,
  category and value range references must match the expected `Sheet1` ranges.
  For every series, name/category/value caches must have the exact expected
  `ptCount` and complete ordered zero-based `idx` sequence; missing, duplicate,
  reordered or out-of-range point indices fail validation.
  Embedded worksheet data is validated independently of chart caches.

### Content, native objects, geometry and density

- Exact canvas, deck title, slide counts in both reopened and OOXML views,
  slide order and `id:kind` identity, titles, page numbers and layout-specific
  text must match the expected spec; missing/changed/extra text is rejected.
- Every object must be an allowed native text box, auto shape, table or chart.
  Duplicate object names and unsupported/non-native objects are rejected.
  Native object counts and corresponding OOXML shape/table/chart counts agree.
- Tables must have exactly the expected dimensions and stringified cell content.
  Charts/tables must appear exactly where the corresponding layout requires one;
  decorative native shape counts are checked per layout.
- All objects have positive dimensions, no rotation, and lie within the canvas.
  Table columns are at least 0.5 inches and rows at least 0.3 inches; their summed
  dimensions must stay within the table shape. Charts are at least 2 × 2 inches.
- Reopened text boxes require explicit font sizes in 10–72 pt and at least
  0.3-inch width. An independent conservative density estimate uses box/font
  metrics, counts East Asian wide/fullwidth characters as two units and combining
  characters as zero. The renderer also preflights text boxes and table cells.
  Validator table checks cover cell content and row/column geometry, not an
  independent cell-by-cell font-shaping proof.
- `min_layouts` is an explicit distinct-layout coverage threshold, default 1.
  The demo requests 4; its fixture actually covers all 8.

`ValidationReport` contains `valid`, `slide_count`, sorted distinct `layouts`,
`native_objects` (text boxes, auto shapes, tables, charts), `geometry_checked`,
`file_bytes`, and per-slide `SlideInspection` values (`id`, `layout`, `title`,
`text`, native counts and geometry count). `to_dict()` provides the report data.
`visual_rendering_verified` remains **false**. Per-slide `text` lists text-box
paragraphs; chart/workbook and table data are checked separately.

## Safe offline demo

From a provisioned checkout with dependencies available locally, select a fresh
caller-owned directory and run exactly:

```powershell
uv run --offline python -m mini_deerflow.demo.phase2_pptx_offline --output-dir .mini-deerflow/phase2-review-evidence
```

The demo needs no credentials, provider, network resource or runtime service.
`uv --offline` also prevents dependency fetching; it requires the installed/cached
dependencies. It renders and validates bytes, exclusively creates
`phase2-offline.pptx`, reopens that file for independent validation, and prints a
JSON report. It writes no report JSON file automatically.

Use a fresh directory for reproducible review evidence. The implementation
permits an existing directory but opens the output file with `xb`: an existing
`phase2-offline.pptx`, including an existing symlink, causes refusal rather than
overwrite. The directory is a trusted local CLI choice, not an agent-controlled
workspace path. Without `--output-dir`, a persistent temporary directory is
created; the caller owns cleanup. Optional `--theme light|dark|academic` defaults
to `light`.

### Reopened eight-slide fixture summary

The deck title from `demo_spec()` is `Mini DeerFlow — Minh họa ngoại tuyến`.
These are illustrative fixture claims/data, not research findings or benchmarks.

| # / ID | Layout and exact slide title | Content summary |
| --- | --- | --- |
| 1 / `intro` | `title` — Báo cáo minh họa: Nghiên cứu có cấu trúc | Subtitle: “Mini DeerFlow — PowerPoint chỉnh sửa được, hoàn toàn ngoại tuyến” |
| 2 / `goals` | `bullets` — Mục tiêu và phạm vi | Preserve Vietnamese/content; editable text, shapes, table and chart; illustrative rather than real research data |
| 3 / `compare` | `comparison` — So sánh cách trình bày | “Ảnh tĩnh”: hard to edit individual parts, unstructured data; “Đối tượng gốc”: direct text editing, embedded chart data |
| 4 / `metric` | `metric` — Chỉ số minh họa | Value `8`; label “Trang trình chiếu có nội dung”; insight explicitly says fixture slide count, not performance |
| 5 / `steps` | `steps` — Quy trình dựng bản trình chiếu | Validate DeckSpec → apply layout/theme → create native PowerPoint objects → independently reopen/check |
| 6 / `chart` | `chart` — Biểu đồ — dữ liệu giả lập | Column chart, categories `Tháng 1`, `Tháng 2`, `Tháng 3`; `Mẫu A` = [12, 18, 24], `Mẫu B` = [10, 16, 21]; caption identifies fictional fixture data and unit “lượt” |
| 7 / `table` | `table` — Bảng kiểm tra khả năng chỉnh sửa | Three headers and three data rows (below); caption “Nguồn: cấu trúc bộ mẫu ngoại tuyến.” |
| 8 / `summary` | `conclusion` — Kết luận và nguồn | Genuine editable PPTX; structural checks do not replace display checks; sources identify the internal fictional fixture and python-pptx, with no downloaded resources |

The table's headers are `Thành phần`, `Số lượng`, `Trạng thái`. Its three rows
are (`Biểu đồ gốc`, 1, `Có dữ liệu nhúng`), (`Bảng gốc`, 1, `Sửa từng ô`), and
(`Trang minh họa`, 8, `Mở lại thành công`). Numeric cells reopen as `1.0`, `1.0`
and `8.0` after the float contract; this is not lost data.

Final accepted evidence is at
`.mini-deerflow/phase2-final-evidence/phase2-offline.pptx`, produced by the
actual final demo command:

```powershell
uv run --offline python -m mini_deerflow.demo.phase2_pptx_offline --output-dir .mini-deerflow/phase2-final-evidence
```

The final reopened report records **`valid=true`, 8 slides, 8 layouts,
33 text boxes, 14 native auto shapes, 1 table, 1 chart, 49 geometry checks,
46,136 bytes**, and `visual_rendering_verified=false`. The byte count identifies
this evidence run only; it is not a fixed-size or byte-determinism requirement.
For reproduction, use a fresh directory such as `phase2-review-evidence` in the
command above; the artifact name remains `phase2-offline.pptx` and overwrite is
refused. Earlier evidence at `.mini-deerflow/phase2-evidence/phase2-offline.pptx`
was also independently reopened with all eight layouts and the same object
counts; the final-evidence artifact is the acceptance reference.

## Manual visual limitations

No `soffice`, PowerPoint (`POWERPNT`) or `pdftoppm` executable was found on PATH
in this environment. No visual rendering was verified. Reopening/OOXML checks
and density estimates do not establish actual viewer rendering, font/glyph
fallback, line wrapping, clipping, contrast, chart label placement or edit UX.
There is **no guarantee all text fits in an actual viewer**. Manual inspection
in a real viewer remains pending, including all layouts and themes.

## Final automated verification — task 2.1

**PASS — automated task 2.1 acceptance. Final review: APPROVE.** The following
accepted results were supplied by the final test/review handoff; this docs-only
finalization does not claim to rerun those suites. They are local verification
results, not a claim of a CI run or manual visual acceptance. The PR provides
the exact reviewed HEAD; this document does not embed a self-referential commit
hash. Project-management review confirmed no applicable implementation plan.

| Check | Exact command | Accepted result |
| --- | --- | --- |
| Focused PPTX core and review regressions | `uv run --offline python -B -m pytest -p no:cacheprovider tests/test_pptx_review_regressions.py tests/test_pptx_core.py -q --tb=short` | **224 passed** (191 core + 33 review), 21.52 s |
| Full Python regression | `uv run --offline pytest -q --basetemp .test-tmp/phase2-accepted --tb=short` | **1,040 passed, 6 skipped, 223 subtests**, 52.79 s |
| Ruff lint | `uv run --offline ruff check . --no-cache` | PASS |
| Ruff format | `uv run --offline ruff format --check . --no-cache` | PASS; 132 files |
| Offline lock validation | `uv lock --check --offline` | PASS; 99 packages |
| Deterministic evaluator | `uv run --offline --no-sync python evals/run_evals.py --dataset evals/dataset.json` | **9/9 cases, 36/36 invariants** |
| Phase 1 offline demo | `uv run --offline --no-sync python -m mini_deerflow.demo.phase1_offline` | **14/14 boolean checks** |
| Frontend typecheck / build / lint (from `frontend/`) | `npm run typecheck`; `npm run build`; `npm run lint` | PASS / PASS / PASS |
| Frontend tests (from `frontend/`) | `npm test` | **20 passed in 4 files** |
| Final offline demo | `uv run --offline python -m mini_deerflow.demo.phase2_pptx_offline --output-dir .mini-deerflow/phase2-final-evidence` | Valid; 8 slides / 8 layouts; final artifact and counts above |

The README's previous React/API migration acceptance record remains historical;
its 816-test and 95-package results are superseded for this task by the full
1,040-test and 99-package results above. Manual viewer inspection remains
unverified for the reasons in [Manual visual limitations](#manual-visual-limitations).
Automated acceptance establishes the bounded standalone core contracts, not
runtime integration, live-model quality, viewer-perfect text fit or production
readiness.
