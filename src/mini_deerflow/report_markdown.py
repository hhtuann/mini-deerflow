"""Inert, deterministic Markdown export of historical document reports.

Only exporter-owned Markdown is emitted. Untrusted text is flattened to one line,
URL/domain and slash-bearing tokens are redacted, and ASCII punctuation is encoded
as numeric character references. This deliberately conservative display policy can
redact prose such as fractions: canonical reports and their citations are never
modified. No filenames, storage paths, link destinations, HTML, images, or macros
are generated. DOCX export is deferred.
"""

from __future__ import annotations

import re
import string
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_deerflow.document_reports import ReportSpec


# Slash-bearing tokens cover absolute/relative POSIX, Windows, UNC, URI and
# Markdown destination forms, including paths embedded inside punctuation.
# Scheme/domain tokens are removed as well: some renderers linkify plain text
# after decoding character references, so entity escaping alone is insufficient.
_UNSAFE_TOKEN = re.compile(
    r"[/\\@]|[A-Za-z][A-Za-z0-9+.-]*:|www\.|"
    r"(?:[\w-]+\.)+[A-Za-z\u0080-\uffff]{2,}(?::\d+)?",
    re.UNICODE,
)
_PUNCTUATION = frozenset(string.punctuation)
_LOCATOR_FIELDS = (
    "page",
    "paragraph_start",
    "paragraph_end",
    "table",
    "line_start",
    "line_end",
)


def _safe_text(value: str) -> str:
    """Render text inertly; strip controls and redact rather than expose paths."""
    # Normalize whitespace/controls before token checks so neither newline nor
    # bidi/zero-width format characters can hide a scheme or path segment.
    normalized = "".join(
        " "
        if char.isspace()
        else ""
        if unicodedata.category(char).startswith("C")
        else char
        for char in value
    )
    tokens = normalized.split()
    redacted = " ".join(
        "[redacted]" if _UNSAFE_TOKEN.search(token) else token for token in tokens
    )
    return "".join(
        f"&#{ord(char)};" if char in _PUNCTUATION else char for char in redacted
    )


def render_report_markdown(report: ReportSpec) -> str:
    """Export a report without resolving sources or changing canonical evidence.

    Citation labels are assigned in section/claim/citation traversal order and
    reused for identical document/version/chunk/typed-locator provenance. Quotes
    are not repeated: claim text is already the verified extract, and arbitrary
    source filenames and storage metadata are never included.
    """
    lines = [
        f"# {_safe_text(report.title)}",
        "",
        f"Kind: {_safe_text(report.kind)} | Version: {report.version}",
        "",
        (
            "Historical provenance: citations describe the source versions used when "
            "each section was generated. They do not assert current source availability "
            "or freshness. Untouched sections retain their original evidence."
        ),
        "",
        (
            "Export safety: unsafe URL and filesystem-path tokens are redacted for "
            "display only; canonical report evidence is unchanged."
        ),
        "",
    ]
    citations: dict[tuple, str] = {}
    for section in report.sections:
        lines.extend(
            [
                f"## {_safe_text(section.title)}",
                "",
                f"Question: {_safe_text(section.question)}",
                "",
                (
                    f"Origin version: {section.origin_version} | "
                    f"Approved: {'yes' if section.approved else 'no'}"
                ),
                "",
            ]
        )
        if not section.claims:
            lines.extend(
                ["Abstention: no verified claims are available for this section.", ""]
            )
        for claim in section.claims:
            labels = []
            for citation in claim.citations:
                locator = citation.locator
                key = (
                    citation.document_id,
                    citation.version,
                    citation.chunk_id,
                    locator.kind,
                    *(getattr(locator, field) for field in _LOCATOR_FIELDS),
                )
                if key not in citations:
                    citations[key] = f"C{len(citations) + 1}"
                labels.append(citations[key])
            lines.append(f"- {_safe_text(claim.text)} ({', '.join(labels)})")
        if section.claims:
            lines.append("")
        if section.limitations:
            lines.extend(["### Limitations", ""])
            lines.extend(f"- {_safe_text(item)}" for item in section.limitations)
            lines.append("")
        if section.source_snapshot:
            lines.extend(["### Source snapshot", ""])
            lines.extend(
                f"- Document: {_safe_text(source.document_id)} | Version: {source.version}"
                for source in section.source_snapshot
            )
            lines.append("")
    if citations:
        lines.extend(["## Citations", ""])
        for key, label in citations.items():
            document_id, version, chunk_id, kind, *positions = key
            metadata = [f"kind={_safe_text(kind)}"]
            metadata.extend(
                f"{name}={value}"
                for name, value in zip(_LOCATOR_FIELDS, positions, strict=True)
                if value is not None
            )
            lines.append(
                f"- {label}: Document: {_safe_text(document_id)} | Version: {version} | "
                f"Chunk: {_safe_text(chunk_id)} | Locator: {', '.join(metadata)}"
            )
        lines.append("")
    return "\n".join(lines)
