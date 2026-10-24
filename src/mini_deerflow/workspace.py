import os
import re
import stat
from pathlib import Path, PureWindowsPath
from uuid import uuid4


def _is_reserved_windows_component(part: str) -> bool:
    """Prefer current Windows rules, with a Python 3.12-compatible fallback."""
    # Validate component syntax before the pathname API can interpret "z:stream"
    # as a drive-relative path and overlook its alternate-stream separator.
    if part.endswith((".", " ")) or any(
        ord(char) < 32 or char in '<>:"/\\|?*' for char in part
    ):
        return True

    isreserved = getattr(os.path, "isreserved", None)
    if isreserved is not None:
        return isreserved(part)

    stem = part.partition(".")[0].rstrip(" ").upper()
    return stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or (
        len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3] in "123456789¹²³"
    )


def _filesystem_path(path: Path) -> Path:
    """Use extended Windows syntax only for absolute internal filesystem paths.

    Keep policy checks and public paths in the ordinary namespace. Prefix even
    short I/O roots so recursive directory walks can reach long descendants.
    """
    text = str(path)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return path
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def _resolved_path(path: Path, *, strict: bool = False) -> Path:
    """Resolve links through long paths, then restore ordinary comparison syntax."""
    resolved = _filesystem_path(path).resolve(strict=strict)
    text = str(resolved)
    if os.name == "nt" and text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[8:])
    if os.name == "nt" and text.startswith("\\\\?\\"):
        ordinary = Path(text[4:])
        if len(ordinary.drive) != 2 or not ordinary.is_absolute():
            raise WorkspacePathError("Unsupported Windows filesystem namespace.")
        return ordinary
    return resolved


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

        self._root = _resolved_path(root_path.absolute(), strict=True)

        if not self._root.is_dir():
            raise WorkspaceError("Workspace root must be a directory.")

        self._max_read_bytes = max_read_bytes
        self._max_write_bytes = max_write_bytes

    @property
    def root(self) -> Path:
        return self._root

    def resolve_path(self, relative_path: str | Path) -> Path:
        requested_path = Path(relative_path)

        if requested_path.is_absolute() or (
            os.name == "nt" and (requested_path.drive or requested_path.root)
        ):
            raise WorkspacePathError("Absolute paths are not allowed.")

        if os.name == "nt" and any(
            part not in {".", ".."} and _is_reserved_windows_component(part)
            for part in requested_path.parts
        ):
            # Extended I/O must not introduce device names, alternate streams,
            # or trailing-dot/space aliases that ordinary Win32 paths normalize.
            raise WorkspacePathError("Ambiguous Windows file names are not allowed.")

        resolved_path = _resolved_path(self._root / requested_path)
        self._ensure_inside_workspace(resolved_path)

        return resolved_path

    def read_text(self, relative_path: str | Path) -> str:
        resolved_path = _filesystem_path(self.resolve_path(relative_path))

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
        _filesystem_path(resolved_path.parent).mkdir(parents=True, exist_ok=True)

        # Resolve again after creating parent directories. This reduces the chance
        # of writing through a newly introduced symlink or junction.
        resolved_path = _filesystem_path(self.resolve_path(relative_path))

        if resolved_path.exists() and not resolved_path.is_file():
            raise WorkspaceError("Workspace path is not a writable file.")

        resolved_path.write_bytes(encoded_content)

        return len(encoded_content)

    def list_files(
        self,
        relative_directory: str | Path = ".",
    ) -> tuple[str, ...]:
        resolved_directory = self.resolve_path(relative_directory)

        filesystem_directory = _filesystem_path(resolved_directory)
        if not filesystem_directory.is_dir():
            raise WorkspaceError("Workspace path is not a directory.")

        files: list[str] = []

        for current_root, directory_names, file_names in os.walk(
            filesystem_directory,
            followlinks=False,
        ):
            current_path = resolved_directory / Path(current_root).relative_to(
                filesystem_directory
            )

            safe_directories: list[str] = []

            for directory_name in directory_names:
                directory_path = current_path / directory_name

                if self._is_link_or_junction(directory_path):
                    continue

                resolved_child = _resolved_path(directory_path)

                if self._is_inside_workspace(resolved_child):
                    safe_directories.append(directory_name)

            # Mutating this list prevents os.walk from entering unsafe directories.
            directory_names[:] = safe_directories

            for file_name in file_names:
                file_path = current_path / file_name

                if self._is_link_or_junction(file_path):
                    continue

                resolved_file = _resolved_path(file_path)

                if not self._is_inside_workspace(resolved_file):
                    continue

                if _filesystem_path(resolved_file).is_file():
                    files.append(resolved_file.relative_to(self._root).as_posix())

        return tuple(sorted(files))

    def _binary_path(self, relative_path: str | Path) -> Path:
        """Binary artifacts reject links and ambiguous syntax even inside the root."""
        raw = (
            relative_path.as_posix()
            if isinstance(relative_path, Path)
            else relative_path
        )
        requested = Path(raw)
        if (
            requested.is_absolute()
            or PureWindowsPath(raw).drive
            or "\\" in raw
            or not requested.parts
            or any(
                part == ".." or _is_reserved_windows_component(part)
                for part in requested.parts
            )
        ):
            raise WorkspacePathError("Invalid binary artifact path.")
        current = self._root
        for part in requested.parts:
            current = current / part
            if self._is_link_or_junction(current):
                raise WorkspacePathError("Binary artifact links are not allowed.")
        return self.resolve_path(requested)

    def read_bytes(self, relative_path: str | Path) -> bytes:
        """Read a bounded regular binary file, refusing link aliases and hardlinks."""
        path = _filesystem_path(self._binary_path(relative_path))
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkspacePathError(
                    "Binary artifact must be an unaliased regular file."
                )
            if info.st_size > self._max_read_bytes:
                raise WorkspaceLimitError("File exceeds the workspace read limit.")
            content = file.read(self._max_read_bytes + 1)
        if len(content) > self._max_read_bytes:
            raise WorkspaceLimitError("File exceeds the workspace read limit.")
        return content

    def write_bytes_exclusive(self, relative_path: str | Path, content: bytes) -> int:
        """Publish complete bytes atomically without replacing any existing file.

        A same-directory temporary file is fsynced, then linked exclusively to
        its final name. Filesystems must support same-volume hardlinks. This
        reduces ordinary crash/race windows; it is not an OS sandbox against a
        hostile process concurrently replacing parent directories.
        """
        if not isinstance(content, bytes):
            raise TypeError("binary content must be bytes")
        if len(content) > self._max_write_bytes:
            raise WorkspaceLimitError("Content exceeds the workspace write limit.")
        path = self._binary_path(relative_path)
        _filesystem_path(path.parent).mkdir(parents=True, exist_ok=True)
        path = self._binary_path(relative_path)
        relative_temp = (
            path.relative_to(self._root).parent / f".pending-{path.name}-{uuid4().hex}"
        )
        temporary = _filesystem_path(self._binary_path(relative_temp))
        created = False
        try:
            with temporary.open("xb") as file:
                created = True
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            path = self._binary_path(relative_path)
            os.link(temporary, _filesystem_path(path))
        finally:
            if created:
                temporary.unlink(missing_ok=True)
        return len(content)

    def recover_bytes_publication(
        self, relative_path: str | Path, expected: bytes
    ) -> None:
        """Adopt only identical complete bytes, cleaning this protocol's staging aliases.

        Called for an uncommitted orphan, never to make an ordinary download
        tolerate hardlinks. Every extra link must be a same-directory staging
        name belonging to this exact final filename and inode. Unrelated or
        unaccounted-for links fail closed without being removed.
        """
        if not isinstance(expected, bytes) or len(expected) > self._max_write_bytes:
            raise WorkspaceLimitError("Invalid binary recovery content.")
        path = _filesystem_path(self._binary_path(relative_path))
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(os.open(path, flags), "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > self._max_read_bytes:
                raise WorkspacePathError("Invalid binary recovery file.")
            if file.read(self._max_read_bytes + 1) != expected:
                raise WorkspacePathError("Binary recovery bytes differ.")
            aliases = []
            pattern = re.compile(rf"\.pending-{re.escape(path.name)}-[0-9a-f]{{32}}")
            for candidate in path.parent.iterdir():
                if not pattern.fullmatch(candidate.name) or self._is_link_or_junction(
                    candidate
                ):
                    continue
                other = candidate.stat(follow_symlinks=False)
                if stat.S_ISREG(other.st_mode) and (other.st_dev, other.st_ino) == (
                    info.st_dev,
                    info.st_ino,
                ):
                    aliases.append(candidate)
            if info.st_nlink != 1 + len(aliases):
                raise WorkspacePathError("Unrelated binary recovery hardlink.")
        for alias in aliases:
            alias.unlink()
        if self.read_bytes(relative_path) != expected:
            raise WorkspacePathError("Binary recovery verification failed.")

    def _ensure_inside_workspace(self, path: Path) -> None:
        if not self._is_inside_workspace(path):
            raise WorkspacePathError("Path escapes the workspace boundary.")

    def _is_inside_workspace(self, path: Path) -> bool:
        return path == self._root or path.is_relative_to(self._root)

    @staticmethod
    def _is_link_or_junction(path: Path) -> bool:
        path = _filesystem_path(path)
        return path.is_symlink() or path.is_junction()

    @staticmethod
    def _validate_limit(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer.")
