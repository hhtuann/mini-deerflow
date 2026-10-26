"""Phase 3.3: real SQLite + filesystem fault-injection and safe ops CLI.

No simulated successful publications, no cleanup on normal API startup, no
provider/model calls. Destructive tests operate ONLY on pytest tmp_path.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from mini_deerflow.conversation import SQLiteConversationRepository
from mini_deerflow.demo.phase2_pptx_offline import demo_spec
from mini_deerflow.pptx import render_deck
from mini_deerflow.pptx.artifacts import PptxArtifactStore
from scripts import artifact_operations as ops

NOW = 2_000_000_000_000_000_000
OLD = NOW - 50 * 86_400 * 1_000_000_000
RECENT = NOW - 2 * 86_400 * 1_000_000_000
THREAD = "ops-thread"


async def initialize_artifact_mode(root: Path) -> None:
    store = PptxArtifactStore(root, mode="offline")
    async with store._connection() as connection:
        await store._schema(connection)


async def seed(root: Path, turn: str, *, complete: bool = True) -> None:
    repo = SQLiteConversationRepository(root / "checkpoints.sqlite")
    if turn == "first":
        await repo.create_conversation_with_turn(
            thread_id=THREAD,
            turn_id=turn,
            user_message="Recovery test with a persistent local thread.",
            session_tool_call_limit=20,
            reserved_tool_calls=5,
        )
    else:
        await repo.reserve_follow_up(
            thread_id=THREAD,
            turn_id=turn,
            user_message="Another safe local step for a separate turn.",
            reserved_tool_calls=5,
        )
    if complete:
        await repo.complete_turn(
            thread_id=THREAD,
            turn_id=turn,
            assistant_response="Completed in deterministic test.",
            total_tool_calls=0,
        )


def owned(root: Path, turn: str) -> Path:
    directory = ops._owner_directory(root, "offline", THREAD, turn)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def stage(root: Path, turn: str, *, age_ns: int = OLD, fill=b"stale") -> Path:
    folder = owned(root, turn)
    filename = f".pending-{'a' * 64}.pptx-{'b' * 32}"
    path = folder / filename
    path.write_bytes(fill)
    os.utime(path, ns=(age_ns, age_ns))
    return path


def inspect(root: Path, **kwargs):
    return ops.inspect(root, "offline", now_ns=NOW, **kwargs)


@pytest.fixture
def ledger(tmp_path):
    asyncio.run(seed(tmp_path, "first"))
    asyncio.run(seed(tmp_path, "second"))
    asyncio.run(initialize_artifact_mode(tmp_path))
    return tmp_path


def test_dry_run_no_mutation_on_stale_file(ledger):
    pending = stage(ledger, "second")
    before = pending.stat()
    out = inspect(ledger)
    assert out["status"] == "DRY_RUN"
    assert out["operation"] == "DRY_RUN"
    assert out["ledger_turn_count"] == 2
    assert out["committed_artifact_count"] == 0
    assert out["eligible_count"] == 1
    assert out["deleted_count"] == 0
    assert out["staging"][0]["status"] == "ELIGIBLE_STALE_STAGING"
    assert out["staging"][0]["relative_path"].startswith("workspaces/sessions/v1-")
    assert pending.read_bytes() == b"stale"
    assert pending.stat().st_mtime_ns == before.st_mtime_ns


def test_running_turn_never_marked_safe_for_cleanup(tmp_path):
    asyncio.run(seed(tmp_path, "first", complete=False))
    asyncio.run(initialize_artifact_mode(tmp_path))
    pending = stage(tmp_path, "first")
    report = inspect(tmp_path)
    assert report["eligible_count"] == 0
    assert report["staging"][0]["status"] == "PROTECTED_RESUMABLE_TURN"
    assert pending.exists()
    # New repository instance preserves a running turn after restart: do NOT
    # infer the process-local job succeeded, or auto-replay the POST.
    saved = asyncio.run(
        SQLiteConversationRepository(tmp_path / "checkpoints.sqlite").get_turn(
            THREAD, "first"
        )
    )
    assert saved.status == "running"


def test_postlink_orphan_and_hardlinks_never_deleted(ledger):
    pending = stage(ledger, "second")
    final = pending.with_name(f"{'a' * 64}.pptx")
    os.link(pending, final)
    report = inspect(ledger)
    assert report["eligible_count"] == 0
    assert report["staging"][0]["status"] == "PROTECTED_NONREGULAR_OR_LINKED"
    assert final.read_bytes() == pending.read_bytes() == b"stale"
    assert report["protected_orphan_final_count"] == 1


def test_final_file_alone_always_protected_even_without_metadata(ledger):
    folder = owned(ledger, "second")
    orphan = folder / ("e" * 64 + ".pptx")
    orphan.write_bytes(b"crash remnant; must never delete")
    report = inspect(ledger, apply=False)
    assert report["protected_orphan_final_count"] == 1
    assert orphan.read_bytes().startswith(b"crash remnant")


def test_symlink_staging_never_deleted_and_external_bytes_safe(ledger, tmp_path):
    other = tmp_path / "external.txt"
    other.write_bytes(b"DO-NOT-DELETE")
    candidate = owned(ledger, "second") / (
        f".pending-{'a' * 64}.pptx-{'d' * 32}"
    )
    try:
        candidate.symlink_to(other)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    report = inspect(ledger)
    assert report["eligible_count"] == 0
    assert report["staging"][0]["status"] == "PROTECTED_LINK"
    assert other.read_bytes() == b"DO-NOT-DELETE"


def test_bad_mode_missing_db_and_mode_binding_fail_closed(tmp_path, ledger):
    (tmp_path / "missing").mkdir()
    with pytest.raises(ValueError, match="ledger_unavailable"):
        inspect(tmp_path / "missing")
    with pytest.raises(ValueError, match="mode_binding_mismatch"):
        ops.inspect(ledger, "live")
    with pytest.raises(ValueError, match="mode must"):
        ops.inspect(ledger, "invalid")
    with pytest.raises(ValueError, match="retention_days"):
        inspect(ledger, retention_days=29)
    with pytest.raises(ValueError, match="explicit offline"):
        inspect(ledger, apply=True)


def test_young_stage_is_protected_by_retention(ledger):
    pending = stage(ledger, "second", age_ns=RECENT)
    report = inspect(ledger)
    assert report["eligible_count"] == 0
    assert report["staging"][0]["status"] == "PROTECTED_RETENTION"
    assert pending.exists()


def test_exact_committed_content_audited_but_never_deleted(ledger):
    spec = demo_spec()
    data = render_deck(spec)
    store = PptxArtifactStore(ledger, mode="offline")
    record = asyncio.run(store.publish(THREAD, "first", data, spec))
    report = inspect(ledger, apply=False)
    assert report["verified_committed"] == 1
    assert report["missing_or_corrupt_committed"] == 0
    directory = owned(ledger, "first")
    published = directory / f"{record.sha256}.pptx"
    assert published.read_bytes() == data
    assert record.sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.skipif(os.name != "posix", reason="offline safe unlink requires POSIX dirfd")
def test_apply_requires_both_operator_confirmations_and_preserves_other_files(ledger):
    pending = stage(ledger, "second")
    folder = owned(ledger, "second")
    unknown = folder / "README.md"
    unknown.write_text("never touch", encoding="utf-8")
    young = folder / f".pending-{'c' * 64}.pptx-{'e' * 32}"
    young.write_bytes(b"recent")
    os.utime(young, ns=(RECENT, RECENT))
    with pytest.raises(ValueError, match="explicit offline"):
        inspect(ledger, apply=True, confirms_offline=True)
    report = inspect(
        ledger, apply=True, confirms_offline=True, backup_verified=True
    )
    assert report["status"] == "APPLIED"
    assert report["eligible_count"] == report["deleted_count"] == 1
    assert not pending.exists()
    assert unknown.read_text(encoding="utf-8") == "never touch"
    assert young.read_bytes() == b"recent"


@pytest.mark.skipif(os.name != "posix", reason="POSIX dirfd identity check")
def test_stale_stage_swap_detected_before_unlink(ledger):
    pending = stage(ledger, "second")
    observation = pending.stat()
    fingerprint = (
        observation.st_dev, observation.st_ino,
        observation.st_size, observation.st_mtime_ns,
    )
    parent = pending.parent.stat()
    parent_fingerprint = (parent.st_dev, parent.st_ino)
    pending.write_bytes(b"changed!")
    with pytest.raises(RuntimeError, match="staging_identity_changed"):
        ops._unlink_exact_staging(
            pending, fingerprint=fingerprint, parent_fingerprint=parent_fingerprint
        )
    assert pending.read_bytes() == b"changed!"


@pytest.mark.skipif(os.name != "posix", reason="POSIX apply integration")
def test_corrupt_committed_artifact_blocks_even_safe_staging_cleanup(ledger):
    spec = demo_spec()
    data = render_deck(spec)
    record = asyncio.run(
        PptxArtifactStore(ledger, mode="offline").publish(
            THREAD, "first", data, spec
        )
    )
    pending = stage(ledger, "second")
    path = owned(ledger, "first") / f"{record.sha256}.pptx"
    path.write_bytes(b"tampered")
    report = inspect(
        ledger, apply=True, confirms_offline=True, backup_verified=True
    )
    assert report["status"] == "BLOCKED_COMMITTED_INTEGRITY"
    assert report["missing_or_corrupt_committed"] == 1
    assert report["deleted_count"] == 0
    assert pending.exists()


def test_cleanup_does_not_probe_unowned_sandbox(ledger):
    stranger = ledger / "workspaces" / "sessions" / ("v1-" + "f" * 64)
    location = stranger / "presentations" / ("a" * 64)
    location.mkdir(parents=True)
    sentinel = location / f".pending-{'a' * 64}.pptx-{'b' * 32}"
    sentinel.write_bytes(b"outside-ledger")
    os.utime(sentinel, ns=(OLD, OLD))
    report = inspect(ledger)
    assert report["eligible_count"] == 0
    assert sentinel.read_bytes() == b"outside-ledger"


def test_successful_committed_artifact_never_deleted_if_staging_alias_exists(ledger):
    spec = demo_spec()
    data = render_deck(spec)
    record = asyncio.run(
        PptxArtifactStore(ledger, mode="offline").publish(THREAD, "first", data, spec)
    )
    directory = owned(ledger, "first")
    existing = directory / f"{record.sha256}.pptx"
    pending = directory / f".pending-{record.sha256}.pptx-{'a' * 32}"
    os.link(existing, pending)
    result = inspect(ledger)
    assert result["missing_or_corrupt_committed"] == 1  # hardlink is refused
    assert result["status"] == "BLOCKED_COMMITTED_INTEGRITY"
    assert pending.read_bytes() == existing.read_bytes()


def test_artifact_ops_main_fails_closed_without_creating_db(tmp_path, capsys):
    from scripts.artifact_operations import main

    root = tmp_path / "missing"
    code = main(["--storage-root", str(root), "--mode", "offline"])
    assert code == 2
    assert not root.exists()
    message = json.loads(capsys.readouterr().out)
    assert message == {"status": "BLOCKED", "reason": "untrusted_storage_or_policy"}


def test_duplicate_turn_key_survives_restart_without_duplicate_work(ledger):
    from mini_deerflow.conversation import ActiveTurnError

    repo = SQLiteConversationRepository(ledger / "checkpoints.sqlite")

    async def replay():
        existing, created = await repo.reserve_follow_up(
            thread_id=THREAD,
            turn_id="second",
            user_message="Another safe local step for a separate turn.",
            reserved_tool_calls=5,
        )
        assert not created and existing.turn_id == "second"
        assert len(await repo.list_turns(THREAD)) == 2
        with pytest.raises(ActiveTurnError):
            # This request is a NEW turn. The ledger forbids competing work
            # while another turn is awaiting resume.
            await repo.reserve_follow_up(
                thread_id=THREAD,
                turn_id="third",
                user_message="Another potential operation after uncertain POST.",
                reserved_tool_calls=5,
            )

    # Need an interrupted/running turn to exercise no-competing-work.
    # The initial ledger fixture contains completed turns; mark the second
    # turn interrupted only in this isolated test's transaction.
    with sqlite3.connect(ledger / "checkpoints.sqlite") as connection:
        connection.execute(
            "UPDATE turns SET status='interrupted' WHERE thread_id=? AND turn_id=?",
            (THREAD, "second"),
        )
        connection.commit()
    asyncio.run(replay())
