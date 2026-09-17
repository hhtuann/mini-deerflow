import asyncio
import socket
from pathlib import Path

import pytest
from langchain_openai import ChatOpenAI
from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource

from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.service import (
    DemoRuntimeService,
    ResumeDemoCommand,
    RunDemoCommand,
)
from mini_deerflow.persistence import ThreadAlreadyExistsError


def test_offline_run_list_duplicate_and_completed_resume_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poisoned = tmp_path / ".env"
    poisoned.write_text("MINI_DEERFLOW_API_KEY=must-not-be-read\n", encoding="utf-8")
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
        with pytest.raises(ThreadAlreadyExistsError):
            await service.run(command)
        resumed = await service.resume(ResumeDemoCommand(command.thread_id))
        return backend, first, diagnostics, threads, resumed

    backend, first, before, threads, resumed = asyncio.run(scenario())
    after = backend.diagnostics("offline-test")

    assert threads == ("offline-test",)
    assert before == after
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
    assert any(
        event.kind == "checkpoint" and event.outcome == "resumed"
        for event in resume_events
    )
    assert not any(event.kind in {"tool", "delegation"} for event in resume_events)


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
        "MINI_DEERFLOW_JINA_API_KEY": "environment-jina-key-must-not-be-used",
        "MINI_DEERFLOW_WEB_REQUEST_TIMEOUT": "0",
        "MINI_DEERFLOW_WEB_MAX_RESPONSE_BYTES": "1",
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
        after_restart = resumed_backend.diagnostics("restart-thread")
        return baseline, before_restart, resumed, after_restart

    baseline, before_restart, resumed, after_restart = asyncio.run(scenario())

    assert before_restart is not None
    assert before_restart.web_search_calls == 2
    assert before_restart.web_fetch_calls == 1
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
