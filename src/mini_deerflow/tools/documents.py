"""Document Q&A tool with workspace/source authority pinned by its caller."""

from pydantic import Field

from mini_deerflow.document_qa import DocumentQAService
from mini_deerflow.documents import DocumentWorkspaceStore
from mini_deerflow.tools.contracts import ToolInput, ToolResult


class DocumentQuestionInput(ToolInput):
    question: str = Field(min_length=1, max_length=400)


class DocumentQuestionTool:
    """Opt-in existing ToolRegistry tool; model input cannot change source scope.

    Results remain document provenance, not web EvidenceRecords. In particular,
    this adapter never fabricates URLs to enter the web-only citation validator.
    """

    name = "document_question"
    description = (
        "Answer from the caller-selected workspace documents with verified citations."
    )
    input_model = DocumentQuestionInput
    timeout_seconds = 30.0
    idempotent = True

    def __init__(
        self,
        store: DocumentWorkspaceStore,
        workspace_id: str,
        document_ids: tuple[str, ...],
        *,
        service: DocumentQAService | None = None,
    ):
        if not document_ids or len(set(document_ids)) != len(document_ids):
            raise ValueError("Document source selection is invalid.")
        self._store = store
        self._workspace_id = workspace_id
        self._document_ids = tuple(document_ids)
        self._service = service or DocumentQAService()
        self.timeout_seconds = (
            self._service.provider_timeout_seconds
            + self._service.retrieval_limits.deadline_seconds
            + 30.0
        )

    async def run(self, tool_input: DocumentQuestionInput) -> ToolResult:
        answer = await self._service.answer(
            self._store,
            self._workspace_id,
            tool_input.question,
            document_ids=self._document_ids,
        )
        return ToolResult.ok(answer.model_dump(mode="json"))
