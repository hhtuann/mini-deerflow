"""Mode/turn-owned immutable PPTX publication and verified local retrieval.

The store extends the conversation repository so artifact transactions share
its connection setup, schema initialization, and foreign-key enforcement.
No artifact paths or metadata are accepted from an HTTP caller.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mini_deerflow.conversation import SQLiteConversationRepository
from mini_deerflow.persistence import normalize_thread_id
from mini_deerflow.pptx import DeckSpec, validate_pptx
from mini_deerflow.pptx.validator import MAX_FILE_BYTES, PptxValidationError
from mini_deerflow.sandbox import SessionSandboxResolver
from mini_deerflow.workspace import Workspace, WorkspaceError

Mode = Literal["offline", "live"]
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MAX_SPEC_BYTES = 1_000_000


class ArtifactError(RuntimeError):
    """Controlled storage failure; messages contain no host paths or deck data."""


class ArtifactNotFoundError(ArtifactError):
    """No published artifact belongs to this eligible turn."""


class ArtifactConflictError(ArtifactError):
    """A different immutable deck already belongs to this turn."""


class ArtifactIntegrityError(ArtifactError):
    """Stored metadata, ownership, file bytes, or presentation failed verification."""


class PptxArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Mode
    thread_id: str
    turn_id: str
    artifact_type: Literal["pptx"] = "pptx"
    byte_size: int = Field(gt=0, le=MAX_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_json: str = Field(min_length=1, max_length=MAX_SPEC_BYTES)


class PptxArtifactStore(SQLiteConversationRepository):
    """One trusted mode-local storage directory, same DB as its conversation ledger.

    Publish while a turn is running or completed; only completed turns may
    download. One PPTX per turn. Same canonical spec retries return the original
    validated publication, even if rerendering changes ZIP timestamps.
    """

    def __init__(
        self, storage_root: str | Path, *, mode: Mode, max_bytes: int = MAX_FILE_BYTES
    ) -> None:
        if mode not in {"offline", "live"}:
            raise ValueError("Unsupported artifact mode.")
        Workspace._validate_limit("max_bytes", max_bytes)
        if max_bytes > MAX_FILE_BYTES:
            raise ValueError("Artifact limit exceeds presentation limit.")
        self._root = Path(storage_root).resolve(strict=False)
        self.mode = mode
        self.max_bytes = max_bytes
        super().__init__(self._root / "checkpoints.sqlite")

    async def _schema(self, connection: aiosqlite.Connection) -> None:
        await connection.executescript("""
            CREATE TABLE IF NOT EXISTS pptx_artifact_mode (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                mode TEXT NOT NULL CHECK(mode IN ('offline','live'))
            );
            CREATE TABLE IF NOT EXISTS pptx_artifacts (
                mode TEXT NOT NULL CHECK(mode IN ('offline','live')),
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL CHECK(artifact_type = 'pptx'),
                byte_size INTEGER NOT NULL CHECK(byte_size > 0 AND byte_size <= 10000000),
                sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
                spec_sha256 TEXT NOT NULL CHECK(length(spec_sha256) = 64),
                spec_json TEXT NOT NULL CHECK(length(spec_json) <= 1000000),
                PRIMARY KEY(mode, thread_id, turn_id),
                FOREIGN KEY(thread_id, turn_id) REFERENCES turns(thread_id, turn_id) ON DELETE CASCADE
            );
        """)
        await connection.execute(
            "INSERT OR IGNORE INTO pptx_artifact_mode VALUES (1, ?)", (self.mode,)
        )
        await connection.commit()
        row = await (
            await connection.execute(
                "SELECT mode FROM pptx_artifact_mode WHERE singleton = 1"
            )
        ).fetchone()
        if row is None or row["mode"] != self.mode:
            raise ArtifactIntegrityError("Artifact mode binding failed.")

    async def _turn(self, connection, thread_id, turn_id, *, download: bool) -> None:
        row = await (
            await connection.execute(
                """
            SELECT t.status FROM turns t JOIN conversations c ON c.thread_id = t.thread_id
            WHERE t.thread_id = ? AND t.turn_id = ?
        """,
                (thread_id, turn_id),
            )
        ).fetchone()
        statuses = {"completed"} if download else {"running", "completed"}
        if row is None or row["status"] not in statuses:
            raise ArtifactNotFoundError("Presentation was not found.")

    def _workspace(self, thread_id: str) -> Workspace:
        # Check lexical ancestors BEFORE SessionSandboxResolver resolves its base.
        base = Workspace(
            self._root, max_read_bytes=self.max_bytes, max_write_bytes=self.max_bytes
        )
        base._binary_path("workspaces")
        try:
            session = SessionSandboxResolver(self._root / "workspaces").resolve(
                thread_id
            )
        except ValueError as exc:
            raise ArtifactIntegrityError(
                "Artifact sandbox verification failed."
            ) from exc
        base._binary_path(session.relative_to(self._root))
        return Workspace(
            session, max_read_bytes=self.max_bytes, max_write_bytes=self.max_bytes
        )

    @staticmethod
    def _path(record: PptxArtifact) -> str:
        # Hash the complete ownership tuple: same turn IDs in different modes or
        # threads cannot alias even if a deployment accidentally shares roots.
        identity = json.dumps(
            (record.mode, record.thread_id, record.turn_id), separators=(",", ":")
        )
        key = hashlib.sha256(identity.encode()).hexdigest()
        return f"presentations/{key}/{record.sha256}.pptx"

    def _verified(
        self, row, thread_id: str, turn_id: str
    ) -> tuple[PptxArtifact, bytes]:
        record = PptxArtifact.model_validate(dict(row))
        if (record.mode, record.thread_id, record.turn_id) != (
            self.mode,
            thread_id,
            turn_id,
        ):
            raise ArtifactIntegrityError("Artifact ownership mismatch.")
        if (
            len(record.spec_json.encode()) > MAX_SPEC_BYTES
            or hashlib.sha256(record.spec_json.encode()).hexdigest()
            != record.spec_sha256
        ):
            raise ArtifactIntegrityError("Artifact specification integrity failed.")
        spec = DeckSpec.model_validate_json(record.spec_json)
        data = self._workspace(thread_id).read_bytes(self._path(record))
        if (
            len(data) != record.byte_size
            or hashlib.sha256(data).hexdigest() != record.sha256
        ):
            raise ArtifactIntegrityError("Artifact byte integrity failed.")
        validate_pptx(data, spec)
        return record, data

    async def publish(
        self, thread_id: str, turn_id: str, data: bytes, spec: DeckSpec
    ) -> PptxArtifact:
        """Validate, exclusively publish bytes, then commit immutable metadata.

        A SQLite write transaction serializes publishers. A completed file left
        by a crash before metadata commit is adopted only if exactly identical.
        An incomplete/unrelated file is never overwritten or reported successful.
        """
        thread_id = normalize_thread_id(thread_id)
        turn_id = normalize_thread_id(turn_id)
        if not isinstance(data, bytes) or not 0 < len(data) <= self.max_bytes:
            raise ArtifactError("Invalid presentation byte size or type.")
        spec = DeckSpec.model_validate(spec.model_dump())
        snapshot = json.dumps(
            spec.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(snapshot.encode()) > MAX_SPEC_BYTES:
            raise ArtifactError("Presentation specification limit exceeded.")
        try:
            validate_pptx(data, spec)
            record = PptxArtifact(
                mode=self.mode,
                thread_id=thread_id,
                turn_id=turn_id,
                byte_size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                spec_sha256=hashlib.sha256(snapshot.encode()).hexdigest(),
                spec_json=snapshot,
            )
            async with self._connection() as connection:
                await self._schema(connection)
                await connection.execute("BEGIN IMMEDIATE")
                await self._turn(connection, thread_id, turn_id, download=False)
                row = await (
                    await connection.execute(
                        "SELECT * FROM pptx_artifacts WHERE mode=? AND thread_id=? AND turn_id=?",
                        (self.mode, thread_id, turn_id),
                    )
                ).fetchone()
                if row is not None:
                    existing, _ = self._verified(row, thread_id, turn_id)
                    if existing.spec_json != snapshot:
                        raise ArtifactConflictError(
                            "A different presentation already belongs to this turn."
                        )
                    return existing
                workspace = self._workspace(thread_id)
                path = self._path(record)
                try:
                    workspace.write_bytes_exclusive(path, data)
                except FileExistsError:
                    workspace.recover_bytes_publication(path, data)
                # Verify persisted bytes, not just the renderer's in-memory result.
                self._verified(record.model_dump(), thread_id, turn_id)
                await connection.execute(
                    "INSERT INTO pptx_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(record.model_dump().values()),
                )
                await connection.commit()
                return record
        except ArtifactError:
            raise
        except (
            OSError,
            WorkspaceError,
            sqlite3.Error,
            PptxValidationError,
            ValidationError,
        ) as exc:
            raise ArtifactIntegrityError(
                "Presentation publication failed verification."
            ) from exc

    async def retrieve(
        self, thread_id: str, turn_id: str
    ) -> tuple[PptxArtifact, bytes]:
        """Return only the exact, fully verified bytes from a completed owning turn."""
        thread_id = normalize_thread_id(thread_id)
        turn_id = normalize_thread_id(turn_id)
        try:
            async with self._connection() as connection:
                await self._schema(connection)
                await connection.execute("BEGIN")
                await self._turn(connection, thread_id, turn_id, download=True)
                row = await (
                    await connection.execute(
                        "SELECT * FROM pptx_artifacts WHERE mode=? AND thread_id=? AND turn_id=?",
                        (self.mode, thread_id, turn_id),
                    )
                ).fetchone()
                if row is None:
                    raise ArtifactNotFoundError("Presentation was not found.")
                return self._verified(row, thread_id, turn_id)
        except ArtifactError:
            raise
        except (
            OSError,
            WorkspaceError,
            sqlite3.Error,
            PptxValidationError,
            ValidationError,
        ) as exc:
            raise ArtifactIntegrityError(
                "Presentation retrieval failed verification."
            ) from exc
