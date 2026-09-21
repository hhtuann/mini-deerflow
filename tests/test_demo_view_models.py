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
    project_chat_session,
    sanitize_answer_markdown,
)
from mini_deerflow.evidence import render_research_report
from mini_deerflow.runtime import (
    ConversationSnapshot,
    ConversationTurnSnapshot,
    RuntimeLimits,
)


def test_answer_markdown_denies_remote_images_html_and_unvalidated_links() -> None:
    safe = sanitize_answer_markdown(
        (
            "## Answer\n\n"
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
    assert "cannot be reconstructed" in (turn.assistant_message or "")


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
