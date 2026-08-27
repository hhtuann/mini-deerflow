import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from mini_deerflow.tools.files import (
    ListFilesInput,
    ListFilesTool,
    ReadFileInput,
    ReadFileTool,
    WriteFileInput,
    WriteFileTool,
)
from mini_deerflow.tools.registry import ToolRegistry
from mini_deerflow.tools.runner import ToolRunner
from mini_deerflow.workspace import Workspace


def test_list_files_input_defaults_to_workspace_root() -> None:
    tool_input = ListFilesInput()

    assert tool_input.directory == "."


def test_file_input_normalizes_surrounding_whitespace() -> None:
    tool_input = ReadFileInput(path="  notes/result.md  ")

    assert tool_input.path == "notes/result.md"


@pytest.mark.parametrize(
    "invalid_path",
    [
        "",
        "   ",
        "a" * 501,
    ],
)
def test_file_input_rejects_invalid_path(
    invalid_path: str,
) -> None:
    with pytest.raises(ValidationError):
        ReadFileInput(path=invalid_path)


def test_read_file_tool_returns_utf8_content_and_byte_size(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.write_text("notes/result.md", "Tuấn")

    tool = ReadFileTool(workspace)
    result = asyncio.run(
        tool.run(
            ReadFileInput(path="notes/result.md"),
        )
    )

    assert result.success is True
    assert result.data == {
        "path": "notes/result.md",
        "content": "Tuấn",
        "size_bytes": len("Tuấn".encode()),
    }
    assert result.error is None


def test_read_file_tool_returns_failure_for_missing_file(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    tool = ReadFileTool(workspace)

    result = asyncio.run(
        tool.run(
            ReadFileInput(path="missing.md"),
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Unable to read workspace file."
    assert result.metadata["error_type"] == "WorkspaceError"


def test_read_file_tool_returns_failure_for_invalid_utf8(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.resolve_path("binary.dat").write_bytes(b"\xff\xfe")

    tool = ReadFileTool(workspace)
    result = asyncio.run(
        tool.run(
            ReadFileInput(path="binary.dat"),
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Workspace file is not valid UTF-8 text."
    assert result.metadata["error_type"] == "UnicodeDecodeError"


def test_read_file_tool_hides_traversal_details(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    tool = ReadFileTool(workspace)

    result = asyncio.run(
        tool.run(
            ReadFileInput(path="../.env"),
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Unable to read workspace file."
    assert result.metadata["error_type"] == "WorkspacePathError"

    serialized_result = result.model_dump_json()

    assert str(tmp_path) not in serialized_result


def test_write_file_tool_is_idempotent_for_same_content(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    tool = WriteFileTool(workspace)
    tool_input = WriteFileInput(
        path="reports/final.md",
        content="Final report",
    )

    first_result = asyncio.run(tool.run(tool_input))
    second_result = asyncio.run(tool.run(tool_input))

    assert first_result.success is True
    assert second_result.success is True
    assert first_result.data == second_result.data
    assert workspace.read_text("reports/final.md") == "Final report"


def test_write_file_tool_returns_failure_above_limit(
    tmp_path: Path,
) -> None:
    workspace = Workspace(
        tmp_path / "workspace",
        max_write_bytes=2,
    )
    tool = WriteFileTool(workspace)

    result = asyncio.run(
        tool.run(
            WriteFileInput(
                path="result.md",
                content="éa",
            ),
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Unable to write workspace file."
    assert result.metadata["error_type"] == "WorkspaceLimitError"
    assert not workspace.resolve_path("result.md").exists()


def test_list_files_tool_returns_sorted_files(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.write_text("z.txt", "z")
    workspace.write_text("notes/b.txt", "b")
    workspace.write_text("notes/a.txt", "a")

    tool = ListFilesTool(workspace)
    result = asyncio.run(
        tool.run(
            ListFilesInput(),
        )
    )

    assert result.success is True
    assert result.data == {
        "directory": ".",
        "files": [
            "notes/a.txt",
            "notes/b.txt",
            "z.txt",
        ],
        "count": 3,
    }


def test_list_files_tool_returns_failure_for_missing_directory(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    tool = ListFilesTool(workspace)

    result = asyncio.run(
        tool.run(
            ListFilesInput(directory="missing"),
        )
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Unable to list workspace files."
    assert result.metadata["error_type"] == "WorkspaceError"


def test_unexpected_filesystem_error_propagates_to_runner_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace(tmp_path / "workspace")

    def raise_permission_error(path: str) -> str:
        raise PermissionError(f"Internal path: {path}")

    monkeypatch.setattr(
        workspace,
        "read_text",
        raise_permission_error,
    )

    tool = ReadFileTool(workspace)

    with pytest.raises(
        PermissionError,
        match="Internal path",
    ):
        asyncio.run(
            tool.run(
                ReadFileInput(path="secret.md"),
            )
        )


def test_file_tools_execute_through_runner(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    registry = ToolRegistry(
        [
            ReadFileTool(workspace),
            WriteFileTool(workspace),
            ListFilesTool(workspace),
        ]
    )
    runner = ToolRunner(registry)

    async def execute_tools() -> tuple:
        write_result = await runner.run(
            "write_file",
            {
                "path": "notes/result.md",
                "content": "Evidence",
            },
        )
        read_result = await runner.run(
            "read_file",
            {
                "path": "notes/result.md",
            },
        )
        list_result = await runner.run(
            "list_files",
            {},
        )

        return write_result, read_result, list_result

    write_result, read_result, list_result = asyncio.run(execute_tools())

    assert write_result.success is True
    assert read_result.success is True
    assert list_result.success is True

    assert read_result.data == {
        "path": "notes/result.md",
        "content": "Evidence",
        "size_bytes": 8,
    }
    assert list_result.data == {
        "directory": ".",
        "files": ["notes/result.md"],
        "count": 1,
    }
