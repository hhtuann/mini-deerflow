"""Read-only presentation metadata endpoint: verified facts, scoped ownership."""

import asyncio
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api import create_app
from mini_deerflow.conversation import SQLiteConversationRepository
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.phase2_pptx_offline import demo_spec
from mini_deerflow.demo.service import DemoRuntimeService, RunDemoCommand
from mini_deerflow.pptx import render_deck, validate_pptx
from mini_deerflow.pptx.artifacts import PptxArtifactStore
from mini_deerflow.pptx.schemas import DeckSpec
from mini_deerflow.workspace import _filesystem_path

THREAD = "metadata-thread"
TURN = "turn-1"
OTHER_TURN = "turn-2"


async def _seed(root: Path, *, skill: str | None = None) -> None:
    service = DemoRuntimeService(OfflineDemoBackend(root))
    await service.run(RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=skill))


async def _publish(
    root: Path, spec: DeckSpec | None = None, *, turn: str = TURN
) -> dict:
    spec = spec or demo_spec()
    data = render_deck(spec)
    record = await PptxArtifactStore(root, mode="offline").publish(
        THREAD, turn, data, spec
    )
    return {"record": record, "data": data, "spec": spec}


def _metadata(client: TestClient, turn: str = TURN):
    return client.get(
        f"/api/offline/conversations/{THREAD}/turns/{turn}/presentation/metadata"
    )


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("presentation-metadata")
    asyncio.run(_seed(root))
    asyncio.run(_publish(root))
    return root


def test_metadata_returns_verified_facts_from_persisted_spec(seeded):
    with TestClient(create_app(seeded)) as client:
        response = _metadata(client)
        assert response.status_code == 200
        payload = response.json()
        spec = DeckSpec.model_validate_json(
            asyncio.run(
                PptxArtifactStore(seeded, mode="offline").retrieve(THREAD, TURN)
            )[0].spec_json
        )
        assert payload["artifact_type"] == "pptx"
        assert payload["title"] == spec.title
        assert payload["theme"] == spec.theme
        assert payload["slide_count"] == len(spec.slides) == 8
        assert payload["layouts"] == sorted(
            {slide.content.kind for slide in spec.slides}
        )
        assert [item["index"] for item in payload["outline"]] == list(range(1, 9))
        assert [item["title"] for item in payload["outline"]] == [
            slide.title for slide in spec.slides
        ]
        assert [item["layout"] for item in payload["outline"]] == [
            slide.content.kind for slide in spec.slides
        ]
        assert payload["byte_size"] == len(
            asyncio.run(
                PptxArtifactStore(seeded, mode="offline").retrieve(THREAD, TURN)
            )[1]
        )
        assert (
            payload["sha256"]
            == hashlib.sha256(
                asyncio.run(
                    PptxArtifactStore(seeded, mode="offline").retrieve(THREAD, TURN)
                )[1]
            ).hexdigest()
        )
        # No paths, no raw spec, no job-history coupling.
        assert set(payload) == {
            "artifact_type",
            "title",
            "theme",
            "slide_count",
            "layouts",
            "outline",
            "byte_size",
            "sha256",
        }


def test_metadata_survives_fresh_app_and_ignores_job_history(seeded):
    with TestClient(create_app(seeded)) as client:
        assert client.get("/api/offline/jobs").json() == []
        response = _metadata(client)
        assert response.status_code == 200
        assert response.json()["slide_count"] == 8


def test_metadata_missing_artifact_turn_and_thread_are_404(seeded):
    with TestClient(create_app(seeded)) as client:
        assert _metadata(client, OTHER_TURN).status_code == 404
        assert (
            client.get(
                f"/api/offline/conversations/{THREAD}/turns/unknown/metadata"
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/offline/conversations/missing-thread/turns/{TURN}/presentation/metadata"
            ).status_code
            == 404
        )


def test_metadata_is_mode_scoped(seeded):
    with TestClient(create_app(seeded)) as client:
        assert (
            client.get(
                f"/api/live/conversations/{THREAD}/turns/{TURN}/presentation/metadata"
            ).status_code
            == 404
        )


def test_metadata_running_turn_is_ineligible(tmp_path):
    async def scenario():
        repository = SQLiteConversationRepository(tmp_path / "checkpoints.sqlite")
        await repository.create_conversation_with_turn(
            thread_id=THREAD,
            turn_id="running-turn",
            user_message="Presentation metadata eligibility check.",
            session_tool_call_limit=10,
            reserved_tool_calls=2,
        )

    asyncio.run(scenario())
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(
            f"/api/offline/conversations/{THREAD}/turns/running-turn/presentation/metadata"
        )
        assert response.status_code == 404


def test_metadata_tampered_file_is_a_safe_409(seeded):
    store = PptxArtifactStore(seeded, mode="offline")
    record, _ = asyncio.run(store.retrieve(THREAD, TURN))
    path = _filesystem_path(Path(store._workspace(THREAD).root) / store._path(record))
    original = path.read_bytes()
    try:
        path.write_bytes(original[:100])
        with TestClient(create_app(seeded)) as client:
            response = _metadata(client)
            assert response.status_code == 409
            assert response.json() == {"detail": "Presentation is unavailable."}
            # Download stays consistently disabled.
            assert (
                client.get(
                    f"/api/offline/conversations/{THREAD}/turns/{TURN}/presentation"
                ).status_code
                == 409
            )
    finally:
        path.write_bytes(original)
    with TestClient(create_app(seeded)) as client:
        assert _metadata(client).status_code == 200


def test_metadata_reports_the_actually_persisted_spec(tmp_path):
    asyncio.run(_seed(tmp_path))
    spec = demo_spec("dark").model_copy(update={"title": "Metadata identity deck"})
    asyncio.run(_publish(tmp_path, spec))
    with TestClient(create_app(tmp_path)) as client:
        body = _metadata(client).json()
        assert body["title"] == "Metadata identity deck"
        assert body["theme"] == "dark"
        data = asyncio.run(
            PptxArtifactStore(tmp_path, mode="offline").retrieve(THREAD, TURN)
        )[1]
        assert validate_pptx(data, spec).valid
        assert body["sha256"] == hashlib.sha256(data).hexdigest()
