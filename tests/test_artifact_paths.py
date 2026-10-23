"""Artifact ownership and Windows paths beyond the legacy MAX_PATH boundary."""

import asyncio
import os
from pathlib import Path

import pytest

from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.service import (
    ContinueDemoCommand,
    DemoRuntimeService,
    ResumeDemoCommand,
    RunDemoCommand,
)
from mini_deerflow.sandbox import SessionSandboxResolver
from mini_deerflow.workspace import Workspace, WorkspacePathError, _filesystem_path


@pytest.mark.parametrize("long_path", [False, True], ids=["short", "long"])
def test_artifacts_persist_per_turn_and_thread(tmp_path: Path, long_path: bool) -> None:
    storage = tmp_path / "storage"
    if long_path:
        first_path = (
            SessionSandboxResolver(storage / "workspaces").resolve("owner-alpha")
            / "reports/day-15-demo.md"
        )
        # Force the audited 254/266-character boundary regardless of temp-root size.
        padding = 254 - len(str(first_path)) - 1
        assert padding > 0, "Run this boundary test with a shorter pytest basetemp"
        storage /= "x" * padding

    async def scenario():
        backend = OfflineDemoBackend(storage)
        service = DemoRuntimeService(backend)
        for thread in ("owner-alpha", "owner-beta"):
            await service.run(
                RunDemoCommand(
                    thread_id=thread,
                    turn_id="turn-first",
                    goal=f"Research the first bounded topic for {thread}.",
                )
            )
            await service.continue_thread(
                ContinueDemoCommand(
                    thread_id=thread,
                    turn_id="turn-second",
                    user_message=f"Compare a different second topic for {thread}.",
                )
            )
        reopened = OfflineDemoBackend(storage)
        snapshots = [
            await reopened.load_conversation(thread)
            for thread in ("owner-alpha", "owner-beta")
        ]
        await DemoRuntimeService(reopened).resume(ResumeDemoCommand("owner-alpha"))
        diagnostics = reopened.diagnostics("owner-alpha")
        assert diagnostics is not None
        assert diagnostics.web_search_calls == diagnostics.web_fetch_calls == 0
        assert diagnostics.researcher_branches == ()
        return snapshots

    snapshots = asyncio.run(scenario())
    physical_paths: set[Path] = set()
    for thread, snapshot in zip(("owner-alpha", "owner-beta"), snapshots, strict=True):
        workspace = Workspace(
            SessionSandboxResolver(storage / "workspaces").resolve(thread)
        )
        assert len(snapshot.turns) == 2
        for turn, expected in zip(
            snapshot.turns,
            ("reports/day-15-demo.md", "reports/day-15-demo-turn-second.md"),
            strict=True,
        ):
            assert turn.state is not None
            assert turn.state["artifact_path"] == expected
            content = workspace.read_text(expected)
            assert content == turn.state["research_report"]
            assert thread in content
            assert turn.record.user_message in content
            physical_paths.add(workspace.resolve_path(expected))
        assert workspace.list_files() == (
            "reports/day-15-demo-turn-second.md",
            "reports/day-15-demo.md",
        )
    assert len(physical_paths) == 4
    if long_path:
        assert sorted(len(str(path)) for path in physical_paths) == [254, 254, 266, 266]


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length filesystem I/O")
def test_long_nested_artifact_can_be_read_listed_and_reopened(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    relative = "/".join(["nested" * 8] * 6) + "/report.md"
    assert len(str(workspace.resolve_path(relative))) > 300
    assert workspace.write_text(relative, "long evidence") == 13
    assert Workspace(workspace.root).read_text(relative) == "long evidence"
    assert workspace.list_files() == (relative,)
    assert workspace.list_files("nested" * 8) == (relative,)


@pytest.mark.skipif(os.name != "nt", reason="Windows path namespace validation")
@pytest.mark.parametrize(
    "relative",
    [
        "C:report.md",
        r"\report.md",
        "/report.md",
        r"\\server\share\report.md",
        r"\\?\C:\report.md",
        r"\\.\NUL",
        "report.md:stream",
        "report.md.",
        "report.md ",
        "NUL",
        "nested./report.md",
        r"..\outside\report.md",
    ],
)
def test_windows_artifact_inputs_cannot_change_namespace(
    tmp_path: Path, relative: str
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    with pytest.raises(WorkspacePathError):
        workspace.write_text(relative, "denied")
    assert workspace.list_files() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junctions")
@pytest.mark.parametrize("long_path", [False, True], ids=["short", "long"])
def test_artifact_junction_cannot_escape_workspace(
    tmp_path: Path, long_path: bool
) -> None:
    import _winapi

    workspace = Workspace(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.md").write_text("outside evidence", encoding="utf-8")
    directory = "/".join(["nested" * 8] * 5) if long_path else "nested"
    workspace.write_text(f"{directory}/safe.md", "inside evidence")
    junction = workspace.resolve_path(f"{directory}/linked")
    io_junction = Path("\\\\?\\" + str(junction))
    _winapi.CreateJunction(str(outside), str(io_junction))
    try:
        assert io_junction.is_junction()
        for relative in (f"{directory}/linked/report.md", f"{directory}/linked/new.md"):
            with pytest.raises(WorkspacePathError):
                workspace.write_text(relative, "must not escape")
        with pytest.raises(WorkspacePathError):
            workspace.read_text(f"{directory}/linked/report.md")
        assert workspace.list_files() == (f"{directory}/safe.md",)
        assert (outside / "report.md").read_text(encoding="utf-8") == "outside evidence"
        assert not (outside / "new.md").exists()
    finally:
        # Remove only the junction itself, never recurse into its target.
        io_junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junctions")
@pytest.mark.parametrize("component", ["sessions", "session"])
def test_artifact_sandbox_rejects_real_junctions(
    tmp_path: Path, component: str
) -> None:
    import _winapi

    resolver = SessionSandboxResolver(tmp_path / "sandboxes")
    session = resolver.resolve("owner-alpha")
    junction = session.parent if component == "sessions" else session
    junction.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(junction))
    try:
        assert junction.is_junction()
        with pytest.raises(ValueError, match="link or junction"):
            resolver.resolve("owner-alpha")
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junctions")
def test_artifact_rechecks_junction_after_parent_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import _winapi

    workspace = Workspace(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = Path("\\\\?\\" + str(workspace.root / "reports"))
    original_mkdir = Path.mkdir

    def replace_parent(path: Path, *args, **kwargs) -> None:
        original_mkdir(path, *args, **kwargs)
        if path == parent:
            path.rmdir()
            _winapi.CreateJunction(str(outside), str(path))

    monkeypatch.setattr(Path, "mkdir", replace_parent)
    try:
        with pytest.raises(WorkspacePathError):
            workspace.write_text("reports/report.md", "must not escape")
        assert not (outside / "report.md").exists()
    finally:
        if parent.is_junction():
            parent.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows internal path representation")
def test_internal_unc_conversion_is_idempotent_without_network_access() -> None:
    ordinary = Path(r"\\server\share\folder\report.md")
    extended = _filesystem_path(ordinary)
    assert extended == Path(r"\\?\UNC\server\share\folder\report.md")
    assert _filesystem_path(extended) == extended
