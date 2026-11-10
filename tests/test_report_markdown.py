"""Synthetic report exports: evidence fidelity, provenance and inert rendering."""

from __future__ import annotations

from html import unescape

import pytest

from mini_deerflow.document_extraction import SourceLocator
from mini_deerflow.document_qa import (
    DocumentCitation,
    DocumentClaim,
    DocumentSourceSnapshot,
)
from mini_deerflow.document_reports import ReportSection, ReportSpec
from mini_deerflow.report_markdown import render_report_markdown


def _citation(**updates) -> DocumentCitation:
    values = {
        "document_id": "doc-123",
        "version": 2,
        "chunk_id": "0123456789abcdef",
        "locator": SourceLocator(kind="lines", line_start=3, line_end=7),
        "quote": "Doanh thu tăng 12% — 日本語 và العربية.",
    }
    return DocumentCitation(**(values | updates))


def _section(**updates) -> ReportSection:
    values = {
        "section_id": "overview",
        "title": "Tổng quan 日本語",
        "question": "Doanh thu thay đổi thế nào?",
        "claims": (DocumentClaim(text=_citation().quote, citations=(_citation(),)),),
        "limitations": ("Chỉ có dữ liệu đã xác minh.",),
        "source_snapshot": (DocumentSourceSnapshot(document_id="doc-123", version=2),),
        "approved": False,
        "origin_version": 1,
    }
    return ReportSection(**(values | updates))


def _report(**updates) -> ReportSpec:
    values = {
        "report_id": "report-0123456789abcdef0123456789abcdef",
        "workspace_id": "workspace-123",
        "version": 2,
        "parent_version": 1,
        "title": "Báo cáo 日本語 العربية",
        "kind": "report",
        "document_ids": ("doc-123",),
        "sections": (_section(),),
        "created_at": "2026-11-10T00:00:00Z",
    }
    return ReportSpec(**(values | updates))


def test_markdown_keeps_exact_claim_text_multilingual_and_historical_provenance():
    report = _report()
    canonical = report.model_dump(mode="json")
    markdown = render_report_markdown(report)
    visible = unescape(markdown)

    assert markdown.startswith("# Báo cáo 日本語 العربية\n")
    assert "Kind: report | Version: 2" in markdown
    assert "## Tổng quan 日本語" in markdown
    assert report.sections[0].claims[0].text in visible
    assert report.sections[0].question in visible
    assert report.sections[0].limitations[0] in visible
    assert "Historical provenance:" in markdown
    assert "current source availability or freshness" in markdown
    assert "Origin version: 1 | Approved: no" in markdown
    assert "C1: Document: doc-123 | Version: 2 | Chunk: 0123456789abcdef" in visible
    assert "Locator: kind=lines, line_start=3, line_end=7" in visible
    assert "### Source snapshot" in markdown
    assert report.model_dump(mode="json") == canonical
    assert render_report_markdown(report) == markdown


def test_citation_labels_are_deduplicated_by_full_typed_provenance():
    quote = "First claim; Second claim"
    first = _citation(quote=quote)
    page = _citation(quote=quote, locator=SourceLocator(kind="page", page=4))
    newer = _citation(document_id="doc-456", version=3, quote=quote)
    section = _section(
        claims=(
            DocumentClaim(text="First claim", citations=(first, page)),
            DocumentClaim(text="Second claim", citations=(first, newer)),
        ),
        source_snapshot=(
            DocumentSourceSnapshot(document_id="doc-123", version=2),
            DocumentSourceSnapshot(document_id="doc-456", version=3),
        ),
    )
    markdown = render_report_markdown(
        _report(sections=(section,), document_ids=("doc-123", "doc-456"))
    )
    assert "- First claim (C1, C2)" in markdown
    assert "- Second claim (C1, C3)" in markdown
    assert markdown.count("- C1:") == 1
    assert "Locator: kind=page, page=4" in markdown
    assert "C3: Document:" in markdown
    assert "Version: 3" in markdown


@pytest.mark.parametrize(
    "locator,expected",
    [
        (SourceLocator(kind="page", page=2), "kind=page, page=2"),
        (
            SourceLocator(kind="paragraph", paragraph_start=2, paragraph_end=5),
            "kind=paragraph, paragraph_start=2, paragraph_end=5",
        ),
        (SourceLocator(kind="table", table=3), "kind=table, table=3"),
        (
            SourceLocator(kind="lines", line_start=7, line_end=9),
            "kind=lines, line_start=7, line_end=9",
        ),
    ],
)
def test_all_typed_locator_metadata_is_exported(locator, expected):
    citation = _citation(locator=locator, quote="Verified")
    section = _section(claims=(DocumentClaim(text="Verified", citations=(citation,)),))
    assert expected in render_report_markdown(_report(sections=(section,)))


def test_abstention_and_approved_section_are_explicit():
    section = _section(claims=(), approved=True, limitations=("No relevant evidence.",))
    markdown = render_report_markdown(_report(sections=(section,)))
    assert "Abstention: no verified claims" in markdown
    assert "Approved: yes" in markdown
    assert "No relevant evidence" in markdown
    assert "## Citations" not in markdown


@pytest.mark.parametrize(
    "hostile,secret",
    [
        ('<script src="https://evil.example/run.js">alert(1)</script>', "evil.example"),
        ('<img src="https://evil.example/pixel" onerror="alert(1)">', "evil.example"),
        ("![tracking](https://evil.example/pixel)", "evil.example"),
        ("[click](javascript:alert(1))", "javascript:"),
        ("<javascript:alert(1)>", "javascript:"),
        ("data:text/html;base64,PHNjcmlwdD4=", "data:"),
        ("file:///etc/shadow", "/etc/shadow"),
        (r"C:\Users\Alice\credentials.txt", "credentials.txt"),
        (r"C:/Users/Alice/credentials.txt", "credentials.txt"),
        (r"\\server\share\credentials.txt", "credentials.txt"),
        ("//server/share/credentials.txt", "credentials.txt"),
        ("/home/alice/credentials.txt", "credentials.txt"),
        ("../private/credentials.txt", "credentials.txt"),
        ("~/private/credentials.txt", "credentials.txt"),
        ("www.evil.example", "evil.example"),
        ("evil.example", "evil.example"),
        ("mailto:alice@evil.example", "evil.example"),
        ("java\u200bscript:alert(1)", "script:"),
        ("https:\n//evil.example/path", "evil.example"),
    ],
)
def test_hostile_tokens_are_redacted_in_every_untrusted_field(hostile, secret):
    citation = _citation(quote=hostile)
    section = _section(
        title=hostile,
        question=hostile,
        claims=(DocumentClaim(text=hostile, citations=(citation,)),),
        limitations=(hostile,),
        source_snapshot=(DocumentSourceSnapshot(document_id="doc-123", version=2),),
    )
    report = _report(title=hostile, sections=(section,))
    before = report.model_dump(mode="json")
    markdown = render_report_markdown(report)
    assert secret not in markdown
    assert secret not in unescape(markdown)
    assert "&#91;redacted&#93;" in markdown
    assert "<" not in markdown
    assert ">" not in markdown
    assert "![" not in markdown
    assert "](" not in markdown
    assert "`" not in markdown
    assert "\\" not in markdown
    assert report.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)",
        "<svg onload=alert(1)>",
        "```html\n<img src=x onerror=alert(1)>\n```",
        "~~~html\n<script>alert(1)\n~~~",
        "![image][target]\n[target]: dangerous",
        "# Injected heading\n---\n| table | cell |",
        r"$\include{secret}$ $$\input{secret}$$ {{execute()}}",
        "&#60;script&#62;alert(1)",
    ],
)
def test_markdown_html_fences_images_and_macros_are_inert(payload):
    section = _section(
        claims=(DocumentClaim(text=payload, citations=(_citation(quote=payload),)),),
    )
    markdown = render_report_markdown(_report(title=payload, sections=(section,)))
    claim_line = next(line for line in markdown.splitlines() if line.startswith("- "))
    for dangerous in ("<", ">", "`", "~", "!", "[", "]", "{", "}", "$", "\\"):
        assert dangerous not in claim_line
    assert claim_line.endswith(" (C1)")
    assert "\n# Injected" not in markdown
    assert "\n[target]:" not in markdown
    assert "\n---\n" not in markdown


def test_untrusted_metadata_not_selected_for_export():
    # Corrupt internal metadata defensively; this is not an accepted API input.
    report = _report().model_copy(
        update={
            "report_id": "/private/report.json",
            "workspace_id": r"C:\private\workspace",
            "created_at": "https://evil.example/tracker",
        }
    )
    markdown = render_report_markdown(report)
    assert "private" not in markdown
    assert "evil.example" not in markdown
    assert "report.json" not in markdown


def test_redaction_preserves_legitimate_surrounding_claim_text():
    text = (
        "Revenue increased. <script>alert(1)</script> https://evil.example /private/key"
    )
    claim = DocumentClaim(
        text=text,
        citations=(_citation(quote=text),),
    )
    report = _report(sections=(_section(claims=(claim,)),))
    markdown = render_report_markdown(report)
    assert "Revenue increased" in markdown
    assert "<script>" not in markdown
    assert "https://evil.example" not in markdown
    assert "/private/key" not in markdown
