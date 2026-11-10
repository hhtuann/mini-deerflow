"""Untrusted document strings remain inert at the actual download boundary."""

from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from tests.test_api_document_qa import _document, _workspace


def test_markdown_download_keeps_hostile_sources_inert(tmp_path):
    text = "Revenue <script>alert(1)</script> [click](javascript:alert(2)) ![track](https://invalid.example/pixel) C:\\private\\secret.txt /etc/passwd"
    with TestClient(create_app(tmp_path)) as client:
        ws = _workspace(client)
        doc = _document(client, ws, text)
        root = f"/api/offline/workspaces/{ws}/reports"
        created = client.post(
            root,
            json={
                "title": "<img src=x onerror=alert(1)>",
                "kind": "summary",
                "document_ids": [doc],
                "sections": [
                    {
                        "section_id": "revenue",
                        "title": "[x](file:///etc/passwd)",
                        "question": "Revenue",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        report = created.json()
        # Grounded canonical evidence is not modified by export sanitization.
        assert report["sections"][0]["claims"][0]["citations"][0]["quote"] == text
        response = client.get(f"{root}/{report['report_id']}/versions/1/markdown")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert (
            response.headers["content-disposition"]
            == 'attachment; filename="report.md"'
        )
        export = response.text
        assert "<script>" not in export and "<img" not in export
        assert "javascript:" not in export and "file:" not in export
        assert "https://invalid.example" not in export
        assert "C:\\private\\secret.txt" not in export and "/etc/passwd" not in export
        assert "Revenue" in export
        assert client.get(f"{root}/{report['report_id']}/versions/1").json() == report
