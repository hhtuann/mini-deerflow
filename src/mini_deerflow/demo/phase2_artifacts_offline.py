"""Offline runtime -> validated publication -> fresh-process HTTP download proof.

Both mode-local ledgers use scripted offline runtime seams. The 'live' namespace
here proves storage isolation, not live-provider behavior or slide planning.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from tempfile import mkdtemp

from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.phase2_pptx_offline import demo_spec
from mini_deerflow.demo.service import (
    ContinueDemoCommand,
    DemoRuntimeService,
    RunDemoCommand,
)
from mini_deerflow.pptx import DeckSpec, render_deck, validate_pptx
from mini_deerflow.pptx.artifacts import PPTX_MIME, PptxArtifactStore
from mini_deerflow.sandbox import SessionSandboxResolver

THREAD = "phase2-artifact-demo"
TURN = "turn-1"
FOLLOWUP = "turn-2"


async def _prepare(root: Path) -> dict:
    records = {}
    for mode, directory in (("offline", root), ("live", root / "live")):
        service = DemoRuntimeService(OfflineDemoBackend(directory))
        await service.run(RunDemoCommand(thread_id=THREAD, turn_id=TURN))
        spec = demo_spec("light" if mode == "offline" else "academic")
        store = PptxArtifactStore(directory, mode=mode)
        data = render_deck(spec)
        record = await store.publish(THREAD, TURN, data, spec)
        repeated = await store.publish(THREAD, TURN, render_deck(spec), spec)
        if repeated != record:
            raise RuntimeError("Idempotent publication failed.")
        records[mode] = {"sha256": record.sha256, "byte_size": record.byte_size}
        if mode == "offline":
            await service.continue_thread(
                ContinueDemoCommand(
                    thread_id=THREAD,
                    user_message="Summarize the fixture limitations.",
                    turn_id=FOLLOWUP,
                )
            )
            records["source_file"] = str(
                SessionSandboxResolver(directory / "workspaces").resolve(THREAD)
                / store._path(record)
            )
    return records


def _verify(root: Path) -> dict:
    """Executed in a new interpreter with fresh services, stores and API app."""
    report = {}
    services = {
        "offline": DemoRuntimeService(OfflineDemoBackend(root)),
        "live": DemoRuntimeService(OfflineDemoBackend(root / "live")),
    }
    with TestClient(create_app(root, services=services)) as client:
        for mode, directory in (("offline", root), ("live", root / "live")):
            record, stored = asyncio.run(
                PptxArtifactStore(directory, mode=mode).retrieve(THREAD, TURN)
            )
            url = f"/api/{mode}/conversations/{THREAD}/turns/{TURN}"
            response = client.get(url + "/presentation")
            if response.status_code != 200 or response.content != stored:
                raise RuntimeError("Verified download differs from stored bytes.")
            if response.headers["content-type"] != PPTX_MIME:
                raise RuntimeError("Incorrect download MIME type.")
            inspection = validate_pptx(
                response.content,
                DeckSpec.model_validate_json(record.spec_json),
                min_layouts=8,
            )
            if client.get(url + "/artifact").status_code != 200:
                raise RuntimeError("Existing Markdown artifact retrieval failed.")
            if (
                client.get(
                    f"/api/{mode}/conversations/{THREAD}/turns/{FOLLOWUP}/presentation"
                ).status_code
                != 404
            ):
                raise RuntimeError("Presentation leaked into another turn.")
            if (
                client.get(
                    url.replace(THREAD, "unknown-thread") + "/presentation"
                ).status_code
                != 404
            ):
                raise RuntimeError("Presentation leaked into another conversation.")
            if client.get(f"/api/{mode}/jobs").json() != []:
                raise RuntimeError("Process-local jobs were unexpectedly restored.")
            report[mode] = {
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "byte_size": len(response.content),
                "validation": inspection.to_dict(),
            }
            if mode == "offline":
                with (root / "downloaded.pptx").open("xb") as file:
                    file.write(response.content)
        if report["offline"]["sha256"] == report["live"]["sha256"]:
            raise RuntimeError("Mode fixtures did not exercise distinct publications.")
    return {
        "valid": True,
        "fresh_process": True,
        "download_matches_storage": True,
        "turn_isolation": True,
        "mode_isolation": True,
        "markdown_preserved": True,
        "records": report,
        "downloaded_file": str(root / "downloaded.pptx"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path)
    parser.add_argument("--verify-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.verify_only and args.storage_root is None:
        parser.error("--verify-only requires --storage-root")
    root = (
        args.storage_root or Path(mkdtemp(prefix="mini-deerflow-artifacts-"))
    ).resolve()
    if args.verify_only:
        print(json.dumps(_verify(root), ensure_ascii=True))
        return
    # Fresh identity/outputs only; repeated publication is exercised internally.
    root.mkdir(parents=True, exist_ok=True)
    if (root / "checkpoints.sqlite").exists() or (root / "downloaded.pptx").exists():
        raise RuntimeError("Choose a fresh demonstration storage directory.")
    records = asyncio.run(_prepare(root))
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            __spec__.name,
            "--verify-only",
            "--storage-root",
            str(root),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    evidence = json.loads(child.stdout)
    for mode in ("offline", "live"):
        if any(
            evidence["records"][mode][key] != records[mode][key]
            for key in ("sha256", "byte_size")
        ):
            raise RuntimeError("Publication changed after process restart.")
    print(
        json.dumps(
            {
                "storage_root": str(root),
                "source_file": records["source_file"],
                **evidence,
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
