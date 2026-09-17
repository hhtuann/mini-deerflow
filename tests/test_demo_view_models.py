import asyncio
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

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
