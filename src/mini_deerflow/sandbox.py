"""Resolve isolated filesystem roots for durable agent sessions."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from mini_deerflow.persistence import normalize_thread_id


@dataclass(frozen=True, slots=True, init=False)
class SessionSandboxResolver:
    """Map one validated public thread identifier to one sandbox root."""

    base_root: Path

    def __init__(self, base_root: str | Path) -> None:
        if not isinstance(base_root, str | Path):
            raise TypeError("base_root must be a path-like string or Path")

        resolved_base = Path(base_root).expanduser().resolve(strict=False)
        object.__setattr__(self, "base_root", resolved_base)

    def resolve(self, thread_id: str) -> Path:
        """Return the deterministic sandbox root without creating it."""

        if not isinstance(thread_id, str):
            raise TypeError("thread_id must be a string")

        normalized_thread_id = normalize_thread_id(thread_id)
        session_digest = hashlib.sha256(
            normalized_thread_id.encode("utf-8")
        ).hexdigest()
        session_key = f"v1-{session_digest}"
        sessions_root = self.base_root / "sessions"
        session_root = sessions_root / session_key

        for path in (sessions_root, session_root):
            if path.is_symlink() or path.is_junction():
                raise ValueError("session sandbox path must not be a link or junction")

        session_root = session_root.resolve(strict=False)

        if not session_root.is_relative_to(self.base_root):
            raise ValueError("session sandbox must remain inside base_root")

        return session_root
