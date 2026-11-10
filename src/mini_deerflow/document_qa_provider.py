"""Optional existing LangChain provider seam; never instantiated by default."""

from langchain_core.messages import HumanMessage, SystemMessage

from mini_deerflow.context_budget import render_llm_payload
from mini_deerflow.document_qa import DocumentQAContext, DocumentQADraft
from mini_deerflow.structured_output import (
    StructuredOutputMode,
    create_structured_output_runnable,
)


class LLMDocumentQASynthesizer:
    """Use the repository's structured-output adapter without tool bindings.

    Supply an already configured model explicitly. Credentials are not resolved
    here, and application defaults remain credential-free/extractive in both modes.
    The service owns deadlines, draft validation, source rechecks and fallback.
    """

    def __init__(
        self,
        model: object,
        *,
        structured_output_mode: StructuredOutputMode = "prompt_json",
    ):
        self._runnable = create_structured_output_runnable(
            model,
            DocumentQADraft,
            method="json_schema",
            mode=structured_output_mode,
            max_response_chars=32_768,
        )

    async def synthesize_answer(self, context: DocumentQAContext) -> DocumentQADraft:
        response = await self._runnable.ainvoke(
            [
                SystemMessage(content=context.system_prompt),
                HumanMessage(content=render_llm_payload(context)),
            ]
        )
        return DocumentQADraft.model_validate(response)
