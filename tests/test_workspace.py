from pathlib import Path

import pytest

from mini_deerflow.workspace import (
    Workspace,
    WorkspaceError,
    WorkspaceLimitError,
    WorkspacePathError,
)


def create_symlink_or_skip(
    link_path: Path,
    target_path: Path,
) -> None:
    try:
        link_path.symlink_to(
            target_path,
            target_is_directory=target_path.is_dir(),
        )
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")


def test_workspace_creates_root_and_reads_written_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    workspace = Workspace(root)

    written_bytes = workspace.write_text(
        "notes/result.md",
        "LangGraph",
    )

    assert workspace.root == root.resolve()
    assert workspace.root.is_dir()
    assert written_bytes == 9
    assert workspace.read_text("notes/result.md") == "LangGraph"


def test_absolute_path_is_rejected(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    absolute_path = tmp_path / "outside.txt"

    with pytest.raises(
        WorkspacePathError,
        match="Absolute paths are not allowed",
    ):
        workspace.resolve_path(absolute_path)


def test_parent_traversal_is_rejected(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")

    with pytest.raises(
        WorkspacePathError,
        match="escapes the workspace boundary",
    ):
        workspace.resolve_path("../secret.txt")


def test_workspace_root_cannot_be_a_file(tmp_path: Path) -> None:
    root_file = tmp_path / "workspace"
    root_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(
        WorkspaceError,
        match="Workspace root must be a directory",
    ):
        Workspace(root_file)


def test_missing_file_cannot_be_read(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")

    with pytest.raises(
        WorkspaceError,
        match="not a readable file",
    ):
        workspace.read_text("missing.txt")


def test_directory_cannot_be_read_as_file(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.resolve_path("notes").mkdir()

    with pytest.raises(
        WorkspaceError,
        match="not a readable file",
    ):
        workspace.read_text("notes")


def test_directory_cannot_be_overwritten_as_file(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.resolve_path("notes").mkdir()

    with pytest.raises(
        WorkspaceError,
        match="not a writable file",
    ):
        workspace.write_text("notes", "content")


def test_read_accepts_content_at_exact_byte_limit(
    tmp_path: Path,
) -> None:
    workspace = Workspace(
        tmp_path / "workspace",
        max_read_bytes=2,
    )
    workspace.resolve_path("result.txt").write_bytes("é".encode())

    assert workspace.read_text("result.txt") == "é"


def test_read_rejects_content_above_byte_limit(
    tmp_path: Path,
) -> None:
    workspace = Workspace(
        tmp_path / "workspace",
        max_read_bytes=2,
    )
    workspace.resolve_path("result.txt").write_bytes("éa".encode())

    with pytest.raises(
        WorkspaceLimitError,
        match="read limit",
    ):
        workspace.read_text("result.txt")


def test_write_accepts_content_at_exact_byte_limit(
    tmp_path: Path,
) -> None:
    workspace = Workspace(
        tmp_path / "workspace",
        max_write_bytes=2,
    )

    written_bytes = workspace.write_text("result.txt", "é")

    assert written_bytes == 2
    assert workspace.read_text("result.txt") == "é"


def test_write_rejects_content_above_byte_limit(
    tmp_path: Path,
) -> None:
    workspace = Workspace(
        tmp_path / "workspace",
        max_write_bytes=2,
    )

    with pytest.raises(
        WorkspaceLimitError,
        match="write limit",
    ):
        workspace.write_text("result.txt", "éa")

    assert not workspace.resolve_path("result.txt").exists()


@pytest.mark.parametrize("invalid_limit", [0, -1, True, 1.5])
def test_invalid_read_limit_is_rejected(
    tmp_path: Path,
    invalid_limit: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="max_read_bytes must be a positive integer",
    ):
        Workspace(
            tmp_path / "workspace",
            max_read_bytes=invalid_limit,
        )


@pytest.mark.parametrize("invalid_limit", [0, -1, True, 1.5])
def test_invalid_write_limit_is_rejected(
    tmp_path: Path,
    invalid_limit: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="max_write_bytes must be a positive integer",
    ):
        Workspace(
            tmp_path / "workspace",
            max_write_bytes=invalid_limit,
        )


def test_list_files_returns_sorted_workspace_relative_paths(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")

    workspace.write_text("z-last.txt", "z")
    workspace.write_text("notes/b.txt", "b")
    workspace.write_text("notes/a.txt", "a")

    assert workspace.list_files() == (
        "notes/a.txt",
        "notes/b.txt",
        "z-last.txt",
    )


def test_list_files_can_start_from_subdirectory(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")

    workspace.write_text("root.txt", "root")
    workspace.write_text("notes/a.txt", "a")
    workspace.write_text("notes/nested/b.txt", "b")

    assert workspace.list_files("notes") == (
        "notes/a.txt",
        "notes/nested/b.txt",
    )


def test_external_file_symlink_cannot_be_read(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")

    link_path = workspace.root / "outside-link.txt"
    create_symlink_or_skip(link_path, outside_file)

    with pytest.raises(
        WorkspacePathError,
        match="escapes the workspace boundary",
    ):
        workspace.read_text("outside-link.txt")


def test_list_files_excludes_symlinks(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")

    workspace.write_text("safe.txt", "safe")

    link_path = workspace.root / "outside-link.txt"
    create_symlink_or_skip(link_path, outside_file)

    assert workspace.list_files() == ("safe.txt",)
