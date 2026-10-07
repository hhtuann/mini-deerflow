import asyncio
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from langchain_openai import ChatOpenAI
from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource

from mini_deerflow.demo import offline_scenario
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.service import (
    ContinueDemoCommand,
    DemoRuntimeService,
    ResumeDemoCommand,
    RunDemoCommand,
)
from mini_deerflow.sandbox import SessionSandboxResolver


def test_offline_backend_uses_distinct_opaque_sandboxes_for_case_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots: list[Path] = []
    snapshot = object()

    class FakeRuntime:
        async def load_conversation(self, _thread_id: str) -> object:
            return snapshot

    @asynccontextmanager
    async def fake_open_runtime(
        _settings: object,
        workspace_root: str | Path,
        _checkpoint_path: str | Path,
        **_kwargs: object,
    ):
        roots.append(Path(workspace_root))
        yield FakeRuntime()

    monkeypatch.setattr(
        offline_scenario,
        "open_default_agent_runtime",
        fake_open_runtime,
    )
    storage_root = tmp_path / "offline"
    backend = OfflineDemoBackend(storage_root)

    assert asyncio.run(backend.load_conversation("Alpha")) is snapshot
    assert asyncio.run(backend.load_conversation("alpha")) is snapshot

    resolver = SessionSandboxResolver(storage_root / "workspaces")
    assert roots == [resolver.resolve("Alpha"), resolver.resolve("alpha")]
    assert roots[0] != roots[1]


def test_offline_run_list_duplicate_and_completed_resume_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poisoned = tmp_path / ".env"
    poisoned.write_text(
        "MINI_DEERFLOW_API_KEY=must-not-be-read\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    def deny_external(*_args, **_kwargs):
        raise AssertionError("external access attempted")

    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_file", deny_external)
    monkeypatch.setattr(ChatOpenAI, "__init__", deny_external)

    async def scenario():
        backend = OfflineDemoBackend(tmp_path / "demo-data")
        service = DemoRuntimeService(backend)
        # Patch transports only after asyncio has created its Windows self-pipe.
        # The runtime must use the injected resolver/provider from this point on.
        monkeypatch.setattr(socket, "getaddrinfo", deny_external)
        monkeypatch.setattr(socket, "create_connection", deny_external)
        monkeypatch.setattr(socket.socket, "connect", deny_external)
        command = RunDemoCommand(thread_id="offline-test")
        first = await service.run(command)
        diagnostics = backend.diagnostics(command.thread_id)
        threads = await service.list_threads()
        duplicate = await service.run(command)
        resumed = await service.resume(ResumeDemoCommand(command.thread_id))
        return backend, first, diagnostics, threads, duplicate, resumed

    backend, first, before, threads, duplicate, resumed = asyncio.run(scenario())
    after = backend.diagnostics("offline-test")

    assert threads == ("offline-test",)
    assert before == after
    assert duplicate.artifact.markdown_source == first.artifact.markdown_source
    assert first.artifact.markdown_source == resumed.artifact.markdown_source
    outcomes = {(event.kind, event.outcome) for event in first.traces}
    assert ("tool", "safety_denied") in outcomes
    assert ("tool", "provider_failed") in outcomes
    assert ("citation_validation", "rejected") in outcomes
    assert ("review", "replan") in outcomes
    assert ("review", "continue") in outcomes
    assert ("review", "finish") in outcomes
    assert ("delegation", "partial_failure") in outcomes
    assert ("artifact", "written") in outcomes

    resume_events = [
        event for event in resumed.traces if event.run_id.startswith("day15-resume-")
    ]
    # A completed conversational turn is loaded idempotently; resume executes
    # only when the durable ledger has an interrupted active turn.
    assert resume_events == []
    assert not any(event.kind in {"tool", "delegation"} for event in resume_events)


def test_offline_service_persists_two_isolated_turns_on_one_thread(
    tmp_path: Path,
) -> None:
    async def scenario():
        backend = OfflineDemoBackend(tmp_path / "multi-turn")
        service = DemoRuntimeService(backend)
        first = await service.run(
            RunDemoCommand(
                thread_id="same-public-thread",
                turn_id="turn-first",
                goal="Research the first bounded public topic.",
            )
        )
        second = await service.continue_thread(
            ContinueDemoCommand(
                thread_id="same-public-thread",
                turn_id="turn-second",
                user_message="Now compare it with a second public topic.",
            )
        )
        chat = await service.load_conversation("same-public-thread")
        raw = await backend.load_conversation("same-public-thread")
        return first, second, chat, raw

    first, second, chat, raw = asyncio.run(scenario())

    assert first.thread_id == second.thread_id == "same-public-thread"
    assert first.turn_id == "turn-first"
    assert second.turn_id == "turn-second"
    assert [turn.turn_id for turn in chat.turns] == ["turn-first", "turn-second"]
    assert all(turn.status == "completed" for turn in chat.turns)
    assert all(turn.run is not None for turn in chat.turns)
    assert all(turn.assistant_message for turn in chat.turns)
    assert all(
        "# Research Report" not in (turn.assistant_message or "")
        and "Tool calls:" not in (turn.assistant_message or "")
        for turn in chat.turns
    )
    assert all(
        turn.record.assistant_response == turn.state["final_answer"]
        for turn in raw.turns
        if turn.state is not None
    )
    assert all(
        turn.state["research_report"] != turn.state["final_answer"]
        for turn in raw.turns
        if turn.state is not None
    )
    artifact_paths = [
        turn.state["artifact_path"] for turn in raw.turns if turn.state is not None
    ]
    assert artifact_paths[0] == "reports/day-15-demo.md"
    assert artifact_paths[1] == "reports/day-15-demo-turn-second.md"
    assert all(
        event.turn_id == turn.turn_id
        for turn in chat.turns
        if turn.run is not None
        for event in turn.run.traces
    )


def test_offline_run_ignores_all_runtime_environment_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poisoned_settings = {
        "MINI_DEERFLOW_API_KEY": "environment-key-must-not-be-used",
        "MINI_DEERFLOW_BASE_URL": "not-a-url",
        "MINI_DEERFLOW_MODEL_NAME": "",
        "MINI_DEERFLOW_TEMPERATURE": "not-a-number",
        "MINI_DEERFLOW_REQUEST_TIMEOUT": "-1",
        "MINI_DEERFLOW_MAX_RETRIES": "999",
        "MINI_DEERFLOW_WIKI_REQUEST_TIMEOUT": "0",
        "MINI_DEERFLOW_STRUCTURED_OUTPUT_MODE": "invalid-mode",
    }
    for name, value in poisoned_settings.items():
        monkeypatch.setenv(name, value)

    def deny_external(*_args, **_kwargs):
        raise AssertionError("external access attempted")

    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_file", deny_external)
    monkeypatch.setattr(ChatOpenAI, "__init__", deny_external)

    async def scenario():
        backend = OfflineDemoBackend(tmp_path / "environment-isolation")
        service = DemoRuntimeService(backend)
        monkeypatch.setattr(socket, "getaddrinfo", deny_external)
        monkeypatch.setattr(socket, "create_connection", deny_external)
        monkeypatch.setattr(socket.socket, "connect", deny_external)
        return await service.run(RunDemoCommand(thread_id="poisoned-environment"))

    view = asyncio.run(scenario())

    assert view.workflow_status == "completed"
    assert view.artifact.available is True


def test_fresh_backend_resumes_interrupted_checkpoint_deterministically(
    tmp_path: Path,
) -> None:
    async def scenario():
        baseline_service = DemoRuntimeService(OfflineDemoBackend(tmp_path / "baseline"))
        baseline = await baseline_service.run(
            RunDemoCommand(thread_id="baseline-thread")
        )

        storage_root = tmp_path / "restart"
        interrupted_backend = OfflineDemoBackend(
            storage_root,
            interrupt_after_delegation=True,
        )
        interrupted_service = DemoRuntimeService(interrupted_backend)
        with pytest.raises(
            RuntimeError,
            match="deterministic interruption after delegation checkpoint",
        ):
            await interrupted_service.run(RunDemoCommand(thread_id="restart-thread"))
        before_restart = interrupted_backend.diagnostics("restart-thread")

        resumed_backend = OfflineDemoBackend(storage_root)
        resumed_service = DemoRuntimeService(resumed_backend)
        resumed = await resumed_service.resume(
            ResumeDemoCommand(thread_id="restart-thread")
        )
        chat = await resumed_service.load_conversation("restart-thread")
        after_restart = resumed_backend.diagnostics("restart-thread")
        return baseline, before_restart, resumed, after_restart, chat

    baseline, before_restart, resumed, after_restart, chat = asyncio.run(scenario())

    assert before_restart is not None
    assert before_restart.web_search_calls == 3
    assert before_restart.web_fetch_calls == 0
    assert before_restart.researcher_branches == ("alpha", "beta")
    assert after_restart is not None
    assert after_restart.web_search_calls == 0
    assert after_restart.web_fetch_calls == 0
    assert after_restart.resolver_calls == 0
    assert after_restart.researcher_branches == ()
    assert resumed.workflow_status == "completed"
    assert resumed.artifact.markdown_source == baseline.artifact.markdown_source
    assert all(event.run_id.startswith("day15-resume-") for event in resumed.traces)
    assert not any(event.kind in {"tool", "delegation"} for event in resumed.traces)
    durable_parent_run_ids = {
        event.run_id
        for event in chat.turns[0].run.traces  # type: ignore[union-attr]
        if event.kind == "run"
    }
    assert durable_parent_run_ids == {"day15-run-001", "day15-resume-001"}
