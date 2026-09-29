import asyncio
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

from mini_deerflow.conversation import ConversationRecord, TurnRecord
from mini_deerflow.demo.offline_scenario import (
    FALSE_FACT_CANARY,
    INVENTED_SOURCE,
    MACHINE_PATH_CANARY,
    RAW_PAYLOAD_CANARY,
    SECRET_CANARY,
    UNSAFE_URL,
    OfflineDemoBackend,
)
from mini_deerflow.demo.service import DemoRuntimeService, RunDemoCommand
from mini_deerflow.demo.view_models import (
    _answer_markdown,
    project_chat_session,
    project_demo_run,
    sanitize_answer_markdown,
)
from mini_deerflow.evidence import (
    EvidenceProvenance,
    EvidenceRecord,
    StepFinding,
    render_research_report,
)
from mini_deerflow.review import ReviewFinding, ReviewVerdict
from mini_deerflow.runtime import (
    ConversationSnapshot,
    ConversationTurnSnapshot,
    RuntimeLimits,
)
from mini_deerflow.state import AgentState


def test_answer_markdown_denies_remote_images_html_and_unvalidated_links() -> None:
    safe = sanitize_answer_markdown(
        (
            "## Answer&#xD;\r\n\r\n"
            "![pixel](https://tracker.invalid/pixel) "
            "![reference pixel][tracker]\n"
            "[tracker]: https://tracker.invalid/reference\n"
            "[validated](https://example.com/source) "
            "[invented](https://invented.invalid/source) "
            '<img src="https://tracker.invalid/second">'
        ),
        allowed_urls=("https://example.com/source",),
    )

    assert safe.startswith("## Answer")
    assert "&#xD;" not in safe
    assert "\r" not in safe
    assert "[validated](https://example.com/source)" in safe
    assert "invented" in safe
    assert "invented.invalid" not in safe
    assert "tracker.invalid" not in safe
    assert "![" not in safe
    assert "<img" not in safe


def test_legacy_report_without_checkpoint_has_no_nonexistent_details_cta() -> None:
    report = render_research_report(
        goal="Legacy goal",
        findings=[],
        evidence=[],
        errors=[],
        total_tool_calls=1,
        successful_tool_calls=1,
        failed_tool_calls=0,
    )
    snapshot = ConversationSnapshot(
        conversation=ConversationRecord(
            thread_id="legacy-thread",
            title="Legacy conversation",
            status="completed",
            created_at="2026-09-21T00:00:00+00:00",
            updated_at="2026-09-21T00:00:00+00:00",
            session_tool_calls=1,
            session_tool_call_limit=20,
        ),
        turns=(
            ConversationTurnSnapshot(
                record=TurnRecord(
                    thread_id="legacy-thread",
                    turn_id="turn-001",
                    sequence=1,
                    user_message="Run a legacy research turn.",
                    assistant_response=report,
                    status="completed",
                    checkpoint_namespace="turn-turn-001",
                    created_at="2026-09-21T00:00:00+00:00",
                    completed_at="2026-09-21T00:00:01+00:00",
                    total_tool_calls=1,
                    reserved_tool_calls=5,
                ),
                state=None,
                traces=(),
            ),
        ),
    )

    session = project_chat_session(snapshot, limits=RuntimeLimits())
    turn = session.turns[0]

    assert turn.run is None
    assert "# Research Report" not in (turn.assistant_message or "")
    assert "Agent details" not in (turn.assistant_message or "")
    assert (turn.assistant_message or "").startswith("## Kết quả")
    assert "Không thể dựng lại" in (turn.assistant_message or "")


def test_checkpointed_legacy_public_answer_is_rebuilt_from_findings() -> None:
    provenance = EvidenceProvenance(
        tool_name="web_fetch",
        step_number=1,
        step_tool_call_number=1,
        total_tool_call_number=1,
        observation_index=1,
    )
    evidence = EvidenceRecord(
        url="https://example.com/source",
        source_tool="web_fetch",
        title="Source",
        excerpt="Observed content",
        provenance=provenance,
    )
    state: AgentState = {
        "final_answer": "## Answer\n\nBước 1 hoàn tất: status 200.",
        "findings": [
            StepFinding(
                step_number=1,
                summary="Kết luận: A durable public claim.",
                citations=["https://example.com/source"],
            )
        ],
        "evidence": [evidence],
        "review_verdicts": [
            ReviewVerdict(
                verdict="continue",
                rationale="Historical review.",
                findings=[
                    ReviewFinding(
                        category="gap",
                        description="Resolved historical gap.",
                        related_step_numbers=[1],
                    )
                ],
            ),
            ReviewVerdict(
                verdict="finish",
                rationale="Current review.",
                findings=[
                    ReviewFinding(
                        category="gap",
                        description="Current public limitation.",
                        related_step_numbers=[1],
                    )
                ],
            ),
        ],
        "errors": [],
        "tool_observations": [],
    }

    answer = _answer_markdown(state, state["final_answer"] or "")

    assert "A durable public claim. [1]" in answer
    assert "Bước 1 hoàn tất" not in answer
    assert "Current public limitation." in answer
    assert "Resolved historical gap." not in answer


def test_checkpointed_english_partial_answer_preserves_localization() -> None:
    provenance = EvidenceProvenance(
        tool_name="web_fetch",
        step_number=1,
        step_tool_call_number=1,
        total_tool_call_number=1,
        observation_index=1,
    )
    evidence = EvidenceRecord(
        url="https://example.com/source",
        source_tool="web_fetch",
        title="Source",
        excerpt="Observed content",
        provenance=provenance,
    )
    state: AgentState = {
        "goal": "Compare Pelé and Messi",
        "output_language": "en",
        "completion_status": "partial",
        "final_answer": "# Research Report\n\nInternal execution summary.",
        "findings": [
            StepFinding(
                step_number=1,
                summary="Conclusion: A supported public claim.",
                citations=["https://example.com/source"],
            )
        ],
        "evidence": [evidence],
        "review_verdicts": [],
        "errors": [],
        "tool_observations": [],
    }

    answer = _answer_markdown(state, state["final_answer"] or "")

    assert answer.startswith("# Result")
    assert "Partial answer" in answer
    assert "# Kết quả" not in answer


def test_legacy_execution_only_answer_is_not_rendered_as_public_content() -> None:
    state: AgentState = {
        "final_answer": "## Answer\n\nBước 1 hoàn tất: fetch status = 200.",
        "findings": [],
        "evidence": [],
        "review_verdicts": [],
        "errors": [],
        "tool_observations": [],
    }

    answer = _answer_markdown(state, state["final_answer"] or "")

    assert answer.startswith("# Kết quả")
    assert "Bước 1 hoàn tất" not in answer
    assert "fetch status" not in answer


def test_legacy_mixed_answer_keeps_public_point_and_removes_trace() -> None:
    state: AgentState = {
        "final_answer": "Public result.\n\nStep 1: fetch status = 200.",
        "findings": [],
        "evidence": [],
        "review_verdicts": [],
        "errors": [],
        "tool_observations": [],
    }

    answer = _answer_markdown(state, state["final_answer"] or "")

    assert "Public result." in answer
    assert "fetch status" not in answer


def test_projector_is_frozen_allowlisted_and_does_not_leak_canaries(
    tmp_path: Path,
) -> None:
    async def scenario():
        service = DemoRuntimeService(OfflineDemoBackend(tmp_path / "demo"))
        return await service.run(RunDemoCommand(thread_id="view-test"))

    view = asyncio.run(scenario())

    with pytest.raises(FrozenInstanceError):
        view.thread_id = "changed"  # type: ignore[misc]

    public = repr(asdict(view))
    for forbidden in (
        UNSAFE_URL,
        INVENTED_SOURCE,
        SECRET_CANARY,
        RAW_PAYLOAD_CANARY,
        MACHINE_PATH_CANARY,
        FALSE_FACT_CANARY,
        str(tmp_path),
        "tool_observations",
        "pending_action",
        "messages",
    ):
        assert forbidden not in public

    evidence_urls = {item.canonical_url for item in view.evidence}
    assert set(view.accepted_citations) <= evidence_urls
    assert view.rejected_citation_count == 2
    assert all(
        branch.finding_summary is None
        for wave in view.delegations
        for branch in wave.branches
        if branch.status != "success"
    )
    assert any(wave.failed_branch_count == 1 for wave in view.delegations)
    assert view.artifact.markdown_source


def test_partial_answer_is_not_projected_as_completed() -> None:
    state: AgentState = {
        "goal": "Compare two systems",
        "final_answer": "# Result\n\n> Partial answer",
        "completion_status": "partial",
    }

    view = project_demo_run(
        state,
        (),
        thread_id="partial-demo",
        limits=RuntimeLimits(),
    )

    assert view.workflow_status == "incomplete"
