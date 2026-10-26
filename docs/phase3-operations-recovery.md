# Phase 3 · Task 3.3 — Single-worker job / artifact operations & recovery

Issue [#20](https://github.com/hhtuann/mini-deerflow/issues/20) · [Phase 3 roadmap #16](https://github.com/hhtuann/mini-deerflow/issues/16)

## Boundaries and verdict

This remains a **single-process, one-job-at-a-time local API**. Jobs and their historical HTTP job IDs are held in memory, while research turns/checkpoints and PPTX publications are SQLite/filesystem persisted. There is **no distributed queue**, no persisted HTTP job journal, no remote cancellation, and no automatic replay of an ambiguous POST after restart. The new operator tool adds a narrowly scoped, **read-only-by-default** artifact inventory with strictly gated cleanup of aged, detached, ledger-owned staging files. It does not run as an HTTP endpoint, API startup hook, background task or CI cleanup.

Task 3.3's automated acceptance tests are **not proof of real power-loss durability**. Remaining human/operational gates (controlled restart during a live in-flight provider call, backup restore trial, filesystem-specific crash durability) must be recorded separately if required for deployment readiness.

## Deterministic crash/failure matrix

| Failure / ambiguity | Existing or new test coverage | Required behavior |
| --- | --- | --- |
| Worker fails before turn checkpoint | Existing job/turn tests + restart tests | Job fails; no fabricated completed turn |
| HTTP POST response lost after accepted job | Conversation repository idempotent turn reservation and active-turn test | Do NOT auto-resubmit a new turn; inspect ledger, resume existing turn when eligible |
| Process restart / old `job_id` | API `ApiJobs` is process-local, conversation ledger persistent | Old job ID may return 404; ledger turn state, not old job ID, drives recovery |
| API shutdown while job running | Single `DemoJobManager.shutdown(wait=True)` | Graceful shutdown *waits* for worker; not a hard cancellation deadline |
| Forced process kill during provider request | Checkpoint/ledger may persist; network billing uncertain | No automatic replay; use explicit resume after inspecting turn |
| Fail before temporary PPTX fsync | Exclusive staging write; no final publication | Never report published success; old detached staging is potentially cleanable after retention |
| Fail after staged file fsync, before final hardlink | Unlinked `.pending-*` may remain | Only aged unlinked regular staging may be cleanup eligible (and only for terminal owning turn) |
| **Fail after hardlink, before SQLite insert** | Existing subprocess `os._exit` fault-injection in `test_pptx_artifact_recovery.py` | Exact original bytes adopted on idempotent retry; staging/final hardlinks never cleaned by maintenance |
| Commit fails after complete file exists | Existing real SQLite/FS retry tests | Pre-metadata orphan final **preserved**; retry verifies exact bytes, never overwrites |
| Commit succeeds, HTTP ACK lost | Artifact store's immutable canonical spec retry tests | Same spec yields original verified publication; different spec conflicts; no duplicate deck |
| Corrupt/missing committed file | `test_artifact_operations.py`, existing retrieval integrity tests | Report `BLOCKED_COMMITTED_INTEGRITY`, **zero cleanup**; never serve unverified bytes |
| Staging symlink, hardlink, unknown folder or forged name | Explicit adversarial maintenance and workspace tests | **Never eligible**; do not unlink or traverse foreign namespace |
| In-flight/resumable turn's staging file | Explicit status-based maintenance test | Never clean, regardless of age |
| Worker exceeds timeout | Live E2E supervised subprocess, no provider cancellation guarantee | Process kill limits local worker, not remote billable request; avoid automatic retry |

Earlier Phase 2 artifact recovery tests already inject crash exactly after hardlink and confirm adoption of the **original** inode, bytes and transaction. The new Phase 3 tests verify the *other side* of this boundary: safe inventory, conservative retention, and no deletion of recoverable or owner-unknown files. There is deliberately no sweeping deletion of database rows, checkpoint files, final PPTX, orphan finals, turn data or live provider output.

## Retention policy

**Default: preserve everything.** The operator must first run a dry-run report against the **stopped service's mode-local storage root**. The mode binding (`pptx_artifact_mode` row), SQLite quick/foreign-key checks and committed record hashes must pass before an apply is even considered. The tool never creates SQLite databases, directories, new artifacts or mode bindings.

The only eligible files have **all** these properties:

1. Located under a session and turn represented by the **same mode-bound SQLite `turns` ledger** and exactly derived SHA-256 ownership directory. Unreferenced `workspaces/` trees are **never scanned for deletion**.
2. Exact same-directory protocol pattern `.pending-<64hex>.pptx-<32hex>`.
3. Regular unaliased file: **one hardlink**, no symlink/junction, at most the native PPTX byte limit.
4. Owner turn `completed` or `failed`; no artifact metadata row for that owner (ever).
5. Corresponding final `<64hex>.pptx` file **absent**. Orphan final, published final, post-link crash hardlinks and any unaccounted aliases are preserved.
6. Modification age **at least 30 days** (minimum allowed operator-retention window).
7. Explicit `--apply --confirm-all-workers-stopped --confirm-backup-verified`, on POSIX with `O_NOFOLLOW`, `dir_fd` and before-unlink file/parent identity rechecks. **Windows apply is disabled**; Windows dry-run still works. Directory identity checks reduce mistakes but cannot defend against a hostile concurrent process changing the tree between checks and unlink.

A stored PPTX is never auto-deleted, no matter how old. Final orphan files remain intact for retry/recovery/manual forensics. All operator cleanups need an offline maintenance window and tested backups. The maintenance process may report `STOPPED_STAGING_CHANGED` or `BLOCKED_COMMITTED_INTEGRITY`; both are failure statuses, not a reason to force deletion.

## Operator commands

From the repo root, with Python dependencies installed and **the app stopped**, inspect the offline mode:

```sh
uv run --no-sync python scripts/artifact_operations.py \
  --storage-root .mini-deerflow/demo --mode offline
```

For the live SQLite/storage mode (separate root):

```sh
uv run --no-sync python scripts/artifact_operations.py \
  --storage-root .mini-deerflow/live --mode live
```

The default JSON includes committed count, verified/missing count, protected orphan-final count, and staging entries with relative **hashed ownership paths**, age, byte size and explicit status. It does **not** print source text, raw turns, API keys, SQLite data, or absolute host paths. No mutation occurs on dry-run. If the mode's artifact ledger has never been initialized, report `BLOCKED` rather than create it.

**Applying cleanup is exceptional:** after manually checking dry-run output, confirming the process is OFF, backups have been verified, and there are no new writes:

```sh
uv run --no-sync python scripts/artifact_operations.py \
  --storage-root .mini-deerflow/demo --mode offline \
  --apply --confirm-all-workers-stopped --confirm-backup-verified
```

The above apply is **only supported on POSIX**, not Windows; the tool refuses unsupported platforms. Preview report is not a merge/approval to delete, and absence of an eligible file is normal.

## SQLite/filesystem durability and concurrency

SQLite `BEGIN IMMEDIATE` serializes published metadata; a same-directory fsynced staging file is hardlinked exclusively before metadata commit. SQLite commit and filesystem link are **not one atomic transaction**. Recovery can adopt an exact pre-commit orphan on a safe same-spec retry; partial files and unknown aliases fail closed. A file fsync does not guarantee its directory entries survive an abrupt power loss unless the directory is also persisted; storage/controller write caches, journal mode and filesystem semantics vary. Neither maintenance nor tests claim an atomic fsync of *both* SQLite and directory metadata, end-to-end exactly-once external provider execution, or multi-worker locking.

Deletion uses POSIX no-follow directory/file descriptors and inode checks, but even this cannot provide OS-level isolation against a malicious same-host concurrent process that can change directory entries. **Offline operator-only local trust boundary is mandatory.** No API route, web caller or model/tool should ever select cleanup paths, toggle apply, or control the retention policy.

## Acceptance and remaining work

Run locked Python full pytest, Ruff check/format, evaluator, frontend typecheck/Vitest/build and browser Playwright on the exact PR head. Review maintenance/operational failure tests in `tests/test_artifact_operations.py` together with existing `tests/test_pptx_artifact_recovery.py` and `tests/test_pptx_artifacts.py`. Keep the PR open until independently reviewed and explicitly merged by the owner.

Before claiming **production operations readiness**: conduct a real stopped-server maintenance window/backup restore drill and test actual controlled restart with representative in-flight workloads. These cannot be inferred from green CI, and are distinct from Phase 3.4 deployment/security acceptance.
