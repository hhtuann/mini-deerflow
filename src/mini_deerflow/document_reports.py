"""D5 grounded report specifications and immutable local revision ledger.

Reports are inert extracts, not executable documents. Like the D1 metadata
ledger, publication uses SQLite transactions; keeping bounded report bytes in
that transaction avoids a second filesystem publication/recovery protocol.
Checksums detect corruption, not a malicious owner of the local storage root.
Historical reads verify stored integrity only, never current source availability.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mini_deerflow.document_extraction import DocumentChunk, SourceLocator
from mini_deerflow.document_qa import (
    DocumentClaim,
    DocumentQAService,
    DocumentQAValidationError,
    DocumentSourceSnapshot,
)
from mini_deerflow.document_retrieval import (
    RetrievalDeadlineExceededError,
    RetrievalError,
)
from mini_deerflow.documents import (
    DocumentError,
    DocumentValidationError,
    DocumentWorkspaceStore,
)
from mini_deerflow.workspace import Workspace, WorkspaceError, _filesystem_path

Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]
SectionIdentifier = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
]
Title = Annotated[str, Field(min_length=1, max_length=200)]
Question = Annotated[str, Field(min_length=1, max_length=400)]


class ReportError(RuntimeError):
    """Controlled report failure; messages contain no local paths or evidence."""


class ReportNotFoundError(ReportError):
    """No report in the exact requested workspace/version."""


class ReportConflictError(ReportError):
    """The requested base is no longer the latest revision."""


class ReportIntegrityError(ReportError):
    """Persisted report data failed verification."""


class ReportValidationError(ReportError):
    """The report request, source state, or quota is invalid."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReportSectionRequest(_Frozen):
    section_id: SectionIdentifier
    title: Title
    question: Question

    @model_validator(mode="after")
    def nonblank(self):
        if not self.title.strip() or not self.question.strip():
            raise ValueError("section text must not be blank")
        return self


class ReportRequest(_Frozen):
    title: Title
    kind: Literal["summary", "comparison", "report"]
    document_ids: tuple[Identifier, ...] = Field(
        min_length=1, max_length=50, strict=False
    )
    sections: tuple[ReportSectionRequest, ...] = Field(
        min_length=1, max_length=20, strict=False
    )

    @model_validator(mode="after")
    def unique(self):
        if not self.title.strip() or len(set(self.document_ids)) != len(
            self.document_ids
        ):
            raise ValueError("title or source scope is invalid")
        if len({item.section_id for item in self.sections}) != len(self.sections):
            raise ValueError("section ids must be unique")
        return self


class ReportRevisionRequest(_Frozen):
    base_version: int = Field(ge=1)
    sections: tuple[ReportSectionRequest, ...] = Field(
        default=(), max_length=20, strict=False
    )
    approved_section_ids: tuple[SectionIdentifier, ...] = Field(
        default=(), max_length=20, strict=False
    )

    @model_validator(mode="after")
    def unique(self):
        selected = {item.section_id for item in self.sections}
        approved = set(self.approved_section_ids)
        if len(selected) != len(self.sections) or len(approved) != len(
            self.approved_section_ids
        ):
            raise ValueError("section ids must be unique")
        if selected & approved or not (selected or approved):
            raise ValueError(
                "regenerated sections cannot be approved in the same revision"
            )
        return self


class ReportSection(_Frozen):
    section_id: SectionIdentifier
    title: Title
    question: Question
    claims: tuple[DocumentClaim, ...] = Field(default=(), max_length=50)
    limitations: tuple[Annotated[str, Field(max_length=2000)], ...] = Field(
        default=(), max_length=50
    )
    source_snapshot: tuple[DocumentSourceSnapshot, ...] = Field(
        default=(), max_length=50
    )
    approved: bool = False
    origin_version: int = Field(ge=1)


class ReportSpec(_Frozen):
    report_id: str = Field(pattern=r"^report-[0-9a-f]{32}$")
    workspace_id: Identifier
    version: int = Field(ge=1)
    parent_version: int | None
    title: Title
    kind: Literal["summary", "comparison", "report"]
    document_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    sections: tuple[ReportSection, ...] = Field(min_length=1, max_length=20)
    created_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def lineage(self):
        if self.parent_version != (None if self.version == 1 else self.version - 1):
            raise ValueError("invalid revision parent")
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("duplicate source")
        if len({s.section_id for s in self.sections}) != len(self.sections):
            raise ValueError("duplicate section")
        for section in self.sections:
            snapshot = {
                item.document_id: item.version for item in section.source_snapshot
            }
            if (
                section.origin_version > self.version
                or set(snapshot) != set(self.document_ids)
                or len(snapshot) != len(section.source_snapshot)
            ):
                raise ValueError("invalid section provenance")
            for claim in section.claims:
                for citation in claim.citations:
                    if (
                        snapshot.get(citation.document_id) != citation.version
                        or claim.text not in citation.quote
                    ):
                        raise ValueError("claim outside provenance")
        return self


@dataclass(frozen=True)
class ReportLimits:
    max_reports_per_workspace: int = 50
    max_versions_per_report: int = 20
    max_revision_bytes: int = 1_000_000
    max_workspace_bytes: int = 20_000_000

    def __post_init__(self):
        for value in vars(self).values():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("report limits must be positive integers")


class ReportStore:
    """Single ledger commit is the visibility point; rollback leaves no revision.

    Bounded JSON BLOBs and checksums live in the same row. There are no report
    filenames, staging artifacts, or pointers that could expose partial bytes.
    SQLite recovery rolls back an interrupted uncommitted publication.
    """

    def __init__(self, storage_root: str | Path, *, limits: ReportLimits | None = None):
        root = Path(storage_root).expanduser()
        if root.is_symlink() or root.is_junction():
            raise ReportIntegrityError("Report storage root must not be a link.")
        self._root = root
        self.limits = limits or ReportLimits()
        self.database_path = root / "reports.sqlite"

    @asynccontextmanager
    async def _connection(self):
        try:
            if self._root.is_symlink() or self._root.is_junction():
                raise ReportIntegrityError("Report storage root must not be a link.")
            # Reuse D1's trusted-local SQLite ledger convention, rejecting link
            # aliases before opening the database (the database is not a blob).
            workspace = Workspace(self._root)
            workspace._binary_path("reports.sqlite")
            for name in (
                "reports.sqlite",
                "reports.sqlite-journal",
                "reports.sqlite-wal",
                "reports.sqlite-shm",
            ):
                path = workspace._binary_path(name)
                if path.exists() and os.stat(path).st_nlink != 1:
                    raise ReportIntegrityError("Report ledger aliases are not allowed.")
            async with aiosqlite.connect(
                str(_filesystem_path(self.database_path))
            ) as connection:
                connection.row_factory = aiosqlite.Row
                await connection.execute("PRAGMA busy_timeout = 10000")
                await connection.execute(
                    "CREATE TABLE IF NOT EXISTS report_store_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL)"
                )
                await connection.execute(
                    "INSERT OR IGNORE INTO report_store_meta VALUES (1,1)"
                )
                row = await (
                    await connection.execute(
                        "SELECT schema_version FROM report_store_meta WHERE singleton=1"
                    )
                ).fetchone()
                if row is None or row[0] != 1:
                    raise ReportIntegrityError("Report ledger schema is not supported.")
                await connection.execute(
                    "CREATE TABLE IF NOT EXISTS report_revisions (workspace_id TEXT NOT NULL, report_id TEXT NOT NULL, version INTEGER NOT NULL, payload BLOB NOT NULL, checksum TEXT NOT NULL, PRIMARY KEY(workspace_id, report_id, version))"
                )
                await connection.commit()
                yield connection
        except (aiosqlite.Error, OSError, WorkspaceError) as error:
            raise ReportIntegrityError("Report ledger operation failed.") from error

    def _decode(self, row) -> ReportSpec:
        try:
            payload = bytes(row["payload"])
            if (
                len(payload) > self.limits.max_revision_bytes
                or hashlib.sha256(payload).hexdigest() != row["checksum"]
            ):
                raise ValueError("checksum")
            report = ReportSpec.model_validate_json(payload)
            if (report.workspace_id, report.report_id, report.version) != (
                row["workspace_id"],
                row["report_id"],
                row["version"],
            ):
                raise ValueError("identity")
            return report
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ReportIntegrityError("Stored report failed verification.") from None

    async def _rows(self, connection, workspace_id, report_id):
        # Inspect byte lengths before fetching potentially corrupt oversized data.
        sizes = await (
            await connection.execute(
                "SELECT version, length(payload) AS size FROM report_revisions WHERE workspace_id=? AND report_id=? ORDER BY version",
                (workspace_id, report_id),
            )
        ).fetchall()
        if not sizes:
            raise ReportNotFoundError("Report was not found.")
        if len(sizes) > self.limits.max_versions_per_report or any(
            row["version"] != index
            or not 0 < row["size"] <= self.limits.max_revision_bytes
            for index, row in enumerate(sizes, 1)
        ):
            raise ReportIntegrityError("Report history failed verification.")
        rows = await (
            await connection.execute(
                "SELECT * FROM report_revisions WHERE workspace_id=? AND report_id=? ORDER BY version",
                (workspace_id, report_id),
            )
        ).fetchall()
        return tuple(self._decode(row) for row in rows)

    async def get(
        self, workspace_id: str, report_id: str, version: int | None = None
    ) -> ReportSpec:
        if version is not None and (
            isinstance(version, bool) or not isinstance(version, int) or version < 1
        ):
            raise ReportValidationError("Invalid report version.")
        history = await self.history(workspace_id, report_id)
        if version is None:
            return history[-1]
        if version > len(history):
            raise ReportNotFoundError("Report version was not found.")
        return history[version - 1]

    async def history(
        self, workspace_id: str, report_id: str
    ) -> tuple[ReportSpec, ...]:
        async with self._connection() as connection:
            await connection.execute("BEGIN")
            return await self._rows(connection, workspace_id, report_id)

    async def _publish(self, report: ReportSpec) -> ReportSpec:
        try:
            report = ReportSpec.model_validate_json(report.model_dump_json())
        except ValidationError:
            raise ReportValidationError("Invalid report specification.") from None
        payload = report.model_dump_json().encode()
        if len(payload) > self.limits.max_revision_bytes:
            raise ReportValidationError("Report revision byte quota exceeded.")
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await (
                await connection.execute(
                    "SELECT MAX(version) AS latest FROM report_revisions WHERE workspace_id=? AND report_id=?",
                    (report.workspace_id, report.report_id),
                )
            ).fetchone()
            if row["latest"] != report.parent_version:
                raise ReportConflictError("Report base version is no longer current.")
            if report.version > self.limits.max_versions_per_report:
                raise ReportValidationError("Report history quota exceeded.")
            usage = await (
                await connection.execute(
                    "SELECT COUNT(DISTINCT report_id) AS reports, COALESCE(SUM(length(payload)),0) AS bytes FROM report_revisions WHERE workspace_id=?",
                    (report.workspace_id,),
                )
            ).fetchone()
            if (
                report.version == 1
                and usage["reports"] >= self.limits.max_reports_per_workspace
            ) or usage["bytes"] + len(payload) > self.limits.max_workspace_bytes:
                raise ReportValidationError("Report workspace quota exceeded.")
            await connection.execute(
                "INSERT INTO report_revisions VALUES (?,?,?,?,?)",
                (
                    report.workspace_id,
                    report.report_id,
                    report.version,
                    payload,
                    hashlib.sha256(payload).hexdigest(),
                ),
            )
            await connection.commit()
        return report

    async def recover(self) -> None:
        """SQLite recovers interrupted transactions; verify committed envelopes."""
        async with self._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            check = await (await connection.execute("PRAGMA quick_check")).fetchone()
            if check[0] != "ok":
                raise ReportIntegrityError("Report ledger failed verification.")
            keys = await (
                await connection.execute(
                    "SELECT DISTINCT workspace_id, report_id FROM report_revisions"
                )
            ).fetchall()
            for key in keys:
                await self._rows(connection, key["workspace_id"], key["report_id"])
            await connection.commit()


class ReportWriterAgent:
    def __init__(
        self,
        documents: DocumentWorkspaceStore,
        reports: ReportStore,
        qa: DocumentQAService,
    ):
        self.documents, self.reports, self.qa = documents, reports, qa

    async def _generate(self, workspace_id, document_ids, requests, version):
        generated = {}
        for request in requests:
            answer = await self.qa.answer(
                self.documents,
                workspace_id,
                request.question,
                document_ids=document_ids,
            )
            if answer.workspace_id != workspace_id:
                raise ReportIntegrityError("QA workspace mismatch.")
            generated[request.section_id] = ReportSection(
                section_id=request.section_id,
                title=request.title,
                question=request.question,
                claims=answer.claims,
                limitations=answer.limitations,
                source_snapshot=answer.source_snapshot,
                approved=False,
                origin_version=version,
            )
        return generated

    async def _verify_and_publish(self, report, generated):
        # Use one D1 write transaction for final validation: calling public D1
        # readers here would open another connection and initialize its schema,
        # contending with our write lock. Only selected regenerated evidence is
        # checked; historical untouched sections are deliberately not refreshed.
        async with self.documents._connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await self.documents._active_workspace_row(connection, report.workspace_id)
            for section in generated.values():
                versions = {
                    item.document_id: item.version for item in section.source_snapshot
                }
                if set(versions) != set(report.document_ids):
                    raise ReportIntegrityError("QA source scope mismatch.")
                for document_id, version in versions.items():
                    document = await self.documents._active_document_row(
                        connection, report.workspace_id, document_id
                    )
                    if document["latest_version"] != version:
                        raise ReportValidationError(
                            "Report sources changed during generation."
                        )
                for claim in section.claims:
                    for citation in claim.citations:
                        if (
                            versions.get(citation.document_id) != citation.version
                            or claim.text not in citation.quote
                        ):
                            raise ReportIntegrityError("QA claim provenance mismatch.")
                        row = await (
                            await connection.execute(
                                "SELECT c.* FROM document_chunks c JOIN document_extractions e USING(workspace_id,document_id,version) JOIN document_versions v USING(workspace_id,document_id,version) WHERE c.workspace_id=? AND c.document_id=? AND c.version=? AND c.chunk_id=? AND e.status='completed' AND e.source_sha256=v.sha256",
                                (
                                    report.workspace_id,
                                    citation.document_id,
                                    citation.version,
                                    citation.chunk_id,
                                ),
                            )
                        ).fetchone()
                        if row is None:
                            raise ReportValidationError(
                                "Report citation is no longer current."
                            )
                        chunk = DocumentChunk(
                            chunk_id=row["chunk_id"],
                            index=row["chunk_index"],
                            kind=row["kind"],
                            text=row["text"],
                            char_start=row["char_start"],
                            char_end=row["char_end"],
                            locator=SourceLocator.model_validate_json(
                                row["locator_json"]
                            ),
                        )
                        if (
                            chunk.locator != citation.locator
                            or citation.quote not in chunk.text
                        ):
                            raise ReportIntegrityError(
                                "QA citation failed verification."
                            )
            return await self.reports._publish(report)

    async def create(self, workspace_id: str, request: ReportRequest) -> ReportSpec:
        try:
            request = ReportRequest.model_validate_json(request.model_dump_json())
            await self.documents.get_workspace(workspace_id)
            generated = await self._generate(
                workspace_id, request.document_ids, request.sections, 1
            )
            report = ReportSpec(
                report_id=f"report-{uuid4().hex}",
                workspace_id=workspace_id,
                version=1,
                parent_version=None,
                title=request.title,
                kind=request.kind,
                document_ids=request.document_ids,
                sections=tuple(generated.values()),
                created_at=datetime.now(UTC).isoformat(),
            )
            return await self._verify_and_publish(report, generated)
        except RetrievalDeadlineExceededError:
            raise
        except (
            DocumentError,
            DocumentValidationError,
            DocumentQAValidationError,
            RetrievalError,
            ValidationError,
        ):
            raise ReportValidationError(
                "Report source or request is not valid."
            ) from None

    async def revise(
        self, workspace_id: str, report_id: str, request: ReportRevisionRequest
    ) -> ReportSpec:
        try:
            request = ReportRevisionRequest.model_validate_json(
                request.model_dump_json()
            )
            await self.documents.get_workspace(workspace_id)
            current = await self.reports.get(workspace_id, report_id)
            if current.version != request.base_version:
                raise ReportConflictError("Report base version is no longer current.")
            known = {section.section_id for section in current.sections}
            if (
                any(item.section_id not in known for item in request.sections)
                or not set(request.approved_section_ids) <= known
            ):
                raise ReportValidationError("Unknown report section.")
            version = current.version + 1
            generated = await self._generate(
                workspace_id, current.document_ids, request.sections, version
            )
            sections = tuple(
                generated[section.section_id]
                if section.section_id in generated
                else section.model_copy(update={"approved": True})
                if section.section_id in request.approved_section_ids
                else section
                for section in current.sections
            )
            report = current.model_copy(
                update={
                    "version": version,
                    "parent_version": current.version,
                    "sections": sections,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
            return await self._verify_and_publish(report, generated)
        except RetrievalDeadlineExceededError:
            raise
        except (
            DocumentError,
            DocumentValidationError,
            DocumentQAValidationError,
            RetrievalError,
            ValidationError,
        ):
            raise ReportValidationError(
                "Report source or request is not valid."
            ) from None
