# Document Retrieval & Citations — D3 (issue #49)

Local lexical retrieval over the D2 chunk store with verifiable citations.
**No FTS5 dependency, no embeddings, no network.** Pure-Python BM25
(k1=1.5, b=0.75) scans the persisted `document_chunks` rows per query —
deterministic by construction, nothing to re-sync or corrupt.

## Retrieval

- **Version pinning**: only chunks of the **latest version** of each
  **active** selected document whose extraction is `completed` are
  candidates (SQL join on `documents.latest_version =
  extractions.version AND status='completed' AND documents.status='active'`).
  Superseded versions, deleted documents, not-yet-extracted versions, and
  other workspaces are excluded by construction; orphan rows (chunks
  without a live extraction join) can never surface.
- **Tokenizer**: casefold + NFKD + diacritic strip + word split — Vietnamese
  base-letter matching ("truong hoc" ↔ "trường học").
- **Ranking**: BM25 over the caller-scoped corpus; ties broken by
  `(document_id, chunk_index)` for determinism; identical queries on
  identical stores produce identical orders across processes.
- **Budgets** (env-overridable, fail-closed like D1/D2): max hits (8),
  max context chars (12,000), max query chars (400), deadline seconds (5,
  cooperative — checked per chunk during scoring). Requests may shrink
  budgets only; larger values are rejected. Unknown scope document ids are
  rejected rather than silently narrowing the corpus.
- **Injection resistance**: the query is data — tokenized in Python, never
  interpolated into SQL (all statements parameterized) and never passed to
  any matcher DSL; previews are ≤200 chars; errors are closed prose.

## Citations

A citation is `(workspace, document_id, version, chunk_id)`.
`resolve_citation` allowlist-validates every resolution against persisted
rows: active document in the *given* workspace → completed extraction for
the latest version → chunk row with a re-validating locator. Closed
outcomes: `not_found` (deleted/cross-workspace/unknown —
indistinguishable), `stale_version` (chunk exists only under a superseded
version, or the latest version has no completed extraction),
`integrity` (persisted locator/data fails allowlist validation). The D2
chunk-detail endpoint now serves this contract: 200 verified, 404
not-found, **409 stale**.

## API (additive)

| Method | Path | Success | Errors |
|---|---|---|---|
| POST | `/api/{mode}/workspaces/{workspace_id}/retrieval` — body `{"query", "document_ids"?, "max_hits"?, "max_context_chars"?}` | 200 `{hits[], total_candidates, truncated_by_budget, scoped_documents}` | 404 unknown workspace · 422 invalid body/scope/query or budget-above-limit · 409 while another operation is active · 503 scan deadline exhausted · 403/413 from the global boundary |
| GET | `/api/{mode}/workspaces/{ws}/documents/{doc}/chunks/{chunk_id}` (citation resolution) | 200 verified chunk | 404 not found (deleted/cross-workspace/unknown, indistinguishable) · 422 malformed chunk-id shape (path pattern) · **409 stale** · 409 integrity |

## Benchmark (AC1) — `scripts/document_retrieval_benchmark.py`

Deterministic, credential-free, synthetic corpus (6 docs, VI+EN). Results:

| Metric | Value |
|---|---|
| Recall@8 (lexical/diacritic-folded VI+EN queries) | **1.00** (8/8) |
| MRR | **1.00** |
| Citation-match rate (top hit ↔ persisted source) | **1.00** |

**Documented limitation (by design):** pure lexical retrieval does not
handle semantic paraphrases — "renewable energy from sunlight" does not
retrieve the solar document, and "học đường mới" surfaces the relevant
school document only by lexical accident (token overlap on "học"/"mới")
alongside an unrelated farming document. This is the accepted D3
baseline; semantic recall is out of scope (issue #49), as are managed
vector services and paid embeddings. Trust-boundary note: the read path
validates chunk rows against the live extraction join; a local operator
directly tampering with the SQLite file (e.g. inserting extra chunk rows
under a live completed extraction) is outside the single-user
trusted-local threat model, consistent with D1/D2.

## Testing

`tests/test_document_retrieval.py` (tokenizer, ranking, diacritic
matching, scoping, budgets, version pinning, deletion/isolation/dedup,
orphan-row immunity, restart determinism, citation matrix incl. stale and
tampered locator, fail-closed limits) and
`tests/test_api_documents_retrieval.py` (HTTP contract, budgets
shrink-only, origin policy, boundary cap, leak canaries incl. SQL-injection
shaped queries, restart persistence, stale 409).
