"""Lexical retrieval, version pinning, budgets, and citation validation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from mini_deerflow.document_extraction import (
    DEFAULT_EXTRACTION_LIMITS,
    ExtractionRecord,
    extract_document,
)
from mini_deerflow.document_retrieval import (
    DEFAULT_RETRIEVAL_LIMITS,
    CitationIntegrityError,
    CitationNotFoundError,
    CitationStaleError,
    LexicalRetriever,
    RetrievalLimits,
    RetrievalValidationError,
    resolve_citation,
    tokenize,
)
from mini_deerflow.documents import (
    DocumentLimits,
    DocumentLimitsError,
    DocumentWorkspaceStore,
    resolve_document_retrieval_limits,
)

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT_MIME = "text/plain"


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def _retrieve(store, workspace_id, query, scope, limits):
    return _run(LexicalRetriever().retrieve(store, workspace_id, query, scope, limits))


def _resolve(store, workspace_id, document_id, chunk_id, version=None):
    return _run(resolve_citation(store, workspace_id, document_id, chunk_id, version))


def _store(root: Path) -> DocumentWorkspaceStore:
    return DocumentWorkspaceStore(
        root, limits=DocumentLimits(max_document_bytes=2_000_000)
    )


def _build_docx(paragraphs: list[str]) -> bytes:
    from tests.test_document_extraction import _build_docx as builder

    return builder(paragraphs)


def _seed_extraction(
    store: DocumentWorkspaceStore,
    workspace_id: str,
    filename: str,
    data: bytes,
    mime: str,
):
    """Upload + extract one document; returns (document_id, version, chunks)."""

    import hashlib

    from mini_deerflow.document_extraction import EXTRACTOR_VERSION

    document = _run(
        store.upload_document(
            workspace_id, filename=filename, content_type=mime, data=data
        )
    )
    _, chunks, total = extract_document(mime, data, DEFAULT_EXTRACTION_LIMITS)
    now = "2026-11-10T00:00:00+00:00"
    record = ExtractionRecord(
        workspace_id=workspace_id,
        document_id=document.document_id,
        version=1,
        source_sha256=hashlib.sha256(data).hexdigest(),
        status="completed",
        error_class=None,
        extractor_version=EXTRACTOR_VERSION,
        chunk_count=len(chunks),
        total_chars=total,
        created_at=now,
        updated_at=now,
    )
    _run(store.save_extraction(record, chunks))
    return document.document_id, 1, chunks


class TestTokenizer:
    def test_vietnamese_diacritics_folded(self) -> None:
        assert tokenize("trường học") == tokenize("truong hoc")

    def test_lowercase_and_split(self) -> None:
        assert tokenize("State-of-the-Art, 2026!") == (
            "state",
            "of",
            "the",
            "art",
            "2026",
        )

    def test_empty_and_punctuation_only(self) -> None:
        assert tokenize("!!! ... ???") == ()
        assert tokenize("") == ()


class TestRetrieval:
    def _workspace(self, store: DocumentWorkspaceStore) -> str:
        return _run(store.create_workspace("Research")).workspace_id

    def test_matching_chunk_ranks_first(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        _seed_extraction(store, workspace_id, "a.txt", b"alpha beta gamma\n", TXT_MIME)
        _seed_extraction(
            store,
            workspace_id,
            "b.txt",
            b"completely unrelated content about gardening\n",
            TXT_MIME,
        )
        result = _retrieve(
            store, workspace_id, "alpha beta", None, DEFAULT_RETRIEVAL_LIMITS
        )
        assert result.hits
        assert "alpha" in result.hits[0].preview.lower()

    def test_vietnamese_diacritic_insensitive_match(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        _seed_extraction(
            store,
            workspace_id,
            "vi.docx",
            _build_docx(["Trường học ở thành phố Hồ Chí Minh có nhiều học sinh giỏi."]),
            DOCX_MIME,
        )
        result = _retrieve(
            store, workspace_id, "truong hoc", None, DEFAULT_RETRIEVAL_LIMITS
        )
        assert result.hits, "diacritic-stripped query must match"
        result2 = _retrieve(
            store, workspace_id, "thành phố", None, DEFAULT_RETRIEVAL_LIMITS
        )
        assert result2.hits

    def test_no_token_overlap_returns_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        _seed_extraction(
            store, workspace_id, "a.txt", b"quantum entanglement basics\n", TXT_MIME
        )
        result = _retrieve(
            store, workspace_id, "gardening roses", None, DEFAULT_RETRIEVAL_LIMITS
        )
        assert result.hits == ()
        assert result.total_candidates >= 1

    def test_scope_limits_corpus(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        doc_a, _, _ = _seed_extraction(
            store, workspace_id, "a.txt", b"solar panels efficiency\n", TXT_MIME
        )
        _seed_extraction(store, workspace_id, "b.txt", b"solar panels cost\n", TXT_MIME)
        result = _retrieve(
            store, workspace_id, "solar panels", (doc_a,), DEFAULT_RETRIEVAL_LIMITS
        )
        assert {hit.document_id for hit in result.hits} == {doc_a}

    def test_scope_with_unknown_document_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        with pytest.raises(RetrievalValidationError):
            _retrieve(
                store,
                workspace_id,
                "query",
                ("doc-does-not-exist",),
                DEFAULT_RETRIEVAL_LIMITS,
            )

    def test_query_length_cap(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        with pytest.raises(RetrievalValidationError):
            _retrieve(store, workspace_id, "x" * 401, None, DEFAULT_RETRIEVAL_LIMITS)

    def test_hit_budget_and_context_budget(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        for index in range(6):
            _seed_extraction(
                store,
                workspace_id,
                f"doc{index}.txt",
                f"shared keyword number {index}\n".encode(),
                TXT_MIME,
            )
        tight = RetrievalLimits(max_hits=3, max_context_chars=100_000)
        result = _retrieve(store, workspace_id, "shared keyword", None, tight)
        assert len(result.hits) == 3
        char_budget = RetrievalLimits(
            max_hits=100, max_context_chars=len("shared keyword number 0")
        )
        result2 = _retrieve(store, workspace_id, "shared keyword", None, char_budget)
        assert result2.truncated_by_budget

    def test_preview_bounded(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        _seed_extraction(
            store,
            workspace_id,
            "long.txt",
            b"keyword " + b"filler " * 900 + b"\n",
            TXT_MIME,
        )
        result = _retrieve(
            store, workspace_id, "keyword", None, DEFAULT_RETRIEVAL_LIMITS
        )
        assert all(len(hit.preview) <= 200 for hit in result.hits)

    def test_deterministic_across_instances(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        for index in range(4):
            _seed_extraction(
                store,
                workspace_id,
                f"d{index}.txt",
                f"common token payload variant {index}\n".encode(),
                TXT_MIME,
            )
        first = _retrieve(
            store, workspace_id, "common token", None, DEFAULT_RETRIEVAL_LIMITS
        )
        reopened = _store(tmp_path)
        second = _retrieve(
            reopened, workspace_id, "common token", None, DEFAULT_RETRIEVAL_LIMITS
        )
        assert [hit.chunk_id for hit in first.hits] == [
            hit.chunk_id for hit in second.hits
        ]
        assert [round(hit.score, 10) for hit in first.hits] == [
            round(hit.score, 10) for hit in second.hits
        ]


class TestVersionPinningAndIsolation:
    def _workspace(self, store: DocumentWorkspaceStore, name: str = "W") -> str:
        return _run(store.create_workspace(name)).workspace_id

    def test_new_version_supersedes_old_chunks(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        import hashlib

        from mini_deerflow.document_extraction import EXTRACTOR_VERSION

        document = _run(
            store.upload_document(
                workspace_id,
                filename="doc.txt",
                content_type=TXT_MIME,
                data=b"version one says apples\n",
            )
        )
        _run(
            store.upload_version(
                workspace_id,
                document.document_id,
                content_type=TXT_MIME,
                data=b"version two says bananas\n",
            )
        )
        # Extract only version 1.
        _, chunks, _ = extract_document(
            TXT_MIME, b"version one says apples\n", DEFAULT_EXTRACTION_LIMITS
        )
        record = ExtractionRecord(
            workspace_id=workspace_id,
            document_id=document.document_id,
            version=1,
            source_sha256=hashlib.sha256(b"version one says apples\n").hexdigest(),
            status="completed",
            error_class=None,
            extractor_version=EXTRACTOR_VERSION,
            chunk_count=len(chunks),
            total_chars=30,
            created_at="2026-11-10T00:00:00+00:00",
            updated_at="2026-11-10T00:00:00+00:00",
        )
        _run(store.save_extraction(record, chunks))
        # Latest version (2) has no completed extraction: nothing retrievable.
        result = _retrieve(
            store, workspace_id, "apples", None, DEFAULT_RETRIEVAL_LIMITS
        )
        assert result.hits == ()

    def test_deleted_document_never_retrieved(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        document_id, _, _ = _seed_extraction(
            store, workspace_id, "a.txt", b"oranges and lemons\n", TXT_MIME
        )
        _run(store.delete_document(workspace_id, document_id))
        result = _retrieve(
            store, workspace_id, "oranges", None, DEFAULT_RETRIEVAL_LIMITS
        )
        assert result.hits == ()

    def test_cross_workspace_isolation(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_a = self._workspace(store, "A")
        workspace_b = self._workspace(store, "B")
        _seed_extraction(
            store, workspace_a, "secret.txt", b"classified kiwi recipes\n", TXT_MIME
        )
        result = _retrieve(store, workspace_b, "kiwi", None, DEFAULT_RETRIEVAL_LIMITS)
        assert result.hits == ()

    def test_duplicate_content_distinct_citations(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        data = b"identical content about plums\n"
        doc_a, version_a, chunks_a = _seed_extraction(
            store, workspace_id, "one.txt", data, TXT_MIME
        )
        doc_b, version_b, chunks_b = _seed_extraction(
            store, workspace_id, "two.txt", data, TXT_MIME
        )
        assert doc_a != doc_b
        assert chunks_a[0].chunk_id == chunks_b[0].chunk_id
        result = _retrieve(store, workspace_id, "plums", None, DEFAULT_RETRIEVAL_LIMITS)
        assert {hit.document_id for hit in result.hits} == {doc_a, doc_b}
        assert all(hit.version in (version_a, version_b) for hit in result.hits)

    def test_orphan_chunk_rows_never_retrieved(self, tmp_path: Path) -> None:
        """Corrupt-index analog: tampered rows without a live extraction join."""
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        document_id, _, chunks = _seed_extraction(
            store, workspace_id, "a.txt", b"real mango content\n", TXT_MIME
        )
        connection = sqlite3.connect(tmp_path / "documents.sqlite")
        connection.execute(
            "DELETE FROM document_extractions WHERE document_id = ?",
            (document_id,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO document_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                document_id,
                1,
                1,
                chunks[0].chunk_id,
                "lines",
                "fake injected chunk text",
                0,
                10,
                '{"kind": "lines", "line_start": 1, "line_end": 1}',
            ),
        )
        connection.commit()
        connection.close()
        result = _retrieve(store, workspace_id, "mango", None, DEFAULT_RETRIEVAL_LIMITS)
        assert result.hits == ()
        result2 = _retrieve(
            store, workspace_id, "injected", None, DEFAULT_RETRIEVAL_LIMITS
        )
        assert result2.hits == ()


class TestCitationResolution:
    def _workspace(self, store: DocumentWorkspaceStore) -> str:
        return _run(store.create_workspace("Cite")).workspace_id

    def test_resolves_persisted_chunk(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        document_id, _, chunks = _seed_extraction(
            store, workspace_id, "a.txt", b"citation target text\n", TXT_MIME
        )
        record, chunk = _resolve(store, workspace_id, document_id, chunks[0].chunk_id)
        assert record.document_id == document_id
        assert chunk.text == "citation target text"
        assert chunk.locator.kind == "lines"

    def test_stale_version_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        data = b"first version content\n"
        document_id, _, chunks = _seed_extraction(
            store, workspace_id, "a.txt", data, TXT_MIME
        )
        _run(
            store.upload_version(
                workspace_id,
                document_id,
                content_type=TXT_MIME,
                data=b"second version content\n",
            )
        )
        with pytest.raises(CitationStaleError):
            _resolve(store, workspace_id, document_id, chunks[0].chunk_id)

    def test_deleted_document_not_found(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        document_id, _, chunks = _seed_extraction(
            store, workspace_id, "a.txt", b"deleted soon\n", TXT_MIME
        )
        _run(store.delete_document(workspace_id, document_id))
        with pytest.raises(CitationNotFoundError):
            _resolve(store, workspace_id, document_id, chunks[0].chunk_id)

    def test_cross_workspace_not_found(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_a = _run(store.create_workspace("A")).workspace_id
        workspace_b = _run(store.create_workspace("B")).workspace_id
        document_id, _, chunks = _seed_extraction(
            store, workspace_a, "a.txt", b"private content\n", TXT_MIME
        )
        with pytest.raises(CitationNotFoundError):
            _resolve(store, workspace_b, document_id, chunks[0].chunk_id)

    def test_unknown_chunk_not_found(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        document_id, _, _ = _seed_extraction(
            store, workspace_id, "a.txt", b"some content\n", TXT_MIME
        )
        with pytest.raises(CitationNotFoundError):
            _resolve(store, workspace_id, document_id, "0" * 16)

    def test_tampered_locator_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        document_id, _, chunks = _seed_extraction(
            store, workspace_id, "a.txt", b"tamper target\n", TXT_MIME
        )
        connection = sqlite3.connect(tmp_path / "documents.sqlite")
        connection.execute(
            "UPDATE document_chunks SET locator_json = 'not-json' "
            "WHERE document_id = ? AND chunk_id = ?",
            (document_id, chunks[0].chunk_id),
        )
        connection.commit()
        connection.close()
        with pytest.raises(CitationIntegrityError):
            _resolve(store, workspace_id, document_id, chunks[0].chunk_id)

    def test_every_hit_resolves_to_persisted_source(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        workspace_id = self._workspace(store)
        _seed_extraction(
            store,
            workspace_id,
            "a.docx",
            _build_docx(
                [
                    "Kiwi birds are flightless.",
                    "Mango trees need tropical climates.",
                    "Solar power is renewable energy.",
                ]
            ),
            DOCX_MIME,
        )
        result = _retrieve(
            store, workspace_id, "mango tropical", None, DEFAULT_RETRIEVAL_LIMITS
        )
        assert result.hits
        for hit in result.hits:
            record, chunk = _resolve(store, workspace_id, hit.document_id, hit.chunk_id)
            assert record.version == hit.version
            assert chunk.chunk_id == hit.chunk_id
            assert hit.preview in chunk.text or chunk.text in hit.preview


class TestLimitsAndDeadline:
    def test_defaults(self) -> None:
        limits = DEFAULT_RETRIEVAL_LIMITS
        assert limits.max_hits == 8
        assert limits.max_context_chars == 12_000
        assert limits.max_query_chars == 400

    def test_validation(self) -> None:
        for kwargs in (
            {"max_hits": 0},
            {"max_context_chars": 0},
            {"max_query_chars": -1},
            {"deadline_seconds": -0.5},
            {"deadline_seconds": float("nan")},
        ):
            with pytest.raises(ValueError):
                RetrievalLimits(**kwargs)

    def test_invalid_env_override_fails_closed(self, monkeypatch) -> None:
        monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_RETRIEVAL_MAX_HITS", "0")
        with pytest.raises(DocumentLimitsError):
            resolve_document_retrieval_limits()

    def test_valid_env_override_applies(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("MINI_DEERFLOW_DOCUMENT_RETRIEVAL_MAX_HITS", "3")
        limits = resolve_document_retrieval_limits()
        assert limits.max_hits == 3

    def test_zero_deadline_refuses(self, tmp_path: Path) -> None:
        from mini_deerflow.document_retrieval import (
            RetrievalDeadlineExceededError,
        )

        store = _store(tmp_path)
        workspace_id = _run(store.create_workspace("D")).workspace_id
        _seed_extraction(
            store, workspace_id, "a.txt", b"deadline probe content\n", TXT_MIME
        )
        with pytest.raises(RetrievalDeadlineExceededError):
            _retrieve(
                store,
                workspace_id,
                "deadline",
                None,
                RetrievalLimits(deadline_seconds=0.0),
            )
