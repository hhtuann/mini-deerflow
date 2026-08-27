import os
from pathlib import Path


class WorkspaceError(Exception):
    """Base error raised by workspace operations."""


class WorkspacePathError(WorkspaceError):
    """Raised when a path escapes the workspace boundary."""


class WorkspaceLimitError(WorkspaceError):
    """Raised when file content exceeds a configured limit."""


class Workspace:
    def __init__(
        self,
        root: str | Path,
        *,
        max_read_bytes: int = 1_000_000,
        max_write_bytes: int = 1_000_000,
    ) -> None:
        self._validate_limit("max_read_bytes", max_read_bytes)
        self._validate_limit("max_write_bytes", max_write_bytes)

        root_path = Path(root).expanduser()

        if root_path.exists() and not root_path.is_dir():
            raise WorkspaceError("Workspace root must be a directory.")

        root_path.mkdir(parents=True, exist_ok=True)

        self._root = root_path.resolve(strict=True)

        if not self._root.is_dir():
            raise WorkspaceError("Workspace root must be a directory.")

        self._max_read_bytes = max_read_bytes
        self._max_write_bytes = max_write_bytes

    @property
    def root(self) -> Path:
        return self._root

    def resolve_path(self, relative_path: str | Path) -> Path:
        requested_path = Path(relative_path)

        if requested_path.is_absolute():
            raise WorkspacePathError("Absolute paths are not allowed.")

        resolved_path = (self._root / requested_path).resolve(strict=False)
        self._ensure_inside_workspace(resolved_path)

        return resolved_path

    def read_text(self, relative_path: str | Path) -> str:
        resolved_path = self.resolve_path(relative_path)

        if not resolved_path.is_file():
            raise WorkspaceError("Workspace path is not a readable file.")

        with resolved_path.open("rb") as file:
            content = file.read(self._max_read_bytes + 1)

        if len(content) > self._max_read_bytes:
            raise WorkspaceLimitError("File exceeds the workspace read limit.")

        return content.decode("utf-8")

    def write_text(
        self,
        relative_path: str | Path,
        content: str,
    ) -> int:
        encoded_content = content.encode("utf-8")

        if len(encoded_content) > self._max_write_bytes:
            raise WorkspaceLimitError("Content exceeds the workspace write limit.")

        resolved_path = self.resolve_path(relative_path)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

        # Resolve again after creating parent directories. This reduces the chance
        # of writing through a newly introduced symlink or junction.
        resolved_path = self.resolve_path(relative_path)

        if resolved_path.exists() and not resolved_path.is_file():
            raise WorkspaceError("Workspace path is not a writable file.")

        resolved_path.write_bytes(encoded_content)

        return len(encoded_content)

    def list_files(
        self,
        relative_directory: str | Path = ".",
    ) -> tuple[str, ...]:
        resolved_directory = self.resolve_path(relative_directory)

        if not resolved_directory.is_dir():
            raise WorkspaceError("Workspace path is not a directory.")

        files: list[str] = []

        for current_root, directory_names, file_names in os.walk(
            resolved_directory,
            followlinks=False,
        ):
            current_path = Path(current_root)

            safe_directories: list[str] = []

            for directory_name in directory_names:
                directory_path = current_path / directory_name

                if self._is_link_or_junction(directory_path):
                    continue

                resolved_child = directory_path.resolve(strict=False)

                if self._is_inside_workspace(resolved_child):
                    safe_directories.append(directory_name)

            # Mutating this list prevents os.walk from entering unsafe directories.
            directory_names[:] = safe_directories

            for file_name in file_names:
                file_path = current_path / file_name

                if self._is_link_or_junction(file_path):
                    continue

                resolved_file = file_path.resolve(strict=False)

                if not self._is_inside_workspace(resolved_file):
                    continue

                if resolved_file.is_file():
                    files.append(resolved_file.relative_to(self._root).as_posix())

        return tuple(sorted(files))

    def _ensure_inside_workspace(self, path: Path) -> None:
        if not self._is_inside_workspace(path):
            raise WorkspacePathError("Path escapes the workspace boundary.")

    def _is_inside_workspace(self, path: Path) -> bool:
        return path == self._root or path.is_relative_to(self._root)

    @staticmethod
    def _is_link_or_junction(path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

    @staticmethod
    def _validate_limit(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer.")
