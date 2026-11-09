"""Entry-point and cancellation-hardening regressions for Track C (#45, PR #54).

Red-first against the NEEDS_FIX review at HEAD f52753b8:
config propagation at real Live/CLI entrypoints, swallowed-cancellation
deadline enforcement, safe CLI diagnostics, and slide-model output-cap scope.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage

from mini_deerflow.answer_synthesis import (
    AnswerSynthesisContext,
    AnswerSynthesisFormatError,
    LLMAnswerSynthesizer,
)
from mini_deerflow.cli import _safe_cli_error, main
from mini_deerflow.config import Settings
from mini_deerflow.demo import live_backend
from mini_deerflow.demo.live_backend import LiveDemoBackend
from mini_deerflow.demo.service import ResumeDemoCommand, RunDemoCommand
from mini_deerflow.resource_limits import ResourceLimitExceeded
from mini_deerflow.runtime import AgentRuntime, RuntimeLimits
from mini_deerflow.state import AgentState


def _settings(deadline: float) -> Settings:
    return Settings(
        api_key="test-key",
        live_run_deadline_seconds=deadline,
        _env_file=None,
    )


class FakeRuntime:
    conversation_repository = None

    async def run(self, goal, *, thread_id, turn_id, **kwargs):
        del goal, thread_id, turn_id, kwargs
        return cast(AgentState, {"final_answer": "completed", "turn_id": "t-1"})

    async def resume(self, *, thread_id):
        del thread_id
        return cast(AgentState, {"final_answer": "completed", "turn_id": "t-1"})

    async def record_turn_traces(self, *_args):
        return None


def _as_context(captured):
    @asynccontextmanager
    async def fake_open_runtime(settings, workspace_root, checkpoint_path, **kwargs):
        captured["settings"] = settings
        captured["kwargs"] = kwargs
        yield FakeRuntime()

    return fake_open_runtime


# ------------------------------------------------ F1: effective config propagation


def test_live_backend_without_explicit_limits_uses_configured_deadline(
    tmp_path, monkeypatch
):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        live_backend, "open_default_agent_runtime", _as_context(captured)
    )
    backend = LiveDemoBackend(tmp_path, settings=_settings(deadline=10.0))

    asyncio.run(
        backend.run(
            RunDemoCommand(
                thread_id="deadline-run",
                turn_id="turn-1",
                goal="Find public sources about energy.",
            )
        )
    )

    limits = cast(dict[str, object], captured["kwargs"])["limits"]
    assert limits is None or limits.live_run_deadline_seconds == 10.0


def test_live_backend_run_and_resume_inherit_settings_deadline(tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        live_backend, "open_default_agent_runtime", _as_context(captured)
    )
    backend = LiveDemoBackend(tmp_path, settings=_settings(deadline=10.0))

    asyncio.run(
        backend.run(
            RunDemoCommand(
                thread_id="deadline-run",
                turn_id="turn-1",
                goal="Find public sources about energy.",
            )
        )
    )
    first = cast(dict[str, object], captured["kwargs"])["limits"]
    assert first is None or first.live_run_deadline_seconds == 10.0

    asyncio.run(backend.resume(ResumeDemoCommand(thread_id="deadline-run")))
    second = cast(dict[str, object], captured["kwargs"])["limits"]
    assert second is None or second.live_run_deadline_seconds == 10.0


def test_live_backend_explicit_limits_deadline_wins(tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        live_backend, "open_default_agent_runtime", _as_context(captured)
    )
    explicit = RuntimeLimits(live_run_deadline_seconds=7.0)
    backend = LiveDemoBackend(
        tmp_path, settings=_settings(deadline=10.0), limits=explicit
    )

    asyncio.run(
        backend.run(
            RunDemoCommand(
                thread_id="deadline-run",
                turn_id="turn-1",
                goal="Find public sources about energy.",
            )
        )
    )

    assert cast(dict[str, object], captured["kwargs"])["limits"] is explicit


def test_cli_main_includes_configured_deadline_in_limits(tmp_path, capsys):
    settings = _settings(deadline=10.0)
    state = cast(AgentState, {"final_answer": "done"})
    runtime = Mock(spec=AgentRuntime)
    runtime.run = AsyncMock(return_value=state)
    runtime_context = MagicMock()
    runtime_context.__aenter__.return_value = runtime
    runtime_context.__aexit__.return_value = None
    captured: dict[str, object] = {}

    def fake_open(_settings, _root, _path, **kwargs):
        captured["kwargs"] = kwargs
        return runtime_context

    with (
        patch("mini_deerflow.cli.Settings", return_value=settings),
        patch("mini_deerflow.cli.open_default_agent_runtime", side_effect=fake_open),
    ):
        exit_code = main(
            [
                "run",
                "Research bounded agent runtimes.",
                "--thread-id",
                "cli-deadline",
                "--checkpoint-db",
                str(tmp_path / "cp.sqlite"),
                "--workspace",
                str(tmp_path / "ws"),
            ]
        )

    assert exit_code == 0
    limits = cast(dict[str, object], captured["kwargs"])["limits"]
    assert isinstance(limits, RuntimeLimits)
    assert limits.live_run_deadline_seconds == 10.0
    capsys.readouterr()


# --------------------------------- F2: swallowed cancellation cannot yield success


class SwallowingSlowGraph:
    """Catch cancellation and return a late "success" after the deadline."""

    def __init__(self) -> None:
        self.cancelled = 0

    async def ainvoke(self, state, *, config):
        del config
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled += 1
            return {"final_answer": "late success"}
        raise AssertionError("unreachable")


def test_swallowed_cancellation_cannot_return_late_success_runtime():
    async def scenario():
        graph = SwallowingSlowGraph()
        runtime = AgentRuntime(
            graph=graph,
            limits=RuntimeLimits(live_run_deadline_seconds=0.01),
        )
        with pytest.raises(ResourceLimitExceeded) as caught:
            await runtime.run("A sufficiently long research goal", thread_id="swallow")
        assert caught.value.reason == "overall_deadline_exceeded"
        assert graph.cancelled == 1

    asyncio.run(scenario())


class SwallowingModel:
    model_name = "swallow-double"

    def __init__(self, late_payload) -> None:
        self.late_payload = late_payload
        self.calls = 0

    def invoke(self, messages):
        del messages
        raise AssertionError("async path expected")

    async def ainvoke(self, messages):
        del messages
        self.calls += 1
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return AIMessage(content=self.late_payload)
        raise AssertionError("unreachable")


def _synthesis_context() -> AnswerSynthesisContext:
    return AnswerSynthesisContext(
        goal="Tóm tắt bằng chứng công khai",
        language="vi",
        completion_status="complete",
        findings=[],
        evidence=[],
        limitations=[],
    )


def test_swallowed_cancellation_cannot_return_late_valid_synthesis():
    async def scenario():
        late_valid = (
            '{"language": "vi", "summary": [], "evidence": [], '
            '"discrepancies": [], "limitations": []}'
        )
        model = SwallowingModel(late_valid)
        synthesizer = LLMAnswerSynthesizer(
            model,
            structured_output_mode="prompt_json",
            deadline_seconds=0.01,
            max_response_chars=32_768,
        )
        with pytest.raises(AnswerSynthesisFormatError) as caught:
            await synthesizer.synthesize_answer(_synthesis_context())
        assert caught.value.category.value == "resource_limit"
        assert caught.value.reason.value == "synthesis_deadline_exceeded"
        assert model.calls == 1

    asyncio.run(scenario())


def test_swallowed_cancellation_prevents_second_synthesis_attempt():
    async def scenario():
        late_invalid = "{not json"
        model = SwallowingModel(late_invalid)
        synthesizer = LLMAnswerSynthesizer(
            model,
            structured_output_mode="prompt_json",
            deadline_seconds=0.01,
            max_response_chars=32_768,
        )
        with pytest.raises(AnswerSynthesisFormatError) as caught:
            await synthesizer.synthesize_answer(_synthesis_context())
        assert caught.value.category.value == "resource_limit"
        assert caught.value.reason.value == "synthesis_deadline_exceeded"
        assert model.calls == 1

    asyncio.run(scenario())


# ---------------------------------------------------- F3: safe CLI diagnostics


def test_cli_reports_resource_limit_specifically_without_raw_text():
    message = _safe_cli_error(ResourceLimitExceeded("overall_deadline_exceeded"))
    assert "configured time or output limit" in message
    assert "Resume" in message
    assert "overall_deadline_exceeded" not in message


# ------------------------------------- F4: slide-model output-cap scope disclosure


def test_live_slide_planner_model_factory_inherits_output_cap(monkeypatch):
    from mini_deerflow import api

    monkeypatch.setenv("MINI_DEERFLOW_API_KEY", "slide-key")
    monkeypatch.setenv("MINI_DEERFLOW_MODEL_MAX_OUTPUT_TOKENS", "2048")

    captured: dict[str, object] = {}

    def fake_create_chat_model(received_settings):
        captured["settings"] = received_settings
        return object()

    with patch(
        "mini_deerflow.model.create_chat_model",
        side_effect=fake_create_chat_model,
    ):
        api._live_slide_planner_model()

    received = cast(Settings, captured["settings"])
    assert received.model_max_output_tokens == 2_048
