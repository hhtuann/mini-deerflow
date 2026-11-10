"""Report revisions bind newly generated sections to current selected versions."""

from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from tests.test_api_document_qa import _document, _extract, _workspace
from tests.test_api_document_reports import _request


def test_report_revision_rebinds_only_targeted_section_after_source_update(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        ws = _workspace(client)
        doc = _document(client, ws, "Revenue is 10 credits. Cost is 4 credits.")
        root = f"/api/offline/workspaces/{ws}"
        created = client.post(f"{root}/reports", json=_request(doc))
        assert created.status_code == 201, created.text
        first = created.json()
        report = f"{root}/reports/{first['report_id']}"
        upload = client.post(
            f"{root}/documents/{doc}/versions?filename=fixture.txt",
            content=b"Revenue is 20 credits. Cost is 8 credits.",
            headers={"Content-Type": "text/plain"},
        )
        assert upload.status_code == 200, upload.text
        _extract(client, ws, doc)
        result = client.post(
            f"{report}/revisions",
            json={
                "base_version": 1,
                "sections": [
                    {
                        "section_id": "budget",
                        "title": "Current revenue",
                        "question": "Revenue",
                    }
                ],
            },
        )
        assert result.status_code == 201, result.text
        second = result.json()
        assert second["sections"][1] == first["sections"][1]
        assert {
            c["version"]
            for claim in second["sections"][0]["claims"]
            for c in claim["citations"]
        } == {2}
        assert {
            c["version"]
            for claim in second["sections"][1]["claims"]
            for c in claim["citations"]
        } == {1}
        assert client.get(f"{report}/versions/1").json() == first
        export = client.get(f"{report}/versions/2/markdown")
        assert export.status_code == 200
        assert "histor" in export.text.lower()
