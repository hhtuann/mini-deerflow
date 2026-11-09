"""HTTP contract for document extraction jobs, records, and chunks."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from mini_deerflow.documents import DocumentLimits

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT_MIME = "text/plain"
LIMITS = DocumentLimits(max_document_bytes=2_000_000)


def _client(root: Path) -> TestClient:
    return TestClient(create_app(root, document_limits=LIMITS))


def _pdf_bytes(pages: list[list[str]]) -> bytes:
    from tests.test_document_extraction import _build_pdf

    return _build_pdf(pages)


def _docx_bytes(paragraphs: list[str], tables=None, **kwargs) -> bytes:
    from tests.test_document_extraction import _build_docx

    return _build_docx(paragraphs, tables, **kwargs)


def _workspace(client: TestClient) -> str:
    response = client.post("/api/live/workspaces", json={"name": "D2"})
    assert response.status_code == 201
    return response.json()["workspace_id"]


def _upload(
    client: TestClient, workspace_id: str, filename: str, data: bytes, mime: str
) -> str:
    response = client.post(
        f"/api/live/workspaces/{workspace_id}/documents",
        params={"filename": filename},
        content=data,
        headers={"Content-Type": mime},
    )
    assert response.status_code == 201, response.text
    return response.json()["document_id"]


def _wait_job(client: TestClient, job: dict) -> dict:
    import time

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        current = client.get(f"/api/live/jobs/{job['job_id']}").json()
        if current["status"] in {"completed", "failed"}:
            return current
        time.sleep(0.1)
    raise AssertionError("extraction job did not finish")


def _extract(client: TestClient, workspace_id: str, document_id: str) -> dict:
    response = client.post(
        f"/api/live/workspaces/{workspace_id}/documents/{document_id}/extraction"
    )
    assert response.status_code == 202, response.text
    return _wait_job(client, response.json())


class TestExtractionJobFlow:
    def test_extract_poll_and_read_chunks(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload(
                client,
                workspace_id,
                "guide.pdf",
                _pdf_bytes([["Chapter one", "details"], ["Chapter two"]]),
                PDF_MIME,
            )
            job = _extract(client, workspace_id, document_id)
            assert job["status"] == "completed"
            assert job["operation"] == "document_extraction"

            extraction = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/extraction"
            )
            assert extraction.status_code == 200
            body = extraction.json()
            assert set(body) == {
                "workspace_id",
                "document_id",
                "version",
                "source_sha256",
                "status",
                "error_class",
                "extractor_version",
                "chunk_count",
                "total_chars",
                "created_at",
                "updated_at",
            }
            assert body["status"] == "completed"
            assert body["chunk_count"] == 2

            chunks = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/chunks"
            )
            assert chunks.status_code == 200
            listing = chunks.json()
            assert len(listing) == 2
            first = listing[0]
            assert set(first) == {
                "chunk_id",
                "index",
                "kind",
                "preview",
                "char_start",
                "char_end",
                "locator",
            }
            assert first["kind"] == "page"
            assert first["locator"]["page"] == 1
            assert len(first["preview"]) <= 200

            detail = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}"
                f"/chunks/{first['chunk_id']}"
            )
            assert detail.status_code == 200
            detail_body = detail.json()
            assert "text" in detail_body
            assert "Chapter one" in detail_body["text"]
            assert detail_body["locator"]["page"] == 1

    def test_extraction_of_unavailable_document_records_error_class(
        self, tmp_path: Path
    ) -> None:
        from tests.test_document_extraction import _encrypted_pdf

        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload(
                client, workspace_id, "locked.pdf", _encrypted_pdf(), PDF_MIME
            )
            job = _extract(client, workspace_id, document_id)
            assert job["status"] == "completed"
            extraction = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/extraction"
            ).json()
            assert extraction["status"] == "unavailable"
            assert extraction["error_class"] == "encrypted"
            chunks = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/chunks"
            )
            assert chunks.status_code == 409

    def test_repost_completed_extraction_is_409_but_unavailable_requeues(
        self, tmp_path: Path
    ) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload(
                client, workspace_id, "a.pdf", _pdf_bytes([["text"]]), PDF_MIME
            )
            _extract(client, workspace_id, document_id)
            conflict = client.post(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/extraction"
            )
            assert conflict.status_code == 409

    def test_new_version_gets_new_extraction(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload(
                client, workspace_id, "doc.pdf", _pdf_bytes([["v1"]]), PDF_MIME
            )
            _extract(client, workspace_id, document_id)
            version = client.post(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/versions",
                params={"filename": "doc.pdf"},
                content=_pdf_bytes([["v2 page"], ["v2 second"]]),
                headers={"Content-Type": PDF_MIME},
            )
            assert version.status_code == 200
            _extract(client, workspace_id, document_id)
            extraction = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/extraction"
            ).json()
            assert extraction["version"] == 2
            assert extraction["chunk_count"] == 2

    def test_docx_extraction_through_api(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload(
                client,
                workspace_id,
                "Báo cáo.docx",
                _docx_bytes(
                    ["Đoạn văn tiếng Việt thứ nhất.", "Second English paragraph."],
                    tables=[[["Cột A", "Cột B"], ["1", "2"]]],
                ),
                DOCX_MIME,
            )
            job = _extract(client, workspace_id, document_id)
            assert job["status"] == "completed"
            chunks = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/chunks"
            ).json()
            kinds = {chunk["kind"] for chunk in chunks}
            assert "paragraph" in kinds
            assert "table" in kinds
            joined = "\n".join(
                client.get(
                    f"/api/live/workspaces/{workspace_id}/documents/{document_id}"
                    f"/chunks/{chunk['chunk_id']}"
                ).json()["text"]
                for chunk in chunks
            )
            assert "Đoạn văn tiếng Việt thứ nhất." in joined
            assert "Cột A | Cột B" in joined


class TestExtractionSafety:
    def test_wrong_workspace_extraction_404(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_a = _workspace(client)
            workspace_b = client.post(
                "/api/live/workspaces", json={"name": "B"}
            ).json()["workspace_id"]
            document_id = _upload(
                client, workspace_a, "a.pdf", _pdf_bytes([["text"]]), PDF_MIME
            )
            for method, path in (
                (
                    "post",
                    f"/api/live/workspaces/{workspace_b}/documents/{document_id}/extraction",
                ),
                (
                    "get",
                    f"/api/live/workspaces/{workspace_b}/documents/{document_id}/extraction",
                ),
                (
                    "get",
                    f"/api/live/workspaces/{workspace_b}/documents/{document_id}/chunks",
                ),
            ):
                response = getattr(client, method)(path)
                assert response.status_code == 404

    def test_missing_extraction_record_is_404(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload(
                client, workspace_id, "a.pdf", _pdf_bytes([["text"]]), PDF_MIME
            )
            assert (
                client.get(
                    f"/api/live/workspaces/{workspace_id}/documents/{document_id}/extraction"
                ).status_code
                == 404
            )

    def test_extraction_responses_never_leak_internals(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload(
                client, workspace_id, "bad.pdf", b"%PDF-1.4 junk junk junk", PDF_MIME
            )
            job = _extract(client, workspace_id, document_id)
            assert job["status"] == "completed"
            extraction = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/extraction"
            )
            body = extraction.json()
            assert body["status"] == "unavailable"
            assert body["error_class"] == "malformed"
            text = extraction.text
            assert str(tmp_path) not in text
            assert "Traceback" not in text
            assert "document.xml" not in text
            assert "word/" not in text

    def test_extraction_survives_restart(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload(
                client, workspace_id, "r.pdf", _pdf_bytes([["restart"]]), PDF_MIME
            )
            _extract(client, workspace_id, document_id)
        with _client(tmp_path) as restarted:
            extraction = restarted.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/extraction"
            )
            assert extraction.status_code == 200
            assert extraction.json()["status"] == "completed"
            chunks = restarted.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/chunks"
            ).json()
            assert len(chunks) == 1

    def test_invalid_extraction_limit_override_fails_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from mini_deerflow.documents import DocumentLimitsError

        monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_EXTRACTION_MAX_PAGES", "0")
        with pytest.raises(DocumentLimitsError):
            create_app(tmp_path)

    def test_extraction_origin_policy_enforced(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document_id = _upload(
                client, workspace_id, "a.pdf", _pdf_bytes([["text"]]), PDF_MIME
            )
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/documents/{document_id}/extraction",
                headers={"Origin": "http://evil.example"},
            )
            assert response.status_code == 403
