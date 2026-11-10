"""Synthetic end-to-end report lifecycle, immutable revisions and safe exports."""

from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from tests.test_api_document_qa import _document, _workspace


def _request(doc):
    return {
        "title": "Synthetic report",
        "kind": "comparison",
        "document_ids": [doc],
        "sections": [
            {"section_id": "budget", "title": "Budget", "question": "Revenue"},
            {"section_id": "cost", "title": "Cost", "question": "Cost"},
        ],
    }


def test_report_restart_revision_history_and_download(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        ws = _workspace(client)
        doc = _document(
            client,
            ws,
            "Revenue is 10 synthetic credits.\n\nCost is 4 synthetic credits.",
        )
        root = f"/api/offline/workspaces/{ws}/reports"
        response = client.post(root, json=_request(doc))
        assert response.status_code == 201, response.text
        first = response.json()
        report_id = first["report_id"]
        assert first["version"] == 1 and first["parent_version"] is None
        for section in first["sections"]:
            assert section["claims"]
            for claim in section["claims"]:
                for citation in claim["citations"]:
                    source = client.get(
                        f"/api/offline/workspaces/{ws}/documents/{doc}/chunks/{citation['chunk_id']}?version={citation['version']}"
                    )
                    assert source.status_code == 200
                    assert citation["quote"] in source.json()["text"]
                    assert citation["locator"] == source.json()["locator"]
        approved = client.post(
            f"{root}/{report_id}/revisions",
            json={"base_version": 1, "sections": [], "approved_section_ids": ["cost"]},
        )
        assert approved.status_code == 201, approved.text
        second = approved.json()
        assert second["sections"][1]["approved"] is True
        revised = client.post(
            f"{root}/{report_id}/revisions",
            json={
                "base_version": 2,
                "sections": [
                    {
                        "section_id": "budget",
                        "title": "Revised budget",
                        "question": "Revenue credits",
                    }
                ],
            },
        )
        assert revised.status_code == 201, revised.text
        third = revised.json()
        assert third["version"] == 3 and third["parent_version"] == 2
        assert third["sections"][0]["title"] == "Revised budget"
        assert third["sections"][1] == second["sections"][1]
        assert third["sections"][0]["origin_version"] == 3
        assert (
            client.post(
                f"{root}/{report_id}/revisions",
                json={
                    "base_version": 1,
                    "sections": [
                        {
                            "section_id": "budget",
                            "title": "Stale",
                            "question": "Revenue",
                        }
                    ],
                },
            ).status_code
            == 409
        )
        export = client.get(f"{root}/{report_id}/versions/1/markdown")
        assert export.status_code == 200
        original_bytes = export.content
        assert "attachment" in export.headers["content-disposition"]
        assert "report.md" in export.headers["content-disposition"]
        assert b"Revenue" in original_bytes
        assert client.get(f"{root}/{report_id}/versions/1/docx").status_code == 404
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"{root}/{report_id}/versions/1").json() == first
        assert client.get(f"{root}/{report_id}").json() == third
        history = client.get(f"{root}/{report_id}/versions")
        assert history.status_code == 200
        assert [item["version"] for item in history.json()] == [1, 2, 3]
        assert (
            client.get(f"{root}/{report_id}/versions/1/markdown").content
            == original_bytes
        )
        other = _workspace(client)
        assert (
            client.get(
                f"/api/offline/workspaces/{other}/reports/{report_id}"
            ).status_code
            == 404
        )
        assert (
            client.get(f"/api/live/workspaces/{ws}/reports/{report_id}").status_code
            == 404
        )
        deleted = client.delete(f"/api/offline/workspaces/{ws}/documents/{doc}")
        assert deleted.status_code == 204, deleted.text
        # Historical evidence is retained as a versioned snapshot, not presented
        # as a newly verified current source. New generation cannot use it.
        assert client.get(f"{root}/{report_id}/versions/1").json() == first
        assert (
            client.get(f"{root}/{report_id}/versions/1/markdown").content
            == original_bytes
        )
        unavailable = client.post(
            f"{root}/{report_id}/revisions",
            json={
                "base_version": 3,
                "sections": [
                    {"section_id": "budget", "title": "Budget", "question": "Revenue"}
                ],
            },
        )
        assert unavailable.status_code in (409, 422), unavailable.text
        assert len(client.get(f"{root}/{report_id}/versions").json()) == 3


def test_report_explicit_sources_unsupported_queries_and_input_boundary(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        ws = _workspace(client)
        doc = _document(client, ws, "Revenue is 10 synthetic credits.")
        root = f"/api/offline/workspaces/{ws}/reports"
        request = _request(doc)
        request["sections"] = [
            {"section_id": "none", "title": "Unknown", "question": "quasar taxonomy"}
        ]
        response = client.post(root, json=request)
        assert response.status_code == 201, response.text
        assert response.json()["sections"][0]["claims"] == []
        assert response.json()["sections"][0]["limitations"]
        for docs in ([], [doc, doc], ["doc-foreign"]):
            request["document_ids"] = docs
            assert client.post(root, json=request).status_code == 422
        request = _request(doc)
        request["sections"].append(request["sections"][0])
        assert client.post(root, json=request).status_code == 422
        request = _request(doc)
        request["sections"][0]["claims"] = [{"text": "invented"}]
        assert client.post(root, json=request).status_code == 422
        report_id = response.json()["report_id"]
        for base in (True, "1", 1.5):
            invalid = client.post(
                f"{root}/{report_id}/revisions",
                json={
                    "base_version": base,
                    "sections": [],
                    "approved_section_ids": ["none"],
                },
            )
            assert invalid.status_code == 422
        assert len(client.get(f"{root}/{report_id}/versions").json()) == 1
