"""Bounded PDF/DOCX/TXT text extraction with provenance-aware chunking.

Stored documents are untrusted input (the D1 handoff contract): parsing is
bounded by explicit budgets, ZIP decompression is capped before any XML is
read, XML is parsed with ``defusedxml`` (no DTD/entities), and every failure
maps to a closed error class — raw exception text, paths, and document
internals never surface. Chunk identifiers and locators are deterministic
pure functions of the extracted text and the source version checksum.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from defusedxml import ElementTree as SafeElementTree
from pydantic import BaseModel, ConfigDict, Field

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TXT_MIME = "text/plain"

EXTRACTOR_VERSION = "d2-v1"

CHUNK_TARGET_CHARS = 1_600
CHUNK_HARD_LIMIT_CHARS = 2_000
PREVIEW_CHARS = 200

_ERROR_MESSAGES = {
    "encrypted": "Document is encrypted.",
    "malformed": "Document structure is malformed.",
    "budget_exceeded": "Extraction budget was exceeded.",
    "deadline_exceeded": "Extraction deadline was exceeded.",
    "no_extractable_text": "Document has no extractable text.",
    "embedded_macro": "Document contains embedded macros.",
    "embedded_remote": "Document references remote resources.",
    "embedded_script": "Document contains embedded scripting.",
    "storage_error": "Extraction storage failed.",
}
ERROR_CLASSES = tuple(_ERROR_MESSAGES)


class ExtractionError(RuntimeError):
    """Base extraction failure; messages are closed prose."""


class ExtractionUnavailableError(ExtractionError):
    """Extraction cannot produce text for this version (terminal state)."""

    def __init__(self, error_class: str) -> None:
        if error_class not in _ERROR_MESSAGES:
            raise ValueError("unknown extraction error class")
        super().__init__(_ERROR_MESSAGES[error_class])
        self.error_class = error_class


@dataclass(frozen=True)
class DocumentExtractionLimits:
    """Cooperative budgets for one bounded extraction operation."""

    deadline_seconds: float = 30.0
    max_pages: int = 300
    max_chars: int = 2_000_000
    max_xml_bytes: int = 20_971_520
    max_zip_entries: int = 5_000
    max_total_uncompressed: int = 104_857_600

    def __post_init__(self) -> None:
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, (int, float))
            or not math.isfinite(self.deadline_seconds)
            or self.deadline_seconds < 0
        ):
            raise ValueError("deadline_seconds must be a finite non-negative number.")
        for name in (
            "max_pages",
            "max_chars",
            "max_xml_bytes",
            "max_zip_entries",
            "max_total_uncompressed",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")


DEFAULT_EXTRACTION_LIMITS = DocumentExtractionLimits(
    deadline_seconds=30.0,
    max_pages=300,
    max_chars=2_000_000,
    max_xml_bytes=20_971_520,
    max_zip_entries=5_000,
    max_total_uncompressed=104_857_600,
)


class SourceLocator(BaseModel):
    """Closed, integer-only provenance for a chunk's source position."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["page", "paragraph", "table", "lines"]
    page: int | None = Field(default=None, ge=1)
    paragraph_start: int | None = Field(default=None, ge=1)
    paragraph_end: int | None = Field(default=None, ge=1)
    table: int | None = Field(default=None, ge=1)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)


class DocumentChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    index: int = Field(ge=1)
    kind: Literal["page", "paragraph", "table", "lines"]
    text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    locator: SourceLocator


class ExtractionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    document_id: str
    version: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["completed", "unavailable"]
    error_class: str | None = None
    extractor_version: str
    chunk_count: int = Field(ge=0)
    total_chars: int = Field(ge=0)
    created_at: str
    updated_at: str


class _Unit:
    __slots__ = ("kind", "locator", "text")

    def __init__(self, kind: str, text: str, locator: SourceLocator) -> None:
        self.kind = kind
        self.text = text
        self.locator = locator


class _Deadline:
    __slots__ = ("_deadline",)

    def __init__(self, seconds: float) -> None:
        self._deadline = time.monotonic() + seconds

    def check(self) -> None:
        # ``>=`` (not ``>``): a zero deadline must fire deterministically even
        # when two monotonic readings land on the same clock tick.
        if time.monotonic() >= self._deadline:
            raise ExtractionUnavailableError("deadline_exceeded")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _strip_control_noise(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)


# ---------------------------------------------------------------------------
# PDF (text layer only)
# ---------------------------------------------------------------------------
_EMBEDDING_ANNOTATION_SUBTYPES = frozenset(
    {"/FileAttachment", "/RichMedia", "/Movie", "/3D", "/Sound"}
)


def _pdf_active_node(node: Any) -> bool:
    """Key-presence active-content check for one annotation/field node."""

    if "/AA" in node or "/AF" in node:
        return True
    if node.get("/Subtype") in _EMBEDDING_ANNOTATION_SUBTYPES:
        return True
    action = node.get("/A")
    return action is not None and action.get("/S") in ("/JavaScript", "/Launch")


def _pdf_has_scripting(reader: Any, pages: Any) -> bool:
    """Refuse embedded active content: scripts, actions, attachments.

    Cheap key-presence checks across the catalog (incl. the Names tree,
    XFA, and PDF 2.0 associated files), pages (incl. /AF), page annotations
    (additional-actions, JavaScript/Launch actions, media/FileAttachment
    subtypes), and the AcroForm field tree (field-level scripts are the
    classic form-scripting vector). The service never renders or executes
    PDFs, so this is structural refusal, not interpretation.
    """

    root = reader.trailer.get("/Root")
    if root is not None:
        for flag in ("/JavaScript", "/OpenAction", "/EmbeddedFiles", "/AA", "/AF"):
            if flag in root:
                return True
        names = root.get("/Names")
        if names is not None and any(
            flag in names for flag in ("/JavaScript", "/EmbeddedFiles")
        ):
            return True
        acroform = root.get("/AcroForm")
        if acroform is not None:
            if "/XFA" in acroform:
                return True
            fields = acroform.get("/Fields")
            if fields is not None and _pdf_fields_active(fields):
                return True
    for page in pages:
        for flag in ("/AA", "/AF"):
            if flag in page:
                return True
        annotations = page.get("/Annots")
        if annotations is None:
            continue
        for annotation in annotations:
            if _pdf_active_node(annotation):
                return True
    return False


def _pdf_fields_active(
    fields: Any, depth: int = 0, budget: list[int] | None = None
) -> bool:
    """Bounded walk of the AcroForm field tree (fields may nest via /Kids)."""

    if depth > 32:
        return True  # pathological nesting: fail closed
    counter = budget if budget is not None else [0]
    for field in fields:
        counter[0] += 1
        if counter[0] > 10_000:
            return True  # refuse rather than walk unbounded field trees
        if _pdf_active_node(field):
            return True
        kids = field.get("/Kids")
        if kids is not None and _pdf_fields_active(kids, depth + 1, counter):
            return True
    return False


class _PageBudgetExceeded(Exception):
    """Internal cooperative abort: within-page text budget exhausted."""


def _make_page_visitor(bucket: list[str], cap: int):
    """Build a pypdf text visitor that aborts once a page exceeds ``cap``."""

    chars = 0

    def visitor(text: str, *_args: object) -> None:
        nonlocal chars
        if text:
            bucket.append(text)
            chars += len(text)
            if chars > cap:
                raise _PageBudgetExceeded

    return visitor


# NOTE: pypdf's extract_text catches broad exceptions inside its Do/XObject
# branch and only logs a warning, so a visitor abort raised within a Form
# XObject may be swallowed there. Termination stays correct regardless: the
# post-page running-char check still enforces the budget. Do not refactor
# to depend on the visitor exception surviving extract_text.


def _extract_pdf_units(
    data: bytes, limits: DocumentExtractionLimits, deadline: _Deadline
) -> list[_Unit]:
    from pypdf import PdfReader
    from pypdf.errors import LimitReachedError, PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ExtractionUnavailableError("encrypted")
        if len(reader.pages) > limits.max_pages:
            raise ExtractionUnavailableError("budget_exceeded")
        if _pdf_has_scripting(reader, reader.pages):
            raise ExtractionUnavailableError("embedded_script")
        units: list[_Unit] = []
        running_chars = 0
        for number, page in enumerate(reader.pages, start=1):
            deadline.check()
            # Cooperative within-page bound: a text-generating content
            # stream aborts as soon as it exceeds the char budget, so bombs
            # die at max_chars of extracted text instead of after a full
            # decompression.
            accumulated: list[str] = []
            visitor = _make_page_visitor(accumulated, limits.max_chars)
            try:
                page.extract_text(visitor_text=visitor)
            except _PageBudgetExceeded:
                raise ExtractionUnavailableError("budget_exceeded") from None
            text = _strip_control_noise("".join(accumulated)).strip()
            if text:
                running_chars += len(text)
                if running_chars > limits.max_chars:
                    raise ExtractionUnavailableError("budget_exceeded")
                units.append(
                    _Unit(
                        "page",
                        text,
                        SourceLocator(kind="page", page=number),
                    )
                )
        return units
    except ExtractionUnavailableError:
        raise
    except LimitReachedError:
        # pypdf's own decompression/stream heuristics tripped: this is a
        # resource-budget outcome, not malformed structure.
        raise ExtractionUnavailableError("budget_exceeded") from None
    except (
        PyPdfError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        ZeroDivisionError,
    ) as error:
        # AttributeError: pypdf exposes non-dictionary shapes for /Fields,
        # /Kids, and /Annots in malformed documents (NameObject keys, plain
        # strings); that is malformed structure, not a raw escape.
        raise ExtractionUnavailableError("malformed") from error


# ---------------------------------------------------------------------------
# DOCX (bounded ZIP + defusedxml)
# ---------------------------------------------------------------------------
_DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_paragraph_text(paragraph: Any) -> str:
    parts = []
    for node in paragraph.iter():
        if node.tag == f"{_DOCX_NS}t" and node.text:
            parts.append(node.text)
    return "".join(parts).strip()


def _docx_cell_text(cell: Any, depth: int = 0) -> str:
    """Cell text: paragraphs, nested tables, and sdt controls, in order."""

    if depth > 32:
        raise ExtractionUnavailableError("malformed")
    parts: list[str] = []
    for child in cell:
        if child.tag == f"{_DOCX_NS}p":
            parts.append(_docx_paragraph_text(child))
        elif child.tag == f"{_DOCX_NS}tbl":
            nested = _docx_table_text(child, depth + 1)
            if nested:
                parts.append(nested)
        elif child.tag == f"{_DOCX_NS}sdt":
            content = child.find(f"{_DOCX_NS}sdtContent")
            if content is not None:
                wrapped = _docx_cell_text(content, depth + 1)
                if wrapped:
                    parts.append(wrapped)
    return " ".join(part for part in parts if part)


def _docx_table_text(table: Any, depth: int = 0) -> str:
    rows: list[str] = []
    for row in table.findall(f"{_DOCX_NS}tr"):
        cells = [_docx_cell_text(cell, depth) for cell in row.findall(f"{_DOCX_NS}tc")]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _extract_docx_units(
    data: bytes, limits: DocumentExtractionLimits, deadline: _Deadline
) -> list[_Unit]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        raise ExtractionUnavailableError("malformed") from error
    with archive:
        try:
            infos = archive.infolist()
        except (zipfile.BadZipFile, OSError) as error:
            raise ExtractionUnavailableError("malformed") from error
        if len(infos) > limits.max_zip_entries:
            raise ExtractionUnavailableError("budget_exceeded")
        names = archive.namelist()
        if any(name.lower() == "word/vbaproject.bin" for name in names):
            # Case-exact OPC name plus case-variant forms: a macro part is
            # refused even under obfuscated casing (fail-closed family).
            raise ExtractionUnavailableError("embedded_macro")
        if names.count("word/document.xml") > 1:
            # Duplicate main-part entries resolve ambiguously across
            # readers; refuse rather than silently trusting one of them.
            raise ExtractionUnavailableError("malformed")
        # Decompression bounding: declared entry sizes are checked BEFORE
        # any entry is read (zipfile enforces declared sizes/CRC at read
        # time), only document.xml and its rels are ever decompressed, and
        # unused entries stay unread. A ratio heuristic was deliberately
        # rejected: legitimately compressible XML false-positives.
        total_uncompressed = 0
        for info in infos:
            deadline.check()
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                # General-purpose bit 0: encrypted entry. zipfile would
                # raise a raw RuntimeError on read; refuse first with the
                # closed encrypted class (password-protected DOCX is a
                # routine real-world input).
                raise ExtractionUnavailableError("encrypted")
            total_uncompressed += info.file_size
            if total_uncompressed > limits.max_total_uncompressed:
                raise ExtractionUnavailableError("budget_exceeded")
            if info.file_size > limits.max_xml_bytes:
                raise ExtractionUnavailableError("budget_exceeded")
        try:
            rels = archive.read("word/_rels/document.xml.rels")
        except KeyError:
            rels = b""
        except (
            OSError,
            zipfile.BadZipFile,
            RuntimeError,
            NotImplementedError,
        ) as error:
            raise ExtractionUnavailableError("malformed") from error
        if rels:
            # Parse the relationships as bounded XML (size already capped by
            # the declared-entry budget above) instead of substring matching:
            # single-quoted, spaced, or reordered attributes are
            # standards-valid and must not evade the external check.
            try:
                rels_root = SafeElementTree.fromstring(rels)
            except (SyntaxError, ValueError) as error:
                raise ExtractionUnavailableError("malformed") from error
            for relationship in rels_root.iter():
                if str(relationship.get("TargetMode", "")).lower() == "external":
                    raise ExtractionUnavailableError("embedded_remote")
        try:
            document = archive.read("word/document.xml")
        except KeyError as error:
            raise ExtractionUnavailableError("malformed") from error
        except (
            OSError,
            zipfile.BadZipFile,
            RuntimeError,
            NotImplementedError,
        ) as error:
            raise ExtractionUnavailableError("malformed") from error
        if len(document) > limits.max_xml_bytes:
            raise ExtractionUnavailableError("budget_exceeded")
        try:
            root = SafeElementTree.fromstring(document)
        except (
            SyntaxError,  # defusedxml raises on DTD/entities and bad syntax
            ValueError,
        ) as error:
            raise ExtractionUnavailableError("malformed") from error
        body = root.find(f"{_DOCX_NS}body")
        if body is None:
            raise ExtractionUnavailableError("malformed")
        units: list[_Unit] = []
        paragraph_index = 0
        table_index = 0

        def dispatch(node: Any, depth: int = 0) -> None:
            nonlocal paragraph_index, table_index
            if depth > 32:
                # Pathological nesting; the XML budget bounds size, this
                # bounds structure. Fail closed instead of dropping text.
                raise ExtractionUnavailableError("malformed")
            for child in node:
                deadline.check()
                if child.tag == f"{_DOCX_NS}p":
                    paragraph_index += 1
                    text = _strip_control_noise(_docx_paragraph_text(child)).strip()
                    if text:
                        units.append(
                            _Unit(
                                "paragraph",
                                text,
                                SourceLocator(
                                    kind="paragraph",
                                    paragraph_start=paragraph_index,
                                    paragraph_end=paragraph_index,
                                ),
                            )
                        )
                elif child.tag == f"{_DOCX_NS}tbl":
                    table_index += 1
                    text = _strip_control_noise(_docx_table_text(child)).strip()
                    if text:
                        units.append(
                            _Unit(
                                "table",
                                text,
                                SourceLocator(kind="table", table=table_index),
                            )
                        )
                elif child.tag == f"{_DOCX_NS}sdt":
                    # Structured document tags wrap regular body content;
                    # descend so control-wrapped text is never dropped.
                    content = child.find(f"{_DOCX_NS}sdtContent")
                    if content is not None:
                        dispatch(content, depth + 1)

        dispatch(body)
        return units


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------
def _extract_txt_units(
    data: bytes, limits: DocumentExtractionLimits, deadline: _Deadline
) -> list[_Unit]:
    del limits
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ExtractionUnavailableError("malformed") from error
    units: list[_Unit] = []
    for number, line in enumerate(text.splitlines(), start=1):
        deadline.check()
        cleaned = _strip_control_noise(line).strip()
        if cleaned:
            units.append(
                _Unit(
                    "lines",
                    cleaned,
                    SourceLocator(kind="lines", line_start=number, line_end=number),
                )
            )
    return units


# ---------------------------------------------------------------------------
# Deterministic chunking
# ---------------------------------------------------------------------------
def _hard_split(text: str, deadline: _Deadline) -> list[str]:
    pieces: list[str] = []
    remaining = text
    while len(remaining) > CHUNK_HARD_LIMIT_CHARS:
        deadline.check()
        window = remaining[:CHUNK_HARD_LIMIT_CHARS]
        cut = window.rfind("\n")
        if cut < CHUNK_HARD_LIMIT_CHARS // 2:
            cut = CHUNK_HARD_LIMIT_CHARS
        pieces.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        pieces.append(remaining)
    return pieces


def _pack_units(
    units: list[_Unit], source_sha: str, deadline: _Deadline
) -> tuple[str, list[DocumentChunk]]:

    full_parts: list[str] = []
    chunks: list[DocumentChunk] = []
    running_offset = 0

    def emit(parts: list[str], kind: str, locator: SourceLocator) -> None:
        nonlocal running_offset
        text = "\n".join(part for part in parts if part)
        if not text.strip():
            return
        char_start = running_offset
        running_offset += len(text) + 1
        full_parts.append(text)
        index = len(chunks) + 1
        chunk_id = hashlib.sha256(f"{source_sha}:{index}".encode()).hexdigest()[:16]
        chunks.append(
            DocumentChunk(
                chunk_id=chunk_id,
                index=index,
                kind=kind,  # type: ignore[arg-type]
                text=text,
                char_start=char_start,
                char_end=char_start + len(text),
                locator=locator,
            )
        )

    current_units: list[_Unit] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current_units, current_chars
        if current_units:
            first = current_units[0]
            last = current_units[-1]
            # Consecutive same-kind units merge into a range locator
            # (paragraph i..j, lines i..j); single-unit chunks keep the
            # unit's own locator shape (page, table).
            if first.locator.kind == "paragraph":
                locator = SourceLocator(
                    kind="paragraph",
                    paragraph_start=first.locator.paragraph_start,
                    paragraph_end=last.locator.paragraph_end,
                )
            elif first.locator.kind == "lines":
                locator = SourceLocator(
                    kind="lines",
                    line_start=first.locator.line_start,
                    line_end=last.locator.line_end,
                )
            else:
                locator = first.locator
            emit(
                [unit.text for unit in current_units],
                first.kind,
                locator,
            )
        current_units = []
        current_chars = 0

    for unit in units:
        # Packing is pure but not free (join + sha per chunk); keep the
        # cooperative deadline honored here too.
        deadline.check()
        if len(unit.text) > CHUNK_HARD_LIMIT_CHARS:
            flush()
            for piece in _hard_split(unit.text, deadline):
                emit([piece], unit.kind, unit.locator)
            continue
        kind_mismatch = bool(current_units) and current_units[0].kind != unit.kind
        locator_incompatible = bool(current_units) and current_units[
            0
        ].locator.kind not in ("paragraph", "lines")
        if kind_mismatch or locator_incompatible:
            # Page/table units carry singular locators and never merge.
            flush()
        if current_chars + len(unit.text) > CHUNK_TARGET_CHARS and current_units:
            flush()
        current_units.append(unit)
        current_chars += len(unit.text) + 1
    flush()
    return "\n".join(full_parts), chunks


def extract_document(
    content_type: str,
    data: bytes,
    limits: DocumentExtractionLimits,
) -> tuple[str, tuple[DocumentChunk, ...], int]:
    """Extract and chunk one document version deterministically.

    Returns ``(full_text, chunks, total_chars)`` or raises
    :class:`ExtractionUnavailableError` with a closed error class.
    """

    if not isinstance(data, bytes) or not data:
        raise ExtractionUnavailableError("malformed")
    source_sha = hashlib.sha256(data).hexdigest()
    deadline = _Deadline(limits.deadline_seconds)
    if content_type == PDF_MIME:
        units = _extract_pdf_units(data, limits, deadline)
    elif content_type == DOCX_MIME:
        units = _extract_docx_units(data, limits, deadline)
    elif content_type == TXT_MIME:
        units = _extract_txt_units(data, limits, deadline)
    else:
        raise ExtractionUnavailableError("malformed")
    if not units:
        raise ExtractionUnavailableError("no_extractable_text")
    full_text, chunk_list = _pack_units(units, source_sha, deadline)
    total = len(full_text)
    if total > limits.max_chars:
        raise ExtractionUnavailableError("budget_exceeded")
    return full_text, tuple(chunk_list), total


async def run_document_extraction(
    store: Any,
    workspace_id: str,
    document_id: str,
    limits: DocumentExtractionLimits,
) -> ExtractionRecord:
    """Bounded worker operation: extract latest version and persist results.

    ``ExtractionUnavailableError`` is a recorded terminal outcome, not a job
    failure: the extraction record stores the closed error class and the job
    completes normally. In-flight state lives only in the process-local job
    queue (consistent with the app's existing job contract); a crash leaves
    no partial rows and the operation can simply be re-submitted.
    """

    from mini_deerflow.documents import DocumentIntegrityError

    version, source_sha, content_type, data = await store.begin_extraction(
        workspace_id, document_id
    )
    created = _now()
    fields = {
        "workspace_id": workspace_id,
        "document_id": document_id,
        "version": version,
        "source_sha256": source_sha,
        "extractor_version": EXTRACTOR_VERSION,
        "created_at": created,
        "updated_at": created,
    }
    try:
        _, chunks, total = extract_document(content_type, data, limits)
    except ExtractionUnavailableError as error:
        record = ExtractionRecord(
            **fields,
            status="unavailable",
            error_class=error.error_class,
            chunk_count=0,
            total_chars=0,
        )
        chunks = ()
    else:
        record = ExtractionRecord(
            **fields,
            status="completed",
            error_class=None,
            chunk_count=len(chunks),
            total_chars=total,
        )
    try:
        await store.save_extraction(record, chunks)
    except (OSError, ValueError) as error:
        raise DocumentIntegrityError("Extraction storage failed.") from error
    return await store.get_extraction(workspace_id, document_id, version=version)


__all__ = [
    "DEFAULT_EXTRACTION_LIMITS",
    "DOCX_MIME",
    "ERROR_CLASSES",
    "EXTRACTOR_VERSION",
    "PDF_MIME",
    "PREVIEW_CHARS",
    "TXT_MIME",
    "DocumentChunk",
    "DocumentExtractionLimits",
    "ExtractionRecord",
    "ExtractionUnavailableError",
    "SourceLocator",
    "extract_document",
    "run_document_extraction",
]
