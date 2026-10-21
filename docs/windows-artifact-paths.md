# Windows artifact path portability

Artifact names and ownership are unchanged. The first offline turn writes
`reports/day-15-demo.md`; a later turn writes
`reports/day-15-demo-{turn_id}.md`. Each thread retains its existing
`workspaces/sessions/v1-<sha256(normalized thread_id)>` sandbox. Checkpointed
`artifact_path` values and file-tool payloads remain workspace-relative;
conversation and turn identities are unchanged.

## Observed failure

On Python 3.12.14 on Windows with `LongPathsEnabled=0`, a controlled offline run
persisted its first artifact at a 254-character absolute path. Its second-turn
artifact at 266 characters raised `FileNotFoundError` (`errno=2`) in
`Workspace.write_text -> Path.write_bytes -> Path.open -> io.open`.
`ToolRunner` normalized the exception; synthesis recorded `artifact_path=None`.
The report remained available in memory, so a UI report preview alone did not
prove filesystem persistence. The same destination accepted I/O through Windows
extended-length syntax. This reproduction ran outside pytest.

The numbers describe this reproduction, not a universal filesystem limit. See
[Microsoft's Windows path-length documentation](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation).

## Filesystem boundary

`workspace.py` uses private `_filesystem_path` and `_resolved_path` helpers:

- Logical inputs are validated as workspace-relative paths.
- Resolution uses extended-length Windows syntax so long symlinks and junctions
  are followed before containment is checked. Ordinary resolution alone can miss
  a junction beyond the legacy path limit.
- Canonical results return to ordinary drive/UNC syntax for component-based
  containment checks and public `Workspace.root` / `resolve_path()` results.
- Read, write, directory creation, metadata checks, and recursive listing use
  internal extended-length paths. Listing retains that syntax from its walk root
  so deeper descendants remain accessible.
- Writes still resolve and validate again after creating parent directories.
  Listings still exclude symlinks and junctions.

Tool callers cannot supply extended or device namespace paths. On Windows,
drive-relative and rooted paths, alternate data streams, reserved device names,
and trailing-dot/space components are explicitly rejected. This intentionally
tightens ambiguous Windows inputs; ordinary relative artifact names and POSIX
behavior are preserved. Internal prefixes are not persisted as artifact names
or returned by file-tool success payloads. Existing safe failure messages remain.

## Supported contract and limits

Workspace artifact descendants can exceed the legacy Windows path limit without
changing system policy. Storage roots and checkpoint/database paths must still
be accessible to their existing host APIs: root directory creation and SQLite
path handling are not extended by this fix. Filesystem component limits, total
path limits, permissions, and available storage still apply. This is not a
guarantee for arbitrary-length storage roots or filenames.

External programs opening ordinary absolute paths may still encounter their
own path limits. UNC prefix construction is unit-tested without accessing a
network; network-share operation has not been verified. Existing revalidation
reduces filesystem replacement races but does not make the sandbox race-proof.
The fix changes no graph, execution-event, delegation, budget, or resume semantics.

## Regression verification

`tests/test_artifact_paths.py` exercises:

- First/second-turn persistence at short and controlled 254/266-character paths.
- Four distinct report files across two turns and two threads, exact persisted
  report contents, reopen, and completed resume without replayed research.
- Long nested artifact creation, reads, and relative listings.
- Windows namespace and ambiguous-name rejection.
- Real short/long directory junction escapes, sandbox-component junction
  rejection, and revalidation after parent creation.

Windows-specific cases are skipped on other platforms. Existing symlink tests
remain unchanged and report a skip when the host lacks symlink privileges.
Run these together with `test_workspace.py`, `test_sandbox.py`,
`test_file_tools.py`, and the demo/persistence regressions. A short pytest
`--basetemp` allows the boundary test to construct the exact observed lengths;
the test itself deliberately creates the long destination rather than avoiding it.
