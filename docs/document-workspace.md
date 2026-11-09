# Document Workspace — D1 foundation (issue #47)

Local-only workspace/document persistence, safe blob storage, and a bounded
HTTP API. Track D1 of the Document Workspace roadmap (#46). **Trust model:
trusted single-user loopback service.** Opaque server-generated identifiers
provide enumeration resistance; they are **not** authentication, and this is
explicitly not multi-tenancy (#46 non-goals). No parsing, retrieval, or
question-answering happens here — those arrive in D2–D4.

## Identity and storage layout

- Workspace IDs: `ws-<uuid4 hex>`; document IDs: `doc-<uuid4 hex>`. Clients
  never supply storage paths; filenames are validated display metadata only.
- Per mode root (offline `<root>`, live `<root>/live`):
  - `documents.sqlite` — metadata ledger (workspaces, documents, versions).
  - `document-store/ws-<sha256(workspace_id)>/doc-<sha256(workspace:doc)>/v<version>/<sha256>.<ext>`
    — blob tree derived entirely from server-side identifiers and checksums.
- Blob I/O goes through the hardened `Workspace` primitives: exclusive
  temp+fsync+link atomic publication, `O_NOFOLLOW` reads, `st_nlink == 1`,
  Windows reserved-name/traversal rejection, and long-path (`\\?\`) syntax on
  Windows.

## SQLite schema and migration

Idempotent `CREATE TABLE IF NOT EXISTS` DDL plus a `document_store_meta`
singleton row pinning `schema_version = 1`; a database written by a newer
schema version fails closed (`DocumentIntegrityError`) instead of being
partially interpreted. Future column additions follow the guarded
`ALTER TABLE` pattern used by the conversation ledger.

## Lifecycle, versions, checksums

- Statuses: `active` → `deleted` (tombstone). Deleted workspaces cascade to
  their documents; reads of deleted objects are 404.
- Every version stores `byte_size` + server-computed `sha256`; **every read
  re-verifies the persisted bytes** and returns integrity failure (409)
  rather than suspect data.
- Re-uploading byte-identical content as a new version of the same document
  is idempotent: the existing version is returned and no quota is consumed.
  Identical bytes under a *different* document are a separate document.
- Uploads are single raw-body requests (no multipart; no new dependency).
  Blob write happens inside the `BEGIN IMMEDIATE` transaction; the metadata
  row commits only after the persisted bytes verify. A crash therefore
  leaves at worst sweepable orphans: startup and every delete run
  `recover()`, which removes `.pending-*` staging files, blobs not
  referenced by any active version, and empty directories.

## Quotas (defaults; owner approval required before real data)

| Limit | Default | Env override | Valid range |
|---|---|---|---|
| Max document bytes | 10 MiB | `MINI_DEERFLOW_DOCUMENT_MAX_BYTES` | 1 KiB – 50 MiB |
| Documents per workspace | 50 | `MINI_DEERFLOW_DOCUMENT_WORKSPACE_MAX_DOCUMENTS` | 1 – 1000 |
| Versions per document | 20 | `MINI_DEERFLOW_DOCUMENT_MAX_VERSIONS` | 1 – 100 |
| Storage per workspace | 200 MiB | `MINI_DEERFLOW_DOCUMENT_WORKSPACE_MAX_BYTES` | 1 MiB – 2 GiB |
| Workspaces | 100 | `MINI_DEERFLOW_DOCUMENT_MAX_WORKSPACES` | 1 – 1000 |

Workspace byte quota counts **all versions of active documents**. Apps may
inject tighter fixture limits via `create_app(document_limits=...)`: explicit
injection overrides the resolved quota *values* but never bypasses quota
validation — an app constructed with injected limits still refuses to start
on an invalid explicit document override.

**Fail-closed quota configuration:** any explicitly configured
`MINI_DEERFLOW_DOCUMENT_*` value (environment or `.env`) makes quota
resolution fatal when it cannot be honored exactly:

- an invalid value (wrong type or outside the valid range) raises
  `DocumentLimitsError` ("Document quota configuration is invalid.");
- a **valid** value combined with an unrelated invalid provider setting also
  raises `DocumentLimitsError` ("…could not be verified.") rather than being
  silently discarded and broadened to the audited defaults.

Only when **no** document override is configured at all do unrelated invalid
provider settings (or a missing provider key) fall back to the audited
defaults.

**Retention proposal (owner decision pending):** no automatic expiry in D1;
documents persist until explicit deletion and survive restart. Real-data
retention/backup policy is *not* approved by this feature; the local pilot
with real data remains blocked per #21/#35.

## Allowed types and fail-closed validation

Allowed: PDF, DOCX, TXT. All three must agree:

1. filename extension ∈ {`pdf`, `docx`, `txt`} (≤200 chars, no path
   separators/control characters),
2. `Content-Type` exactly the allowlisted MIME (parameters stripped),
3. content sniff: `%PDF-` within the first KiB / `PK\x03\x04` zip header /
   strict UTF-8 decode.

Bytes are stored inert — never parsed or executed. Unsupported, mismatched,
oversize, empty, or malformed requests fail closed (415/413/422) and leave
no partial storage.

**Known limitation (D2 handoff):** the sniff is a shallow signature check
only — `%PDF-` anywhere in the first KiB and the ZIP local-file-header
prefix do **not** validate document structure. Upload acceptance in D1 is
therefore not a claim of format validity: stored documents must be treated
as untrusted input, and D2 (parsing/chunking) owns structural validation,
bounded ZIP decompression, and malformed-structure failure states.

## HTTP API contract

All routes are mode-scoped (`live`/`offline`), enforce the existing
Origin/Host boundary, serialize behind the single-operation submission lock,
and return fixed-prose errors that never include storage paths or document
contents.

| Method | Path | Success | Errors |
|---|---|---|---|
| POST | `/api/{mode}/workspaces` — body `{"name": str≤80}` | 201 `WorkspaceRecord` | 422 |
| GET | `/api/{mode}/workspaces` | 200 `[WorkspaceRecord]` | — |
| GET | `/api/{mode}/workspaces/{workspace_id}` | 200 `WorkspaceDetailDTO` | 404 |
| DELETE | `/api/{mode}/workspaces/{workspace_id}` | 204 | 404 |
| POST | `/api/{mode}/workspaces/{workspace_id}/documents?filename=…` — raw body | 201 `DocumentRecord` | 404/413/415/422/409 |
| GET | `/api/{mode}/workspaces/{workspace_id}/documents` | 200 `[DocumentRecord]` | 404 |
| GET | `/api/{mode}/workspaces/{workspace_id}/documents/{document_id}` | 200 `DocumentRecord` | 404 |
| POST | `/api/{mode}/workspaces/{workspace_id}/documents/{document_id}/versions?filename=…` — raw body | 200 `DocumentRecord` (idempotent for identical bytes) | 404/413/415/422/409 |
| GET | `/api/{mode}/workspaces/{workspace_id}/documents/{document_id}/content` | 200 bytes (latest) | 404/409 |
| GET | `…/content/{version}` (integer ≥1) | 200 bytes | 404/409/422 |
| DELETE | `/api/{mode}/workspaces/{workspace_id}/documents/{document_id}` | 204 | 404 |

- `WorkspaceRecord`: `workspace_id, name, status, created_at, updated_at,
  document_count, total_bytes`.
- `DocumentRecord`: `workspace_id, document_id, filename, content_type,
  status, created_at, updated_at, latest_version, byte_size, sha256,
  versions[]` — list responses return each record with an empty `versions`
  array; the single-document GET returns the full version history.
- Content downloads carry `ETag` = version sha256, `X-Document-Version`,
  `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, and a
  **fixed ASCII** `Content-Disposition` filename; user filenames never
  enter headers.
- Wrong-workspace access is indistinguishable from nonexistent (404).

## Request-size boundary

The global 16 KiB `MAX_REQUEST_BYTES` cap is unchanged and audited by the
deployment-readiness gate. The **only** exception is the two raw-body upload
routes above, which accept exactly the configured per-file quota
(`upload_max_bytes`), matched by the audited `UPLOAD_ROUTE_PATTERN`. The new
`document_upload_boundary` readiness check fails closed if the default cap,
route scoping, quota maxima, or app wiring change.

## Testing

`tests/test_documents.py` (store: persistence/restart, quotas, versions,
dedup, delete lifecycle, crash/ orphan recovery, tombstone sweep, symlink
and corruption rejection, cross-workspace isolation, schema fail-closed) and
`tests/test_api_documents.py` (HTTP contract, boundary scoping, origin
policy on uploads, mode isolation, restart durability, leak canaries). All
fixtures are synthetic; no credentials, network, or real documents.

## Extraction and chunking (D2, issue #48)

Bounded text extraction runs as a `document_extraction` job on the app's
single worker (one active operation at a time; in-flight state is
process-local, so a crash leaves no partial rows and the operation can be
re-submitted). Result records are terminal-only: `completed` or
`unavailable` with a closed error class (`encrypted`, `malformed`,
`budget_exceeded`, `deadline_exceeded`, `no_extractable_text`,
`embedded_macro`, `embedded_remote`, `embedded_script`, `storage_error`).
Raw exception text, paths, and document internals never surface.

- **PDF** (pypdf, text layer only): genuine 1-based page locators, never
  synthesized; encrypted, scripted (catalog `/JavaScript` / `/OpenAction` /
  `/EmbeddedFiles` / `/AA`, `/Names` JavaScript tree, `/AcroForm` XFA,
  page-level `/AA`, annotation `/AA` or `/JavaScript` actions), or no-text
  documents are marked unavailable.
- **DOCX** (stdlib `zipfile` + `defusedxml`, no new parser dependency):
  paragraph and table locators only — **no invented page numbers**; macro
  parts (`word/vbaProject.bin`) and external relationship targets are
  refused; ZIP decompression is bounded by declared entry sizes checked
  before any read (only `document.xml` and its rels are ever decompressed);
  a compression-ratio heuristic was deliberately rejected because
  legitimately compressible XML false-positives.
- **TXT**: 1-based line-range locators; strict UTF-8.
- **Chunks**: deterministic — `chunk_id = sha256(version_sha256:index)[:16]`,
  target ≈1,600 / hard cap 2,000 chars, split at unit boundaries;
  `extractor_version` ("d2-v1") is persisted per extraction.
- **Budgets** (cooperative): deadline (default 30 s, checked per
  page/paragraph/entry), max pages 300, max extracted chars 2 M, max XML
  bytes 20 MiB, max zip entries 5,000, max total uncompressed 100 MiB.
  The character budget is enforced **incrementally**: a running total is
  checked after every PDF page, and a within-page pypdf text visitor aborts
  a page as soon as its flushed text exceeds the cap — at pypdf's visitor
  granularity (positioning operations, TJ-array boundaries, end of stream).
  A degenerate stream of bare `Tj` repeats without positioning ops defers
  the abort to end-of-stream: it is bounded by pypdf's own
  decompression/parsing caps (zlib output length, stream-parsing work)
  rather than the deadline, and can hold the single worker for a bounded
  stretch before terminating as `budget_exceeded` (pypdf's
  `LimitReachedError` maps there too). Honest residual limit: extraction is
  cooperative, not preemptive — the D1 input-size quota plus these caps
  bound the worst case; hard preemption would need process isolation
  (out of D2 scope).
- **API (additive)**: `POST …/extraction` (202 job; 409 when a completed
  extraction exists for the version; unavailable can be re-queued),
  `GET …/extraction`, `GET …/chunks` (≤200-char previews), and
  `GET …/chunks/{chunk_id}` (full text + typed locator). Extraction
  configuration uses the same fail-closed resolution as quotas (invalid
  explicit `MINI_DEERFLOW_DOCUMENT_EXTRACTION_*` overrides refuse startup).

D2 tests: `tests/test_document_extraction.py` (parsers, determinism,
reimport/restart identity, budgets, embedded-content refusal, zip bombs)
and `tests/test_api_documents_extraction.py` (job flow, error classes,
wrong-workspace 404, leak canaries, restart persistence, fail-closed
config). Fixtures are synthetic hand-built documents; Vietnamese diacritics
are covered on DOCX/TXT (PDF-with-Vietnamese fixtures would require
PDF-writing tooling outside the dependency budget; pypdf passes UTF-16
text through when a ToUnicode map exists).
