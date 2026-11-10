"""The model cannot replace the caller's document source authority."""

import asyncio

import pytest
from pydantic import ValidationError

from mini_deerflow.document_qa import DocumentQAService
from mini_deerflow.tools.documents import DocumentQuestionInput, DocumentQuestionTool
from mini_deerflow.tools.registry import ToolRegistry
from tests.test_document_retrieval import TXT_MIME, _seed_extraction, _store


def test_agent_tool_pins_scope_and_keeps_document_provenance(tmp_path):
    async def scenario():
        store = _store(tmp_path)
        ws = (await store.create_workspace("Synthetic tool")).workspace_id
        return store, ws

    store, ws = asyncio.run(scenario())
    selected = _seed_extraction(
        store, ws, "a.txt", b"Solar budget is 10 credits.", TXT_MIME
    )[0]
    _seed_extraction(
        store, ws, "b.txt", b"Solar budget PRIVATE_UNSELECTED is 999 credits.", TXT_MIME
    )
    tool = DocumentQuestionTool(store, ws, (selected,), service=DocumentQAService())
    registry = ToolRegistry([tool])
    with pytest.raises(ValidationError):
        DocumentQuestionInput.model_validate(
            {"question": "solar budget", "document_ids": ["other"]}
        )
    result = asyncio.run(
        registry.get("document_question").run(
            DocumentQuestionInput(question="solar budget")
        )
    )
    assert result.success
    assert result.data["status"] == "answered"
    assert {
        citation["document_id"]
        for claim in result.data["claims"]
        for citation in claim["citations"]
    } == {selected}
    assert "PRIVATE_UNSELECTED" not in str(result.data)


def test_runner_allows_service_timeout_fallback(tmp_path):
    from mini_deerflow.tools.runner import ToolRunner

    class StalledProvider:
        async def synthesize_answer(self, context):
            await asyncio.sleep(1)

    store = _store(tmp_path)
    ws = asyncio.run(store.create_workspace("Synthetic timeout")).workspace_id
    doc = _seed_extraction(
        store, ws, "fixture.txt", b"Revenue is 10 million.", TXT_MIME
    )[0]
    service = DocumentQAService(
        synthesizer=StalledProvider(), provider_timeout_seconds=0.01
    )
    tool = DocumentQuestionTool(store, ws, (doc,), service=service)
    result = asyncio.run(
        ToolRunner(ToolRegistry([tool])).run(
            "document_question", {"question": "Revenue"}
        )
    )
    assert result.success
    assert result.data["synthesis_status"] == "provider_failed"
    assert result.data["claims"][0]["text"] == "Revenue is 10 million."
