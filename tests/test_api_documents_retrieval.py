"""HTTP contract for retrieval and citation resolution endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from mini_deerflow.documents import DocumentLimits


def _client(root: Path) -> TestClient:
    return TestClient(create_app(root, document_limits=DocumentLimits(2_000_000)))


def _extract(client: TestClient, workspace_id: str, document_id: str) -> None:
    import time

    response = client.post(
        f"/api/live/workspaces/{workspace_id}/documents/{document_id}/extraction"
    )
    assert response.status_code == 202, response.text
    job = response.json()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        current = client.get(f"/api/live/jobs/{job['job_id']}").json()
        if current["status"] in {"completed", "failed"}:
            assert current["status"] == "completed", current
            return
        time.sleep(0.1)
    raise AssertionError("extraction job did not finish")


def _upload_and_extract(
    client: TestClient, workspace_id: str, filename: str, content: bytes
) -> str:
    response = client.post(
        f"/api/live/workspaces/{workspace_id}/documents",
        params={"filename": filename},
        content=content,
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["document_id"]
    _extract(client, workspace_id, document_id)
    return document_id


def _workspace(client: TestClient, name: str = "Retrieval") -> str:
    response = client.post("/api/live/workspaces", json={"name": name})
    assert response.status_code == 201
    return response.json()["workspace_id"]


class TestRetrievalEndpoint:
    def test_retrieval_returns_ranked_hits(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            _upload_and_extract(
                client, workspace_id, "a.txt", b"solar panel efficiency study\n"
            )
            _upload_and_extract(
                client, workspace_id, "b.txt", b"unrelated cooking recipes\n"
            )
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                json={"query": "solar panels"},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert set(body) == {
                "hits",
                "total_candidates",
                "truncated_by_budget",
                "scoped_documents",
            }
            assert body["hits"]
            first = body["hits"][0]
            assert set(first) == {
                "document_id",
                "version",
                "chunk_id",
                "score",
                "preview",
                "char_start",
                "char_end",
                "locator",
            }
            assert len(first["preview"]) <= 200
            assert isinstance(first["score"], float)
            scores = [hit["score"] for hit in body["hits"]]
            assert scores == sorted(scores, reverse=True)
            assert "solar" in first["preview"].lower()

    def test_scoped_retrieval(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            doc_a = _upload_and_extract(
                client, workspace_id, "a.txt", b"wind turbine output\n"
            )
            _upload_and_extract(client, workspace_id, "b.txt", b"wind turbine noise\n")
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                json={"query": "wind turbine", "document_ids": [doc_a]},
            )
            assert response.status_code == 200
            body = response.json()
            assert {hit["document_id"] for hit in body["hits"]} == {doc_a}
            assert body["scoped_documents"] == [doc_a]

    def test_unknown_scope_document_rejected(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                json={"query": "x", "document_ids": ["doc-missing"]},
            )
            assert response.status_code == 422
            assert "doc-missing" not in response.json()["detail"]

    def test_request_budgets_shrink_only(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            for index in range(4):
                _upload_and_extract(
                    client,
                    workspace_id,
                    f"d{index}.txt",
                    f"shared topic number {index}\n".encode(),
                )
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                json={"query": "shared topic", "max_hits": 2},
            )
            assert response.status_code == 200
            assert len(response.json()["hits"]) == 2
            exceeded = client.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                json={"query": "shared topic", "max_hits": 5_000},
            )
            assert exceeded.status_code == 422

    def test_invalid_bodies_rejected(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            base = f"/api/live/workspaces/{workspace_id}/retrieval"
            for payload in (
                {},
                {"query": ""},
                {"query": "x" * 401},
                {"query": 12},
                {"query": "ok", "document_ids": "not-a-list"},
                {"query": "ok", "max_hits": "many"},
                {"query": "ok", "max_context_chars": -5},
                {"query": "ok", "extra": 1},
            ):
                response = client.post(base, json=payload)
                assert response.status_code == 422, payload

    def test_retrieval_after_restart(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            _upload_and_extract(
                client, workspace_id, "persist.txt", b"durable pineapple facts\n"
            )
        with _client(tmp_path) as restarted:
            response = restarted.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                json={"query": "pineapple"},
            )
            assert response.status_code == 200
            assert response.json()["hits"]

    def test_wrong_workspace_retrieval_empty_or_404(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_a = _workspace(client, "A")
            workspace_b = _workspace(client, "B")
            _upload_and_extract(
                client, workspace_a, "s.txt", b"workspace secret papaya\n"
            )
            response = client.post(
                f"/api/live/workspaces/{workspace_b}/retrieval",
                json={"query": "papaya"},
            )
            assert response.status_code == 200
            assert response.json()["hits"] == []
            assert response.json()["total_candidates"] == 0

    def test_origin_policy_enforced(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                json={"query": "x"},
                headers={"Origin": "http://evil.example"},
            )
            assert response.status_code == 403

    def test_oversized_body_rejected_by_boundary(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                content=b'{"query": "' + b"a" * 20_000 + b'"}',
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 413

    def test_responses_never_leak_internals(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            _upload_and_extract(
                client, workspace_id, "l.txt", b"leakage probe lychee\n"
            )
            checks = [
                client.post(
                    f"/api/live/workspaces/{workspace_id}/retrieval",
                    json={"query": "lychee"},
                ),
                client.post(
                    f"/api/live/workspaces/{workspace_id}/retrieval",
                    json={"query": "'; DROP TABLE chunks; --"},
                ),
            ]
            for response in checks:
                assert response.status_code == 200
                text = response.text
                assert str(tmp_path) not in text
                assert "document-store" not in text
                assert "Traceback" not in text
                assert "DROP TABLE" not in text


class TestCitationEndpoint:
    def test_citation_resolves_verified_chunk(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload_and_extract(
                client, workspace_id, "c.txt", b"citable durian content\n"
            )
            retrieval = client.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                json={"query": "durian"},
            ).json()
            chunk_id = retrieval["hits"][0]["chunk_id"]
            response = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}"
                f"/chunks/{chunk_id}"
            )
            assert response.status_code == 200
            body = response.json()
            assert set(body) == {
                "chunk_id",
                "index",
                "kind",
                "text",
                "char_start",
                "char_end",
                "locator",
            }
            assert "durian" in body["text"]
            assert body["chunk_id"] == chunk_id

    def test_stale_version_returns_409_closed_prose(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload_and_extract(
                client, workspace_id, "v.txt", b"first rambutan version\n"
            )
            retrieval = client.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                json={"query": "rambutan"},
            ).json()
            chunk_id = retrieval["hits"][0]["chunk_id"]
            superseded = client.post(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/versions",
                params={"filename": "v.txt"},
                content=b"second rambutan version\n",
                headers={"Content-Type": "text/plain"},
            )
            assert superseded.status_code == 200
            response = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}"
                f"/chunks/{chunk_id}"
            )
            assert response.status_code == 409
            assert "stale" in response.json()["detail"].lower()

    def test_deleted_or_cross_workspace_404(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_a = _workspace(client, "A")
            workspace_b = _workspace(client, "B")
            document_id = _upload_and_extract(
                client, workspace_a, "p.txt", b"private salak notes\n"
            )
            retrieval = client.post(
                f"/api/live/workspaces/{workspace_a}/retrieval",
                json={"query": "salak"},
            ).json()
            chunk_id = retrieval["hits"][0]["chunk_id"]
            cross = client.get(
                f"/api/live/workspaces/{workspace_b}/documents/{document_id}"
                f"/chunks/{chunk_id}"
            )
            assert cross.status_code == 404
            deleted = client.delete(
                f"/api/live/workspaces/{workspace_a}/documents/{document_id}"
            )
            assert deleted.status_code == 204
            gone = client.get(
                f"/api/live/workspaces/{workspace_a}/documents/{document_id}"
                f"/chunks/{chunk_id}"
            )
            assert gone.status_code == 404

    def test_invalid_chunk_id_shape_rejected(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            response = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/doc-x"
                "/chunks/not-a-chunk-id"
            )
            assert response.status_code in (404, 422)


class TestRetrievalConfig:
    def test_invalid_retrieval_env_fails_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from mini_deerflow.documents import DocumentLimitsError

        monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_RETRIEVAL_MAX_HITS", "0")
        with pytest.raises(DocumentLimitsError):
            create_app(tmp_path)

    def test_valid_retrieval_env_reaches_endpoint(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_RETRIEVAL_MAX_HITS", "1")
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            for index in range(3):
                _upload_and_extract(
                    client,
                    workspace_id,
                    f"h{index}.txt",
                    f"shared longan batch {index}\n".encode(),
                )
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/retrieval",
                json={"query": "longan"},
            )
            assert response.status_code == 200
            body = response.json()
            assert len(body["hits"]) <= 1
