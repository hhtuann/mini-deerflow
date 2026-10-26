"""Offline-only artifact inventory and conservative stale-staging cleanup.

Never runs at server startup, in jobs, or automatically in CI. Defaults to a
read-only dry run. Committed PPTX, orphan finals, live/resumable turns, unknown
names, links and ambiguous files are never deletion candidates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from mini_deerflow.pptx.artifacts import MAX_SPEC_BYTES, PptxArtifact
from mini_deerflow.pptx.validator import MAX_FILE_BYTES
from mini_deerflow.sandbox import SessionSandboxResolver
from mini_deerflow.workspace import _filesystem_path

MIN_RETENTION_DAYS = 30
MAX_SCAN_TURNS = 100_000
MAX_SCAN_FILES = 100_000
STAGING = re.compile(r"\.pending-([0-9a-f]{64})\.pptx-[0-9a-f]{32}\Z")
FINAL = re.compile(r"[0-9a-f]{64}\.pptx\Z")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _trusted_directory(path: Path, root: Path) -> bool:
    """Reject a moved/link ancestor without creating paths on disk."""
    if not path.is_relative_to(root):
        return False
    cursor = root
    if _is_link(cursor) or not cursor.is_dir():
        return False
    for part in path.relative_to(root).parts:
        cursor = cursor / part
        if _is_link(cursor) or not cursor.is_dir():
            return False
    return True


def _owner_directory(root: Path, mode: str, thread: str, turn: str) -> Path:
    session = SessionSandboxResolver(root / "workspaces").resolve(thread)
    identity = json.dumps((mode, thread, turn), separators=(",", ":"))
    key = hashlib.sha256(identity.encode()).hexdigest()
    return session / "presentations" / key


def _read_committed(path: Path, root: Path, record: PptxArtifact) -> bool:
    if not _trusted_directory(path.parent, root) or _is_link(path):
        return False
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(_filesystem_path(path), flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size != record.byte_size
                or info.st_size > MAX_FILE_BYTES
            ):
                return False
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        return False
    return (
        len(data) == record.byte_size
        and hashlib.sha256(data).hexdigest() == record.sha256
        and len(record.spec_json.encode()) <= MAX_SPEC_BYTES
        and hashlib.sha256(record.spec_json.encode()).hexdigest() == record.spec_sha256
    )


def _open_ledger(
    root: Path, mode: str
) -> tuple[list[tuple[str, str, str]], list[PptxArtifact]]:
    database = root / "checkpoints.sqlite"
    if not database.is_file() or _is_link(database):
        raise ValueError("ledger_unavailable")
    # SQLite URI mode=ro: no migrations, schema creation or accidental new DB.
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("ledger_integrity_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("ledger_foreign_keys_failed")
        marker = connection.execute(
            "SELECT mode FROM pptx_artifact_mode WHERE singleton=1"
        ).fetchone()
        if marker is None or marker["mode"] != mode:
            raise ValueError("mode_binding_mismatch")
        turns = connection.execute(
            "SELECT thread_id, turn_id, status FROM turns ORDER BY thread_id, turn_id"
        ).fetchmany(MAX_SCAN_TURNS + 1)
        if len(turns) > MAX_SCAN_TURNS:
            raise ValueError("scan_turn_limit_exceeded")
        rows = connection.execute(
            "SELECT * FROM pptx_artifacts WHERE mode=?", (mode,)
        ).fetchall()
        if len(rows) > MAX_SCAN_TURNS:
            raise ValueError("scan_artifact_limit_exceeded")
        return (
            [(x["thread_id"], x["turn_id"], x["status"]) for x in turns],
            [PptxArtifact.model_validate(dict(row)) for row in rows],
        )


def _stage_info(
    path: Path,
    root: Path,
    *,
    turn_status: str,
    has_ledger_artifact: bool,
    now_ns: int,
    retention_days: int,
) -> dict[str, object]:
    """One already ledger-owned directory. Invalid files are NEVER unlinked."""
    relative = path.relative_to(root).as_posix()
    result: dict[str, object] = {
        "relative_path": relative,
        "status": "PROTECTED_UNKNOWN",
        "age_days": None,
        "byte_size": None,
    }
    match = STAGING.fullmatch(path.name)
    if match is None:
        return result
    try:
        # Staging names push the path past MAX_PATH on LongPathsEnabled=0
        # hosts; always stat through the extended-path form (idempotent),
        # matching _read_committed/final-exists handling below.
        safe_path = _filesystem_path(path)
        if _is_link(safe_path):
            result["status"] = "PROTECTED_LINK"
            return result
        info = safe_path.stat(follow_symlinks=False)
    except OSError:
        return result
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        result["status"] = "PROTECTED_NONREGULAR_OR_LINKED"
        return result
    if not 0 <= info.st_size <= MAX_FILE_BYTES:
        result["status"] = "PROTECTED_SIZE"
        return result
    age = (now_ns - info.st_mtime_ns) / (86_400 * 1_000_000_000)
    result["age_days"] = max(0, round(age, 1))
    result["byte_size"] = info.st_size
    if os.path.lexists(_filesystem_path(path.with_name(f"{match[1]}.pptx"))):
        result["status"] = "PROTECTED_FINAL_EXISTS"
    elif has_ledger_artifact:
        result["status"] = "PROTECTED_LEDGER"
    elif turn_status not in {"completed", "failed"}:
        result["status"] = "PROTECTED_RESUMABLE_TURN"
    elif age < retention_days:
        result["status"] = "PROTECTED_RETENTION"
    else:
        result["status"] = "ELIGIBLE_STALE_STAGING"
        # Never expose a token/path from outside an owned directory.
        result["_fingerprint"] = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        )
        result["_parent_fingerprint"] = (
            path.parent.stat().st_dev,
            path.parent.stat().st_ino,
        )
    return result


def _unlink_exact_staging(
    path: Path,
    *,
    fingerprint: tuple[int, int, int, int],
    parent_fingerprint: tuple[int, int],
) -> None:
    """Best-effort identity recheck in a trusted OFFLINE, single-writer directory."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("cleanup_apply_requires_posix_nofollow")
    match = STAGING.fullmatch(path.name)
    if match is None:
        raise RuntimeError("invalid_staging_name")
    directory = os.open(
        _filesystem_path(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        parent = os.fstat(directory)
        if (parent.st_dev, parent.st_ino) != parent_fingerprint:
            raise RuntimeError("staging_parent_changed")
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        file_fd = os.open(path.name, flags, dir_fd=directory)
        try:
            info = os.fstat(file_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                != fingerprint
            ):
                raise RuntimeError("staging_identity_changed")
            candidate = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if (candidate.st_dev, candidate.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError("staging_name_changed")
            try:
                os.stat(f"{match[1]}.pptx", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("final_appeared_since_dry_run")
            os.unlink(path.name, dir_fd=directory)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory)


def inspect(
    storage_root: str | Path,
    mode: str,
    *,
    retention_days: int = MIN_RETENTION_DAYS,
    apply: bool = False,
    confirms_offline: bool = False,
    backup_verified: bool = False,
    now_ns: int | None = None,
) -> dict[str, object]:
    """Inventory first; cleanup only with explicit offline+backup consent."""
    if mode not in {"offline", "live"}:
        raise ValueError("mode must be offline or live")
    if not MIN_RETENTION_DAYS <= retention_days <= 3650:
        raise ValueError("retention_days must be between 30 and 3650")
    if apply and not (confirms_offline and backup_verified):
        raise ValueError("apply requires explicit offline and backup confirmation")
    if apply and (os.name != "posix" or not hasattr(os, "O_NOFOLLOW")):
        raise ValueError("cleanup apply unsupported on this platform")
    root = Path(storage_root).expanduser().absolute()
    if _is_link(root) or not root.is_dir():
        raise ValueError("storage_root_not_regular_directory")
    resolved = root.resolve(strict=True)
    if root != resolved:
        raise ValueError("storage_root_has_link_ancestor")
    turns, records = _open_ledger(root, mode)
    current_ns = time.time_ns() if now_ns is None else now_ns
    record_map = {(r.thread_id, r.turn_id): r for r in records}
    directories: dict[Path, tuple[str, bool]] = {}
    for thread, turn, status in turns:
        path = _owner_directory(root, mode, thread, turn)
        directories[path] = (status, (thread, turn) in record_map)
    expected_finals = {
        _owner_directory(
            root, mode, record.thread_id, record.turn_id
        ): f"{record.sha256}.pptx"
        for record in records
    }
    integrity = Counter()
    for record in records:
        folder = _owner_directory(root, mode, record.thread_id, record.turn_id)
        path = folder / f"{record.sha256}.pptx"
        integrity[
            "VERIFIED" if _read_committed(path, root, record) else "MISSING_OR_CORRUPT"
        ] += 1

    candidates: list[dict[str, object]] = []
    unknown_finals = 0
    scanned_files = 0
    for folder, (status, has_record) in sorted(
        directories.items(), key=lambda pair: str(pair[0])
    ):
        if not folder.exists():
            continue
        if not _trusted_directory(folder, root):
            raise ValueError("owned_directory_link_or_tamper")
        for path in sorted(folder.iterdir()):
            scanned_files += 1
            if scanned_files > MAX_SCAN_FILES:
                raise ValueError("scan_file_limit_exceeded")
            if STAGING.fullmatch(path.name):
                candidates.append(
                    _stage_info(
                        path,
                        root,
                        turn_status=status,
                        has_ledger_artifact=has_record,
                        now_ns=current_ns,
                        retention_days=retention_days,
                    )
                )
            elif FINAL.fullmatch(path.name) and path.name != expected_finals.get(
                folder
            ):
                unknown_finals += 1
    results: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "retention_days": retention_days,
        "operation": "APPLY" if apply else "DRY_RUN",
        "ledger_turn_count": len(turns),
        "committed_artifact_count": len(records),
        "verified_committed": integrity["VERIFIED"],
        "missing_or_corrupt_committed": integrity["MISSING_OR_CORRUPT"],
        "protected_orphan_final_count": unknown_finals,
        "staging": [
            {k: v for k, v in item.items() if not k.startswith("_")}
            for item in candidates
        ],
        "eligible_count": sum(
            item["status"] == "ELIGIBLE_STALE_STAGING" for item in candidates
        ),
        "deleted_count": 0,
        "status": "DRY_RUN",
    }
    if integrity["MISSING_OR_CORRUPT"]:
        results["status"] = "BLOCKED_COMMITTED_INTEGRITY"
        return results
    if apply:
        for item in candidates:
            if item["status"] != "ELIGIBLE_STALE_STAGING":
                continue
            path = root / str(item["relative_path"])
            if not _trusted_directory(path.parent, root):
                results["status"] = "STOPPED_DIRECTORY_CHANGED"
                break
            try:
                _unlink_exact_staging(
                    path,
                    fingerprint=item["_fingerprint"],
                    parent_fingerprint=item["_parent_fingerprint"],
                )
            except (OSError, RuntimeError):
                results["status"] = "STOPPED_STAGING_CHANGED"
                break
            results["deleted_count"] += 1
        else:
            results["status"] = "APPLIED"
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("offline", "live"), required=True)
    parser.add_argument("--retention-days", type=int, default=MIN_RETENTION_DAYS)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-all-workers-stopped", action="store_true")
    parser.add_argument("--confirm-backup-verified", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = inspect(
            args.storage_root,
            args.mode,
            retention_days=args.retention_days,
            apply=args.apply,
            confirms_offline=args.confirm_all_workers_stopped,
            backup_verified=args.confirm_backup_verified,
        )
    except (ValueError, OSError, sqlite3.Error):
        print(
            json.dumps({"status": "BLOCKED", "reason": "untrusted_storage_or_policy"})
        )
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] in {"DRY_RUN", "APPLIED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
