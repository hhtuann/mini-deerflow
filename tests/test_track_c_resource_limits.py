"""Credential-free regressions for Phase 2 Track C resource limits (#45)."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from mini_deerflow.answer_synthesis import (
    AnswerSynthesisContext,
    AnswerSynthesisDraft,
    AnswerSynthesisFormatError,
    LLMAnswerSynthesizer,
)
from mini_deerflow.config import Settings
from mini_deerflow.conversation import SQLiteConversationRepository
from mini_deerflow.demo.view_models import project_trace_event
from mini_deerflow.model import create_chat_model
from mini_deerflow.runtime import AgentRuntime, ResourceLimitExceeded, RuntimeLimits
from mini_deerflow.tracing import (
    ExecutionEvent,
    ExecutionEventType,
    ResourceLimitInfo,
)


class SlowGraph:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.completed = False

    async def ainvoke(self, state, *, config):
        del state, config
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.completed = True
        return {}


class ScriptedModel:
    model_name = "offline-track-c"

    def __init__(self, payloads: list[object], *, delay: float = 0.0) -> None:
        self.payloads = list(payloads)
        self.delay = delay
        self.calls = 0
        self.cancelled = 0

    def invoke(self, messages):
        del messages
        raise AssertionError("async path expected")

    async def ainvoke(self, messages):
        del messages
        self.calls += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        value = self.payloads.pop(0)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, AIMessage):
            return value
        return AIMessage(content=value)


def context() -> AnswerSynthesisContext:
    return AnswerSynthesisContext(
        goal="Tóm tắt bằng chứng công khai",
        language="vi",
        completion_status="complete",
        findings=[],
        evidence=[],
        limitations=[],
    )


def valid_payload() -> str:
    return AnswerSynthesisDraft(language="vi").model_dump_json()


def test_track_c_settings_defaults_and_boundaries() -> None:
    settings = Settings(api_key="secret", _env_file=None)

    assert settings.live_run_deadline_seconds == 1_200.0
    assert settings.synthesis_deadline_seconds == 300.0
    assert settings.model_max_output_tokens == 8_192
    assert settings.synthesis_max_response_chars == 32_768

    for field, value in (
        ("live_run_deadline_seconds", 0),
        ("synthesis_deadline_seconds", float("inf")),
        ("model_max_output_tokens", 0),
        ("synthesis_max_response_chars", 100),
    ):
        with pytest.raises(ValidationError):
            Settings(api_key="secret", _env_file=None, **{field: value})


def test_chat_model_requests_output_limit_without_changing_usage() -> None:
    settings = Settings(
        api_key="secret",
        model_max_output_tokens=4_096,
        _env_file=None,
    )
    with patch("mini_deerflow.model.ChatOpenAI") as constructor:
        create_chat_model(settings)

    assert constructor.call_args.kwargs["max_tokens"] == 4_096
    assert "max_completion_tokens" not in constructor.call_args.kwargs


def test_runtime_deadline_cancels_graph_and_prevents_completion() -> None:
    async def scenario() -> None:
        graph = SlowGraph()
        runtime = AgentRuntime(
            graph=graph,
            limits=RuntimeLimits(live_run_deadline_seconds=0.01),
        )
        with pytest.raises(ResourceLimitExceeded) as caught:
            await runtime.run("A sufficiently long research goal", thread_id="deadline")
        assert caught.value.reason == "overall_deadline_exceeded"
        assert graph.started.is_set()
        assert graph.cancelled.is_set()
        assert graph.completed is False

    asyncio.run(scenario())


def test_concurrent_runtime_deadlines_are_isolated() -> None:
    async def scenario() -> None:
        slow = SlowGraph()

        class FastGraph:
            async def ainvoke(self, state, *, config):
                del config
                return state

        slow_runtime = AgentRuntime(
            graph=slow,
            limits=RuntimeLimits(live_run_deadline_seconds=0.01),
        )
        fast_runtime = AgentRuntime(
            graph=FastGraph(),
            limits=RuntimeLimits(live_run_deadline_seconds=1.0),
        )
        slow_result, fast_result = await asyncio.gather(
            slow_runtime.run("A sufficiently long slow goal", thread_id="slow"),
            fast_runtime.run("A sufficiently long fast goal", thread_id="fast"),
            return_exceptions=True,
        )
        assert isinstance(slow_result, ResourceLimitExceeded)
        assert isinstance(fast_result, dict)

    asyncio.run(scenario())


def test_inner_graph_timeout_is_not_reclassified_as_overall_deadline() -> None:
    class ProviderTimeoutGraph:
        async def ainvoke(self, state, *, config):
            del state, config
            raise TimeoutError("provider transport timeout")

    async def scenario() -> None:
        runtime = AgentRuntime(
            graph=ProviderTimeoutGraph(),
            limits=RuntimeLimits(live_run_deadline_seconds=1.0),
        )
        with pytest.raises(TimeoutError, match="provider transport timeout"):
            await runtime.run("A sufficiently long provider goal", thread_id="provider")

    asyncio.run(scenario())


def test_inner_synthesis_timeout_is_not_reclassified_as_resource_limit() -> None:
    async def scenario() -> None:
        model = ScriptedModel([TimeoutError("provider transport timeout")])
        synthesizer = LLMAnswerSynthesizer(
            model,
            structured_output_mode="prompt_json",
            deadline_seconds=1.0,
            max_response_chars=32_768,
        )
        with pytest.raises(TimeoutError, match="provider transport timeout"):
            await synthesizer.synthesize_answer(context())
        assert model.calls == 1

    asyncio.run(scenario())


def test_persisted_timeout_is_interrupted_with_closed_trace_and_resumable(
    tmp_path,
) -> None:
    async def scenario() -> None:
        database = tmp_path / "track-c.sqlite"
        repository = SQLiteConversationRepository(database)
        graph = SlowGraph()
        runtime = AgentRuntime(
            graph=graph,
            limits=RuntimeLimits(live_run_deadline_seconds=0.01),
            conversation_repository=repository,
        )

        with pytest.raises(ResourceLimitExceeded):
            await runtime.run(
                "A sufficiently long persisted research goal",
                thread_id="persisted-timeout",
                turn_id="turn-one",
            )

        turn = await repository.get_turn("persisted-timeout", "turn-one")
        assert turn.status == "interrupted"
        assert turn.safe_error is None
        serialized = " ".join(turn.trace_events)
        assert "overall_deadline_exceeded" in serialized
        assert "RESOURCE_SECRET" not in serialized
        assert any(
            ExecutionEvent.model_validate_json(item).event_type
            is ExecutionEventType.RESOURCE_LIMIT_REACHED
            for item in turn.trace_events
        )

        reopened = SQLiteConversationRepository(database)
        active = await reopened.get_active_turn("persisted-timeout")
        assert active is not None and active.turn_id == "turn-one"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"deadline_seconds": 0}, "deadline_seconds"),
        ({"deadline_seconds": float("inf")}, "deadline_seconds"),
        ({"max_response_chars": 100}, "max_response_chars"),
    ],
)
def test_synthesis_policy_rejects_invalid_direct_configuration(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        LLMAnswerSynthesizer(
            ScriptedModel([valid_payload()]),
            structured_output_mode="prompt_json",
            **kwargs,
        )


def test_native_mode_valid_synthesis_ignores_text_size_guard() -> None:
    class NativeRunnable:
        async def ainvoke(self, messages):
            del messages
            return AnswerSynthesisDraft(language="vi")

    class NativeModel:
        model_name = "native-offline"

        def with_structured_output(self, schema, *, method, include_raw=True):
            del schema, method, include_raw
            return NativeRunnable()

    async def scenario() -> None:
        synthesizer = LLMAnswerSynthesizer(
            NativeModel(),
            structured_output_mode="native",
            deadline_seconds=1.0,
            max_response_chars=1_024,
        )
        answer = await synthesizer.synthesize_answer(context())
        assert answer.language == "vi"

    asyncio.run(scenario())


def test_synthesis_deadline_cancels_first_call_and_never_retries() -> None:
    async def scenario() -> None:
        model = ScriptedModel([valid_payload(), valid_payload()], delay=0.05)
        synthesizer = LLMAnswerSynthesizer(
            model,
            structured_output_mode="prompt_json",
            deadline_seconds=0.01,
            max_response_chars=32_768,
        )
        with pytest.raises(AnswerSynthesisFormatError) as caught:
            await synthesizer.synthesize_answer(context())
        assert caught.value.category.value == "resource_limit"
        assert caught.value.reason.value == "synthesis_deadline_exceeded"
        assert model.calls == 1
        assert model.cancelled == 1
        assert caught.value.attempt_failures[0].attempt == 1

    asyncio.run(scenario())


def test_oversized_synthesis_response_skips_second_attempt_without_leak() -> None:
    async def scenario() -> None:
        secret = "SECRET_RAW_PROVIDER_TEXT"
        model = ScriptedModel(["{" + secret + ("x" * 2_000), valid_payload()])
        synthesizer = LLMAnswerSynthesizer(
            model,
            structured_output_mode="prompt_json",
            deadline_seconds=1.0,
            max_response_chars=1_024,
        )
        with pytest.raises(AnswerSynthesisFormatError) as caught:
            await synthesizer.synthesize_answer(context())
        assert caught.value.category.value == "resource_limit"
        assert caught.value.reason.value == "synthesis_output_limit_exceeded"
        assert model.calls == 1
        assert secret not in repr(caught.value.attempt_failures)
        assert secret not in str(caught.value)

    asyncio.run(scenario())


def test_under_limit_valid_synthesis_is_unchanged() -> None:
    async def scenario() -> None:
        model = ScriptedModel([valid_payload()])
        synthesizer = LLMAnswerSynthesizer(
            model,
            structured_output_mode="prompt_json",
            deadline_seconds=1.0,
            max_response_chars=32_768,
        )
        answer = await synthesizer.synthesize_answer(context())
        assert answer.language == "vi"
        assert model.calls == 1

    asyncio.run(scenario())


def test_resource_limit_event_is_closed_persistent_and_projected() -> None:
    event = ExecutionEvent(
        event_type=ExecutionEventType.RESOURCE_LIMIT_REACHED,
        status="completed",
        timestamp=datetime.now(UTC),
        root_run_id="run-1",
        run_id="run-1",
        thread_id="thread-1",
        sequence=1,
        kind="resource_limit",
        phase="outcome",
        outcome="refused",
        node="synthesize",
        resource_limit=ResourceLimitInfo(
            reason="synthesis_output_limit_exceeded",
        ),
    )
    restored = ExecutionEvent.model_validate_json(event.model_dump_json())
    view = project_trace_event(restored)

    assert view.resource_limit_reason == "synthesis_output_limit_exceeded"
    assert "SECRET" not in event.model_dump_json()
    with pytest.raises(ValidationError):
        ResourceLimitInfo.model_validate(
            {
                "reason": "synthesis_deadline_exceeded",
                "raw_response": "SECRET",
            }
        )


def test_resource_limit_exception_has_closed_identity() -> None:
    error = ResourceLimitExceeded("overall_deadline_exceeded")
    assert error.reason == "overall_deadline_exceeded"
    assert str(error) == "overall_deadline_exceeded"
    assert "prompt" not in str(error).casefold()
