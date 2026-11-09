"""Bounded document extraction: parsers, chunking, determinism, safety."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from mini_deerflow.document_extraction import (
    DEFAULT_EXTRACTION_LIMITS,
    EXTRACTOR_VERSION,
    DocumentExtractionLimits,
    ExtractionRecord,
    ExtractionUnavailableError,
    extract_document,
)
from mini_deerflow.documents import DocumentLimits, DocumentWorkspaceStore

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT_MIME = "text/plain"

VI_TEXT = (
    "Đây là đoạn văn bản tiếng Việt có dấu: trường học, người bạn, xã hội.\n"
    "Thành phố Hồ Chí Minh — nền kinh tế phát triển nhanh.\n"
)
EN_TEXT = "The quick brown fox jumps over the lazy dog.\n"


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def _store(root: Path) -> DocumentWorkspaceStore:
    return DocumentWorkspaceStore(
        root, limits=DocumentLimits(max_document_bytes=2_000_000)
    )


def _upload(store: DocumentWorkspaceStore, filename: str, data: bytes, mime: str):
    workspace = _run(store.create_workspace("Extraction"))
    document = _run(
        store.upload_document(
            workspace.workspace_id,
            filename=filename,
            content_type=mime,
            data=data,
        )
    )
    return workspace.workspace_id, document


# ---------------------------------------------------------------------------
# Fixture builders (synthetic documents only)
# ---------------------------------------------------------------------------
def _build_pdf(
    pages: list[list[str]],
    *,
    scripted: bool = False,
    catalog_extra: str = "",
    page_extra: str = "",
    extra_objects: dict[int, bytes] | None = None,
) -> bytes:
    """Hand-build a minimal but structurally valid PDF with text layers."""

    objects: list[bytes] = []

    def obj(number: int, body: bytes) -> None:
        objects.append(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")

    page_ids = [3 + 2 * index for index in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    obj(1, f"<< /Type /Catalog /Pages 2 0 R {catalog_extra} >>".encode())
    obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for index, lines in enumerate(pages):
        page_id = page_ids[index]
        content_id = page_id + 1
        stream_parts = ["BT", "/F1 12 Tf"]
        y = 720
        for line in lines:
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            if line.startswith("\x00bare "):
                # Deferred-flush shape: bare Tj runs with no positioning ops
                # never flush pypdf's text visitor mid-stream.
                stream_parts.append(f"({escaped[6:]}) Tj")
                continue
            stream_parts.append(f"1 0 0 1 72 {y} Tm ({escaped}) Tj")
            y -= 20
        stream_parts.append("ET")
        stream = "\n".join(stream_parts).encode("latin-1", errors="replace")
        obj(
            page_id,
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 900 0 R >> >> "
                + page_extra.encode()
                + b" /Contents "
                + f"{content_id} 0 R".encode()
                + b" >>"
            ),
        )
        obj(
            content_id,
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream",
        )
    for number, body in sorted((extra_objects or {}).items()):
        obj(number, body)
    obj(900, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for chunk in objects:
        offsets.append(out.tell())
        out.write(chunk)
    xref_at = out.tell()
    count = len(objects) + 1
    out.write(f"xref\n0 {count}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    )
    data = out.getvalue()
    if scripted:
        data = data.replace(
            b"/Type /Catalog /Pages 2 0 R",
            b"/Type /Catalog /Pages 2 0 R /OpenAction []",
        )
    return data


def _encrypted_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


_DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_body(
    paragraphs: list[str], tables: list[list[list[str]]] | None = None
) -> str:
    parts = []
    for text in paragraphs:
        parts.append(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>")
    for table in tables or []:
        rows = []
        for row in table:
            cells = "".join(
                f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row
            )
            rows.append(f"<w:tr>{cells}</w:tr>")
        parts.append("<w:tbl>" + "".join(rows) + "</w:tbl>")
    return "".join(parts)


def _build_docx(
    paragraphs: list[str],
    tables: list[list[list[str]]] | None = None,
    *,
    macro: bool = False,
    external_rels: bool = False,
    rels_xml: str | None = None,
    extra_entries: dict[str, bytes] | None = None,
    document_xml: str | None = None,
) -> bytes:
    body = _docx_body(paragraphs, tables)
    xml = (
        document_xml
        if document_xml is not None
        else f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_DOCX_NS}"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        rels = (
            rels_xml
            if rels_xml is not None
            else (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                'Target="https://example.invalid/image.png" TargetMode="External"/>'
                "</Relationships>"
                if external_rels
                else '<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>'
            )
        )
        archive.writestr("word/_rels/document.xml.rels", rels)
        archive.writestr("word/document.xml", xml)
        if macro:
            archive.writestr("word/vbaProject.bin", b"\x00\x01\x02macro-payload")
        for name, payload in (extra_entries or {}).items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class TestFormatExtraction:
    def test_pdf_pages_become_chunks_with_genuine_page_anchors(self) -> None:
        data = _build_pdf(
            [
                ["Alpha page one", "second line"],
                ["Beta page two"],
                ["Gamma page three"],
            ]
        )
        _, chunks, total = extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert total > 0
        pages = [chunk.locator.page for chunk in chunks]
        assert pages == [1, 2, 3]
        assert chunks[0].text.startswith("Alpha page one")
        assert "Beta page two" in chunks[1].text
        assert chunks[0].kind == "page"

    def test_pdf_long_page_splits_within_same_page(self) -> None:
        long_lines = [
            f"paragraph number {index} with some padding text" for index in range(120)
        ]
        data = _build_pdf([long_lines])
        _, chunks, _ = extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert len(chunks) > 1
        assert all(chunk.locator.page == 1 for chunk in chunks)

    def test_docx_paragraphs_and_tables_extracted(self) -> None:
        data = _build_docx(
            ["Intro paragraph.", "Second paragraph with more words."],
            tables=[[["Tên", "Giá trị"], ["Hàng 2", "42"]]],
        )
        _, chunks, _ = extract_document(DOCX_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        joined = "\n".join(chunk.text for chunk in chunks)
        assert "Intro paragraph." in joined
        assert "Tên | Giá trị" in joined
        assert "Hàng 2 | 42" in joined
        table_chunks = [chunk for chunk in chunks if chunk.kind == "table"]
        assert table_chunks and table_chunks[0].locator.table == 1
        paragraph_chunks = [chunk for chunk in chunks if chunk.kind == "paragraph"]
        assert paragraph_chunks
        # DOCX never invents page numbers.
        assert all(chunk.locator.page is None for chunk in chunks)

    def test_txt_line_offsets_are_one_based_and_accurate(self) -> None:
        text = VI_TEXT + EN_TEXT + "final line\n"
        _, chunks, _ = extract_document(
            TXT_MIME, text.encode(), DEFAULT_EXTRACTION_LIMITS
        )
        joined = "\n".join(chunk.text for chunk in chunks)
        assert VI_TEXT.strip() in joined
        first = chunks[0]
        assert first.locator.kind == "lines"
        assert first.locator.line_start == 1
        lines = text.splitlines()
        for chunk in chunks:
            extracted = lines[chunk.locator.line_start - 1 : chunk.locator.line_end]
            expected = "\n".join(extracted)
            assert expected in chunk.text or chunk.text in expected

    def test_vietnamese_diacritics_preserved_docx_and_txt(self) -> None:
        docx = _build_docx([VI_TEXT.strip()])
        _, chunks, _ = extract_document(DOCX_MIME, docx, DEFAULT_EXTRACTION_LIMITS)
        assert VI_TEXT.strip() in "\n".join(chunk.text for chunk in chunks)
        _, txt_chunks, _ = extract_document(
            TXT_MIME, VI_TEXT.encode(), DEFAULT_EXTRACTION_LIMITS
        )
        assert VI_TEXT.strip() in "\n".join(chunk.text for chunk in txt_chunks)


class TestDeterminism:
    def test_same_bytes_produce_identical_chunk_ids(self) -> None:
        data = _build_pdf([["Determinism", "check"], ["page two"]])
        _, first, _ = extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        _, second, _ = extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert [chunk.chunk_id for chunk in first] == [
            chunk.chunk_id for chunk in second
        ]
        assert [chunk.text for chunk in first] == [chunk.text for chunk in second]

    def test_chunk_ids_tie_to_version_hash_and_index(self) -> None:
        data = _build_pdf([["hash", "tie"]])
        source_sha = hashlib.sha256(data).hexdigest()
        _, chunks, _ = extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        expected = hashlib.sha256(f"{source_sha}:{1}".encode()).hexdigest()[:16]
        assert chunks[0].chunk_id == expected

    def test_different_content_produces_different_chunk_ids(self) -> None:
        first = _build_pdf([["content A"]])
        second = _build_pdf([["content B with different text"]])
        _, first_chunks, _ = extract_document(
            PDF_MIME, first, DEFAULT_EXTRACTION_LIMITS
        )
        _, second_chunks, _ = extract_document(
            PDF_MIME, second, DEFAULT_EXTRACTION_LIMITS
        )
        assert first_chunks[0].chunk_id != second_chunks[0].chunk_id


class TestFailClosedAndBudgets:
    def test_encrypted_pdf_unavailable(self) -> None:
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, _encrypted_pdf(), DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "encrypted"

    def test_scripted_pdf_unavailable(self) -> None:
        data = _build_pdf([["innocent"]], scripted=True)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "embedded_script"

    @pytest.mark.parametrize(
        "catalog_extra,page_extra,extra_objects",
        (
            ("", "/AA << /O << /S /JavaScript /JS (x) >> >>", None),
            ("", "/AA << /C << /S /JavaScript /JS (x) >> >>", None),
            ("/Names << /JavaScript << >> >>", "", None),
            ("/AcroForm << /XFA 901 0 R >>", "", None),
            (
                "",
                "/Annots [901 0 R]",
                {
                    901: (
                        b"<< /Type /Annot /Subtype /Widget "
                        b"/AA << /K << /S /JavaScript /JS (x) >> >> >>"
                    )
                },
            ),
            (
                "",
                "/Annots [901 0 R]",
                {
                    901: (
                        b"<< /Type /Annot /Subtype /Link "
                        b"/A << /S /JavaScript /JS (x) >> >>"
                    )
                },
            ),
        ),
        ids=[
            "page-aa-o",
            "page-aa-c",
            "names-js",
            "acroform-xfa",
            "annot-aa",
            "annot-action",
        ],
    )
    def test_scripted_pdf_variants_are_refused(
        self, catalog_extra: str, page_extra: str, extra_objects
    ) -> None:
        data = _build_pdf(
            ["innocent"],
            catalog_extra=catalog_extra,
            page_extra=page_extra,
            extra_objects=extra_objects,
        )
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "embedded_script"

    def test_pypdf_error_maps_to_budget_not_raw_escape(self, monkeypatch) -> None:
        """LimitReachedError (pypdf's own DoS guard) stays a closed class."""

        from pypdf.errors import LimitReachedError

        class _Boom:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                raise LimitReachedError("limit reached while decompressing")

        monkeypatch.setattr("pypdf.PdfReader", _Boom)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, b"%PDF-1.4 whatever", DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "budget_exceeded"

    def test_multi_page_char_budget_aborts_early(self) -> None:
        big_page = [f"text block {index} padding" for index in range(4_000)]
        data = _build_pdf([big_page] * 6)
        limits = DocumentExtractionLimits(max_chars=50_000)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, data, limits)
        assert error.value.error_class == "budget_exceeded"

    def test_deferred_flush_bomb_still_budget_bounded(self) -> None:
        """Bare-Tj streams defer the visitor abort; the budget still holds."""

        bare = ["\x00bare AAAAAAAAAAAAAAAAAAAA" for _ in range(3_000)]
        data = _build_pdf([bare])
        limits = DocumentExtractionLimits(max_chars=50_000)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, data, limits)
        assert error.value.error_class == "budget_exceeded"

    def test_duplicate_document_xml_entries_are_malformed(self) -> None:
        base = _build_docx(["duplicate entries"])
        buffer = io.BytesIO()
        with (
            zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive,
            zipfile.ZipFile(io.BytesIO(base)) as inner,
        ):
            for info in inner.infolist():
                archive.writestr(info, inner.read(info.filename))
            archive.writestr("word/document.xml", "<duplicate/>")
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, buffer.getvalue(), DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "malformed"

    def test_macro_case_variant_is_refused(self) -> None:
        base = _build_docx(["text"])
        buffer = io.BytesIO()
        with (
            zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive,
            zipfile.ZipFile(io.BytesIO(base)) as inner,
        ):
            for info in inner.infolist():
                archive.writestr(info, inner.read(info.filename))
            archive.writestr("word/VBAproject.BIN", b"\x00macro")
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, buffer.getvalue(), DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "embedded_macro"

    def test_txt_bom_not_preserved_in_first_chunk(self) -> None:
        prefixed = "﻿".encode() + b"BOM-free first line\n"
        _, chunks, _ = extract_document(TXT_MIME, prefixed, DEFAULT_EXTRACTION_LIMITS)
        assert "\ufeff" not in chunks[0].text

    def test_malformed_pdf_unavailable(self) -> None:
        data = b"%PDF-1.4 this is not a real pdf structure at all"
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "malformed"

    def test_pdf_without_text_layer_marks_no_extractable_text(self) -> None:
        data = _build_pdf([[], []])
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "no_extractable_text"

    def test_whitespace_only_txt_marks_no_extractable_text(self) -> None:
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(TXT_MIME, b"   \n \t \n", DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "no_extractable_text"

    def test_empty_docx_marks_no_extractable_text(self) -> None:
        data = _build_docx([])
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "no_extractable_text"

    def test_page_budget_enforced(self) -> None:
        data = _build_pdf([["page"]] * 6)
        limits = DocumentExtractionLimits(max_pages=3)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, data, limits)
        assert error.value.error_class == "budget_exceeded"

    def test_character_budget_enforced(self) -> None:
        data = _build_pdf([[f"filler text block {index}" for index in range(40)]])
        limits = DocumentExtractionLimits(max_chars=200)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, data, limits)
        assert error.value.error_class == "budget_exceeded"

    def test_deadline_exceeded(self) -> None:
        data = _build_pdf([["some text"]] * 30)
        limits = DocumentExtractionLimits(deadline_seconds=0.0)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, data, limits)
        assert error.value.error_class == "deadline_exceeded"

    def test_macro_docx_unavailable(self) -> None:
        data = _build_docx(["text"], macro=True)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "embedded_macro"

    def test_external_relationship_docx_unavailable(self) -> None:
        data = _build_docx(["text"], external_rels=True)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "embedded_remote"

    @pytest.mark.parametrize(
        "rels_xml",
        (
            (
                '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                "Target='https://example.invalid/x' TargetMode='External'/></Relationships>"
            ),
            (
                '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship TargetMode = "External" Id="rId2" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                'Target="https://example.invalid/y"/></Relationships>'
            ),
        ),
        ids=["single-quoted", "spaced-reordered"],
    )
    def test_external_relationship_attribute_variants_refused(
        self, rels_xml: str
    ) -> None:
        """Quoting/spacing/order must not evade the external-reference check."""

        data = _build_docx(["text"], rels_xml=rels_xml)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "embedded_remote"

    def test_malformed_rels_docx_unavailable(self) -> None:
        data = _build_docx(
            ["text"], rels_xml="<?xml version='1.0'?><Relationships <broken"
        )
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "malformed"

    def test_pdf_attachment_in_names_tree_is_refused(self) -> None:
        """Writer-generated attachments live under /Names /EmbeddedFiles."""

        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.add_attachment("payload.txt", b"synthetic attachment")
        buffer = io.BytesIO()
        writer.write(buffer)
        reader = PdfReader(io.BytesIO(buffer.getvalue()))
        assert "/EmbeddedFiles" in reader.trailer["/Root"]["/Names"]
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(PDF_MIME, buffer.getvalue(), DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "embedded_script"

    def test_zip_bomb_uncompressed_total_budget_enforced(self) -> None:
        """Declared-size caps bound decompression before any entry is read."""

        bomb = {"word/notes.xml": b"0" * 2_000_000}
        data = _build_docx(["cover text"], extra_entries=bomb)
        limits = DocumentExtractionLimits(max_total_uncompressed=1_000_000)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, data, limits)
        assert error.value.error_class == "budget_exceeded"

    def test_zip_bomb_single_entry_budget_enforced(self) -> None:
        bomb = {"word/notes.xml": b"0" * 2_000_000}
        data = _build_docx(["cover text"], extra_entries=bomb)
        limits = DocumentExtractionLimits(max_xml_bytes=1_000_000)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, data, limits)
        assert error.value.error_class == "budget_exceeded"

    def test_zip_entry_count_budget_enforced(self) -> None:
        entries = {f"extra/{index}.xml": b"<a/>" for index in range(50)}
        data = _build_docx(["text"], extra_entries=entries)
        limits = DocumentExtractionLimits(max_zip_entries=8)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, data, limits)
        assert error.value.error_class == "budget_exceeded"

    def test_document_xml_budget_enforced(self) -> None:
        padding = "<!-- " + "x" * 300_000 + " -->"
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<w:document xmlns:w="{_DOCX_NS}"><w:body>'
            f"<w:p><w:r><w:t>text</w:t></w:r></w:p>{padding}</w:body></w:document>"
        )
        data = _build_docx([], document_xml=xml)
        limits = DocumentExtractionLimits(max_xml_bytes=100_000)
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, data, limits)
        assert error.value.error_class == "budget_exceeded"

    def test_malformed_zip_unavailable(self) -> None:
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(
                DOCX_MIME, b"PK\x03\x04 truncated", DEFAULT_EXTRACTION_LIMITS
            )
        assert error.value.error_class == "malformed"

    def test_malformed_xml_unavailable(self) -> None:
        data = _build_docx([], document_xml="<?xml version='1.0'?><w:broken")
        with pytest.raises(ExtractionUnavailableError) as error:
            extract_document(DOCX_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        assert error.value.error_class == "malformed"

    def test_error_messages_are_closed_prose(self) -> None:
        cases: list[bytes] = [
            b"%PDF-1.4 garbage garbage",
            _build_docx([], document_xml="<?xml version='1.0'?><broken"),
        ]
        for index, data in enumerate(cases):
            mime = PDF_MIME if index == 0 else DOCX_MIME
            with pytest.raises(ExtractionUnavailableError) as error:
                extract_document(mime, data, DEFAULT_EXTRACTION_LIMITS)
            message = str(error.value)
            assert "Traceback" not in message
            assert "word/" not in message
            assert "document.xml" not in message


class TestStoreIntegration:
    def test_extraction_record_and_chunks_persist_across_restart(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        data = _build_pdf([["persisted", "content"], ["page two"]])
        workspace_id, document = _upload(store, "persist.pdf", data, PDF_MIME)
        source_sha = hashlib.sha256(data).hexdigest()
        _, chunks, total = extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)

        now = "2026-11-09T00:00:00+00:00"
        record = ExtractionRecord(
            workspace_id=workspace_id,
            document_id=document.document_id,
            version=1,
            source_sha256=source_sha,
            status="completed",
            error_class=None,
            extractor_version=EXTRACTOR_VERSION,
            chunk_count=len(chunks),
            total_chars=total,
            created_at=now,
            updated_at=now,
        )
        _run(store.save_extraction(record, chunks))

        reopened = _store(tmp_path)
        fetched = _run(reopened.get_extraction(workspace_id, document.document_id))
        assert fetched.status == "completed"
        assert fetched.chunk_count == len(chunks)
        persisted = _run(reopened.list_chunks(workspace_id, document.document_id))
        assert [chunk.chunk_id for chunk in persisted] == [
            chunk.chunk_id for chunk in chunks
        ]

    def test_unavailable_record_persists_without_chunks(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        data = _encrypted_pdf()
        workspace_id, document = _upload(store, "locked.pdf", data, PDF_MIME)
        record = ExtractionRecord(
            workspace_id=workspace_id,
            document_id=document.document_id,
            version=1,
            source_sha256=hashlib.sha256(data).hexdigest(),
            status="unavailable",
            error_class="encrypted",
            extractor_version=EXTRACTOR_VERSION,
            chunk_count=0,
            total_chars=0,
            created_at="2026-11-09T00:00:00+00:00",
            updated_at="2026-11-09T00:00:00+00:00",
        )
        _run(store.save_extraction(record, ()))
        fetched = _run(store.get_extraction(workspace_id, document.document_id))
        assert fetched.status == "unavailable"
        assert fetched.error_class == "encrypted"

    def test_reimport_same_content_yields_same_chunk_ids(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        data = _build_pdf([["reimport", "determinism"]])
        _, first = _upload(store, "one.pdf", data, PDF_MIME)
        _, second = _upload(store, "two.pdf", data, PDF_MIME)
        _, first_chunks, _ = extract_document(PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        _, second_chunks, _ = extract_document(
            PDF_MIME, data, DEFAULT_EXTRACTION_LIMITS
        )
        assert [c.chunk_id for c in first_chunks] == [c.chunk_id for c in second_chunks]
        assert first.document_id != second.document_id

    def test_missing_extraction_is_not_found(self, tmp_path: Path) -> None:
        from mini_deerflow.documents import DocumentNotFoundError

        store = _store(tmp_path)
        data = _build_pdf([["text"]])
        workspace_id, document = _upload(store, "a.pdf", data, PDF_MIME)
        with pytest.raises(DocumentNotFoundError):
            _run(store.get_extraction(workspace_id, document.document_id))

    def test_wrong_workspace_extraction_not_found(self, tmp_path: Path) -> None:
        from mini_deerflow.documents import DocumentNotFoundError

        store = _store(tmp_path)
        data = _build_pdf([["text"]])
        _, document = _upload(store, "a.pdf", data, PDF_MIME)
        other = _run(store.create_workspace("Other"))
        with pytest.raises(DocumentNotFoundError):
            _run(store.get_extraction(other.workspace_id, document.document_id))


class TestLimitsResolution:
    def test_defaults_available(self) -> None:
        limits = DEFAULT_EXTRACTION_LIMITS
        assert limits.deadline_seconds == 30.0
        assert limits.max_pages == 300
        assert limits.max_chars == 2_000_000

    def test_limits_positive_validation(self) -> None:
        with pytest.raises(ValueError):
            DocumentExtractionLimits(deadline_seconds=-1)
        with pytest.raises(ValueError):
            DocumentExtractionLimits(max_pages=0)
