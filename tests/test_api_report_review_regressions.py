"""Consumer-visible discriminators from fresh independent D5 review."""

from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from mini_deerflow.document_qa import DocumentQAService
from mini_deerflow.document_retrieval import (
    RetrievalDeadlineExceededError,
    RetrievalLimits,
)
from tests.test_api_document_qa import _document, _workspace


def _body(doc):
    return {
        "title": "Synthetic report",
        "kind": "summary",
        "document_ids": [doc],
        "sections": [
            {"section_id": "revenue", "title": "Revenue", "question": "Revenue"}
        ],
    }


def test_configured_qa_nine_claims_survive_report_creation(tmp_path):
    # Nine explicitly selected documents make nine small real D2 chunks; all
    # fit default QA item/text/total limits and configured retrieval max_hits9.
    with TestClient(
        create_app(tmp_path, retrieval_limits=RetrievalLimits(max_hits=9))
    ) as client:
        ws = _workspace(client)
        docs = [
            _document(client, ws, f"Revenue is {i} synthetic credits.")
            for i in range(9)
        ]
        body = _body(docs[0])
        body["document_ids"] = docs
        qa = client.post(
            f"/api/offline/workspaces/{ws}/questions",
            json={"question": "Revenue", "document_ids": docs},
        )
        assert qa.status_code == 200
        assert len(qa.json()["claims"]) == 9
        report = client.post(f"/api/offline/workspaces/{ws}/reports", json=body)
        assert report.status_code == 201, report.text
        claims = report.json()["sections"][0]["claims"]
        assert len(claims) == 9
        assert {
            citation["document_id"]
            for claim in claims
            for citation in claim["citations"]
        } == set(docs)


def test_retrieval_deadline_is_service_unavailable_for_create_and_revision(tmp_path):
    class DeadlineRetriever:
        async def retrieve(self, *args, **kwargs):
            raise RetrievalDeadlineExceededError()

    qa = DocumentQAService()
    app = create_app(tmp_path, document_qa_services={"offline": qa})
    with TestClient(app) as client:
        ws = _workspace(client)
        doc = _document(client, ws, "Revenue is 10 synthetic credits.")
        root = f"/api/offline/workspaces/{ws}/reports"
        first = client.post(root, json=_body(doc))
        assert first.status_code == 201, first.text
        report_id = first.json()["report_id"]
        qa.retriever = DeadlineRetriever()
        failed_create = client.post(root, json=_body(doc))
        assert failed_create.status_code == 503, failed_create.text
        failed_revision = client.post(
            f"{root}/{report_id}/revisions",
            json={"base_version": 1, "sections": _body(doc)["sections"]},
        )
        assert failed_revision.status_code == 503, failed_revision.text
        assert len(client.get(f"{root}/{report_id}/versions").json()) == 1
