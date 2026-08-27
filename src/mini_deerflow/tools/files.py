import asyncio
from typing import Annotated

from pydantic import StringConstraints

from mini_deerflow.tools.contracts import ToolInput, ToolResult
from mini_deerflow.workspace import Workspace, WorkspaceError

WorkspaceRelativePath = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=500,
    ),
]


class ReadFileInput(ToolInput):
    path: WorkspaceRelativePath


class WriteFileInput(ToolInput):
    path: WorkspaceRelativePath
    content: str


class ListFilesInput(ToolInput):
    directory: WorkspaceRelativePath = "."


class ReadFileTool:
    name = "read_file"
    description = "Read a UTF-8 text file from the workspace."
    input_model = ReadFileInput
    timeout_seconds = 5.0
    idempotent = True

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    async def run(self, tool_input: ToolInput) -> ToolResult:
        assert isinstance(tool_input, ReadFileInput)

        try:
            content = await asyncio.to_thread(
                self._workspace.read_text,
                tool_input.path,
            )
        except UnicodeDecodeError:
            return ToolResult.fail(
                error="Workspace file is not valid UTF-8 text.",
                metadata={
                    "tool_name": self.name,
                    "error_type": "UnicodeDecodeError",
                },
            )
        except WorkspaceError as exc:
            return ToolResult.fail(
                error="Unable to read workspace file.",
                metadata={
                    "tool_name": self.name,
                    "error_type": type(exc).__name__,
                },
            )

        return ToolResult.ok(
            data={
                "path": tool_input.path,
                "content": content,
                "size_bytes": len(content.encode("utf-8")),
            }
        )


class WriteFileTool:
    name = "write_file"
    description = "Write or replace a UTF-8 text file in the workspace."
    input_model = WriteFileInput
    timeout_seconds = 5.0
    idempotent = True

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    async def run(self, tool_input: ToolInput) -> ToolResult:
        assert isinstance(tool_input, WriteFileInput)

        try:
            written_bytes = await asyncio.to_thread(
                self._workspace.write_text,
                tool_input.path,
                tool_input.content,
            )
        except WorkspaceError as exc:
            return ToolResult.fail(
                error="Unable to write workspace file.",
                metadata={
                    "tool_name": self.name,
                    "error_type": type(exc).__name__,
                },
            )

        return ToolResult.ok(
            data={
                "path": tool_input.path,
                "bytes_written": written_bytes,
            }
        )


class ListFilesTool:
    name = "list_files"
    description = "List files contained in a workspace directory."
    input_model = ListFilesInput
    timeout_seconds = 5.0
    idempotent = True

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    async def run(self, tool_input: ToolInput) -> ToolResult:
        assert isinstance(tool_input, ListFilesInput)

        try:
            files = await asyncio.to_thread(
                self._workspace.list_files,
                tool_input.directory,
            )
        except WorkspaceError as exc:
            return ToolResult.fail(
                error="Unable to list workspace files.",
                metadata={
                    "tool_name": self.name,
                    "error_type": type(exc).__name__,
                },
            )

        return ToolResult.ok(
            data={
                "directory": tool_input.directory,
                "files": list(files),
                "count": len(files),
            }
        )
