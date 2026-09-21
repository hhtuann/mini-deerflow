from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from mini_deerflow.config import Settings
from mini_deerflow.demo import live_backend
from mini_deerflow.demo.live_backend import LiveDemoBackend
from mini_deerflow.demo.service import RunDemoCommand
from mini_deerflow.runtime import RuntimeLimits
from mini_deerflow.state import AgentState


def test_live_backend_is_lazy_so_offline_mode_can_be_selected_without_credentials(
    tmp_path: Path,
) -> None:
    backend = LiveDemoBackend(tmp_path)

    assert isinstance(backend.limits, RuntimeLimits)


def test_live_backend_runs_the_real_runtime_composition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(api_key="model-key", jina_api_key="jina-key")
    limits = RuntimeLimits(max_total_tool_calls=3)
    captured: dict[str, object] = {}
    state = cast(AgentState, {"final_answer": "completed"})

    class FakeRuntime:
        conversation_repository = None

        async def run(
            self,
            goal: str,
            *,
            thread_id: str,
            turn_id: str,
        ) -> AgentState:
            captured["goal"] = goal
            captured["thread_id"] = thread_id
            captured["turn_id"] = turn_id
            return state

        async def record_turn_traces(self, *_args: object) -> None:
            return None

    @asynccontextmanager
    async def fake_open_runtime(
        received_settings: Settings,
        workspace_root: str | Path,
        checkpoint_path: str | Path,
        **kwargs: object,
    ):
        captured["settings"] = received_settings
        captured["workspace_root"] = Path(workspace_root)
        captured["checkpoint_path"] = Path(checkpoint_path)
        captured["kwargs"] = kwargs
        yield FakeRuntime()

    monkeypatch.setattr(live_backend, "open_default_agent_runtime", fake_open_runtime)
    backend = LiveDemoBackend(tmp_path, settings=settings, limits=limits)

    result = asyncio.run(
        backend.run(
            RunDemoCommand(
                thread_id="live-test",
                turn_id="turn-live",
                goal="Find public sources.",
            ),
        )
    )

    assert result.state is state
    assert result.limits is limits
    assert captured["settings"] is settings
    assert captured["goal"] == "Find public sources."
    assert captured["thread_id"] == "live-test"
    assert captured["turn_id"] == "turn-live"
    assert captured["workspace_root"] == tmp_path / "workspaces" / "live-test"
    assert captured["checkpoint_path"] == tmp_path / "checkpoints.sqlite"
    kwargs = cast(dict[str, object], captured["kwargs"])
    assert kwargs["allow_write"] is True
    assert kwargs["limits"] is limits
    assert "tracer" in kwargs
