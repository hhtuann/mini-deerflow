"""Independent post-link crash recovery and fresh-process demo regressions.

Use real PPTX bytes, SQLite ownership, and filesystem hardlinks. Only the
process exit point or staging unlink is injected; recovery is never mocked.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from mini_deerflow.conversation import SQLiteConversationRepository
from mini_deerflow.demo.phase2_pptx_offline import demo_spec
from mini_deerflow.pptx import DeckSpec, render_deck, validate_pptx
from mini_deerflow.pptx.artifacts import ArtifactIntegrityError, PptxArtifactStore
from mini_deerflow.workspace import Workspace, WorkspacePathError, _filesystem_path

THREAD, TURN = "recovery-thread", "recovery-turn"


@pytest.fixture(scope="module")
def recovery_deck():
    spec = demo_spec()
    data = render_deck(spec)
    validate_pptx(data, spec)
    return spec, data


async def seed_owner(root):
    repo = SQLiteConversationRepository(root / "checkpoints.sqlite")
    await repo.create_conversation_with_turn(
        thread_id=THREAD,
        turn_id=TURN,
        user_message="Exercise interrupted binary publication.",
        session_tool_call_limit=20,
        reserved_tool_calls=5,
    )
    await repo.complete_turn(
        thread_id=THREAD,
        turn_id=TURN,
        assistant_response="Ready for publication.",
        total_tool_calls=0,
    )


def rows(root, query):
    # sqlite3's transaction context alone does not close Windows file handles.
    with closing(sqlite3.connect(root / "checkpoints.sqlite")) as connection:
        return connection.execute(query).fetchall()


def matches(root, pattern):
    # Walk AND access descendants in the extended namespace on Windows.
    return sorted(_filesystem_path(root).rglob(pattern))


def credential_free_env():
    return {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("MINI_DEERFLOW_")
            and not any(
                word in key.upper() for word in ("API_KEY", "TOKEN", "CREDENTIAL")
            )
        },
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def assert_complete_orphan(root, data):
    finals = matches(root, "*.pptx")
    pending = matches(root, ".pending-*")
    assert len(finals) == len(pending) == 1
    final, staging = finals[0], pending[0]
    assert final.read_bytes() == staging.read_bytes() == data
    assert staging.name.startswith(f".pending-{final.name}-")
    suffix = staging.name.removeprefix(f".pending-{final.name}-")
    assert len(suffix) == 32 and all(char in "0123456789abcdef" for char in suffix)
    assert os.path.samefile(final, staging)
    assert final.stat().st_nlink == 2
    assert rows(root, "SELECT count(*) FROM pptx_artifacts") == [(0,)]
    return final, staging


def leave_orphan(root, deck, monkeypatch):
    """Leave the exact post-link state by refusing only pending-name cleanup."""
    asyncio.run(seed_owner(root))
    original = Path.unlink
    attempted = []

    def unavailable(path, *args, **kwargs):
        if path.name.startswith(".pending-"):
            attempted.append(path)
            raise OSError("Injected transient staging cleanup failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unavailable)
        with pytest.raises(ArtifactIntegrityError):
            asyncio.run(
                PptxArtifactStore(root, mode="offline").publish(
                    THREAD, TURN, deck[1], deck[0]
                )
            )
    assert len(attempted) == 1
    return assert_complete_orphan(root, deck[1])


def assert_recovered(root, deck, final):
    before = final.stat()

    async def recover():
        fresh = PptxArtifactStore(root, mode="offline")
        record = await fresh.publish(THREAD, TURN, deck[1], deck[0])
        assert await PptxArtifactStore(root, mode="offline").retrieve(THREAD, TURN) == (
            record,
            deck[1],
        )
        assert await fresh.publish(THREAD, TURN, deck[1], deck[0]) == record
        return record

    record = asyncio.run(recover())
    after = final.stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert before.st_mtime_ns == after.st_mtime_ns
    assert final.read_bytes() == deck[1]
    assert after.st_nlink == 1
    assert matches(root, "*.pptx") == [final]
    assert matches(root, ".pending-*") == []
    assert rows(root, "SELECT count(*) FROM pptx_artifacts") == [(1,)]
    assert record.sha256 == hashlib.sha256(deck[1]).hexdigest()


def test_subprocess_post_link_exit_recovers_exact_bytes_without_replacement(
    tmp_path, recovery_deck
):
    root = tmp_path / "storage"
    asyncio.run(seed_owner(root))
    fixture = _filesystem_path(tmp_path / "input.pptx")
    fixture.write_bytes(recovery_deck[1])
    child = """
import asyncio
import os
import sys
from pathlib import Path
from mini_deerflow.demo.phase2_pptx_offline import demo_spec
from mini_deerflow.pptx.artifacts import PptxArtifactStore
from mini_deerflow.workspace import _filesystem_path

real_link = os.link
def exit_after_link(*args, **kwargs):
    real_link(*args, **kwargs)
    os._exit(73)
os.link = exit_after_link
data = _filesystem_path(Path(sys.argv[2])).read_bytes()
asyncio.run(PptxArtifactStore(sys.argv[1], mode="offline").publish(
    "recovery-thread", "recovery-turn", data, demo_spec()
))
"""
    result = subprocess.run(
        [sys.executable, "-c", child, str(root), str(fixture)],
        cwd=tmp_path,
        env=credential_free_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 73, result.stdout + result.stderr
    final, _ = assert_complete_orphan(root, recovery_deck[1])
    assert_recovered(root, recovery_deck, final)


def test_transient_staging_unlink_failure_recovers_after_normal_io_is_restored(
    tmp_path, recovery_deck, monkeypatch
):
    root = tmp_path / "storage"
    final, _ = leave_orphan(root, recovery_deck, monkeypatch)
    assert_recovered(root, recovery_deck, final)


@pytest.mark.parametrize(
    "alias_kind",
    ["unrelated-name", "wrong-final", "bad-uuid", "outside", "spoofed-inode"],
)
def test_unaccounted_alias_rejects_retry_without_removing_any_staging_or_content(
    tmp_path, recovery_deck, monkeypatch, alias_kind
):
    root = tmp_path / "storage"
    final, staging = leave_orphan(root, recovery_deck, monkeypatch)
    if alias_kind == "unrelated-name":
        extra = final.with_name("unrelated-alias")
    elif alias_kind == "wrong-final":
        extra = final.with_name(f".pending-other.pptx-{'a' * 32}")
    elif alias_kind == "bad-uuid":
        extra = final.with_name(f".pending-{final.name}-not-a-uuid")
    else:
        # Even an exact protocol name outside the final's directory is forbidden.
        extra = _filesystem_path(tmp_path / f".pending-{final.name}-{'a' * 32}")
    os.link(final, extra)
    protected = [final, staging, extra]
    if alias_kind == "spoofed-inode":
        impostor = final.with_name(f".pending-{final.name}-{'b' * 32}")
        impostor.write_bytes(b"Unrelated file with a plausible staging name")
        protected.append(impostor)
    snapshot = {path: (path.read_bytes(), path.stat()) for path in protected}
    assert final.stat().st_nlink == 3
    workspace = Workspace(root, max_read_bytes=len(recovery_deck[1]))
    relative = final.relative_to(_filesystem_path(workspace.root))
    with pytest.raises(WorkspacePathError):
        workspace.read_bytes(relative)
    # Failed recovery must be repeatable and leave all original names untouched.
    for _ in range(2):
        with pytest.raises(ArtifactIntegrityError):
            asyncio.run(
                PptxArtifactStore(root, mode="offline").publish(
                    THREAD, TURN, recovery_deck[1], recovery_deck[0]
                )
            )
        for path, (content, info) in snapshot.items():
            assert path.read_bytes() == content
            current = path.stat()
            assert (current.st_dev, current.st_ino, current.st_mtime_ns) == (
                info.st_dev,
                info.st_ino,
                info.st_mtime_ns,
            )
            assert current.st_nlink == info.st_nlink
        assert set(matches(root, ".pending-*")) == {
            path
            for path in protected
            if path.parent == final.parent and path.name.startswith(".pending-")
        }
        assert rows(root, "SELECT count(*) FROM pptx_artifacts") == [(0,)]


def test_mismatching_orphan_is_never_overwritten_or_cleaned(
    tmp_path, recovery_deck, monkeypatch
):
    root = tmp_path / "storage"
    final, staging = leave_orphan(root, recovery_deck, monkeypatch)
    # Editing either hardlink changes both names, leaving the expected filename.
    canary = b"Unrelated crash remnant; preserve it"
    final.write_bytes(canary)
    before = final.stat()
    with pytest.raises(ArtifactIntegrityError):
        asyncio.run(
            PptxArtifactStore(root, mode="offline").publish(
                THREAD, TURN, recovery_deck[1], recovery_deck[0]
            )
        )
    assert final.read_bytes() == staging.read_bytes() == canary
    assert final.stat().st_ino == before.st_ino
    assert final.stat().st_nlink == 2
    assert matches(root, ".pending-*") == [staging]
    assert rows(root, "SELECT count(*) FROM pptx_artifacts") == [(0,)]


def test_recovery_cleans_multiple_known_aliases_but_preserves_other_inodes(
    tmp_path, recovery_deck, monkeypatch
):
    root = tmp_path / "storage"
    final, staging = leave_orphan(root, recovery_deck, monkeypatch)
    second = final.with_name(f".pending-{final.name}-{'d' * 32}")
    unrelated = final.with_name(f".pending-{final.name}-{'e' * 32}")
    os.link(final, second)
    unrelated.write_bytes(b"Independent staging file, not an alias")
    assert final.stat().st_nlink == 3
    before = final.stat()
    asyncio.run(
        PptxArtifactStore(root, mode="offline").publish(
            THREAD, TURN, recovery_deck[1], recovery_deck[0]
        )
    )
    assert not staging.exists() and not second.exists()
    assert unrelated.read_bytes() == b"Independent staging file, not an alias"
    assert final.stat().st_ino == before.st_ino
    assert final.stat().st_nlink == 1
    assert final.read_bytes() == recovery_deck[1]
    assert rows(root, "SELECT count(*) FROM pptx_artifacts") == [(1,)]


@pytest.mark.parametrize("damage", ["over-limit", "different", "directory"])
def test_recovery_rejects_unbounded_nonregular_or_different_files_without_cleanup(
    tmp_path, damage
):
    workspace = Workspace(tmp_path, max_read_bytes=4, max_write_bytes=4)
    final = _filesystem_path(tmp_path / "deck.pptx")
    staging = final.with_name(f".pending-{final.name}-{'f' * 32}")
    if damage == "directory":
        final.mkdir()
        with pytest.raises((WorkspacePathError, OSError)):
            workspace.recover_bytes_publication("deck.pptx", b"deck")
        assert final.is_dir()
        return
    data = b"large" if damage == "over-limit" else b"else"
    final.write_bytes(data)
    os.link(final, staging)
    with pytest.raises(WorkspacePathError):
        workspace.recover_bytes_publication("deck.pptx", b"deck")
    assert final.read_bytes() == staging.read_bytes() == data
    assert final.stat().st_nlink == 2


def test_protocol_staging_alias_does_not_relax_reads_of_committed_artifacts(
    tmp_path, recovery_deck
):
    root = tmp_path / "storage"
    asyncio.run(seed_owner(root))
    asyncio.run(
        PptxArtifactStore(root, mode="offline").publish(
            THREAD, TURN, recovery_deck[1], recovery_deck[0]
        )
    )
    (final,) = matches(root, "*.pptx")
    staging = final.with_name(f".pending-{final.name}-{'c' * 32}")
    os.link(final, staging)

    async def reject():
        store = PptxArtifactStore(root, mode="offline")
        with pytest.raises(ArtifactIntegrityError):
            await store.retrieve(THREAD, TURN)
        with pytest.raises(ArtifactIntegrityError):
            await store.publish(THREAD, TURN, recovery_deck[1], recovery_deck[0])

    asyncio.run(reject())
    assert staging.exists()
    assert final.read_bytes() == staging.read_bytes() == recovery_deck[1]
    assert final.stat().st_nlink == 2
    assert rows(root, "SELECT count(*) FROM pptx_artifacts") == [(1,)]


def test_artifacts_demo_subprocess_verifies_real_bytes_and_refuses_rerun(tmp_path):
    root = tmp_path / "demo"
    command = [
        sys.executable,
        "-m",
        "mini_deerflow.demo.phase2_artifacts_offline",
        "--storage-root",
        str(root),
    ]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=credential_free_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    for flag in (
        "valid",
        "fresh_process",
        "download_matches_storage",
        "turn_isolation",
        "mode_isolation",
        "markdown_preserved",
    ):
        assert evidence[flag] is True
    assert Path(evidence["storage_root"]) == root.resolve()
    downloaded = _filesystem_path(Path(evidence["downloaded_file"])).read_bytes()
    assert _filesystem_path(Path(evidence["source_file"])).read_bytes() == downloaded

    async def verify():
        for mode, directory, turn_count in (
            ("offline", root, 2),
            ("live", root / "live", 1),
        ):
            record, stored = await PptxArtifactStore(directory, mode=mode).retrieve(
                "phase2-artifact-demo", "turn-1"
            )
            reported = evidence["records"][mode]
            assert (
                reported["sha256"]
                == record.sha256
                == hashlib.sha256(stored).hexdigest()
            )
            assert reported["byte_size"] == record.byte_size == len(stored)
            report = validate_pptx(
                stored, DeckSpec.model_validate_json(record.spec_json), min_layouts=8
            )
            assert reported["validation"] == json.loads(json.dumps(report.to_dict()))
            assert report.slide_count == len(report.layouts) == 8
            assert (
                report.native_objects["tables"] == report.native_objects["charts"] == 1
            )
            assert report.geometry_checked == sum(
                slide.geometry_checked for slide in report.slides
            )
            assert rows(directory, "SELECT count(*) FROM conversations") == [(1,)]
            assert rows(directory, "SELECT count(*) FROM turns") == [(turn_count,)]
            assert rows(directory, "SELECT count(*) FROM pptx_artifacts") == [(1,)]
            if mode == "offline":
                assert stored == downloaded

    asyncio.run(verify())
    assert (
        evidence["records"]["offline"]["sha256"]
        != evidence["records"]["live"]["sha256"]
    )
    assert matches(root, ".pending-*") == []
    before = {path: path.read_bytes() for path in matches(root, "*.pptx")}
    assert len(before) == 3  # Two publications and the downloaded offline deck.
    rerun = subprocess.run(
        command,
        cwd=tmp_path,
        env=credential_free_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert rerun.returncode != 0
    assert "Choose a fresh demonstration storage directory." in rerun.stderr
    assert {path: path.read_bytes() for path in matches(root, "*.pptx")} == before
    asyncio.run(verify())
