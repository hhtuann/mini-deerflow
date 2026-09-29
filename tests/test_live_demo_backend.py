from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from mini_deerflow.config import Settings
from mini_deerflow.demo import live_backend
from mini_deerflow.demo.live_backend import LiveDemoBackend
from mini_deerflow.demo.service import (
    ContinueDemoCommand,
    ResumeDemoCommand,
    RunDemoCommand,
)
from mini_deerflow.runtime import RuntimeLimits
from mini_deerflow.sandbox import SessionSandboxResolver
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
    settings = Settings(api_key="google-key")
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
    assert captured["workspace_root"] == SessionSandboxResolver(
        tmp_path / "workspaces"
    ).resolve("live-test")
    assert captured["checkpoint_path"] == tmp_path / "checkpoints.sqlite"
    kwargs = cast(dict[str, object], captured["kwargs"])
    assert kwargs["allow_write"] is True
    assert kwargs["limits"] is limits
    assert "tracer" in kwargs


def test_live_backend_reuses_one_sandbox_for_all_thread_operations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    roots: list[Path] = []
    state = cast(AgentState, {"final_answer": "completed"})
    snapshot = object()

    class FakeRuntime:
        conversation_repository = None

        async def continue_thread(
            self, *_args: object, **_kwargs: object
        ) -> AgentState:
            return state

        async def resume(self, **_kwargs: object) -> AgentState:
            return state

        async def load_conversation(self, _thread_id: str) -> object:
            return snapshot

        async def record_turn_traces(self, *_args: object) -> None:
            return None

    @asynccontextmanager
    async def fake_open_runtime(
        _settings: Settings,
        workspace_root: str | Path,
        _checkpoint_path: str | Path,
        **_kwargs: object,
    ):
        roots.append(Path(workspace_root))
        yield FakeRuntime()

    monkeypatch.setattr(live_backend, "open_default_agent_runtime", fake_open_runtime)
    backend = LiveDemoBackend(
        tmp_path,
        settings=Settings(api_key="google-key"),
    )

    asyncio.run(
        backend.continue_thread(
            ContinueDemoCommand(
                thread_id="shared-thread",
                turn_id="turn-follow-up",
                user_message="Continue with public evidence.",
            )
        )
    )
    asyncio.run(backend.resume(ResumeDemoCommand(thread_id="shared-thread")))
    loaded = asyncio.run(backend.load_conversation("shared-thread"))

    expected = SessionSandboxResolver(tmp_path / "workspaces").resolve("shared-thread")
    assert roots == [expected, expected, expected]
    assert loaded is snapshot


def test_live_backend_does_not_reuse_existing_legacy_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    legacy_root = tmp_path / "workspaces" / "legacy-thread"
    legacy_root.mkdir(parents=True)
    captured_root: Path | None = None
    state = cast(AgentState, {"final_answer": "completed"})

    class FakeRuntime:
        conversation_repository = None

        async def resume(self, **_kwargs: object) -> AgentState:
            return state

        async def record_turn_traces(self, *_args: object) -> None:
            return None

    @asynccontextmanager
    async def fake_open_runtime(
        _settings: Settings,
        workspace_root: str | Path,
        _checkpoint_path: str | Path,
        **_kwargs: object,
    ):
        nonlocal captured_root
        captured_root = Path(workspace_root)
        yield FakeRuntime()

    monkeypatch.setattr(live_backend, "open_default_agent_runtime", fake_open_runtime)
    backend = LiveDemoBackend(
        tmp_path,
        settings=Settings(api_key="google-key"),
    )

    asyncio.run(backend.resume(ResumeDemoCommand(thread_id="legacy-thread")))

    assert captured_root == SessionSandboxResolver(tmp_path / "workspaces").resolve(
        "legacy-thread"
    )
    assert captured_root != legacy_root.resolve()


def test_live_backend_ignores_legacy_workspace_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    legacy_root = tmp_path / "workspaces" / "legacy-thread"
    legacy_root.mkdir(parents=True)
    original_is_symlink = Path.is_symlink

    def fake_is_symlink(path: Path) -> bool:
        if path == legacy_root:
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    backend = LiveDemoBackend(tmp_path)

    resolved = backend._resolve_workspace_root("legacy-thread")

    assert resolved == SessionSandboxResolver(tmp_path / "workspaces").resolve(
        "legacy-thread"
    )
