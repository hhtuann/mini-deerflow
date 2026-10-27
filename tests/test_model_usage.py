"""Stage A per-node usage observability: capture, correlation, privacy.

All tests are offline. Model doubles emulate provider ``AIMessage`` shapes; no
paid provider call is made anywhere in this module.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError

from mini_deerflow.model_usage import (
    UsageRecordingModel,
    _clean_int,
    _clean_usage,
    close_scope,
    model_name_of,
    open_scope,
)
from mini_deerflow.structured_output import (
    NativeStructuredRawRunnable,
    PromptJsonStructuredRunnable,
    create_structured_output_runnable,
)
from mini_deerflow.tracing import (
    ExecutionEventType,
    ExecutionTracer,
    ModelInvocationInfo,
    TraceKind,
    TraceOutcome,
)


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


class _Plan(BaseModel):
    goal: str = "g"
    steps: list[dict[str, Any]] = []


def _message(usage: dict[str, int] | None = None, content: str = "{}") -> AIMessage:
    # langchain-core's UsageMetadata requires input/output/total keys.
    if usage is not None:
        usage = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            **(
                {"cached_tokens": usage["cached_tokens"]}
                if "cached_tokens" in usage
                else {}
            ),
        }
    return AIMessage(content=content, usage_metadata=usage)


class FakePromptJsonModel:
    """prompt_json surface: invoke/ainvoke returning AIMessage."""

    model_name = "glm-test-1"

    def __init__(self, responses: list[AIMessage] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls = 0

    def invoke(self, messages: object) -> AIMessage:
        self.calls += 1
        if not self._responses:
            return _message()
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def ainvoke(self, messages: object) -> AIMessage:
        return self.invoke(messages)


class FakeNativeModel:
    """Native surface: with_structured_output(include_raw=...)."""

    model_name = "glm-native-2"

    def __init__(
        self, parsed: BaseModel | None = None, error: BaseException | None = None
    ) -> None:
        self._parsed = parsed
        self._error = error

    def with_structured_output(
        self, schema: Any, *, method: str, include_raw: bool = False, **_: Any
    ) -> Any:
        model = self

        class _Runnable:
            def invoke(self, messages: object) -> Any:
                if model._error is not None:
                    raise model._error
                raw = _message(
                    {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
                )
                if not include_raw:
                    return model._parsed
                return {"raw": raw, "parsed": model._parsed, "parsing_error": None}

        return _Runnable()


class LegacyNativeModel(FakeNativeModel):
    """Doubles/providers without ``include_raw`` keep the legacy shape."""

    def with_structured_output(self, schema: Any, *, method: str, **kwargs: Any) -> Any:
        if "include_raw" in kwargs:
            raise TypeError("unexpected keyword argument 'include_raw'")
        model = self

        class _Runnable:
            def invoke(self, messages: object) -> Any:
                return model._parsed

        return _Runnable()


# ---------------------------------------------------------------------------
# Unit: value hygiene
# ---------------------------------------------------------------------------


def test_clean_int_rejects_bool_negative_float_and_overflow():
    assert _clean_int(5) == 5
    assert _clean_int(0) == 0
    assert _clean_int(True) is None
    assert _clean_int(-1) is None
    assert _clean_int(1.5) is None
    assert _clean_int("7") is None
    assert _clean_int(10_000_000_001) is None


def test_clean_usage_requires_sdk_dictionary_with_valid_fields():
    assert _clean_usage(None) is None
    assert _clean_usage("10") is None
    assert _clean_usage({}) is None
    assert _clean_usage({"input_tokens": "x"}) is None
    assert _clean_usage({"input_tokens": 3, "output_tokens": -2}) == {
        "input_tokens": 3,
        "output_tokens": None,
        "total_tokens": None,
        "cached_tokens": None,
    }
    assert _clean_usage({"total_tokens": 0}) == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": 0,
        "cached_tokens": None,
    }


def test_model_name_is_bounded_and_sanitized():
    assert model_name_of(type("M", (), {"model_name": "glm-5.3"})()) == "glm-5.3"
    assert model_name_of(object()) is None
    assert model_name_of(type("M", (), {"model_name": ""})()) is None
    assert model_name_of(type("M", (), {"model_name": 123})()) is None
    long = type("M", (), {"model_name": "a" * 200})()
    assert len(model_name_of(long)) == 64  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Unit: recording delegate (prompt_json path)
# ---------------------------------------------------------------------------


def test_recording_delegate_captures_usage_per_invocation():
    model = FakePromptJsonModel(
        [_message({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})]
    )
    delegate = UsageRecordingModel(model, "Plan")
    scope, token = open_scope()
    try:
        response = delegate.invoke([])
        assert response.usage_metadata is not None
    finally:
        close_scope(token)
    assert len(scope.observations) == 1
    observation = scope.observations[0]
    assert observation.schema_name == "Plan"
    assert observation.model_name == "glm-test-1"
    assert observation.usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": None,
    }
    assert observation.failed is False
    assert observation.duration_ms is not None and observation.duration_ms >= 0


def test_recording_delegate_marks_failed_attempts_without_usage():
    model = FakePromptJsonModel([RuntimeError("provider exploded")])
    delegate = UsageRecordingModel(model, "Plan")
    scope, token = open_scope()
    try:
        with pytest.raises(RuntimeError):
            delegate.invoke([])
    finally:
        close_scope(token)
    observation = scope.observations[0]
    assert observation.failed is True
    assert observation.usage is None


def test_recording_delegate_is_noop_without_scope():
    model = FakePromptJsonModel()
    delegate = UsageRecordingModel(model, "Plan")
    delegate.invoke([])  # must not raise
    assert model.calls == 1


def test_recording_delegate_forwards_attributes_and_preserves_calls():
    model = FakePromptJsonModel()
    delegate = UsageRecordingModel(model, "Plan")
    assert delegate.model_name == "glm-test-1"
    scope, token = open_scope()
    try:
        delegate.invoke([])
        delegate.invoke([])
    finally:
        close_scope(token)
    assert model.calls == 2
    assert len(scope.observations) == 2


# ---------------------------------------------------------------------------
# Unit: structured seam shapes
# ---------------------------------------------------------------------------


def test_prompt_json_seam_returns_parsed_schema_and_records_usage():
    model = FakePromptJsonModel(
        [_message({"total_tokens": 9}, content=json.dumps({"goal": "g"}))]
    )
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="prompt_json"
    )
    assert isinstance(runnable, PromptJsonStructuredRunnable)
    scope, token = open_scope()
    try:
        result = runnable.invoke([])
        assert isinstance(result, _Plan)
    finally:
        close_scope(token)
    assert scope.observations[0].usage is not None
    assert scope.observations[0].schema_name == "_Plan"


def test_native_seam_preserves_parsed_output_and_records_usage():
    parsed = _Plan()
    model = FakeNativeModel(parsed=parsed)
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="native"
    )
    assert isinstance(runnable, NativeStructuredRawRunnable)
    scope, token = open_scope()
    try:
        result = runnable.invoke([])
        assert result is parsed
    finally:
        close_scope(token)
    assert scope.observations[0].usage == {
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
        "cached_tokens": None,
    }


def test_native_seam_reraises_parsing_errors():
    model = FakeNativeModel(error=OutputParserException("bad json"))
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="native"
    )
    scope, token = open_scope()
    try:
        with pytest.raises(OutputParserException):
            runnable.invoke([])
    finally:
        close_scope(token)
    assert scope.observations[0].failed is True


def test_native_seam_without_include_raw_keeps_legacy_shape():
    parsed = _Plan()
    model = LegacyNativeModel(parsed=parsed)
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="native"
    )
    assert not isinstance(runnable, NativeStructuredRawRunnable)
    assert runnable.invoke([]) is parsed


# ---------------------------------------------------------------------------
# Integration: tracer wrap_node emits correlated MODEL_INVOCATION events
# ---------------------------------------------------------------------------


def _model_events(events: list[Any]) -> list[Any]:
    return [e for e in events if e.event_type is ExecutionEventType.MODEL_INVOCATION]


def test_wrap_node_emits_one_usage_event_per_attempt_with_node_correlation():
    sink = _Sink()
    tracer = ExecutionTracer(sink)
    model = FakePromptJsonModel(
        [
            _message({"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}),
            _message(
                {
                    "input_tokens": 8,
                    "output_tokens": 4,
                    "total_tokens": 12,
                    "cached_tokens": 2,
                }
            ),
        ]
    )
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="prompt_json"
    )

    def handler(state: Any) -> dict[str, Any]:
        runnable.invoke([])
        runnable.invoke([])
        return {}

    with tracer.run_scope("thread-1", "run", turn_id="turn-1"):
        traced = tracer.wrap_node("planner", handler)
        asyncio.run(traced({}))

    usage_events = _model_events(sink.events)
    assert len(usage_events) == 2
    first, second = usage_events
    assert first.node == "planner" and second.node == "planner"
    assert first.model_invocation.attempt == 1
    assert second.model_invocation.attempt == 2
    assert first.model_invocation.input_tokens == 7
    assert second.model_invocation.cached_tokens == 2
    assert first.model_invocation.usage_reported is True
    assert first.kind is TraceKind.MODEL
    assert first.outcome is TraceOutcome.SUCCEEDED
    assert first.run_id == first.root_run_id
    assert first.turn_id == "turn-1"


def test_wrap_node_failed_attempt_emits_provider_failed_usage_event():
    sink = _Sink()
    tracer = ExecutionTracer(sink)
    model = FakePromptJsonModel([RuntimeError("boom")])
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="prompt_json"
    )

    def handler(state: Any) -> dict[str, Any]:
        runnable.invoke([])
        return {}

    with tracer.run_scope("thread-1", "run", turn_id="turn-1"):
        traced = tracer.wrap_node("planner", handler)
        with pytest.raises(RuntimeError):
            asyncio.run(traced({}))

    usage_events = _model_events(sink.events)
    assert len(usage_events) == 1
    event = usage_events[0]
    assert event.outcome is TraceOutcome.PROVIDER_FAILED
    assert event.model_invocation.usage_reported is False
    assert event.model_invocation.input_tokens is None
    node_failed = [
        e for e in sink.events if e.event_type is ExecutionEventType.NODE_FAILED
    ]
    assert len(node_failed) == 1


def test_missing_provider_usage_is_null_never_zero():
    sink = _Sink()
    tracer = ExecutionTracer(sink)
    model = FakePromptJsonModel([_message(None)])
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="prompt_json"
    )

    def handler(state: Any) -> dict[str, Any]:
        runnable.invoke([])
        return {}

    with tracer.run_scope("t", "run", turn_id="turn"):
        asyncio.run(tracer.wrap_node("decide_action", handler)({}))

    event = _model_events(sink.events)[0]
    info = event.model_invocation
    assert info.usage_reported is False
    for field in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens"):
        assert getattr(info, field) is None


def test_invalid_and_overflow_usage_fields_are_dropped_safely():
    # langchain-core's AIMessage already rejects non-int/negative metadata at
    # construction, so the seam only ever sees valid ints. Overflow beyond the
    # schema bound must still degrade to "unknown" instead of crashing.
    sink = _Sink()
    tracer = ExecutionTracer(sink)
    model = FakePromptJsonModel(
        [_message({"input_tokens": 10**11, "output_tokens": 2})]
    )
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="prompt_json"
    )

    def handler(state: Any) -> dict[str, Any]:
        runnable.invoke([])
        return {}

    with tracer.run_scope("t", "run", turn_id="turn"):
        asyncio.run(tracer.wrap_node("review", handler)({}))

    info = _model_events(sink.events)[0].model_invocation
    assert info.input_tokens is None  # overflow rejected
    assert info.output_tokens == 2  # valid sibling preserved
    assert info.usage_reported is True


def test_branch_context_correlates_usage_to_delegation_and_branch():
    sink = _Sink()
    tracer = ExecutionTracer(sink)
    model = FakePromptJsonModel([_message({"total_tokens": 5})])
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="prompt_json"
    )

    def handler(state: Any) -> dict[str, Any]:
        runnable.invoke([])
        return {}

    with (
        tracer.run_scope("t", "run", turn_id="turn"),
        tracer.branch_scope("deleg-1", "beta"),
    ):
        asyncio.run(tracer.wrap_node("decide_action", handler)({}))

    event = _model_events(sink.events)[0]
    assert event.delegation_id == "deleg-1"
    assert event.branch_id == "beta"
    assert event.model_invocation.total_tokens == 5


def test_resume_run_gets_new_run_identity_no_cross_attribution():
    sink = _Sink()
    tracer = ExecutionTracer(sink)
    model = FakePromptJsonModel(
        [_message({"total_tokens": 3}), _message({"total_tokens": 4})]
    )
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="prompt_json"
    )

    def handler(state: Any) -> dict[str, Any]:
        runnable.invoke([])
        return {}

    with tracer.run_scope("t", "run", turn_id="turn"):
        asyncio.run(tracer.wrap_node("planner", handler)({}))
    with tracer.run_scope("t", "resume", turn_id="turn"):
        asyncio.run(tracer.wrap_node("planner", handler)({}))

    usage_events = _model_events(sink.events)
    assert len({e.run_id for e in usage_events}) == 2
    assert usage_events[0].operation == "run"
    assert usage_events[1].operation == "resume"
    assert usage_events[0].run_id != usage_events[1].run_id


def test_model_invocation_info_schema_is_closed_and_bounded():
    with pytest.raises(ValidationError):
        ModelInvocationInfo(attempt=1, input_tokens=-1)
    with pytest.raises(ValidationError):
        ModelInvocationInfo(attempt=0)
    with pytest.raises(ValidationError):
        ModelInvocationInfo(attempt=1, model="bad name\nwith newline")
    with pytest.raises(ValidationError):
        ModelInvocationInfo(attempt=1, extra_field="x")
    info = ModelInvocationInfo(attempt=1)
    assert info.usage_reported is False
    assert info.input_tokens is None


def test_usage_events_never_carry_prompt_or_secret_material():
    sink = _Sink()
    tracer = ExecutionTracer(sink)
    secret = "sk-super-secret-canary"
    message = AIMessage(
        content=json.dumps({"goal": "g"}),
        usage_metadata={
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 5,
        },
        additional_kwargs={"api_key": secret, "system_prompt": "hidden"},
    )
    model = FakePromptJsonModel([message])
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="prompt_json"
    )

    def handler(state: Any) -> dict[str, Any]:
        runnable.invoke([])
        return {}

    with tracer.run_scope("t", "run", turn_id="turn"):
        asyncio.run(tracer.wrap_node("planner", handler)({}))

    event = _model_events(sink.events)[0]
    dumped = event.model_dump_json()
    assert secret not in dumped
    assert "system_prompt" not in dumped
    assert "hidden" not in dumped
    assert event.model_invocation.total_tokens == 5


def test_offline_scenario_models_without_usage_report_null_everywhere():
    """Offline doubles fabricate nothing: zero usage events, never fake data.

    The scripted ``_ScenarioModel`` rejects ``include_raw`` and falls back to
    the legacy native shape, so no observation is captured; the offline
    runtime therefore reports no usage rather than inventing numbers.
    """

    from mini_deerflow.demo.offline_scenario import _ScenarioModel
    from mini_deerflow.schemas import Plan

    sink = _Sink()
    tracer = ExecutionTracer(sink)
    model = _ScenarioModel("offline goal")  # native-mode scripted double
    runnable = create_structured_output_runnable(
        model, Plan, method="json_mode", mode="native"
    )

    def handler(state: Any) -> dict[str, Any]:
        result = runnable.invoke([])
        assert isinstance(result, Plan)
        return {}

    with tracer.run_scope("t", "run", turn_id="turn"):
        asyncio.run(tracer.wrap_node("planner", handler)({}))

    usage = _model_events(sink.events)
    assert usage == []  # honest absence, not fabricated zeros


# ---------------------------------------------------------------------------
# Storage round-trip through the public view projection
# ---------------------------------------------------------------------------


def test_usage_projects_through_safe_trace_view_and_survives_json_round_trip():
    from mini_deerflow.demo.view_models import project_trace_event

    sink = _Sink()
    tracer = ExecutionTracer(sink)
    model = FakePromptJsonModel(
        [_message({"input_tokens": 12, "output_tokens": 6, "total_tokens": 18})]
    )
    runnable = create_structured_output_runnable(
        model, _Plan, method="json_mode", mode="prompt_json"
    )

    def handler(state: Any) -> dict[str, Any]:
        runnable.invoke([])
        return {}

    with tracer.run_scope("t", "run", turn_id="turn"):
        asyncio.run(tracer.wrap_node("planner", handler)({}))

    event = _model_events(sink.events)[0]
    view = project_trace_event(event)
    assert view.kind == "model"
    assert view.node == "planner"
    assert view.model_schema == "_Plan"
    assert view.model_name == "glm-test-1"
    assert view.model_attempt == 1
    assert view.model_input_tokens == 12
    assert view.model_output_tokens == 6
    assert view.model_total_tokens == 18
    assert view.model_cached_tokens is None
    assert view.usage_reported is True

    reparsed = type(event).model_validate_json(event.model_dump_json())
    assert reparsed.model_invocation == event.model_invocation


def test_projected_view_defaults_usage_fields_to_none_for_legacy_events():
    from mini_deerflow.demo.view_models import project_trace_event

    sink = _Sink()
    tracer = ExecutionTracer(sink)
    with tracer.run_scope("t", "run", turn_id="turn"):
        tracer.emit_event(
            ExecutionEventType.NODE_COMPLETED, node="planner", duration_ms=5
        )
    view = project_trace_event(sink.events[-1])
    assert view.model_schema is None
    assert view.model_name is None
    assert view.model_attempt is None
    assert view.model_input_tokens is None
    assert view.usage_reported is None
