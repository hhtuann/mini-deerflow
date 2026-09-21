import pytest
from pydantic import ValidationError

from mini_deerflow.actions import ToolCallAction, ToolObservation
from mini_deerflow.evidence import (
    EvidenceProvenance,
    EvidenceRecord,
    StepFinding,
    canonicalize_url,
    extract_evidence_records,
    merge_evidence_records,
    render_research_report,
    render_user_answer,
    validate_citations,
)
from mini_deerflow.tools import ToolResult


def observation(
    tool_name: str,
    result: ToolResult,
    *,
    step_number: int = 1,
    call_number: int = 1,
) -> ToolObservation:
    return ToolObservation(
        step_number=step_number,
        step_tool_call_number=1,
        total_tool_call_number=call_number,
        action=ToolCallAction(
            type="tool_call",
            tool_name=tool_name,
            arguments={},
        ),
        result=result,
    )


def test_successful_search_output_becomes_traceable_evidence() -> None:
    records = extract_evidence_records(
        observation(
            "web_search",
            ToolResult.ok(
                {
                    "query": "evidence",
                    "results": [
                        {
                            "title": "Source",
                            "url": "HTTPS://Example.COM:443/article#section",
                            "snippet": "Observed excerpt",
                        }
                    ],
                    "count": 1,
                }
            ),
            step_number=2,
            call_number=4,
        )
    )

    assert len(records) == 1
    record = records[0]
    assert record.canonical_url == "https://example.com/article"
    assert record.status == "success"
    assert record.excerpt == "Observed excerpt"
    assert record.provenance == EvidenceProvenance(
        tool_name="web_search",
        step_number=2,
        step_tool_call_number=1,
        total_tool_call_number=4,
        observation_index=4,
    )


def test_failed_or_non_web_tool_output_does_not_become_evidence() -> None:
    failed = observation(
        "web_fetch",
        ToolResult.fail("Fetch failed."),
    )
    local = observation(
        "read_file",
        ToolResult.ok(
            {
                "path": "notes.txt",
                "content": "https://invented.example",
            }
        ),
    )

    assert extract_evidence_records(failed) == []
    assert extract_evidence_records(local) == []


def test_evidence_merge_deduplicates_canonical_url_and_is_bounded() -> None:
    provenance = EvidenceProvenance(
        tool_name="web_search",
        step_number=1,
        step_tool_call_number=1,
        total_tool_call_number=1,
        observation_index=1,
    )
    first = EvidenceRecord(
        url="https://EXAMPLE.com:443/page#old",
        source_tool="web_search",
        excerpt="First excerpt",
        provenance=provenance,
    )
    replacement = EvidenceRecord(
        url="https://example.com/page",
        source_tool="web_search",
        excerpt="New excerpt",
        provenance=provenance,
    )

    assert merge_evidence_records([first], [replacement]) == [replacement]

    many = [
        EvidenceRecord(
            url=f"https://example.com/{index}",
            source_tool="web_search",
            excerpt=f"Evidence {index}",
            provenance=provenance,
        )
        for index in range(60)
    ]
    merged = merge_evidence_records([], many)
    assert len(merged) == 50
    assert merged[0].canonical_url == "https://example.com/10"


def test_canonical_url_preserves_ipv6_brackets() -> None:
    assert canonicalize_url("https://[2001:db8::1]:443/page#fragment") == (
        "https://[2001:db8::1]/page"
    )


def test_citation_validation_rejects_unknown_url_and_local_path() -> None:
    provenance = EvidenceProvenance(
        tool_name="web_fetch",
        step_number=1,
        step_tool_call_number=1,
        total_tool_call_number=1,
        observation_index=1,
    )
    evidence = [
        EvidenceRecord(
            url="https://example.com/known",
            source_tool="web_fetch",
            excerpt="Known content",
            provenance=provenance,
        )
    ]

    accepted, rejected = validate_citations(
        [
            "https://EXAMPLE.com:443/known#fragment",
            "https://example.com/invented",
        ],
        evidence,
    )

    assert accepted == ["https://example.com/known"]
    assert rejected == 1

    with pytest.raises(ValidationError):
        StepFinding(
            step_number=1,
            summary="Local evidence must not become a citation.",
            citations=["notes/evidence.txt"],
        )

    with pytest.raises(ValidationError):
        canonicalize_url("file:///workspace/evidence.txt")


def test_report_uses_only_validated_citations_and_marks_unsupported_claims() -> None:
    provenance = EvidenceProvenance(
        tool_name="web_fetch",
        step_number=1,
        step_tool_call_number=2,
        total_tool_call_number=3,
        observation_index=3,
    )
    evidence = [
        EvidenceRecord(
            url="https://example.com/known",
            source_tool="web_fetch",
            title="Known source",
            excerpt="Observed content",
            provenance=provenance,
        )
    ]
    findings = [
        StepFinding(
            step_number=1,
            summary=(
                "Supported finding with https://invented.example hidden from output."
            ),
            citations=["https://example.com/known"],
        ),
        StepFinding(
            step_number=2,
            summary="A conclusion without a web citation.",
        ),
    ]

    report = render_research_report(
        goal="Trace every citation.",
        findings=findings,
        evidence=evidence,
        errors=[],
        total_tool_calls=3,
        successful_tool_calls=3,
        failed_tool_calls=0,
    )

    assert "# Research Report" in report
    assert "## Evidence" in report
    assert "https://example.com/known" in report
    assert "https://invented.example" not in report
    assert "[unverified URL omitted]" in report
    assert "Step 1 (verified)" in report
    assert "Step 2 (unsupported)" in report
    assert "tool call 3, observation 3" in report


def test_user_answer_is_distinct_from_internal_research_report() -> None:
    provenance = EvidenceProvenance(
        tool_name="web_fetch",
        step_number=1,
        step_tool_call_number=1,
        total_tool_call_number=1,
        observation_index=1,
    )
    evidence = [
        EvidenceRecord(
            url="https://example.com/profile",
            source_tool="web_fetch",
            title="Public profile",
            excerpt="The public profile supports only the cited identity clue.",
            provenance=provenance,
        )
    ]
    findings = [
        StepFinding(
            step_number=1,
            summary="Step 1: A public profile supports the name clue.",
            citations=["https://example.com/profile"],
        ),
        StepFinding(
            step_number=2,
            summary=(
                "Review 2: The student identifier remains unverified. "
                "![pixel](https://tracker.invalid/pixel)"
            ),
        ),
        StepFinding(
            step_number=3,
            summary="Completed research step number 3.",
        ),
        StepFinding(
            step_number=4,
            summary="Tool calls: 15; Review cycles: 5.",
            citations=["https://example.com/profile"],
        ),
        StepFinding(
            step_number=5,
            summary="Review cycle 2: hidden execution note.",
        ),
    ]

    answer = render_user_answer(
        findings=findings,
        evidence=evidence,
        review_notes=[
            "Tool calls: 15; Review cycles: 5. Review cycle 3 exposed internals."
        ],
        has_collection_failures=True,
    )

    assert answer.startswith("## Answer")
    assert "https://example.com/profile" in answer
    assert "tracker.invalid" not in answer
    assert "Step 1" not in answer
    assert "Review 2" not in answer
    assert "research step number" not in answer
    assert "# Research Report" not in answer
    assert "## Execution" not in answer
    assert "Tool calls:" not in answer
    assert "Review cycles" not in answer
    assert "Review cycle 3" not in answer
    assert "Review cycle 2" not in answer
    assert "hidden execution note" not in answer
    assert "### Evidence-linked findings" in answer
    assert "Verified findings" not in answer
    assert "review identified unresolved evidence limitations" in answer
    assert "Some collection attempts failed or were blocked." in answer


def test_report_renders_review_conclusions_and_cycle_counts() -> None:
    report = render_research_report(
        goal="Trace every citation.",
        findings=[],
        evidence=[],
        errors=[],
        total_tool_calls=0,
        successful_tool_calls=0,
        failed_tool_calls=0,
        review_conclusions=[
            "- **Review 1 — replan:** Remaining steps missed the gap.",
            "  - [gap] No evidence covers the cost dimension. (steps: 2)",
        ],
        review_cycles=1,
        replan_cycles=1,
    )

    assert "## Review conclusions" in report
    assert "- **Review 1 — replan:** Remaining steps missed the gap." in report
    assert "  - [gap] No evidence covers the cost dimension. (steps: 2)" in report
    assert "- Review cycles: 1" in report
    assert "- Replan cycles: 1" in report


def test_report_without_reviews_states_absence() -> None:
    report = render_research_report(
        goal="Trace every citation.",
        findings=[],
        evidence=[],
        errors=[],
        total_tool_calls=0,
        successful_tool_calls=0,
        failed_tool_calls=0,
    )

    assert "## Review conclusions" in report
    assert "- No review verdicts were recorded." in report
    assert "- Review cycles: 0" in report
    assert "- Replan cycles: 0" in report
