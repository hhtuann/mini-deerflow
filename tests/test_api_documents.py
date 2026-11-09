"""HTTP contract and safety tests for the document workspace API."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from mini_deerflow.api.security import MAX_REQUEST_BYTES
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.service import DemoRuntimeService
from mini_deerflow.documents import DocumentLimits

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
TXT_BYTES = "Nội dung kiểm thử — API fixture\n".encode()
PDF_MIME = "application/pdf"
TXT_MIME = "text/plain"
LIMITS = DocumentLimits(
    max_document_bytes=4_096,
    max_documents_per_workspace=3,
    max_versions_per_document=3,
    max_workspace_bytes=16_384,
    max_workspaces=3,
)


def _client(root: Path) -> TestClient:
    return TestClient(create_app(root, document_limits=LIMITS))


def _workspace(client: TestClient, mode: str = "live", name: str = "Research") -> str:
    response = client.post(f"/api/{mode}/workspaces", json={"name": name})
    assert response.status_code == 201
    return response.json()["workspace_id"]


def _upload(
    client: TestClient,
    workspace_id: str,
    filename: str = "report.pdf",
    data: bytes = PDF_BYTES,
    content_type: str = PDF_MIME,
    mode: str = "live",
):
    return client.post(
        f"/api/{mode}/workspaces/{workspace_id}/documents",
        params={"filename": filename},
        content=data,
        headers={"Content-Type": content_type},
    )


class TestWorkspaceRoutes:
    def test_workspace_crud_contract(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            created = client.post("/api/live/workspaces", json={"name": "Docs"})
            assert created.status_code == 201
            body = created.json()
            assert set(body) == {
                "workspace_id",
                "name",
                "status",
                "created_at",
                "updated_at",
                "document_count",
                "total_bytes",
            }
            workspace_id = body["workspace_id"]
            listed = client.get("/api/live/workspaces").json()
            assert [item["workspace_id"] for item in listed] == [workspace_id]
            detail = client.get(f"/api/live/workspaces/{workspace_id}")
            assert detail.status_code == 200
            assert detail.json()["documents"] == []
            deleted = client.delete(f"/api/live/workspaces/{workspace_id}")
            assert deleted.status_code == 204
            assert client.get(f"/api/live/workspaces/{workspace_id}").status_code == 404

    def test_workspace_name_validation(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            for payload in (
                {"name": ""},
                {"name": "x" * 81},
                {"name": None},
                {},
                {"name": "ok", "extra": 1},
            ):
                assert (
                    client.post("/api/live/workspaces", json=payload).status_code == 422
                )

    def test_unknown_workspace_is_404(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            assert client.get("/api/live/workspaces/ws-missing").status_code == 404
            assert (
                client.get("/api/live/workspaces/ws-missing/documents").status_code
                == 404
            )

    def test_invalid_workspace_identifier_rejected(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            for bad in ("../escape", "has space", "slash/inside", "x" * 129, "%2e%2e"):
                response = client.get(f"/api/live/workspaces/{bad}")
                assert response.status_code in (404, 422)


class TestDocumentUploadDownload:
    def test_upload_list_get_download_roundtrip(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            created = _upload(client, workspace_id)
            assert created.status_code == 201, created.text
            document = created.json()
            assert set(document) == {
                "workspace_id",
                "document_id",
                "filename",
                "content_type",
                "status",
                "created_at",
                "updated_at",
                "latest_version",
                "byte_size",
                "sha256",
                "versions",
            }
            assert document["sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()
            listed = client.get(f"/api/live/workspaces/{workspace_id}/documents").json()
            assert len(listed) == 1
            fetched = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document['document_id']}"
            )
            assert fetched.status_code == 200
            content = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document['document_id']}/content"
            )
            assert content.status_code == 200
            assert content.content == PDF_BYTES
            assert content.headers["content-type"].startswith(PDF_MIME)
            assert content.headers["etag"] == f'"{document["sha256"]}"'
            assert content.headers["x-content-type-options"] == "nosniff"
            assert "attachment" in content.headers["content-disposition"]
            assert "report.pdf" not in content.headers["content-disposition"]

    def test_version_upload_and_versioned_download(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document = _upload(
                client, workspace_id, "notes.txt", TXT_BYTES, TXT_MIME
            ).json()
            second = "Phiên bản thứ hai dài hơn một chút\n".encode()
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/documents/{document['document_id']}/versions",
                params={"filename": "notes.txt"},
                content=second,
                headers={"Content-Type": TXT_MIME},
            )
            assert response.status_code == 200, response.text
            assert response.json()["latest_version"] == 2
            v1 = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document['document_id']}/content/1"
            )
            assert v1.content == TXT_BYTES
            latest = client.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document['document_id']}/content"
            )
            assert latest.content == second

    def test_duplicate_upload_is_idempotent(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            first = _upload(client, workspace_id).json()
            again = _upload(client, workspace_id)
            assert again.status_code == 201
            body = again.json()
            assert body["document_id"] != first["document_id"]
            listed = client.get(f"/api/live/workspaces/{workspace_id}/documents").json()
            assert len(listed) == 2

    def test_delete_document(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document = _upload(client, workspace_id).json()
            base = f"/api/live/workspaces/{workspace_id}/documents/{document['document_id']}"
            assert client.delete(base).status_code == 204
            assert client.get(base).status_code == 404
            assert client.get(f"{base}/content").status_code == 404
            assert client.delete(base).status_code == 404

    def test_wrong_workspace_access_is_404_without_leak(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_a = _workspace(client, name="A")
            workspace_b = _workspace(client, name="B")
            document = _upload(client, workspace_a).json()
            for method, path in (
                (
                    "get",
                    f"/api/live/workspaces/{workspace_b}/documents/{document['document_id']}",
                ),
                (
                    "get",
                    f"/api/live/workspaces/{workspace_b}/documents/{document['document_id']}/content",
                ),
                (
                    "delete",
                    f"/api/live/workspaces/{workspace_b}/documents/{document['document_id']}",
                ),
            ):
                response = getattr(client, method)(path)
                assert response.status_code == 404
                assert "A" not in response.json()["detail"]
                assert document["document_id"] not in response.json()["detail"]

    def test_mode_isolation(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client, mode="live")
            _upload(client, workspace_id, mode="live")
            assert (
                client.get(f"/api/offline/workspaces/{workspace_id}").status_code == 404
            )


class TestUploadFailClosed:
    def test_unsupported_extension_rejected(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            for filename in ("tool.exe", "page.html", "noext", "old.doc"):
                response = _upload(
                    client,
                    workspace_id,
                    filename=filename,
                    data=b"data",
                    content_type="application/octet-stream",
                )
                assert response.status_code == 415, filename

    def test_content_type_mismatch_rejected(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            response = _upload(client, workspace_id, "report.pdf", PDF_BYTES, TXT_MIME)
            assert response.status_code == 415

    def test_malformed_magic_rejected(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            response = _upload(
                client, workspace_id, "fake.pdf", b"definitely not pdf", PDF_MIME
            )
            assert response.status_code == 415

    def test_missing_filename_rejected(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/documents",
                content=PDF_BYTES,
                headers={"Content-Type": PDF_MIME},
            )
            assert response.status_code == 422

    def test_oversize_body_rejected_by_boundary(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            response = _upload(
                client,
                workspace_id,
                "big.pdf",
                data=PDF_BYTES + b"x" * 8_192,
            )
            assert response.status_code == 413
            assert "too large" in response.json()["detail"]

    def test_non_upload_routes_keep_tight_cap(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            response = client.post(
                "/api/live/conversations",
                content=b'{"message": "' + b"x" * (MAX_REQUEST_BYTES + 512) + b'"}',
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 413

    def test_upload_origin_policy_enforced(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/documents",
                params={"filename": "report.pdf"},
                content=PDF_BYTES,
                headers={"Content-Type": PDF_MIME, "Origin": "http://evil.example"},
            )
            assert response.status_code == 403

    def test_quota_exceeded_is_409_fixed_prose(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            for index in range(3):
                assert (
                    _upload(
                        client,
                        workspace_id,
                        filename=f"d{index}.pdf",
                        data=PDF_BYTES,
                    ).status_code
                    == 201
                )
            response = _upload(client, workspace_id, filename="d3.pdf", data=PDF_BYTES)
            assert response.status_code == 409
            detail = response.json()["detail"]
            assert str(tmp_path) not in detail
            assert ".pdf" not in detail

    def test_multipart_rejected(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            response = client.post(
                f"/api/live/workspaces/{workspace_id}/documents",
                params={"filename": "report.pdf"},
                files={"file": ("report.pdf", PDF_BYTES, PDF_MIME)},
            )
            assert response.status_code == 415


class TestPersistenceAndSafety:
    def test_documents_survive_app_restart(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            document = _upload(client, workspace_id).json()
        with _client(tmp_path) as restarted:
            detail = restarted.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document['document_id']}"
            )
            assert detail.status_code == 200
            assert detail.json()["sha256"] == document["sha256"]
            content = restarted.get(
                f"/api/live/workspaces/{workspace_id}/documents/{document['document_id']}/content"
            )
            assert content.content == PDF_BYTES

    def test_startup_recovery_sweeps_orphans(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            _workspace(client)
        orphan = tmp_path / "live" / "document-store" / "ws-deadbeef" / "v1" / "x.pdf"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")
        with _client(tmp_path) as restarted:
            assert restarted.get("/api/health").status_code == 200
        assert not orphan.exists()

    def test_responses_never_leak_storage_root(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            workspace_id = _workspace(client)
            _upload(client, workspace_id)
            checks = [
                client.get("/api/live/workspaces"),
                client.get(f"/api/live/workspaces/{workspace_id}"),
                client.get(f"/api/live/workspaces/{workspace_id}/documents"),
                client.get("/api/live/workspaces/ws-none"),
            ]
            for response in checks:
                text = response.text
                assert str(tmp_path) not in text
                assert ".mini-deerflow" not in text
                assert "document-store" not in text

    def test_document_ops_with_offline_live_services(self, tmp_path: Path) -> None:
        """Documents must not disturb the existing offline/live service wiring."""
        services = {
            "live": DemoRuntimeService(OfflineDemoBackend(tmp_path / "live")),
        }
        with TestClient(
            create_app(tmp_path, services=services, document_limits=LIMITS)
        ) as client:
            workspace_id = _workspace(client, mode="live")
            assert _upload(client, workspace_id, mode="live").status_code == 201
            assert client.get("/api/health").status_code == 200

    def test_chunked_upload_oversize_rejected(self, tmp_path: Path) -> None:
        import asyncio

        app = create_app(tmp_path, document_limits=LIMITS)

        async def scenario() -> int:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                workspace = await client.post(
                    "/api/live/workspaces", json={"name": "chunked"}
                )
                workspace_id = workspace.json()["workspace_id"]

                async def body():
                    for _ in range(64):
                        yield b"x" * 512

                response = await client.post(
                    f"/api/live/workspaces/{workspace_id}/documents",
                    params={"filename": "big.pdf"},
                    content=body(),
                    headers={"Content-Type": PDF_MIME},
                )
                return response.status_code

        assert asyncio.run(scenario()) == 413


def test_create_app_fails_closed_on_invalid_document_quota_override(
    tmp_path: Path, monkeypatch
) -> None:
    """The effective app builder must refuse invalid explicit quotas."""

    from mini_deerflow.documents import DocumentLimitsError

    monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_MAX_BYTES", "999")
    with pytest.raises(DocumentLimitsError) as error:
        create_app(tmp_path)
    assert "999" not in str(error.value)


def test_valid_document_quota_override_reaches_upload_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    """A valid env override configures the real request boundary cap."""

    monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_MAX_BYTES", "2048")
    small = b"%PDF-1.7 " + b"a" * 100
    with TestClient(create_app(tmp_path)) as client:
        workspace_id = _workspace(client)
        created = client.post(
            f"/api/live/workspaces/{workspace_id}/documents",
            params={"filename": "small.pdf"},
            content=small,
            headers={"Content-Type": PDF_MIME},
        )
        assert created.status_code == 201, created.text
        oversized = client.post(
            f"/api/live/workspaces/{workspace_id}/documents",
            params={"filename": "big.pdf"},
            content=small + b"x" * 2_000,
            headers={"Content-Type": PDF_MIME},
        )
        assert oversized.status_code == 413


def test_injected_limits_still_validate_ambient_document_env(
    tmp_path: Path, monkeypatch
) -> None:
    """Injection overrides values, not fail-closed quota validation."""

    from mini_deerflow.documents import DocumentLimitsError

    monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_MAX_BYTES", "999")
    with pytest.raises(DocumentLimitsError) as error:
        create_app(tmp_path, document_limits=LIMITS)
    assert "999" not in str(error.value)


def test_injected_limits_with_valid_ambient_override_keep_injection(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_MAX_BYTES", "2048")
    with TestClient(create_app(tmp_path, document_limits=LIMITS)) as client:
        workspace_id = _workspace(client)
        # LIMITS.max_document_bytes is 4096, not the env value 2048.
        body = b"%PDF-1.7 " + b"b" * 3_000
        created = client.post(
            f"/api/live/workspaces/{workspace_id}/documents",
            params={"filename": "env-vs-injected.pdf"},
            content=body,
            headers={"Content-Type": PDF_MIME},
        )
        assert created.status_code == 201, created.text
