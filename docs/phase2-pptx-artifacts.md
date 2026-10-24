# Phase 2 PPTX artifacts — Issue #10, task 2.2

Task 2.2 adds immutable, turn-owned local PPTX storage and a verified HTTP
download to the [task 2.1 PPTX core](phase2-pptx-core.md). **PASS — final automated
local acceptance**. Security review: **APPROVE**, with no open HIGH/MEDIUM findings.
Manual PowerPoint/LibreOffice viewer verification remains **unverified**, inherited
from task 2.1. Structural validation does not establish visual text fit.

## Architecture and ownership

```text
Trusted Python caller: DeckSpec -> render_deck -> PPTX bytes
                                      |
                              PptxArtifactStore.publish
                                      |
                   validate -> exclusive file -> SQLite metadata
                                      |
Completed owning turn -> retrieve -> verify metadata/spec/bytes/package
                                      |
          GET /api/{mode}/conversations/{thread}/turns/{turn}/presentation
```

| Source | Responsibility |
| --- | --- |
| `src/mini_deerflow/pptx/artifacts.py` | `PptxArtifactStore`, immutable metadata, ownership/status checks and revalidation |
| `src/mini_deerflow/workspace.py` | Bounded binary reads, exclusive publication and narrowly scoped orphan recovery |
| `src/mini_deerflow/conversation.py` | Existing SQLite conversation/turn schema and connection lifecycle |
| `src/mini_deerflow/api/__init__.py` | Trusted store composition and scoped presentation download |
| `src/mini_deerflow/demo/phase2_artifacts_offline.py` | Scripted runtime, real renderer, persistence and fresh-process HTTP proof |
| `tests/test_pptx_artifacts.py` | Publication, isolation, corruption, bounds, filesystem and HTTP contracts |
| `tests/test_pptx_artifact_recovery.py` | Post-link process exit, staging cleanup failures, alias rejection and demo regressions |

`PptxArtifactStore(storage_root, mode="offline" | "live", max_bytes=10_000_000)`
uses one **trusted mode-local root**. It subclasses
`SQLiteConversationRepository`, reusing its connection setup, row factory,
conversation schema initialization and `PRAGMA foreign_keys = ON`. Its operations
open connections through that existing lifecycle; they do not require one global
shared connection object. Artifacts and conversations share
`<storage_root>/checkpoints.sqlite`, alongside the existing checkpoint storage.

The `pptx_artifacts` table has a composite primary key, hence a uniqueness
constraint, on `(mode, thread_id, turn_id)`. Its composite foreign key references
`turns(thread_id, turn_id)` with `ON DELETE CASCADE`. Each row records:

- ownership: `mode`, `thread_id`, `turn_id`;
- `artifact_type`, fixed to `pptx`;
- `byte_size` and lowercase SHA-256 `sha256` for the exact file bytes;
- `spec_sha256` and `spec_json` for the validated canonical `DeckSpec` snapshot.

Canonical JSON uses `ensure_ascii=True`, sorted keys and compact separators.
The snapshot is bounded to **1,000,000 UTF-8 bytes**; SQL/model constraints also
bound its length. Publication revalidates the spec and validates the actual PPTX
against it. Retrieval rechecks the metadata model, ownership, snapshot bound and
digest, parses the stored spec, checks file size/digest and independently validates
the package against that spec.

The singleton `pptx_artifact_mode` table binds the database to its first artifact
mode. Reopening the same database under the other mode fails integrity checking.
Separate mode roots are required even when public thread/turn identifiers match.

Within the session sandbox, the derived layout is:

```text
<mode-root>/workspaces/sessions/v1-<sha256(normalized thread_id)>/
  presentations/<sha256(compact JSON [mode, thread_id, turn_id])>/<sha256(bytes)>.pptx
```

The session resolver retains its existing legacy fallback behavior. Filenames,
paths, digests and metadata are derived internally; HTTP clients cannot select
them. See [Windows artifact paths](windows-artifact-paths.md) for the existing
session/extended-path mechanisms.

## Python usage and lifecycle

Publication is an internal Python operation, with no HTTP upload endpoint.
The owning conversation/turn must already exist in the same mode-local ledger.
For an existing eligible turn:

```python
from mini_deerflow.pptx import DeckSpec, render_deck
from mini_deerflow.pptx.artifacts import PptxArtifactStore

spec = DeckSpec.model_validate({
    "title": "Example deck",
    "slides": [{
        "id": "intro",
        "title": "Example slide",
        "content": {"kind": "title", "subtitle": "Literal editable text"},
    }],
})
store = PptxArtifactStore(".mini-deerflow/demo", mode="offline")

# Inside an async caller, after the owning turn has been reserved:
record = await store.publish("research-001", "turn-1", render_deck(spec), spec)
# Retrieval requires that the ledger now marks that turn completed:
record, exact_bytes = await store.retrieve("research-001", "turn-1")
```

| Ledger status / ownership | Publish | Retrieve |
| --- | --- | --- |
| Existing `running` turn | Allowed | Not found |
| Existing `completed` turn | Allowed | Verified publication only |
| `interrupted` or `failed` turn | Not found | Not found |
| Unknown thread/turn, wrong ownership pair, or no publication | No new owner is created | Not found |

There is **one immutable deck per turn**. `BEGIN IMMEDIATE` serializes publishers.
After a metadata-backed publication exists, a retry with the same canonical spec
returns the original verified record and preserves the **first bytes**, even when
a valid rerender has different ZIP timestamps. Incoming bytes are still validated.
A different canonical spec raises `ArtifactConflictError`; it does not replace
the deck. A corrupted existing publication raises an integrity failure rather
than becoming a successful retry.

`ArtifactNotFoundError` represents missing/ineligible ownership;
`ArtifactIntegrityError` represents failed storage verification, and `ArtifactError`
is the controlled base storage error. Controlled storage/HTTP messages exclude
host paths, raw presentation data and tracebacks.

## HTTP download and trusted configuration

```http
GET /api/{mode}/conversations/{thread}/turns/{turn}/presentation
```

`mode` is `offline` or `live`. Thread and turn identifiers use the existing bounded
API identifier validation. Successful responses return the exact verified bytes:

| Header | Value |
| --- | --- |
| `Content-Type` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` |
| `Content-Disposition` | `attachment; filename="presentation.pptx"` |
| `X-Content-Type-Options` | `nosniff` |
| `Cache-Control` | `no-store` |
| `ETag` | Quoted SHA-256 of the returned bytes |

Missing publications, unknown ownership and non-completed turns return **404**
with `{"detail":"Presentation was not found."}`. Corrupt/missing stored files or
other controlled artifact verification failures return **409** with
`{"detail":"Presentation is unavailable."}`. These errors have no attachment
header or ETag. Invalid identifiers return the existing safe **422** response.
The route uses the API-wide submission lock and idle check; an active operation
can also yield the existing busy **409** response.

The existing Markdown download is unchanged:
`GET /api/{mode}/conversations/{thread}/turns/{turn}/artifact`, with
`text/markdown`, `attachment; filename="report.md"` and `nosniff`.

`create_app()` defaults to `.mini-deerflow/demo` for offline and
`.mini-deerflow/live` for live. With `create_app(storage_root=root)`, offline uses
`root` and live uses `root/live`. The optional `presentation_stores` mapping is
an **internal dependency override**, checked for supported keys and matching
store modes. Trusted composition must align each store with its service ledger.
Neither this override nor the root is caller-controlled HTTP input.

## Atomic publication and crash recovery

1. Validate bytes/spec and check eligible ledger ownership in the SQLite write
   transaction.
2. Create an exclusive same-directory `.pending-{finalfilename}-{uuid}` staging
   file (`uuid` is 32 lowercase hexadecimal characters), write all bytes, flush
   and `fsync` the file.
3. Use a **same-volume hardlink** to install the final name exclusively, without
   replacing any existing destination; remove the staging name.
4. Read and validate persisted bytes, then insert and commit artifact metadata.

The file comes first, metadata second. This is **not a two-resource transaction**
between SQLite and the filesystem. There is no directory-`fsync` power-loss
durability guarantee. The filesystem must support same-volume hardlinks.

A crash after final installation but before metadata commit can leave a complete
orphan. A publish retry can adopt it only when its bounded regular-file contents
are **exactly the supplied bytes**, not merely a matching spec. Rerendering to
different ZIP bytes does not recover that orphan.

If the crash occurred after linking but before staging unlink, recovery accounts
for every extra link: each must be a same-directory staging name for that exact
final filename, with the protocol's 32-lowercase-hex suffix and matching device/
inode. The final file's link count must equal one plus the accounted aliases.
Only those aliases are removed, then a strict read rechecks exact bytes. Unrelated,
outside-directory or otherwise unaccounted aliases are rejected without deleting
them. Independent files with plausible staging names are preserved.

This exception applies only to an **uncommitted orphan**. Normal readers require
an unaliased regular file (`st_nlink == 1`), and committed-record retries use that
same strict verification, even for protocol-shaped staging aliases.

Orphans and pending files can accumulate after interruptions or failed recovery.
There is no automatic garbage collection or arbitrary-file deletion. Narrow
staging cleanup during successful recovery is the exception. Deleting a turn
cascades metadata but does not delete its file. Operators must reconcile retained
files and ledger ownership before any manual cleanup; no general cleanup tool or
retention policy is provided.

## Binary bounds and local trust boundary

Presentations must be nonempty `bytes` of at most **10,000,000 bytes**. A store can
lower `max_bytes`; it cannot raise it above the validator limit. Binary workspace
I/O enforces configured read/write bounds, regular files, confinement and
exclusive destination creation.

Binary paths reject absolute/drive-relative/UNC paths, traversal, backslash string
paths, alternate streams, Windows device names, invalid characters and trailing
dot/space aliases on every platform. Native relative `Path` composition remains
supported. Symlink/junction descendants and sandbox ancestors are checked;
ordinary binary reads reject hardlinks. Existing internal extended-length Windows
I/O supports long descendants without accepting caller-supplied extended paths.
Storage roots and SQLite paths must remain host-accessible.

The root and its parent directories are trusted local configuration. These checks
do not provide an OS sandbox guarantee against a hostile local process mutating
parents concurrently. There are no uploads, authentication, authenticated resource
ownership or production tenancy. Task 2.2 adds no renderer rewrite, runtime graph
rewrite, automatic slide-generation pipeline or React/Streamlit UI rewrite.

## Credential-free demo

From a provisioned checkout, use this fresh reproduction directory (choose another
unused directory if it already contains demo outputs):

```powershell
uv run --offline python -m mini_deerflow.demo.phase2_artifacts_offline --storage-root .mini-deerflow/phase2-artifacts-review-evidence
```

The demo refuses an already-used output root. It creates both mode-local ledgers
using **SCRIPTED OFFLINE runtime execution, including the `live` namespace**.
Both preparation and verification inject `OfflineDemoBackend`; neither requires
credentials or demonstrates live-provider behavior.

The real renderer produces eight-slide decks using the task 2.1 fixture: light
theme for offline, academic for live. Offline has two turns; live has one. Each
mode publishes `turn-1` and retries the same spec. A fresh child Python interpreter
constructs fresh services/stores/API, checks HTTP bytes against stored bytes and
the digest, validates all eight layouts against the persisted spec, checks turn/
conversation isolation, verifies the existing Markdown endpoint and confirms
process-local jobs were not restored. This uses FastAPI `TestClient`, not a
separately listening HTTP server.

JSON output includes both mode records and validation reports, the offline
`source_file` path and `downloaded_file`. The offline HTTP body is written to
`<storage-root>/downloaded.pptx`. Printed local paths are trusted operator evidence,
not fields exposed by the public download/error contract. ZIP timestamps can
change new-run digests; compare each run with its own persisted record.

## Final evidence and verification

**PASS — final automated local acceptance, 2026-10-24.** Security reviewer verdict:
**APPROVE**, with no open HIGH/MEDIUM findings. The final integration demo ran with:

```powershell
uv run --offline python -m mini_deerflow.demo.phase2_artifacts_offline --storage-root .mini-deerflow/phase22-final-evidence
```

The downloaded offline deck is
`.mini-deerflow/phase22-final-evidence/downloaded.pptx`; the demo also printed its
owner-addressed source path. The final persisted/HTTP byte measurements were:

| Mode | Bytes | SHA-256 |
| --- | ---: | --- |
| Offline | 46,136 | `99716df9602ce667ac376e3a5069923e36f79536db14f3c6eea9e808cebc5966` |
| Live namespace (scripted offline) | 46,133 | `b8327c8c6734b7d270b49995f10d913b1bea909cfea88d0b7c6f1a391d224f47` |

Both decks had 8 slides / 8 layouts, 33 text objects, 14 shapes, 1 native chart,
1 native table and 49 geometry checks. The offline ledger contained 2 turns and
1 artifact; the live namespace contained 1 turn and 1 artifact. No pending staging
files remained. All six demo flags were true: `valid`, `fresh_process`,
`download_matches_storage`, `turn_isolation`, `mode_isolation` and
`markdown_preserved`.

### Final verification record

| Check | Command | Final result |
| --- | --- | --- |
| Focused artifact and recovery suites | `uv run --offline python -m pytest tests/test_pptx_artifact_recovery.py tests/test_pptx_artifacts.py -q -rs --basetemp .test-tmp/phase22-focused` | PASS — 114 passed, 5 skipped |
| Full Python regression including recovery file | `uv run --offline python -m pytest -q -rs --basetemp .test-tmp/phase22-final` | PASS — 1,154 passed, 11 skipped, 223 subtests |
| Ruff lint | `uv run --offline ruff check --no-cache .` | PASS |
| Ruff formatting | `uv run --offline ruff format --check .` | PASS — 136 files |
| Offline lock validation | `uv lock --check --offline` | PASS — 99 packages |
| Deterministic evaluator | `uv run --offline python evals/run_evals.py --dataset evals/dataset.json` | PASS — 9/9 cases, 36/36 invariants |
| Phase 1 offline demo | `uv run --offline python -m mini_deerflow.demo.phase1_offline` | PASS — all invariants true |
| Standalone PPTX core demo | `uv run --offline python -m mini_deerflow.demo.phase2_pptx_offline --output-dir .mini-deerflow/phase22-core-evidence` | PASS — 8 slides |
| Integration demo / fresh child verification | Final evidence command above | PASS — all six flags true |
| Frontend typecheck / build / lint | `npm run typecheck`, `npm run build`, `npm run lint` (in `frontend/`) | PASS — zero lint warnings |
| Frontend tests | `npm test` (in `frontend/`) | PASS — 20 tests in 4 files |
| Security review | Final reviewer assessment | APPROVE — no open HIGH/MEDIUM findings |
| Issue #10 / task 2.2 automated local acceptance | Final verification and review | PASS |
| Manual PowerPoint/LibreOffice visual inspection | Actual viewer inspection | Unverified — inherited task 2.1 limitation |

The full suite's 11 skips comprise 9 Windows symlink-privilege cases, 1 native-API
case requiring Python 3.13+, and 1 POSIX-semantics case. Junction and hardlink
tests ran. The new subprocess `os._exit` post-link crash and transient staging-
unlink recovery regressions passed. The credential-free demo regression passed
with `.env` absent and credential environment keys removed. Final workspace
formatting corrections were non-semantic.

Verification was performed on the pre-commit working tree based on HEAD
`6a3cab…`; the final PR supplies the resulting commit SHA. These results establish
the tested local contracts, not live-provider quality, visual acceptance or
production tenancy. Fresh reproduction runs require unused output directories
and may produce different ZIP digests.

PM sync-back: `docs/plans` contains the earlier budget-aware synthesis and
session-scoped sandbox plans; no applicable task 2.2 plan was found. No unrelated
plan status is changed by this guide.
