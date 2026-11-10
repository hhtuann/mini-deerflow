# Document Report Writer — D5 (#51)

## Scope and decisions

Approved local-only Document Track #46, based on merged D4 #50 / PR #58.
`ReportWriterAgent` composes the existing `DocumentQAService`; both API modes
are credential-free extractive by default. Explicit selected documents and a
bounded ordered section outline are required. `summary`, `comparison` and
`report` are organization intents, not a claim of semantic reasoning: section
queries produce verified lexical extracts. Differing sources remain separately
attributed, unsupported sections abstain, and D4 limitations remain visible.
No document instructions become tools, capabilities, export markup or code.

DOCX export is **DEFERRED**, not PASS: Issue #51 has no approved DOCX design or
separately documented open/validation evidence. No DOCX endpoint, conversion,
macros or dependency was added. A future approved implementation must establish
safe package construction, external-reference/macro rejection, provenance
roundtrip and actual application open/validation tests before claiming AC5.

## Typed specification and revision

Frozen, extra-forbidden `ReportSpec` records server-generated report ID,
workspace, title, kind, explicit document scope, immutable version/parent,
creation timestamp and ordered sections. A section records its stable ID,
title, query, D4 claims/citations, limitations, source-version snapshot,
approval state and origin report version. Client requests never supply claims
or citation anchors. Strict scalar types reject boolean/string version numbers;
JSON arrays are accepted only for the declared sequence containers.

Creation obtains D4 verified claims and checks generated section source scope,
current versions and persisted quote/locator membership again before publication.
The final check holds the document-ledger write transaction through report
publication, preventing a concurrent document update/deletion between final
verification and commit. Untouched historical sections are not rebound.

Revisions require the exact latest `base_version`; competing revisions have one
winner, otherwise 409. Only existing requested sections are regenerated. Other
sections, including approved content, remain model-equal with original snapshots
and origin versions. Regenerated sections lose approval. An approval-only
revision can approve existing sections, but regeneration and approval of the
same section in one request is rejected. Approval is a local user annotation,
not a claim that evidence is factually true or still current. There is no LLM
provider spend or hidden automatic source refresh.

## HTTP API

All routes inherit loopback Host/Origin/request-size controls and the existing
submission lock. `mode` storage roots are isolated. IDs are not authentication.
Prefix: `/api/{mode}/workspaces/{workspace_id}/reports`.

|Method/path|Behavior|
|---|---|
|POST prefix|Create v1; 201; body title,kind,document_ids,sections|
|GET /{report_id}|Latest immutable ReportSpec|
|GET /{report_id}/versions|Ordered full immutable version history|
|GET /{report_id}/versions/{version}|Exact historical ReportSpec|
|POST /{report_id}/revisions|201; base_version,sections,approved_section_ids|
|GET /{report_id}/versions/{version}/markdown|Deterministic attachment `report.md`|

Unknown/cross-workspace versions are 404, stale revision or corrupt storage 409,
invalid input/scope/quota 422. Deleted workspaces are unavailable. Historical
reports remain readable after source document deletion or supersession; they
are snapshots, never authority for generating new sections. Regeneration with
missing selected sources fails without appending a version. A changed source
can regenerate one section at its new version while untouched sections retain
their old, explicitly historical provenance.

Report sections accept up to 50 verified claims, matching the supported D3
retrieval-hit ceiling; D4's actual context budget can retain fewer and reports
preserve its honest omission limitations. Retrieval deadline exhaustion remains
a server-side 503 for both creation and revision, never invalid-client 422.

## Persistence, recovery and limits

Each mode has `reports.sqlite`, separate from `documents.sqlite`. Version JSON
bytes and SHA-256 are stored together in one SQLite transaction: the commit is
the sole visibility point; no staged report file or latest pointer exists.
Interrupted uncommitted writes roll back on restart. Startup runs quick-check
and verifies schema version, complete ordered histories, payload sizes,
checksums and model/identity invariants. Corrupt committed data fails closed,
not silent fallback to an older latest version. Checksums are corruption
checks, not signatures against a malicious local operator.

Default fixture-only quotas: 50 reports per workspace, 20 revisions/report,
1,000,000 bytes/revision and 20,000,000 bytes/workspace. Requests allow at most
50 selected documents and 20 sections, titles 200 characters, section queries
400. Report store rejects symlink/junction/hardlink database aliases and checks
SQLite sidecars. Existing trusted-local SQLite convention applies; public
multi-user filesystem race protection is not claimed. No auto-expiry or real-data
retention approval is implied. Back up both ledgers and document blobs while
stopped; historical report extracts remain until storage is deliberately
removed under owner policy, even after source tombstones.

## Safe Markdown export

Only exporter-owned Markdown structure is emitted. Every untrusted text field
is flattened, controls removed, URL/domain/email/slash-bearing tokens redacted,
and ASCII punctuation entity-encoded. This conservative policy can redact
innocent fractions or domain-like prose. Canonical ReportSpec and citations are
unchanged; exports disclose display redaction. Citation labels retain safe
server document/version/chunk identifiers and numeric typed locators, never
user filenames or raw storage paths. No active link, image, HTML, script or
macro is emitted. Download uses a fixed ASCII attachment name, `nosniff` and
`no-store`; no caller-supplied path or filename is interpreted.

## Verification

Synthetic core/API tests cover section citation integrity, approval and targeted
revisions, same-base concurrency, restart/history/download, transaction rollback,
corrupt ledger/schema, quotas, hardlink rejection, deleted/revised sources and
source races between generation steps. Markdown negatives cover hostile HTML,
Markdown, links, paths and Unicode controls while preserving ordinary multilingual
text. Real-loopback FastAPI Playwright exercises upload/extraction → grounded
report → approval → targeted revision → immutable history → Markdown download
and cross-workspace isolation. D6 owns report UI/slide integration; no new editor
or document-UI acceptance is claimed here.
