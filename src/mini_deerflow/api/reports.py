"""Local-only report routes; immutable history is not current source authority."""

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response

from mini_deerflow.api.dto import Mode
from mini_deerflow.api.jobs import ApiJobs
from mini_deerflow.document_qa import DocumentQAService, DocumentQAValidationError
from mini_deerflow.document_reports import (
    ReportConflictError,
    ReportIntegrityError,
    ReportNotFoundError,
    ReportRequest,
    ReportRevisionRequest,
    ReportSpec,
    ReportStore,
    ReportValidationError,
    ReportWriterAgent,
)
from mini_deerflow.document_retrieval import RetrievalDeadlineExceededError
from mini_deerflow.documents import DocumentWorkspaceStore
from mini_deerflow.report_markdown import render_report_markdown

Identifier = Annotated[
    str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", max_length=128)
]
Version = Annotated[int, Path(ge=1, le=100)]


def report_router(
    documents: Mapping[Mode, DocumentWorkspaceStore],
    reports: Mapping[Mode, ReportStore],
    qa: Mapping[Mode, DocumentQAService],
    jobs: ApiJobs,
) -> APIRouter:
    router = APIRouter(prefix="/api/{mode}/workspaces/{workspace_id}/reports")
    writers = {
        mode: ReportWriterAgent(documents[mode], reports[mode], qa[mode])
        for mode in documents
    }

    async def execute(mode, workspace_id, operation):
        async with jobs.submission_lock:
            jobs.check_idle()
            await documents[mode].get_workspace(workspace_id)
            try:
                return await operation()
            except ReportNotFoundError:
                raise HTTPException(404, "Report was not found.") from None
            except (ReportConflictError, ReportIntegrityError):
                raise HTTPException(409, "Report changed or is unavailable.") from None
            except (ReportValidationError, DocumentQAValidationError):
                raise HTTPException(
                    422, "Invalid report request or unavailable sources."
                ) from None
            except RetrievalDeadlineExceededError:
                raise HTTPException(
                    503, "Report retrieval deadline was exceeded."
                ) from None

    @router.post("", response_model=ReportSpec, status_code=201)
    async def create(mode: Mode, workspace_id: Identifier, body: ReportRequest):
        return await execute(
            mode, workspace_id, lambda: writers[mode].create(workspace_id, body)
        )

    @router.get("/{report_id}", response_model=ReportSpec)
    async def latest(mode: Mode, workspace_id: Identifier, report_id: Identifier):
        return await execute(
            mode, workspace_id, lambda: reports[mode].get(workspace_id, report_id)
        )

    @router.get("/{report_id}/versions", response_model=tuple[ReportSpec, ...])
    async def history(mode: Mode, workspace_id: Identifier, report_id: Identifier):
        return await execute(
            mode, workspace_id, lambda: reports[mode].history(workspace_id, report_id)
        )

    @router.get("/{report_id}/versions/{version}", response_model=ReportSpec)
    async def versioned(
        mode: Mode, workspace_id: Identifier, report_id: Identifier, version: Version
    ):
        return await execute(
            mode,
            workspace_id,
            lambda: reports[mode].get(workspace_id, report_id, version),
        )

    @router.post("/{report_id}/revisions", response_model=ReportSpec, status_code=201)
    async def revise(
        mode: Mode,
        workspace_id: Identifier,
        report_id: Identifier,
        body: ReportRevisionRequest,
    ):
        return await execute(
            mode,
            workspace_id,
            lambda: writers[mode].revise(workspace_id, report_id, body),
        )

    @router.get("/{report_id}/versions/{version}/markdown")
    async def markdown(
        mode: Mode, workspace_id: Identifier, report_id: Identifier, version: Version
    ):
        report = await execute(
            mode,
            workspace_id,
            lambda: reports[mode].get(workspace_id, report_id, version),
        )
        return Response(
            render_report_markdown(report),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="report.md"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    return router
