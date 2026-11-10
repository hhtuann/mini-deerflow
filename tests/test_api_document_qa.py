"""Synthetic HTTP document-answer grounding and turn scope regressions."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from mini_deerflow.context_budget import ContextBudget, render_llm_payload
from mini_deerflow.document_qa import DocumentQAService


def _workspace(client):
    response = client.post("/api/offline/workspaces", json={"name": "Synthetic QA"})
    assert response.status_code == 201
    return response.json()["workspace_id"]


def _extract(client, ws, doc):
    response = client.post(f"/api/offline/workspaces/{ws}/documents/{doc}/extraction")
    assert response.status_code == 202, response.text
    job = response.json()["job_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = client.get(f"/api/offline/jobs/{job}").json()
        if result["status"] in ("completed", "failed"):
            assert result["status"] == "completed", result
            return
        time.sleep(0.02)
    raise AssertionError("Synthetic extraction timed out")


def _document(client, ws, text):
    result = client.post(
        f"/api/offline/workspaces/{ws}/documents?filename=fixture.txt",
        content=text.encode(),
        headers={"Content-Type": "text/plain"},
    )
    assert result.status_code == 201, result.text
    doc = result.json()["document_id"]
    _extract(client, ws, doc)
    return doc


def test_answer_citations_and_changed_selection(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        ws = _workspace(client)
        a = _document(client, ws, "Synthetic project budget is 10 credits.")
        b = _document(client, ws, "Synthetic project budget is 20 credits.")
        url = f"/api/offline/workspaces/{ws}/questions"
        response = client.post(
            url,
            json={"question": "What is the project budget?", "document_ids": [a, b]},
        )
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["status"] == "answered"
        cited = {
            citation["document_id"]
            for claim in answer["claims"]
            for citation in claim["citations"]
        }
        assert cited == {a, b}
        for claim in answer["claims"]:
            for citation in claim["citations"]:
                resolved = client.get(
                    f"/api/offline/workspaces/{ws}/documents/{citation['document_id']}/chunks/{citation['chunk_id']}",
                    params={"version": citation["version"]},
                )
                assert resolved.status_code == 200
                assert citation["quote"] in resolved.json()["text"]
                assert citation["locator"] == resolved.json()["locator"]
        followup = client.post(
            url,
            json={
                "question": "And the project budget now?",
                "document_ids": [b],
                "previous_answer": answer["continuation"],
            },
        )
        assert followup.status_code == 200, followup.text
        current = followup.json()
        assert current["source_changes"]
        assert {
            c["document_id"] for claim in current["claims"] for c in claim["citations"]
        } == {b}


def test_abstention_and_wrong_workspace_context(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        ws = _workspace(client)
        doc = _document(client, ws, "Synthetic solar panels produce electricity.")
        url = f"/api/offline/workspaces/{ws}/questions"
        missing = client.post(
            url, json={"question": "quasar taxonomy", "document_ids": [doc]}
        )
        assert missing.status_code == 200, missing.text
        assert missing.json()["status"] == "abstained"
        assert missing.json()["claims"] == []
        other = _workspace(client)
        leaked = client.post(
            f"/api/offline/workspaces/{other}/questions",
            json={
                "question": "solar panels",
                "previous_answer": missing.json()["continuation"],
            },
        )
        assert leaked.status_code == 422
        assert doc not in leaked.text


def test_question_boundary_and_unknown_skill(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        ws = _workspace(client)
        url = f"/api/offline/workspaces/{ws}/questions"
        assert (
            client.post(
                url, json={"question": "solar", "skill_id": "missing-skill"}
            ).status_code
            == 422
        )
        assert (
            client.post(url, json={"question": "solar", "document_ids": []}).status_code
            == 422
        )
        assert (
            client.post(
                url,
                json={"question": "solar"},
                headers={"Origin": "https://hostile.invalid"},
            ).status_code
            == 403
        )


def test_revision_followup_does_not_reuse_stale_claims(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        ws = _workspace(client)
        doc = _document(client, ws, "Synthetic project budget is 10 credits.")
        root = f"/api/offline/workspaces/{ws}"
        previous = client.post(
            f"{root}/questions",
            json={"question": "project budget", "document_ids": [doc]},
        ).json()
        revision = client.post(
            f"{root}/documents/{doc}/versions?filename=fixture.txt",
            content=b"Synthetic project budget is 20 credits.",
            headers={"Content-Type": "text/plain"},
        )
        assert revision.status_code == 200
        unavailable = client.post(
            f"{root}/questions",
            json={
                "question": "And now?",
                "document_ids": [doc],
                "previous_answer": previous["continuation"],
            },
        )
        assert unavailable.status_code == 200, unavailable.text
        assert unavailable.json()["status"] == "abstained"
        assert unavailable.json()["source_changes"]
        _extract(client, ws, doc)
        current = client.post(
            f"{root}/questions",
            json={
                "question": "And now?",
                "document_ids": [doc],
                "previous_answer": previous["continuation"],
            },
        )
        assert current.status_code == 200, current.text
        assert {
            citation["version"]
            for claim in current.json()["claims"]
            for citation in claim["citations"]
        } == {2}
        assert all(
            "10 credits" not in claim["text"] for claim in current.json()["claims"]
        )


def test_large_answer_has_bounded_continuation(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        ws = _workspace(client)
        for index in range(6):
            _document(client, ws, f"Revenue source {index}: " + "x" * 1380)
        url = f"/api/offline/workspaces/{ws}/questions"
        first = client.post(url, json={"question": "Revenue"})
        assert first.status_code == 200, first.text
        answer = first.json()
        assert len(first.content) > 16_384
        next_turn = client.post(
            url,
            json={"question": "And now?", "previous_answer": answer["continuation"]},
        )
        assert next_turn.status_code == 200, next_turn.text
        assert next_turn.json()["status"] == "answered"
        assert next_turn.json()["source_snapshot"] == answer["source_snapshot"]


@pytest.mark.parametrize("length", [1500, 1501, 1600, 2000])
@pytest.mark.parametrize("provider_enabled", [False, True])
def test_real_d2_chunk_survives_default_projection(tmp_path, length, provider_enabled):
    text = "Revenue " + "x" * (length - len("Revenue "))

    class Provider:
        context = None

        async def synthesize_answer(self, context):
            self.context = context
            return {
                "claims": [
                    {"text": text, "citations": [{"evidence_index": 1, "quote": text}]}
                ]
            }

    provider = Provider()
    service = DocumentQAService(synthesizer=provider if provider_enabled else None)
    with TestClient(
        create_app(tmp_path, document_qa_services={"offline": service})
    ) as client:
        ws = _workspace(client)
        doc = _document(client, ws, text)
        root = f"/api/offline/workspaces/{ws}"
        retrieval = client.post(
            f"{root}/retrieval", json={"query": "Revenue", "document_ids": [doc]}
        )
        assert retrieval.status_code == 200
        (hit,) = retrieval.json()["hits"]
        assert hit["char_end"] - hit["char_start"] == length
        source = client.get(
            f"{root}/documents/{doc}/chunks/{hit['chunk_id']}?version=1"
        ).json()
        assert source["text"] == text
        response = client.post(
            f"{root}/questions", json={"question": "Revenue", "document_ids": [doc]}
        )
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["status"] == "answered"
        (claim,) = answer["claims"]
        (citation,) = claim["citations"]
        assert claim["text"] == citation["quote"] == text
        assert citation["document_id"] == doc and citation["version"] == 1
        assert citation["chunk_id"] == hit["chunk_id"]
        assert citation["locator"] == source["locator"] == hit["locator"]
        assert answer["context_projection"]["omitted_items"] == 0
        assert answer["context_projection"]["payload_chars"] <= 60000
        if provider_enabled:
            assert answer["synthesis_status"] == "provider"
            assert answer["context_projection"]["payload_chars"] == len(
                render_llm_payload(provider.context)
            )
            assert provider.context.data["evidence"][0]["text"] == text


@pytest.mark.parametrize(
    "budget",
    [
        ContextBudget(),
        ContextBudget(
            max_total_chars=10000, max_item_chars=2000, max_excerpt_chars=1500
        ),
        ContextBudget(
            max_total_chars=5100, max_item_chars=2000, max_excerpt_chars=2000
        ),
    ],
)
def test_real_d2_chunks_respect_explicit_and_full_envelope_limits(tmp_path, budget):
    class RestrictedService(DocumentQAService):
        async def answer(self, *args, **kwargs):
            return await super().answer(*args, **kwargs, context_budget=budget)

    with TestClient(
        create_app(tmp_path, document_qa_services={"offline": RestrictedService()})
    ) as client:
        ws = _workspace(client)
        # Quotes survive D2 intact but double in JSON, exercising total-envelope
        # pressure independently of the 2000-character per-evidence allowance.
        text = "Revenue " + '"' * 1992
        doc = _document(client, ws, text)
        root = f"/api/offline/workspaces/{ws}"
        retrieval = client.post(
            f"{root}/retrieval", json={"query": "Revenue", "document_ids": [doc]}
        )
        assert len(retrieval.json()["hits"]) == 1
        (hit,) = retrieval.json()["hits"]
        assert hit["char_end"] - hit["char_start"] == len(text)
        response = client.post(
            f"{root}/questions", json={"question": "Revenue", "document_ids": [doc]}
        )
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["status"] == "abstained" and answer["claims"] == []
        assert answer["context_projection"]["omitted_items"] == 1
        assert answer["context_projection"]["payload_chars"] <= budget.max_total_chars
        assert "Context limits omitted whole evidence chunks." in answer["limitations"]
