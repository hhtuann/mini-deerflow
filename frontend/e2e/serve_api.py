"""Real offline browser-test API with temporary, isolated persistence."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import uvicorn

from mini_deerflow.api import create_app
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.phase2_slide_agent_offline import ScriptedSlidePlanner
from mini_deerflow.demo.service import DemoRuntimeService, RunDemoCommand
from mini_deerflow.pptx.artifacts import PptxArtifactStore
from mini_deerflow.pptx.slide_agent import SlideAgentService


async def seed_interrupted(storage: Path) -> None:
    service = DemoRuntimeService(
        OfflineDemoBackend(storage, interrupt_after_delegation=True)
    )
    try:
        await service.run(
            RunDemoCommand(
                "browser-interrupted",
                "Resume this interrupted evidence comparison.",
                "browser-interrupted-turn",
                "research-brief",
            )
        )
    except RuntimeError as error:
        if "deterministic interruption" not in str(error):
            raise


def build_test_app(storage: Path):
    """Production app with the deterministic planner injected for offline mode.

    The injection goes through the real SlideAgentService/store composition; the
    offline default (503 without injection) is exercised by Python API tests.
    """
    service = DemoRuntimeService(OfflineDemoBackend(storage))
    slide_service = SlideAgentService(
        service,
        PptxArtifactStore(storage, mode="offline"),
        planner_model_factory=ScriptedSlidePlanner,
        structured_output_mode="native",
    )
    return create_app(
        storage_root=storage,
        services={"offline": service},
        slide_services={"offline": slide_service},
    )


if __name__ == "__main__":
    storage = Path(os.environ["MINI_DEERFLOW_E2E_STORAGE"])
    if "--seed-interrupted" in sys.argv:
        asyncio.run(seed_interrupted(storage))
    else:
        if storage.exists():
            raise RuntimeError("Browser tests require a fresh storage directory.")
        try:
            uvicorn.run(build_test_app(storage), host="127.0.0.1", port=8000)
        finally:
            shutil.rmtree(storage, ignore_errors=True)
