"""Explicit HTTP boundary: only public, frozen demo projections are nested here."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, StringConstraints

from mini_deerflow.demo.graph_projection import ExecutionGraphProjection
from mini_deerflow.demo.view_models import (
    ChatSessionView,
    ConversationSummaryView,
    DemoRunView,
    TraceEventView,
)

Mode = Literal["live", "offline"]
Message = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=10, max_length=1000
    ),
]
SkillId = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=64
    ),
]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MessageRequest(DTO):
    message: Message
    skill_id: SkillId | None = None


class PresentationRequestDTO(DTO):
    topic: Message
    audience: Annotated[
        str,
        StringConstraints(
            strict=True, strip_whitespace=True, min_length=3, max_length=120
        ),
    ]
    language: Literal["vi", "en"]
    slide_count: Annotated[int, Field(strict=True, ge=5, le=12)] = 8
    theme: Literal["light", "dark", "academic"] = "light"
    instructions: Annotated[str, StringConstraints(strict=True, max_length=500)] = ""


class ResumeRequest(DTO):
    pass


class HealthDTO(DTO):
    status: Literal["ok"] = "ok"
    modes: tuple[Mode, ...] = ("live", "offline")


class SkillDTO(DTO):
    id: str
    description: str
    applicability: str
    version: str


class SkillsDTO(DTO):
    skills: tuple[SkillDTO, ...]
    available: bool


class JobDTO(DTO):
    job_id: str
    mode: Mode
    thread_id: str
    turn_id: str | None
    operation: Literal["run", "continue", "resume", "presentation"]
    status: Literal["queued", "running", "completed", "failed"]
    error_message: str | None = None
    traces: tuple[TraceEventView, ...] = ()
    execution_graph: ExecutionGraphProjection
    view: DemoRunView | None = None


class ConversationsDTO(RootModel[tuple[ConversationSummaryView, ...]]):
    pass


class SessionDTO(RootModel[ChatSessionView]):
    pass


class WorkflowDTO(RootModel[ExecutionGraphProjection]):
    pass
