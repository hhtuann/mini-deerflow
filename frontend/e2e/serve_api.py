"""Real offline browser-test API with temporary, isolated persistence."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import uvicorn

from mini_deerflow.api import create_app
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.service import DemoRuntimeService, RunDemoCommand


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


if __name__ == "__main__":
    storage = Path(os.environ["MINI_DEERFLOW_E2E_STORAGE"])
    if "--seed-interrupted" in sys.argv:
        asyncio.run(seed_interrupted(storage))
    else:
        if storage.exists():
            raise RuntimeError("Browser tests require a fresh storage directory.")
        try:
            uvicorn.run(create_app(storage_root=storage), host="127.0.0.1", port=8000)
        finally:
            shutil.rmtree(storage, ignore_errors=True)
