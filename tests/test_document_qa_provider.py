import asyncio
import json

from mini_deerflow.document_qa import DocumentQAService
from mini_deerflow.document_qa_provider import LLMDocumentQASynthesizer
from tests.test_document_retrieval import TXT_MIME, _seed_extraction, _store


def test_prompt_json_provider_uses_real_adapter_and_verified_claims(tmp_path):
    class SyntheticModel:
        def invoke(self, messages):
            return asyncio.run(self.ainvoke(messages))

        async def ainvoke(self, messages):
            assert messages[0].type == "system"
            return json.dumps(
                {
                    "claims": [
                        {
                            "text": "Revenue is 10 million.",
                            "citations": [
                                {"evidence_index": 1, "quote": "Revenue is 10 million."}
                            ],
                        }
                    ]
                }
            )

    store = _store(tmp_path)
    ws = asyncio.run(store.create_workspace("Synthetic provider")).workspace_id
    doc = _seed_extraction(
        store, ws, "fixture.txt", b"Revenue is 10 million.", TXT_MIME
    )[0]
    provider = LLMDocumentQASynthesizer(SyntheticModel())
    answer = asyncio.run(
        DocumentQAService(synthesizer=provider).answer(store, ws, "Revenue")
    )
    assert answer.synthesis_status == "provider"
    assert answer.claims[0].citations[0].document_id == doc
