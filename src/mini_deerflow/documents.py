"""Local-only document workspace: durable metadata, safe blobs, lifecycle.

D1 foundation (issue #47): workspaces and documents are identified by
server-generated opaque identifiers; user input never reaches a filesystem
path. Blob bytes are published through the hardened :class:`Workspace`
primitives (exclusive atomic writes, ``O_NOFOLLOW`` verified reads), metadata
lives in a dedicated SQLite ledger, and every read re-verifies the sha256
checksum. This is a single-user loopback trust boundary: opaque identifiers
provide enumeration resistance, not authentication or tenancy.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mini_deerflow.workspace import Workspace, WorkspaceError, _filesystem_path

SCHEMA_VERSION = 1
DATABASE_NAME = "documents.sqlite"
BLOB_ROOT_NAME = "document-store"

MAX_FILENAME_CHARS = 200
MAX_WORKSPACE_NAME_CHARS = 80
MAX_PDF_SNIFF_OFFSET = 1_024

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT_MIME = "text/plain"


class DocumentError(RuntimeError):
    """Base error; messages carry no host paths or document content."""


class DocumentNotFoundError(DocumentError):
    """Workspace or document does not exist for the caller's exact ids."""


class DocumentQuotaError(DocumentError):
    """A configured workspace/document limit would be exceeded."""


class DocumentIntegrityError(DocumentError):
    """Stored metadata or bytes failed verification."""


class DocumentValidationError(ValueError):
    """Caller input violates the document contract."""


class DocumentUnsupportedTypeError(DocumentValidationError):
    """Extension, declared media type, or content sniff is not allowed."""


def _sniff_pdf(data: bytes) -> bool:
    return b"%PDF-" in data[:MAX_PDF_SNIFF_OFFSET]


def _sniff_docx(data: bytes) -> bool:
    return data[:4] == b"PK\x03\x04"


def _sniff_utf8_text(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class DocumentType:
    extension: str
    mime: str
    sniff: Any


_DOCUMENT_TYPES: dict[str, DocumentType] = {
    spec.extension: spec
    for spec in (
        DocumentType("pdf", PDF_MIME, _sniff_pdf),
        DocumentType("docx", DOCX_MIME, _sniff_docx),
        DocumentType("txt", TXT_MIME, _sniff_utf8_text),
    )
}

_MIME_TO_TYPE = {spec.mime: spec for spec in _DOCUMENT_TYPES.values()}


@dataclass(frozen=True)
class DocumentLimits:
    """Bounded, fixture-friendly quotas; positive integers only."""

    max_document_bytes: int = 10_485_760
    max_documents_per_workspace: int = 50
    max_versions_per_document: int = 20
    max_workspace_bytes: int = 209_715_200
    max_workspaces: int = 100

    def __post_init__(self) -> None:
        for name in (
            "max_document_bytes",
            "max_documents_per_workspace",
            "max_versions_per_document",
            "max_workspace_bytes",
            "max_workspaces",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")


DEFAULT_DOCUMENT_LIMITS = DocumentLimits(
    max_document_bytes=10_485_760,
    max_documents_per_workspace=50,
    max_versions_per_document=20,
    max_workspace_bytes=209_715_200,
    max_workspaces=100,
)


def resolve_document_limits() -> DocumentLimits:
    """Prefer environment-derived settings; fall back when unparseable.

    ``Settings`` requires a provider API key which unrelated local tools may
    not have, yet operator-set document quotas must still apply; a placeholder
    key (never used for document operations) lets validation run without one.
    A settings payload that is invalid for any other reason falls back to the
    audited defaults instead of failing.
    """

    try:
        from mini_deerflow.config import Settings

        settings = Settings(api_key="document-limits-placeholder")
    except ValidationError:
        return DEFAULT_DOCUMENT_LIMITS
    return DocumentLimits(
        max_document_bytes=settings.document_max_bytes,
        max_documents_per_workspace=settings.document_workspace_max_documents,
        max_versions_per_document=settings.document_max_versions,
        max_workspace_bytes=settings.document_workspace_max_bytes,
        max_workspaces=settings.document_max_workspaces,
    )


class DocumentVersionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str


class DocumentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    document_id: str
    filename: str = Field(min_length=1, max_length=MAX_FILENAME_CHARS)
    content_type: str
    status: str
    created_at: str
    updated_at: str
    latest_version: int = Field(ge=0)
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    versions: tuple[DocumentVersionRecord, ...] = ()


class WorkspaceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    name: str = Field(min_length=1, max_length=MAX_WORKSPACE_NAME_CHARS)
    status: str
    created_at: str
    updated_at: str
    document_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _validated_workspace_name(value: object) -> str:
    if not isinstance(value, str):
        raise DocumentValidationError("workspace name must be text")
    normalized = _WHITESPACE.sub(" ", _CONTROL_CHARACTERS.sub(" ", value)).strip()
    if not 1 <= len(normalized) <= MAX_WORKSPACE_NAME_CHARS:
        raise DocumentValidationError("workspace name must contain 1 to 80 characters")
    return normalized


def _validated_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise DocumentValidationError(f"{name} is invalid")
    return value


def _validated_filename(value: object) -> tuple[str, DocumentType]:
    if not isinstance(value, str):
        raise DocumentValidationError("filename must be text")
    if not value or value != value.strip() or len(value) > MAX_FILENAME_CHARS:
        raise DocumentValidationError("filename is invalid")
    if _CONTROL_CHARACTERS.search(value) or "/" in value or "\\" in value:
        raise DocumentValidationError("filename is invalid")
    extension = value.rsplit(".", 1)[-1].lower() if "." in value else ""
    spec = _DOCUMENT_TYPES.get(extension)
    if spec is None:
        raise DocumentUnsupportedTypeError("unsupported document type")
    return value, spec


def _validated_payload(
    filename: object,
    content_type: object,
    data: object,
    *,
    max_document_bytes: int,
) -> tuple[str, DocumentType, str, int]:
    """Validate display name, declared type, magic sniff, and size together."""

    validated_filename, spec = _validated_filename(filename)
    if not isinstance(content_type, str) or content_type != spec.mime:
        raise DocumentUnsupportedTypeError("unsupported document type")
    if not isinstance(data, bytes) or not 0 < len(data) <= max_document_bytes:
        raise DocumentValidationError("document bytes are empty or too large")
    if not spec.sniff(data):
        raise DocumentUnsupportedTypeError("document content does not match its type")
    return validated_filename, spec, hashlib.sha256(data).hexdigest(), len(data)


def _blob_relative_path(
    workspace_id: str, document_id: str, version: int, sha256: str, extension: str
) -> str:
    workspace_key = hashlib.sha256(workspace_id.encode()).hexdigest()
    document_key = hashlib.sha256(f"{workspace_id}:{document_id}".encode()).hexdigest()
    return f"ws-{workspace_key}/doc-{document_key}/v{version}/{sha256}.{extension}"


class _ConnectionContext:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._connection: aiosqlite.Connection | None = None

    async def __aenter__(self) -> aiosqlite.Connection:
        connection = await aiosqlite.connect(str(self._path))
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await _initialize_schema(connection)
        self._connection = connection
        return connection

    async def __aexit__(self, *args: object) -> None:
        assert self._connection is not None
        await self._connection.close()


async def _initialize_schema(connection: aiosqlite.Connection) -> None:
    await connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS document_store_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            schema_version INTEGER NOT NULL CHECK(schema_version > 0)
        );
        CREATE TABLE IF NOT EXISTS document_workspaces (
            workspace_id TEXT PRIMARY KEY,
            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
            status TEXT NOT NULL CHECK(status IN ('active','deleted')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents (
            workspace_id TEXT NOT NULL
                REFERENCES document_workspaces(workspace_id) ON DELETE CASCADE,
            document_id TEXT NOT NULL,
            filename TEXT NOT NULL CHECK(length(filename) BETWEEN 1 AND 200),
            content_type TEXT NOT NULL CHECK(content_type IN (
                'application/pdf',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'text/plain'
            )),
            status TEXT NOT NULL CHECK(status IN ('active','deleted')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            latest_version INTEGER NOT NULL CHECK(latest_version >= 0),
            PRIMARY KEY(workspace_id, document_id)
        );
        CREATE TABLE IF NOT EXISTS document_versions (
            workspace_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version >= 1),
            byte_size INTEGER NOT NULL CHECK(byte_size > 0),
            sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
            created_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, document_id, version),
            FOREIGN KEY(workspace_id, document_id)
                REFERENCES documents(workspace_id, document_id) ON DELETE CASCADE
        );
        """
    )
    await connection.execute(
        "INSERT OR IGNORE INTO document_store_meta VALUES (1, ?)",
        (SCHEMA_VERSION,),
    )
    await connection.commit()
    row = await (
        await connection.execute(
            "SELECT schema_version FROM document_store_meta WHERE singleton = 1"
        )
    ).fetchone()
    if row is None or row["schema_version"] != SCHEMA_VERSION:
        raise DocumentIntegrityError("Document store schema is not supported.")


class DocumentWorkspaceStore:
    """One trusted local storage root: SQLite ledger + hardened blob tree."""

    def __init__(
        self,
        storage_root: str | Path,
        *,
        limits: DocumentLimits | None = None,
    ) -> None:
        resolved = Path(storage_root).expanduser()
        if resolved.is_symlink() or resolved.is_junction():
            raise DocumentIntegrityError("Document storage root must not be a link.")
        if resolved.exists() and not resolved.is_dir():
            raise DocumentIntegrityError("Document storage root must be a directory.")
        self._root = resolved.resolve(strict=False)
        self._database_path = self._root / DATABASE_NAME
        self.limits = limits or DEFAULT_DOCUMENT_LIMITS

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connection(self) -> _ConnectionContext:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        return _ConnectionContext(self._database_path)

    def _blob_workspace(self) -> Workspace:
        base = Workspace(
            self._root,
            max_read_bytes=self.limits.max_document_bytes,
            max_write_bytes=self.limits.max_document_bytes,
        )
        base._binary_path(BLOB_ROOT_NAME)
        return Workspace(
            self._root / BLOB_ROOT_NAME,
            max_read_bytes=self.limits.max_document_bytes,
            max_write_bytes=self.limits.max_document_bytes,
        )

    def _blob_path(
        self,
        workspace_id: str,
        document_id: str,
        version: int,
        sha256: str,
        extension: str,
    ) -> str:
        return _blob_relative_path(
            workspace_id, document_id, version, sha256, extension
        )

    def _publish_blob(self, relative_path: str, data: bytes) -> None:
        workspace = self._blob_workspace()
        try:
            workspace.write_bytes_exclusive(relative_path, data)
        except FileExistsError:
            workspace.recover_bytes_publication(relative_path, data)

    def _read_blob(self, relative_path: str) -> bytes:
        return self._blob_workspace().read_bytes(relative_path)

    async def _active_workspace_row(
        self, connection: aiosqlite.Connection, workspace_id: str
    ) -> aiosqlite.Row:
        row = await (
            await connection.execute(
                "SELECT * FROM document_workspaces "
                "WHERE workspace_id = ? AND status = 'active'",
                (workspace_id,),
            )
        ).fetchone()
        if row is None:
            raise DocumentNotFoundError("Workspace was not found.")
        return row

    async def _active_document_row(
        self,
        connection: aiosqlite.Connection,
        workspace_id: str,
        document_id: str,
    ) -> aiosqlite.Row:
        await self._active_workspace_row(connection, workspace_id)
        row = await (
            await connection.execute(
                "SELECT * FROM documents "
                "WHERE workspace_id = ? AND document_id = ? AND status = 'active'",
                (workspace_id, document_id),
            )
        ).fetchone()
        if row is None:
            raise DocumentNotFoundError("Document was not found.")
        return row

    async def _workspace_usage(
        self, connection: aiosqlite.Connection, workspace_id: str
    ) -> tuple[int, int]:
        row = await (
            await connection.execute(
                """
                SELECT COUNT(DISTINCT d.document_id) AS document_count,
                       COALESCE(SUM(v.byte_size), 0) AS total_bytes
                FROM documents d
                LEFT JOIN document_versions v
                    ON v.workspace_id = d.workspace_id
                   AND v.document_id = d.document_id
                WHERE d.workspace_id = ? AND d.status = 'active'
                """,
                (workspace_id,),
            )
        ).fetchone()
        return int(row["document_count"]), int(row["total_bytes"])

    async def _record(
        self,
        connection: aiosqlite.Connection,
        workspace_id: str,
        document_id: str,
        *,
        with_versions: bool,
    ) -> DocumentRecord:
        row = await (
            await connection.execute(
                """
                SELECT d.*, v.byte_size, v.sha256, v.created_at AS version_created
                FROM documents d
                LEFT JOIN document_versions v
                    ON v.workspace_id = d.workspace_id
                   AND v.document_id = d.document_id
                   AND v.version = d.latest_version
                WHERE d.workspace_id = ? AND d.document_id = ?
                  AND d.status = 'active'
                """,
                (workspace_id, document_id),
            )
        ).fetchone()
        if row is None or row["sha256"] is None:
            raise DocumentNotFoundError("Document was not found.")
        versions: tuple[DocumentVersionRecord, ...] = ()
        if with_versions:
            version_rows = await (
                await connection.execute(
                    "SELECT version, byte_size, sha256, created_at "
                    "FROM document_versions "
                    "WHERE workspace_id = ? AND document_id = ? ORDER BY version",
                    (workspace_id, document_id),
                )
            ).fetchall()
            versions = tuple(
                DocumentVersionRecord(
                    version=item["version"],
                    byte_size=item["byte_size"],
                    sha256=item["sha256"],
                    created_at=item["created_at"],
                )
                for item in version_rows
            )
        return DocumentRecord(
            workspace_id=row["workspace_id"],
            document_id=row["document_id"],
            filename=row["filename"],
            content_type=row["content_type"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            latest_version=row["latest_version"],
            byte_size=row["byte_size"],
            sha256=row["sha256"],
            versions=versions,
        )

    async def create_workspace(self, name: object) -> WorkspaceRecord:
        validated = _validated_workspace_name(name)
        workspace_id = f"ws-{uuid4().hex}"
        now = _now()
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            count = await (
                await connection.execute(
                    "SELECT COUNT(*) AS total FROM document_workspaces "
                    "WHERE status = 'active'"
                )
            ).fetchone()
            if int(count["total"]) >= self.limits.max_workspaces:
                await connection.rollback()
                raise DocumentQuotaError("Workspace limit reached.")
            await connection.execute(
                "INSERT INTO document_workspaces VALUES (?, ?, 'active', ?, ?)",
                (workspace_id, validated, now, now),
            )
            await connection.commit()
        return WorkspaceRecord(
            workspace_id=workspace_id,
            name=validated,
            status="active",
            created_at=now,
            updated_at=now,
            document_count=0,
            total_bytes=0,
        )

    async def list_workspaces(self) -> tuple[WorkspaceRecord, ...]:
        async with self._connection() as connection:
            await connection.execute("BEGIN")
            rows = await (
                await connection.execute(
                    """
                    SELECT w.workspace_id, w.name, w.status,
                           w.created_at, w.updated_at,
                           COUNT(DISTINCT d.document_id) AS document_count,
                           COALESCE(SUM(v.byte_size), 0) AS total_bytes
                    FROM document_workspaces w
                    LEFT JOIN documents d
                        ON d.workspace_id = w.workspace_id
                       AND d.status = 'active'
                    LEFT JOIN document_versions v
                        ON v.workspace_id = d.workspace_id
                       AND v.document_id = d.document_id
                    WHERE w.status = 'active'
                    GROUP BY w.workspace_id
                    ORDER BY w.created_at, w.workspace_id
                    """
                )
            ).fetchall()
            await connection.commit()
        return tuple(
            WorkspaceRecord(
                workspace_id=row["workspace_id"],
                name=row["name"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                document_count=int(row["document_count"]),
                total_bytes=int(row["total_bytes"]),
            )
            for row in rows
        )

    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        workspace_id = _validated_identifier(workspace_id, "workspace_id")
        records = [
            record
            for record in await self.list_workspaces()
            if record.workspace_id == workspace_id
        ]
        if not records:
            raise DocumentNotFoundError("Workspace was not found.")
        return records[0]

    async def delete_workspace(self, workspace_id: str) -> None:
        workspace_id = _validated_identifier(workspace_id, "workspace_id")
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await self._active_workspace_row(connection, workspace_id)
            now = _now()
            await connection.execute(
                "UPDATE document_workspaces SET status = 'deleted', updated_at = ? "
                "WHERE workspace_id = ?",
                (now, workspace_id),
            )
            await connection.execute(
                "UPDATE documents SET status = 'deleted', updated_at = ? "
                "WHERE workspace_id = ? AND status = 'active'",
                (now, workspace_id),
            )
            await connection.commit()
        await self.recover()

    async def upload_document(
        self,
        workspace_id: str,
        *,
        filename: object,
        content_type: object,
        data: object,
    ) -> DocumentRecord:
        workspace_id = _validated_identifier(workspace_id, "workspace_id")
        validated_filename, spec, sha256, size = _validated_payload(
            filename,
            content_type,
            data,
            max_document_bytes=self.limits.max_document_bytes,
        )
        document_id = f"doc-{uuid4().hex}"
        now = _now()
        blob_path = self._blob_path(
            workspace_id, document_id, 1, sha256, spec.extension
        )
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await self._active_workspace_row(connection, workspace_id)
            document_count, total_bytes = await self._workspace_usage(
                connection, workspace_id
            )
            if document_count + 1 > self.limits.max_documents_per_workspace:
                await connection.rollback()
                raise DocumentQuotaError("Document limit reached.")
            if total_bytes + size > self.limits.max_workspace_bytes:
                await connection.rollback()
                raise DocumentQuotaError("Workspace storage limit reached.")
            try:
                self._publish_blob(blob_path, data)
            except (OSError, WorkspaceError) as exc:
                await connection.rollback()
                raise DocumentIntegrityError(
                    "Document publication failed verification."
                ) from exc
            await connection.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, 'active', ?, ?, 1)",
                (
                    workspace_id,
                    document_id,
                    validated_filename,
                    spec.mime,
                    now,
                    now,
                ),
            )
            await connection.execute(
                "INSERT INTO document_versions VALUES (?, ?, 1, ?, ?, ?)",
                (workspace_id, document_id, size, sha256, now),
            )
            await connection.commit()
        return await self.get_document(workspace_id, document_id)

    async def upload_version(
        self,
        workspace_id: str,
        document_id: str,
        *,
        filename: object = None,
        content_type: object,
        data: object,
    ) -> DocumentRecord:
        workspace_id = _validated_identifier(workspace_id, "workspace_id")
        document_id = _validated_identifier(document_id, "document_id")
        now = _now()
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            document = await self._active_document_row(
                connection, workspace_id, document_id
            )
            if (
                not isinstance(content_type, str)
                or content_type != document["content_type"]
            ):
                await connection.rollback()
                raise DocumentUnsupportedTypeError("unsupported document type")
            effective_filename = (
                filename if filename is not None else document["filename"]
            )
            validated_filename, spec, sha256, size = _validated_payload(
                effective_filename,
                content_type,
                data,
                max_document_bytes=self.limits.max_document_bytes,
            )
            rename = filename is not None and validated_filename != document["filename"]
            latest_version = int(document["latest_version"])
            identical = False
            if latest_version >= 1:
                latest = await (
                    await connection.execute(
                        "SELECT sha256 FROM document_versions "
                        "WHERE workspace_id = ? AND document_id = ? AND version = ?",
                        (workspace_id, document_id, latest_version),
                    )
                ).fetchone()
                identical = latest is not None and latest["sha256"] == sha256
            if identical and rename:
                # Identical bytes stay idempotent, but an explicit rename is
                # display metadata and must still take effect.
                await connection.execute(
                    "UPDATE documents SET filename = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND document_id = ?",
                    (validated_filename, now, workspace_id, document_id),
                )
            if not identical:
                new_version = latest_version + 1
                if new_version > self.limits.max_versions_per_document:
                    await connection.rollback()
                    raise DocumentQuotaError("Document version limit reached.")
                _, total_bytes = await self._workspace_usage(connection, workspace_id)
                if total_bytes + size > self.limits.max_workspace_bytes:
                    await connection.rollback()
                    raise DocumentQuotaError("Workspace storage limit reached.")
                blob_path = self._blob_path(
                    workspace_id, document_id, new_version, sha256, spec.extension
                )
                try:
                    self._publish_blob(blob_path, data)
                except (OSError, WorkspaceError) as exc:
                    await connection.rollback()
                    raise DocumentIntegrityError(
                        "Document publication failed verification."
                    ) from exc
                await connection.execute(
                    "INSERT INTO document_versions VALUES (?, ?, ?, ?, ?, ?)",
                    (workspace_id, document_id, new_version, size, sha256, now),
                )
                if rename:
                    await connection.execute(
                        "UPDATE documents SET latest_version = ?, updated_at = ?, "
                        "filename = ? WHERE workspace_id = ? AND document_id = ?",
                        (
                            new_version,
                            now,
                            validated_filename,
                            workspace_id,
                            document_id,
                        ),
                    )
                else:
                    await connection.execute(
                        "UPDATE documents SET latest_version = ?, updated_at = ? "
                        "WHERE workspace_id = ? AND document_id = ?",
                        (new_version, now, workspace_id, document_id),
                    )
            await connection.commit()
        return await self.get_document(workspace_id, document_id)

    async def list_documents(self, workspace_id: str) -> tuple[DocumentRecord, ...]:
        workspace_id = _validated_identifier(workspace_id, "workspace_id")
        async with self._connection() as connection:
            await connection.execute("BEGIN")
            await self._active_workspace_row(connection, workspace_id)
            rows = await (
                await connection.execute(
                    """
                    SELECT d.document_id FROM documents d
                    WHERE d.workspace_id = ? AND d.status = 'active'
                    ORDER BY d.created_at, d.document_id
                    """,
                    (workspace_id,),
                )
            ).fetchall()
            records = []
            for row in rows:
                records.append(
                    await self._record(
                        connection,
                        workspace_id,
                        row["document_id"],
                        with_versions=False,
                    )
                )
            await connection.commit()
            return tuple(records)

    async def get_document(self, workspace_id: str, document_id: str) -> DocumentRecord:
        workspace_id = _validated_identifier(workspace_id, "workspace_id")
        document_id = _validated_identifier(document_id, "document_id")
        async with self._connection() as connection:
            await connection.execute("BEGIN")
            await self._active_document_row(connection, workspace_id, document_id)
            record = await self._record(
                connection, workspace_id, document_id, with_versions=True
            )
            await connection.commit()
            return record

    async def read_content(
        self, workspace_id: str, document_id: str, version: int | None = None
    ) -> tuple[DocumentRecord, DocumentVersionRecord, bytes]:
        record = await self.get_document(workspace_id, document_id)
        chosen = None
        if version is None:
            for item in record.versions:
                if item.version == record.latest_version:
                    chosen = item
        elif isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise DocumentValidationError("version must be a positive integer")
        else:
            for item in record.versions:
                if item.version == version:
                    chosen = item
        if chosen is None:
            raise DocumentNotFoundError("Document version was not found.")
        spec = _MIME_TO_TYPE[record.content_type]
        blob_path = self._blob_path(
            record.workspace_id,
            record.document_id,
            chosen.version,
            chosen.sha256,
            spec.extension,
        )
        try:
            data = self._read_blob(blob_path)
        except (OSError, WorkspaceError) as exc:
            raise DocumentIntegrityError(
                "Document retrieval failed verification."
            ) from exc
        if len(data) != chosen.byte_size or hashlib.sha256(data).hexdigest() != (
            chosen.sha256
        ):
            raise DocumentIntegrityError("Document retrieval failed verification.")
        return record, chosen, data

    async def delete_document(self, workspace_id: str, document_id: str) -> None:
        workspace_id = _validated_identifier(workspace_id, "workspace_id")
        document_id = _validated_identifier(document_id, "document_id")
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await self._active_document_row(connection, workspace_id, document_id)
            await connection.execute(
                "UPDATE documents SET status = 'deleted', updated_at = ? "
                "WHERE workspace_id = ? AND document_id = ?",
                (_now(), workspace_id, document_id),
            )
            await connection.commit()
        await self.recover()

    async def recover(self) -> None:
        """Sweep crash orphans: staging files and unreferenced blobs.

        Runs under the write lock so it cannot interleave with an in-flight
        upload inside its own ``BEGIN IMMEDIATE`` transaction. Files that
        cannot be removed (for example planted links) are left in place;
        reads keep failing closed on them.
        """

        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            rows = await (
                await connection.execute(
                    """
                    SELECT d.workspace_id, d.document_id, v.version, v.sha256,
                           d.content_type
                    FROM documents d
                    JOIN document_versions v
                        ON v.workspace_id = d.workspace_id
                       AND v.document_id = d.document_id
                    WHERE d.status = 'active'
                    """
                )
            ).fetchall()
            referenced = {
                self._blob_path(
                    row["workspace_id"],
                    row["document_id"],
                    int(row["version"]),
                    row["sha256"],
                    _MIME_TO_TYPE[row["content_type"]].extension,
                )
                for row in rows
            }
            self._sweep(referenced)
            await connection.commit()

    def _sweep(self, referenced: set[str]) -> None:
        workspace = self._blob_workspace()
        for relative in workspace.list_files():
            if relative in referenced:
                continue
            try:
                _filesystem_path(workspace._binary_path(relative)).unlink(
                    missing_ok=True
                )
            except (OSError, WorkspaceError):
                continue
        root = workspace.root
        for directory, _, _ in os.walk(root, topdown=False):
            candidate = Path(directory)
            if candidate == root:
                continue
            # Hashed components make deep paths exceed MAX_PATH on Windows;
            # pruning must use the same extended-path syntax as publication.
            try:
                _filesystem_path(candidate).rmdir()
            except OSError:
                continue


__all__ = [
    "BLOB_ROOT_NAME",
    "DATABASE_NAME",
    "DEFAULT_DOCUMENT_LIMITS",
    "SCHEMA_VERSION",
    "DocumentError",
    "DocumentIntegrityError",
    "DocumentLimits",
    "DocumentNotFoundError",
    "DocumentQuotaError",
    "DocumentRecord",
    "DocumentUnsupportedTypeError",
    "DocumentValidationError",
    "DocumentVersionRecord",
    "DocumentWorkspaceStore",
    "WorkspaceRecord",
    "resolve_document_limits",
]
