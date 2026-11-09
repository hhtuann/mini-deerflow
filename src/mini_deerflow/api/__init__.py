"""Local FastAPI transport over the existing safe demo service."""

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import Path as ApiPath
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response

from mini_deerflow.api.dto import (
    ConversationsDTO,
    HealthDTO,
    JobDTO,
    MessageRequest,
    Mode,
    PresentationMetadataDTO,
    PresentationRequestDTO,
    PresentationSlideOutlineDTO,
    ResumeRequest,
    SessionDTO,
    SkillDTO,
    SkillsDTO,
    WorkflowDTO,
    WorkspaceCreateRequest,
    WorkspaceDetailDTO,
)
from mini_deerflow.api.jobs import ApiJobs
from mini_deerflow.api.security import ALLOWED_ORIGINS, LocalRequestBoundary
from mini_deerflow.conversation import ConversationNotFoundError
from mini_deerflow.demo.graph_projection import project_static_workflow
from mini_deerflow.demo.live_backend import LiveDemoBackend
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.service import (
    ContinueDemoCommand,
    DemoRuntimeService,
    ResumeDemoCommand,
    RunDemoCommand,
)
from mini_deerflow.demo.view_models import ChatSessionView
from mini_deerflow.documents import (
    DOCX_MIME,
    PDF_MIME,
    TXT_MIME,
    DocumentIntegrityError,
    DocumentLimits,
    DocumentNotFoundError,
    DocumentQuotaError,
    DocumentRecord,
    DocumentUnsupportedTypeError,
    DocumentValidationError,
    DocumentWorkspaceStore,
    WorkspaceRecord,
    resolve_document_limits,
)
from mini_deerflow.persistence import ThreadNotFoundError
from mini_deerflow.pptx.artifacts import (
    PPTX_MIME,
    ArtifactError,
    ArtifactNotFoundError,
    PptxArtifactStore,
)
from mini_deerflow.pptx.schemas import DeckSpec
from mini_deerflow.pptx.slide_agent import (
    PresentationRequest,
    SlideAgentService,
)
from mini_deerflow.skills import SkillsError, SkillsRegistry

Identifier = Annotated[
    str, ApiPath(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", max_length=128)
]

# Fixed ASCII download names: user filenames are display metadata only and
# are never placed into Content-Disposition headers.
_DOCUMENT_DOWNLOAD_NAMES = {
    PDF_MIME: "document.pdf",
    DOCX_MIME: "document.docx",
    TXT_MIME: "document.txt",
}


def _live_slide_planner_model():
    """Resolve the live planner model lazily so app creation stays offline-safe."""

    from mini_deerflow.config import Settings
    from mini_deerflow.model import create_chat_model

    return create_chat_model(Settings())


def _skills() -> SkillsDTO:
    try:
        metadata = SkillsRegistry().discover()
    except SkillsError:
        return SkillsDTO(skills=(), available=False)
    return SkillsDTO(
        skills=tuple(
            SkillDTO(
                id=item.id,
                description=item.description,
                applicability=item.applicability,
                version=item.version,
            )
            for item in metadata
        ),
        available=True,
    )


def create_app(
    storage_root: str | Path | None = None,
    services: Mapping[Mode, DemoRuntimeService] | None = None,
    presentation_stores: Mapping[Mode, PptxArtifactStore] | None = None,
    slide_services: Mapping[Mode, SlideAgentService] | None = None,
    document_limits: DocumentLimits | None = None,
) -> FastAPI:
    """Create an isolated app; supplied services override either default mode.

    An explicit storage root is the offline directory; live data is under its
    ``live`` child. Defaults preserve the Streamlit demo/live SQLite locations.
    Jobs are process-local and are never replayed on startup. Use one worker.
    Slide generation defaults to the live provider (credentials are resolved
    lazily at generation time); the offline mode requires an injected scripted
    planner through ``slide_services`` and refuses with 503 otherwise.
    Document workspaces add a per-mode SQLite ledger plus hardened blob tree
    under the same roots; startup sweeps crash orphans. The request boundary
    keeps the tight default cap everywhere except the two raw-body upload
    routes, which accept exactly the configured per-file quota.
    """
    root = (
        Path(storage_root) if storage_root is not None else Path(".mini-deerflow/demo")
    )
    live_root = (
        root / "live" if storage_root is not None else Path(".mini-deerflow/live")
    )
    # Resolve quotas before any Settings-consuming component is built so an
    # invalid explicit document override fails closed with the controlled
    # DocumentLimitsError instead of a raw provider-settings error.
    limits = document_limits or resolve_document_limits()
    runtime_services = {
        "offline": DemoRuntimeService(OfflineDemoBackend(root)),
        "live": DemoRuntimeService(LiveDemoBackend(live_root)),
    }
    if services is not None:
        if set(services) - {"offline", "live"}:
            raise ValueError("Unsupported service mode.")
        runtime_services.update(services)
    jobs = ApiJobs()
    stores = {
        "offline": PptxArtifactStore(root, mode="offline"),
        "live": PptxArtifactStore(live_root, mode="live"),
    }
    if presentation_stores is not None:
        if set(presentation_stores) - {"offline", "live"} or any(
            store.mode != mode for mode, store in presentation_stores.items()
        ):
            raise ValueError("Invalid presentation store modes.")
        stores.update(presentation_stores)
    slide_agents: dict[Mode, SlideAgentService] = {
        "live": SlideAgentService(
            runtime_services["live"],
            stores["live"],
            planner_model_factory=_live_slide_planner_model,
        )
    }
    if slide_services is not None:
        if set(slide_services) - {"offline", "live"}:
            raise ValueError("Unsupported slide service mode.")
        slide_agents.update(slide_services)
    document_stores: dict[Mode, DocumentWorkspaceStore] = {
        "offline": DocumentWorkspaceStore(root, limits=limits),
        "live": DocumentWorkspaceStore(live_root, limits=limits),
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for document_store in document_stores.values():
            await document_store.recover()
        try:
            yield
        finally:
            await asyncio.to_thread(jobs.shutdown)

    app = FastAPI(title="Mini DeerFlow local API", lifespan=lifespan, debug=False)
    app.add_middleware(LocalRequestBoundary, upload_max_bytes=limits.max_document_bytes)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_ORIGINS),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse({"detail": "Invalid request."}, status_code=422)

    @app.exception_handler(ConversationNotFoundError)
    @app.exception_handler(ThreadNotFoundError)
    async def not_found(request: Request, error: Exception):
        return JSONResponse({"detail": "Conversation was not found."}, status_code=404)

    @app.exception_handler(DocumentNotFoundError)
    async def document_not_found(request: Request, error: Exception):
        return JSONResponse({"detail": "Document was not found."}, status_code=404)

    @app.exception_handler(DocumentUnsupportedTypeError)
    async def document_unsupported_type(request: Request, error: Exception):
        return JSONResponse(
            {"detail": "Unsupported document type."},
            status_code=415,
        )

    @app.exception_handler(DocumentValidationError)
    async def document_invalid(request: Request, error: Exception):
        return JSONResponse({"detail": "Invalid document request."}, status_code=422)

    @app.exception_handler(DocumentQuotaError)
    async def document_quota(request: Request, error: Exception):
        return JSONResponse(
            {"detail": "Document quota was exceeded."},
            status_code=409,
        )

    @app.exception_handler(DocumentIntegrityError)
    async def document_integrity(request: Request, error: Exception):
        return JSONResponse(
            {"detail": "Document is unavailable."},
            status_code=409,
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception):
        return JSONResponse({"detail": "Operation failed."}, status_code=500)

    async def load(mode: Mode, thread_id: str) -> ChatSessionView:
        # Callers hold submission_lock, including pre-submission checks. Keep
        # this helper lock-free because asyncio.Lock is not reentrant.
        return await runtime_services[mode].load_conversation(thread_id)

    @app.get("/api/health", response_model=HealthDTO)
    async def health() -> HealthDTO:
        return HealthDTO()

    @app.get("/api/skills", response_model=SkillsDTO)
    async def skills() -> SkillsDTO:
        return _skills()

    @app.get("/api/workflow", response_model=WorkflowDTO)
    async def workflow() -> WorkflowDTO:
        return WorkflowDTO(project_static_workflow())

    @app.get("/api/{mode}/conversations", response_model=ConversationsDTO)
    async def conversations(mode: Mode) -> ConversationsDTO:
        # Even reads can initialize SQLite. Exclude both other reads and worker
        # submission for the complete storage operation, including cold startup.
        async with jobs.submission_lock:
            jobs.check_idle()
            return ConversationsDTO(await runtime_services[mode].list_conversations())

    @app.get("/api/{mode}/conversations/{thread_id}", response_model=SessionDTO)
    async def conversation(mode: Mode, thread_id: Identifier) -> SessionDTO:
        async with jobs.submission_lock:
            jobs.check_idle()
            return SessionDTO(await load(mode, thread_id))

    async def submit_message(
        mode: Mode, body: MessageRequest, thread_id: str | None = None
    ) -> JobDTO:
        async with jobs.submission_lock:
            jobs.check_idle()
            if body.skill_id is not None:
                catalog = _skills()
                if not catalog.available:
                    raise HTTPException(503, "Skill catalog is unavailable.")
                if body.skill_id not in {skill.id for skill in catalog.skills}:
                    raise HTTPException(422, "Unknown skill selection.")
            service = runtime_services[mode]
            turn_id = f"turn-{uuid4().hex}"
            if thread_id is None:
                thread_id = f"chat-{uuid4().hex}"
                command = RunDemoCommand(
                    thread_id, body.message, turn_id, body.skill_id
                )

                def task(sink):
                    return asyncio.run(service.run(command, trace_sink=sink))

                operation = "run"
            else:
                session = await load(mode, thread_id)
                if any(
                    turn.status in {"running", "interrupted"} for turn in session.turns
                ):
                    raise HTTPException(409, "Resume the active turn first.")
                followup = ContinueDemoCommand(
                    thread_id, body.message, turn_id, body.skill_id
                )

                def task(sink):
                    return asyncio.run(
                        service.continue_thread(followup, trace_sink=sink)
                    )

                operation = "continue"
            return jobs.submit(mode, operation, thread_id, turn_id, task)

    @app.post("/api/{mode}/conversations", response_model=JobDTO, status_code=202)
    async def create_conversation(mode: Mode, body: MessageRequest) -> JobDTO:
        return await submit_message(mode, body)

    @app.post(
        "/api/{mode}/conversations/{thread_id}/turns",
        response_model=JobDTO,
        status_code=202,
    )
    async def create_turn(
        mode: Mode, thread_id: Identifier, body: MessageRequest
    ) -> JobDTO:
        return await submit_message(mode, body, thread_id)

    @app.post(
        "/api/{mode}/conversations/{thread_id}/resume",
        response_model=JobDTO,
        status_code=202,
    )
    async def resume(
        mode: Mode, thread_id: Identifier, body: ResumeRequest | None = None
    ) -> JobDTO:
        async with jobs.submission_lock:
            jobs.check_idle()
            session = await load(mode, thread_id)
            active = [
                turn
                for turn in session.turns
                if turn.status in {"running", "interrupted"}
            ]
            if len(active) != 1:
                raise HTTPException(409, "No resumable turn exists.")
            command = ResumeDemoCommand(thread_id)
            service = runtime_services[mode]

            def task(sink):
                return asyncio.run(service.resume(command, trace_sink=sink))

            return jobs.submit(mode, "resume", thread_id, active[0].turn_id, task)

    @app.get("/api/{mode}/jobs", response_model=tuple[JobDTO, ...])
    async def list_jobs(mode: Mode) -> tuple[JobDTO, ...]:
        return jobs.list(mode)

    @app.get("/api/{mode}/jobs/{job_id}", response_model=JobDTO)
    async def job(mode: Mode, job_id: UUID) -> JobDTO:
        return jobs.get(mode, str(job_id))

    @app.get("/api/{mode}/conversations/{thread_id}/turns/{turn_id}/artifact")
    async def artifact(
        mode: Mode, thread_id: Identifier, turn_id: Identifier
    ) -> Response:
        async with jobs.submission_lock:
            jobs.check_idle()
            session = await load(mode, thread_id)
        turn = next((turn for turn in session.turns if turn.turn_id == turn_id), None)
        if turn is None or turn.run is None or not turn.run.artifact.available:
            raise HTTPException(404, "Artifact was not found.")
        return Response(
            content=turn.run.artifact.markdown_source,
            media_type="text/markdown",
            headers={
                "Content-Disposition": 'attachment; filename="report.md"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(
        "/api/{mode}/conversations/{thread_id}/turns/{turn_id}/presentation",
        response_model=JobDTO,
        status_code=202,
    )
    async def generate_presentation(
        mode: Mode,
        thread_id: Identifier,
        turn_id: Identifier,
        body: PresentationRequestDTO,
    ) -> JobDTO:
        service = slide_agents.get(mode)
        if service is None:
            raise HTTPException(
                503, "Presentation generation is unavailable in this mode."
            )
        request = PresentationRequest(
            topic=body.topic,
            audience=body.audience,
            language=body.language,
            slide_count=body.slide_count,
            theme=body.theme,
            instructions=body.instructions,
        )
        async with jobs.submission_lock:
            jobs.check_idle()
            session = await load(mode, thread_id)
        turn = next((turn for turn in session.turns if turn.turn_id == turn_id), None)
        if turn is None:
            raise HTTPException(404, "Turn was not found.")
        if turn.status != "completed":
            raise HTTPException(
                409, "Complete or resume the turn before generating slides."
            )

        def task(sink):
            result = asyncio.run(service.generate(thread_id, turn_id, request))
            return result.view

        return jobs.submit(mode, "presentation", thread_id, turn_id, task)

    @app.get(
        "/api/{mode}/conversations/{thread_id}/turns/{turn_id}/presentation/metadata",
        response_model=PresentationMetadataDTO,
    )
    async def presentation_metadata(
        mode: Mode, thread_id: Identifier, turn_id: Identifier
    ) -> PresentationMetadataDTO:
        """Verified read-only artifact facts for restart-safe UI discovery.

        Backed by the store's full retrieve() verification (metadata, digest
        and presentation structure); availability is never inferred from
        ephemeral job history. Safe scalar/outline fields only: no paths, no
        raw spec, no arbitrary URLs.
        """
        async with jobs.submission_lock:
            jobs.check_idle()
            try:
                record, _ = await stores[mode].retrieve(thread_id, turn_id)
            except ArtifactNotFoundError:
                raise HTTPException(404, "Presentation was not found.") from None
            except ArtifactError:
                raise HTTPException(409, "Presentation is unavailable.") from None
        spec = DeckSpec.model_validate_json(record.spec_json)
        return PresentationMetadataDTO(
            title=spec.title,
            theme=spec.theme,
            slide_count=len(spec.slides),
            layouts=tuple(sorted({slide.content.kind for slide in spec.slides})),
            outline=tuple(
                PresentationSlideOutlineDTO(
                    index=index + 1,
                    slide_id=slide.id,
                    layout=slide.content.kind,
                    title=slide.title,
                )
                for index, slide in enumerate(spec.slides)
            ),
            byte_size=record.byte_size,
            sha256=record.sha256,
        )

    @app.get("/api/{mode}/conversations/{thread_id}/turns/{turn_id}/presentation")
    async def presentation(
        mode: Mode, thread_id: Identifier, turn_id: Identifier
    ) -> Response:
        async with jobs.submission_lock:
            jobs.check_idle()
            try:
                record, data = await stores[mode].retrieve(thread_id, turn_id)
            except ArtifactNotFoundError:
                raise HTTPException(404, "Presentation was not found.") from None
            except ArtifactError:
                raise HTTPException(409, "Presentation is unavailable.") from None
        return Response(
            content=data,
            media_type=PPTX_MIME,
            headers={
                "Content-Disposition": 'attachment; filename="presentation.pptx"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "ETag": f'"{record.sha256}"',
            },
        )

    async def _document_content_response(
        mode: Mode,
        workspace_id: str,
        document_id: str,
        version: int | None,
    ) -> Response:
        async with jobs.submission_lock:
            jobs.check_idle()
            record, version_record, data = await document_stores[mode].read_content(
                workspace_id, document_id, version
            )
        # A fixed ASCII download name: user filenames are display metadata
        # only and never enter HTTP headers.
        download = _DOCUMENT_DOWNLOAD_NAMES.get(record.content_type, "document.bin")
        return Response(
            content=data,
            media_type=record.content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{download}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "ETag": f'"{version_record.sha256}"',
                "X-Document-Version": str(version_record.version),
            },
        )

    @app.post("/api/{mode}/workspaces", response_model=WorkspaceRecord, status_code=201)
    async def create_workspace(
        mode: Mode, body: WorkspaceCreateRequest
    ) -> WorkspaceRecord:
        async with jobs.submission_lock:
            jobs.check_idle()
            return await document_stores[mode].create_workspace(body.name)

    @app.get("/api/{mode}/workspaces", response_model=tuple[WorkspaceRecord, ...])
    async def workspaces(mode: Mode) -> tuple[WorkspaceRecord, ...]:
        async with jobs.submission_lock:
            jobs.check_idle()
            return await document_stores[mode].list_workspaces()

    @app.get("/api/{mode}/workspaces/{workspace_id}", response_model=WorkspaceDetailDTO)
    async def workspace_detail(
        mode: Mode, workspace_id: Identifier
    ) -> WorkspaceDetailDTO:
        store = document_stores[mode]
        async with jobs.submission_lock:
            jobs.check_idle()
            workspace = await store.get_workspace(workspace_id)
            documents = await store.list_documents(workspace_id)
        return WorkspaceDetailDTO(workspace=workspace, documents=documents)

    @app.delete("/api/{mode}/workspaces/{workspace_id}", status_code=204)
    async def delete_workspace(mode: Mode, workspace_id: Identifier) -> Response:
        async with jobs.submission_lock:
            jobs.check_idle()
            await document_stores[mode].delete_workspace(workspace_id)
        return Response(status_code=204)

    @app.post(
        "/api/{mode}/workspaces/{workspace_id}/documents",
        response_model=DocumentRecord,
        status_code=201,
    )
    async def upload_document(
        mode: Mode,
        workspace_id: Identifier,
        request: Request,
        filename: str = Query(min_length=1, max_length=256),
    ) -> DocumentRecord:
        data = await request.body()
        content_type = (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        async with jobs.submission_lock:
            jobs.check_idle()
            return await document_stores[mode].upload_document(
                workspace_id,
                filename=filename,
                content_type=content_type,
                data=data,
            )

    @app.get(
        "/api/{mode}/workspaces/{workspace_id}/documents",
        response_model=tuple[DocumentRecord, ...],
    )
    async def documents(
        mode: Mode, workspace_id: Identifier
    ) -> tuple[DocumentRecord, ...]:
        async with jobs.submission_lock:
            jobs.check_idle()
            return await document_stores[mode].list_documents(workspace_id)

    @app.get(
        "/api/{mode}/workspaces/{workspace_id}/documents/{document_id}",
        response_model=DocumentRecord,
    )
    async def document(
        mode: Mode, workspace_id: Identifier, document_id: Identifier
    ) -> DocumentRecord:
        async with jobs.submission_lock:
            jobs.check_idle()
            return await document_stores[mode].get_document(workspace_id, document_id)

    @app.post(
        "/api/{mode}/workspaces/{workspace_id}/documents/{document_id}/versions",
        response_model=DocumentRecord,
    )
    async def upload_document_version(
        mode: Mode,
        workspace_id: Identifier,
        document_id: Identifier,
        request: Request,
        filename: str | None = Query(default=None, max_length=256),
    ) -> DocumentRecord:
        data = await request.body()
        content_type = (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        async with jobs.submission_lock:
            jobs.check_idle()
            return await document_stores[mode].upload_version(
                workspace_id,
                document_id,
                filename=filename,
                content_type=content_type,
                data=data,
            )

    @app.get(
        "/api/{mode}/workspaces/{workspace_id}/documents/{document_id}/content",
        response_model=None,
    )
    async def document_content(
        mode: Mode, workspace_id: Identifier, document_id: Identifier
    ) -> Response:
        return await _document_content_response(mode, workspace_id, document_id, None)

    @app.get(
        "/api/{mode}/workspaces/{workspace_id}/documents/{document_id}"
        "/content/{version}",
        response_model=None,
    )
    async def document_content_version(
        mode: Mode,
        workspace_id: Identifier,
        document_id: Identifier,
        version: int = ApiPath(ge=1, le=1_000_000),
    ) -> Response:
        return await _document_content_response(
            mode, workspace_id, document_id, version
        )

    @app.delete(
        "/api/{mode}/workspaces/{workspace_id}/documents/{document_id}",
        status_code=204,
    )
    async def delete_document(
        mode: Mode, workspace_id: Identifier, document_id: Identifier
    ) -> Response:
        async with jobs.submission_lock:
            jobs.check_idle()
            await document_stores[mode].delete_document(workspace_id, document_id)
        return Response(status_code=204)

    return app


__all__ = ["create_app"]
