"""Document workspace store: persistence, lifecycle, quotas, and recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mini_deerflow.documents import (
    DEFAULT_DOCUMENT_LIMITS,
    DocumentIntegrityError,
    DocumentLimits,
    DocumentNotFoundError,
    DocumentQuotaError,
    DocumentValidationError,
    DocumentWorkspaceStore,
)
from mini_deerflow.workspace import _filesystem_path

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 64
TXT_BYTES = "Tài liệu thử nghiệm — deterministic fixture\n".encode()
PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT_MIME = "text/plain"


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def _store(root: Path, **overrides: Any) -> DocumentWorkspaceStore:
    values: dict[str, int] = {
        "max_document_bytes": 1_000,
        "max_documents_per_workspace": 3,
        "max_versions_per_document": 3,
        "max_workspace_bytes": 4_000,
        "max_workspaces": 3,
    }
    values.update(overrides)
    return DocumentWorkspaceStore(root, limits=DocumentLimits(**values))


def _workspace(store: DocumentWorkspaceStore, name: str = "Research") -> str:
    return _run(store.create_workspace(name)).workspace_id


def _mime_for(filename: str) -> str:
    extension = filename.rsplit(".", 1)[-1].lower()
    return {
        "pdf": PDF_MIME,
        "docx": DOCX_MIME,
        "txt": TXT_MIME,
    }.get(extension, "application/octet-stream")


def _upload(
    store: DocumentWorkspaceStore, workspace_id: str, filename: str, data: bytes
) -> Any:
    return _run(
        store.upload_document(
            workspace_id,
            filename=filename,
            content_type=_mime_for(filename),
            data=data,
        )
    )


def _version_path(
    root: Path, workspace_id: str, document_id: str, version: int
) -> Path:
    ws_key = hashlib.sha256(workspace_id.encode()).hexdigest()
    doc_key = hashlib.sha256(f"{workspace_id}:{document_id}".encode()).hexdigest()
    # Hashed components exceed MAX_PATH on Windows; use extended syntax so
    # direct fixture manipulation matches the store's own file operations.
    return _filesystem_path(
        root / "document-store" / f"ws-{ws_key}" / f"doc-{doc_key}" / f"v{version}"
    )


class TestWorkspaceLifecycle:
    def test_create_and_list_workspaces(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        first = _run(store.create_workspace("Literature"))
        second = _run(store.create_workspace("Slides"))
        assert first.workspace_id != second.workspace_id
        assert first.name == "Literature"
        assert first.document_count == 0
        assert first.total_bytes == 0
        listed = _run(store.list_workspaces())
        assert [item.name for item in listed] == ["Literature", "Slides"]

    def test_workspace_metadata_survives_restart(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        created = _workspace(store)
        reopened = DocumentWorkspaceStore(
            tmp_path, limits=DocumentLimits(max_document_bytes=1_000)
        )
        record = _run(reopened.get_workspace(created))
        assert record.name == "Research"
        assert record.status == "active"

    def test_unknown_workspace_is_not_found(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(DocumentNotFoundError):
            _run(store.get_workspace("ws-does-not-exist"))

    def test_workspace_name_validation_fails_closed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for name in ("", "   ", "x" * 81, None, 12, "\x00\x01\x02"):
            with pytest.raises(DocumentValidationError):
                _run(store.create_workspace(name))

    def test_workspace_quota_enforced(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for index in range(3):
            _run(store.create_workspace(f"w{index}"))
        with pytest.raises(DocumentQuotaError):
            _run(store.create_workspace("w4"))

    def test_deleted_workspace_disappears_and_frees_quota(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        _upload(store, workspace_id, "a.txt", TXT_BYTES)
        _run(store.delete_workspace(workspace_id))
        with pytest.raises(DocumentNotFoundError):
            _run(store.get_workspace(workspace_id))
        fourth = _run(store.create_workspace("w4"))
        assert fourth.status == "active"

    def test_workspace_name_is_control_sanitized(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        record = _run(store.create_workspace("  Name \x01 with \x02 controls\t"))
        assert record.name == "Name with controls"


class TestUploadValidation:
    def test_allowed_types_upload_as_inert_bytes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        for filename, data in (
            ("report.pdf", PDF_BYTES),
            ("memo.docx", DOCX_BYTES),
            ("notes.txt", TXT_BYTES),
        ):
            record = _upload(store, workspace_id, filename, data)
            assert record.filename == filename
            assert record.status == "active"
            assert record.latest_version == 1
            assert record.versions[0].byte_size == len(data)
            assert record.versions[0].sha256 == hashlib.sha256(data).hexdigest()

    def test_filename_with_unicode_and_spaces_is_metadata_only(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        filename = "Báo cáo tổng hợp - 2026.pdf"
        record = _upload(store, workspace_id, filename, PDF_BYTES)
        assert record.filename == filename
        for blob in (tmp_path / "document-store").rglob("*"):
            assert filename not in blob.name

    @pytest.mark.parametrize(
        "filename",
        ("tool.exe", "page.html", "sheet.xlsx", "old.doc", "noext", "archive.tar.gz"),
    )
    def test_unsupported_extensions_fail_closed(
        self, tmp_path: Path, filename: str
    ) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        with pytest.raises(DocumentValidationError):
            _upload(store, workspace_id, filename, b"anything")

    @pytest.mark.parametrize(
        "filename,data",
        (
            ("fake.pdf", b"not a pdf at all"),
            ("fake.docx", b"%PDF-1.7 nope"),
            ("fake.txt", b"invalid utf-8: \xff\xfe\xfa"),
        ),
    )
    def test_malformed_content_fails_magic_sniff(
        self, tmp_path: Path, filename: str, data: bytes
    ) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        with pytest.raises(DocumentValidationError):
            _upload(store, workspace_id, filename, data)

    def test_content_type_mismatch_with_extension_fails_closed(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        with pytest.raises(DocumentValidationError):
            _run(
                store.upload_document(
                    workspace_id,
                    filename="report.pdf",
                    content_type=TXT_MIME,
                    data=PDF_BYTES,
                )
            )

    def test_empty_body_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        with pytest.raises(DocumentValidationError):
            _upload(store, workspace_id, "empty.pdf", b"")

    def test_oversize_document_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        with pytest.raises(DocumentValidationError):
            _upload(store, workspace_id, "big.pdf", PDF_BYTES + b"x" * 2_000)

    @pytest.mark.parametrize(
        "filename",
        ("", " ", "x" * 201, "path/../escape.pdf", "back\\slash.pdf", "ctrl\x1b.pdf"),
    )
    def test_invalid_filenames_fail_closed(self, tmp_path: Path, filename: str) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        with pytest.raises(DocumentValidationError):
            _upload(store, workspace_id, filename, PDF_BYTES)

    def test_upload_to_missing_workspace_fails(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(DocumentNotFoundError):
            _upload(store, "ws-missing", "a.pdf", PDF_BYTES)


class TestQuotasAndVersions:
    def test_document_count_quota(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        for index in range(3):
            _upload(store, workspace_id, f"doc{index}.txt", TXT_BYTES)
        with pytest.raises(DocumentQuotaError):
            _upload(store, workspace_id, "doc3.txt", TXT_BYTES)

    def test_workspace_byte_quota_includes_all_versions(self, tmp_path: Path) -> None:
        store = _store(tmp_path, max_workspace_bytes=2_000)
        workspace_id = _workspace(store)
        chunk = b"%PDF-1.7 " + b"a" * 791
        assert len(chunk) == 800
        document = _upload(store, workspace_id, "doc.pdf", chunk)
        revised = b"%PDF-1.7 " + b"b" * 791
        _run(
            store.upload_version(
                workspace_id,
                document.document_id,
                content_type=PDF_MIME,
                data=revised,
            )
        )
        # 1600 bytes committed; a third 800-byte version exceeds 2000 only
        # when counted together with prior versions (2400 > 2000).
        with pytest.raises(DocumentQuotaError):
            _run(
                store.upload_version(
                    workspace_id,
                    document.document_id,
                    content_type=PDF_MIME,
                    data=b"%PDF-1.7 " + b"c" * 791,
                )
            )

    def test_versions_grow_and_old_versions_stay_readable(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", b"v1 content")
        updated = _run(
            store.upload_version(
                workspace_id,
                document.document_id,
                content_type=TXT_MIME,
                data=b"v2 content longer",
            )
        )
        assert updated.latest_version == 2
        _, v1, v1_bytes = _run(
            store.read_content(workspace_id, document.document_id, version=1)
        )
        assert v1_bytes == b"v1 content"
        assert v1.version == 1
        _, latest, latest_bytes = _run(
            store.read_content(workspace_id, document.document_id)
        )
        assert latest.version == 2
        assert latest_bytes == b"v2 content longer"
        versions = _run(store.get_document(workspace_id, document.document_id)).versions
        assert [v.version for v in versions] == [1, 2]

    def test_version_quota_enforced(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", b"one")
        _run(
            store.upload_version(
                workspace_id, document.document_id, content_type=TXT_MIME, data=b"two"
            )
        )
        _run(
            store.upload_version(
                workspace_id, document.document_id, content_type=TXT_MIME, data=b"three"
            )
        )
        with pytest.raises(DocumentQuotaError):
            _run(
                store.upload_version(
                    workspace_id,
                    document.document_id,
                    content_type=TXT_MIME,
                    data=b"four",
                )
            )

    def test_identical_latest_version_is_idempotent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", TXT_BYTES)
        again = _run(
            store.upload_version(
                workspace_id,
                document.document_id,
                content_type=TXT_MIME,
                data=TXT_BYTES,
            )
        )
        assert again.latest_version == 1
        assert len(again.versions) == 1

    def test_identical_initial_upload_creates_separate_document(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        first = _upload(store, workspace_id, "a.txt", TXT_BYTES)
        second = _upload(store, workspace_id, "b.txt", TXT_BYTES)
        assert first.document_id != second.document_id

    def test_version_content_type_must_match_document(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.pdf", PDF_BYTES)
        with pytest.raises(DocumentValidationError):
            _run(
                store.upload_version(
                    workspace_id,
                    document.document_id,
                    content_type=TXT_MIME,
                    data=TXT_BYTES,
                )
            )

    def test_version_upload_can_rename_display_filename(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "old.txt", b"v1")
        updated = _run(
            store.upload_version(
                workspace_id,
                document.document_id,
                filename="new name.txt",
                content_type=TXT_MIME,
                data=b"v2",
            )
        )
        assert updated.filename == "new name.txt"


class TestDeleteLifecycle:
    def test_delete_document_then_everything_404s(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", TXT_BYTES)
        _run(store.delete_document(workspace_id, document.document_id))
        for operation in (
            lambda: store.get_document(workspace_id, document.document_id),
            lambda: store.read_content(workspace_id, document.document_id),
            lambda: store.delete_document(workspace_id, document.document_id),
        ):
            with pytest.raises(DocumentNotFoundError):
                _run(operation())
        assert _run(store.list_documents(workspace_id)) == ()

    def test_delete_frees_document_quota(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        for index in range(3):
            _upload(store, workspace_id, f"d{index}.txt", TXT_BYTES)
        listed = _run(store.list_documents(workspace_id))
        _run(store.delete_document(workspace_id, listed[0].document_id))
        _upload(store, workspace_id, "d3.txt", TXT_BYTES)

    def test_deleted_document_blob_is_swept(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", TXT_BYTES)
        blob_dir = _version_path(tmp_path, workspace_id, document.document_id, 1)
        assert blob_dir.is_dir()
        _run(store.delete_document(workspace_id, document.document_id))
        assert not blob_dir.exists()


class TestCrossWorkspaceIsolation:
    def test_wrong_workspace_access_is_not_found(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_a = _workspace(store, "A")
        workspace_b = _workspace(store, "B")
        document = _upload(store, workspace_a, "secret.txt", TXT_BYTES)
        for operation in (
            lambda: store.get_document(workspace_b, document.document_id),
            lambda: store.read_content(workspace_b, document.document_id),
            lambda: store.delete_document(workspace_b, document.document_id),
            lambda: store.upload_version(
                workspace_b,
                document.document_id,
                content_type=TXT_MIME,
                data=b"x",
            ),
        ):
            with pytest.raises(DocumentNotFoundError):
                _run(operation())

    def test_same_filename_different_workspaces_no_aliasing(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        workspace_a = _workspace(store, "A")
        workspace_b = _workspace(store, "B")
        first = _upload(store, workspace_a, "shared.txt", b"content A")
        second = _upload(store, workspace_b, "shared.txt", b"content B")
        _, _, data = _run(store.read_content(workspace_a, first.document_id))
        assert data == b"content A"
        _, _, data = _run(store.read_content(workspace_b, second.document_id))
        assert data == b"content B"

    def test_independent_roots_are_isolated(self, tmp_path: Path) -> None:
        first_root = _store(tmp_path / "demo")
        second_root = _store(tmp_path / "live")
        workspace_id = _workspace(first_root)
        _upload(first_root, workspace_id, "doc.txt", TXT_BYTES)
        with pytest.raises(DocumentNotFoundError):
            _run(second_root.get_workspace(workspace_id))


class TestPersistenceAndRecovery:
    def test_documents_survive_restart(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.pdf", PDF_BYTES)
        _run(
            store.upload_version(
                workspace_id,
                document.document_id,
                content_type=PDF_MIME,
                data=PDF_BYTES + b" v2",
            )
        )
        reopened = _store(tmp_path)
        record = _run(reopened.get_document(workspace_id, document.document_id))
        assert record.latest_version == 2
        _, _, data = _run(
            reopened.read_content(workspace_id, document.document_id, version=1)
        )
        assert data == PDF_BYTES

    def test_interrupted_upload_leaves_no_phantom_document(
        self, tmp_path: Path
    ) -> None:
        """A crash after blob write but before metadata commit is sweepable."""
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        orphan_bytes = b"%PDF-1.7 orphan"
        sha = hashlib.sha256(orphan_bytes).hexdigest()
        ws_key = hashlib.sha256(workspace_id.encode()).hexdigest()
        doc_key = hashlib.sha256(b"ws-orphan:doc-orphan").hexdigest()
        ws_key = hashlib.sha256(b"ws-orphan").hexdigest()
        target = _filesystem_path(
            tmp_path / "document-store" / f"ws-{ws_key}" / f"doc-{doc_key}" / "v1"
        )
        target.mkdir(parents=True, exist_ok=True)
        _filesystem_path(target / f"{sha}.pdf").write_bytes(orphan_bytes)
        assert _run(store.list_documents(workspace_id)) == ()
        _run(store.recover())
        assert not target.exists()

    def test_staging_files_are_swept(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", TXT_BYTES)
        version_dir = _version_path(tmp_path, workspace_id, document.document_id, 1)
        sha = hashlib.sha256(TXT_BYTES).hexdigest()
        staging = version_dir / f".pending-{sha}.txt-{'a' * 32}"
        staging.write_bytes(b"partial")
        _run(store.recover())
        assert not staging.exists()
        assert any(version_dir.iterdir())

    def test_tombstoned_blob_swept_by_recovery(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", TXT_BYTES)
        blob_dir = _version_path(tmp_path, workspace_id, document.document_id, 1)
        assert blob_dir.is_dir()
        connection = sqlite3.connect(tmp_path / "documents.sqlite")
        connection.execute(
            "UPDATE documents SET status = 'deleted' WHERE document_id = ?",
            (document.document_id,),
        )
        connection.commit()
        connection.close()
        assert blob_dir.is_dir()
        _run(store.recover())
        assert not blob_dir.exists()

    def test_recovery_never_removes_active_blobs(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", TXT_BYTES)
        other = _upload(store, workspace_id, "other.pdf", PDF_BYTES)
        _run(store.recover())
        _run(store.recover())
        _, _, data = _run(store.read_content(workspace_id, document.document_id))
        assert data == TXT_BYTES
        _, _, data = _run(store.read_content(workspace_id, other.document_id))
        assert data == PDF_BYTES

    def test_corrupted_blob_detected_on_read(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", TXT_BYTES)
        blob = next(
            _version_path(tmp_path, workspace_id, document.document_id, 1).iterdir()
        )
        blob.write_bytes(b"tampered")
        with pytest.raises(DocumentIntegrityError):
            _run(store.read_content(workspace_id, document.document_id))

    def test_symlinked_blob_rejected_on_read(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", TXT_BYTES)
        secret = tmp_path / "outside-secret.txt"
        secret.write_bytes(b"outside")
        blob = next(
            _version_path(tmp_path, workspace_id, document.document_id, 1).iterdir()
        )
        blob.unlink()
        try:
            blob.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable on this platform")
        with pytest.raises((DocumentIntegrityError, DocumentNotFoundError)):
            _run(store.read_content(workspace_id, document.document_id))

    def test_symlinked_storage_root_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "real-store"
        real.mkdir()
        link = tmp_path / "linked-store"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable on this platform")
        with pytest.raises(DocumentIntegrityError):
            DocumentWorkspaceStore(link, limits=DEFAULT_DOCUMENT_LIMITS)


class TestSchemaIntegrity:
    def test_future_schema_version_fails_closed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _workspace(store)
        connection = sqlite3.connect(tmp_path / "documents.sqlite")
        connection.execute("UPDATE document_store_meta SET schema_version = 99")
        connection.commit()
        connection.close()
        with pytest.raises(DocumentIntegrityError):
            _run(store.list_workspaces())

    def test_error_messages_never_leak_storage_root(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        messages = []
        try:
            _run(store.get_workspace("ws-missing"))
        except DocumentNotFoundError as error:
            messages.append(str(error))
        try:
            _upload(store, "ws-missing", "a.pdf", PDF_BYTES)
        except DocumentNotFoundError as error:
            messages.append(str(error))
        second = _store(tmp_path)
        workspace_id = _workspace(second)
        try:
            _upload(second, workspace_id, "a.exe", b"x")
        except DocumentValidationError as error:
            messages.append(str(error))
        for message in messages:
            assert str(tmp_path) not in message
            assert "Traceback" not in message

    def test_aggregates_reflect_active_documents_only(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        first = _upload(store, workspace_id, "a.txt", TXT_BYTES)
        _upload(store, workspace_id, "b.pdf", PDF_BYTES)
        _run(store.delete_document(workspace_id, first.document_id))
        record = _run(store.get_workspace(workspace_id))
        assert record.document_count == 1
        assert record.total_bytes == len(PDF_BYTES)

    def test_document_record_contract_is_json_serializable(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "doc.txt", TXT_BYTES)
        payload = document.model_dump(mode="json")
        assert set(payload) == {
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
        json.dumps(payload)

    def test_identical_version_upload_still_applies_rename(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        workspace_id = _workspace(store)
        document = _upload(store, workspace_id, "old.txt", TXT_BYTES)
        renamed = _run(
            store.upload_version(
                workspace_id,
                document.document_id,
                filename="renamed-identical.txt",
                content_type=TXT_MIME,
                data=TXT_BYTES,
            )
        )
        assert renamed.latest_version == 1
        assert len(renamed.versions) == 1
        assert renamed.filename == "renamed-identical.txt"


def test_resolve_document_limits_applies_env_without_api_key(monkeypatch) -> None:
    from mini_deerflow.documents import resolve_document_limits

    monkeypatch.delenv("MINI_DEERFLOW_API_KEY", raising=False)
    monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_MAX_BYTES", "1048576")
    limits = resolve_document_limits()
    assert limits.max_document_bytes == 1_048_576
    assert limits.max_documents_per_workspace == 50


def test_resolve_document_limits_falls_back_on_invalid_settings(
    monkeypatch,
) -> None:
    from mini_deerflow.documents import DEFAULT_DOCUMENT_LIMITS, resolve_document_limits

    monkeypatch.setenv("MINI_DEERFLOW_TEMPERATURE", "not-a-number")
    limits = resolve_document_limits()
    assert limits == DEFAULT_DOCUMENT_LIMITS


@pytest.mark.parametrize(
    "env_name,value",
    (
        ("MINI_DEERFLOW_DOCUMENT_MAX_BYTES", "999"),  # below 1 KiB floor
        ("MINI_DEERFLOW_DOCUMENT_MAX_BYTES", "not-a-number"),
        ("MINI_DEERFLOW_DOCUMENT_WORKSPACE_MAX_DOCUMENTS", "0"),
        ("MINI_DEERFLOW_DOCUMENT_MAX_VERSIONS", "-5"),
        ("MINI_DEERFLOW_DOCUMENT_WORKSPACE_MAX_BYTES", "1"),  # below 1 MiB floor
        ("MINI_DEERFLOW_DOCUMENT_MAX_WORKSPACES", "bogus"),
    ),
)
def test_resolve_document_limits_fails_closed_on_invalid_document_override(
    monkeypatch, env_name: str, value: str
) -> None:
    """An explicitly invalid document quota must never broaden to defaults."""

    from mini_deerflow.documents import DocumentLimitsError, resolve_document_limits

    monkeypatch.setenv(env_name, value)
    with pytest.raises(DocumentLimitsError) as error:
        resolve_document_limits()
    message = str(error.value)
    assert str(value) not in message
    assert "quota" in message


def test_invalid_document_override_fails_closed_without_provider_key(
    monkeypatch,
) -> None:
    from mini_deerflow.documents import DocumentLimitsError, resolve_document_limits

    monkeypatch.delenv("MINI_DEERFLOW_API_KEY", raising=False)
    monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_MAX_BYTES", "999")
    with pytest.raises(DocumentLimitsError):
        resolve_document_limits()


def test_invalid_document_override_beats_unrelated_invalid_settings(
    monkeypatch,
) -> None:
    from mini_deerflow.documents import DocumentLimitsError, resolve_document_limits

    monkeypatch.setenv("MINI_DEERFLOW_TEMPERATURE", "not-a-number")
    monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_MAX_BYTES", "999")
    with pytest.raises(DocumentLimitsError):
        resolve_document_limits()


def test_valid_document_override_with_unrelated_invalid_setting_fails_closed(
    monkeypatch,
) -> None:
    """A valid explicit quota must never be discarded via unrelated errors."""

    from mini_deerflow.documents import DocumentLimitsError, resolve_document_limits

    monkeypatch.setenv("MINI_DEERFLOW_TEMPERATURE", "not-a-number")
    monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_MAX_BYTES", "2048")
    with pytest.raises(DocumentLimitsError):
        resolve_document_limits()


def test_document_override_in_env_file_counts_as_explicit(
    tmp_path: Path, monkeypatch
) -> None:
    from mini_deerflow.documents import DocumentLimitsError, resolve_document_limits

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINI_DEERFLOW_TEMPERATURE", "not-a-number")
    (tmp_path / ".env").write_text(
        "MINI_DEERFLOW_DOCUMENT_MAX_BYTES=2048\n", encoding="utf-8"
    )
    with pytest.raises(DocumentLimitsError):
        resolve_document_limits()


def test_unrelated_invalid_setting_without_any_override_keeps_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    from mini_deerflow.documents import DEFAULT_DOCUMENT_LIMITS, resolve_document_limits

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINI_DEERFLOW_TEMPERATURE", "not-a-number")
    for name in (
        "MINI_DEERFLOW_DOCUMENT_MAX_BYTES",
        "MINI_DEERFLOW_DOCUMENT_WORKSPACE_MAX_DOCUMENTS",
        "MINI_DEERFLOW_DOCUMENT_MAX_VERSIONS",
        "MINI_DEERFLOW_DOCUMENT_WORKSPACE_MAX_BYTES",
        "MINI_DEERFLOW_DOCUMENT_MAX_WORKSPACES",
    ):
        monkeypatch.delenv(name, raising=False)
    assert resolve_document_limits() == DEFAULT_DOCUMENT_LIMITS
