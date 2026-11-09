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
inject tighter fixture limits via `create_app(document_limits=...)`
(explicit injection is authoritative and bypasses environment resolution).

**Fail-closed quota configuration:** an explicitly configured but invalid
`MINI_DEERFLOW_DOCUMENT_*` value (wrong type or outside the valid range)
raises `DocumentLimitsError` at `create_app()` — the server refuses to
start rather than silently broadening to defaults. Invalid settings for
unrelated provider fields (or a missing provider key) fall back to the
audited defaults only when no document override is invalid.

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
