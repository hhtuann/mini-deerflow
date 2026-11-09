"""Track A+B regressions for the Live Synthesis fallback remediation (#43).

Credential-free: model doubles implement the prompt_json protocol surface.
Red-first: every test here must fail on unremediated main and pass after
Track A (honest fallback) + Track B (safe diagnostics) land.
"""

import asyncio
from collections import deque

import pytest
from langchain_core.messages import UsageMetadata

from mini_deerflow.actions import CompleteStepAction
from mini_deerflow.agent_workflow import build_agent_workflow
from mini_deerflow.answer_synthesis import (
    AnswerSynthesisContext,
    AnswerSynthesisFormatError,
    LLMAnswerSynthesizer,
)
from mini_deerflow.demo.view_models import project_trace_event
from mini_deerflow.evidence import (
    EvidenceProvenance,
    EvidenceRecord,
    StepFinding,
    build_user_facing_answer,
    render_user_answer,
)
from mini_deerflow.model_usage import close_scope, open_scope
from mini_deerflow.schemas import Plan, PlanStep
from mini_deerflow.state import create_initial_state
from mini_deerflow.tools import ToolInput, ToolRegistry, ToolResult
from mini_deerflow.tracing import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionTracer,
    InMemoryTraceSink,
)

WIKI = [f"https://vi.wikipedia.org/wiki/Synth{i}" for i in range(1, 17)]
GOAL = "So sánh năng lượng mặt trời và năng lượng gió"


def make_evidence() -> list[EvidenceRecord]:
    return [
        EvidenceRecord(
            url=url,
            source_tool="wiki_lookup",
            title=f"Article {i}",
            excerpt=f"Excerpt {i} về năng lượng.",
            provenance=EvidenceProvenance(
                tool_name="wiki_lookup",
                step_number=1,
                step_tool_call_number=i,
                total_tool_call_number=i,
                observation_index=i,
            ),
        )
        for i, url in enumerate(WIKI, 1)
    ]


# ---------------------------------------------------------------- Track A1/A2


def _answer_for_summary(
    summary: str,
    *,
    citations: list[str] | None = None,
):
    finding = StepFinding(
        step_number=1,
        summary=summary,
        citations=WIKI[:2] if citations is None else citations,
    )
    evidence = make_evidence()
    return build_user_facing_answer(
        findings=[finding],
        evidence=evidence,
        language="vi",
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "Bước 1:",
        "Bước 1 -",
        "Bước 1 hoàn tất:",
    ],
)
def test_vietnamese_step_prefix_keeps_cited_substantive_claim(prefix: str) -> None:
    answer = _answer_for_summary(
        f"{prefix} Năng lượng tái tạo tăng theo các nguồn công khai."
    )

    assert [claim.text for claim in answer.summary] == [
        "Năng lượng tái tạo tăng theo các nguồn công khai."
    ]
    assert answer.summary[0].citations == WIKI[:2]


def test_vietnamese_step_prefix_does_not_match_number_followed_by_letters() -> None:
    answer = _answer_for_summary(
        "Bước 1abc: Nội dung này không phải tiền tố thực thi hợp lệ."
    )

    assert [claim.text for claim in answer.summary] == [
        "Bước 1abc: Nội dung này không phải tiền tố thực thi hợp lệ."
    ]


def test_step_prefix_keeps_rejecting_unknown_citations() -> None:
    answer = _answer_for_summary(
        "Bước 1: Nội dung không có nguồn đã xác thực.",
        citations=["https://unverified.example.invalid/source"],
    )

    assert not answer.summary
    assert not answer.sources


def test_step_prefix_preserves_long_multicitation_vietnamese_finding() -> None:
    body = " ".join(
        [
            "Điện gió và điện mặt trời bổ trợ lẫn nhau theo dữ liệu công khai."
            for _ in range(12)
        ]
    )
    answer = _answer_for_summary(
        f"Bước 1: {body}",
        citations=WIKI[:5],
    )

    assert answer.summary
    assert len(answer.summary[0].text) <= 1_000
    assert answer.summary[0].citations == WIKI[:5]
    assert not answer.summary[0].text.startswith("Bước 1:")


def test_step_prefix_never_promotes_unverified_url_marker() -> None:
    answer = _answer_for_summary(
        "Bước 1: Chi tiết [unverified URL omitted] không được công khai. "
        "Năng lượng tái tạo tăng theo nguồn đã xác thực."
    )
    rendered = render_user_answer(evidence=make_evidence(), answer=answer)

    assert answer.summary
    assert "Năng lượng tái tạo tăng" in rendered
    assert "unverified URL omitted" not in rendered
    assert "Chi tiết" not in rendered


def test_vietnamese_leadin_multicitation_finding_keeps_claims() -> None:
    finding = StepFinding(
        step_number=1,
        summary=(
            "Bước 1 hoàn tất: đã trích xuất đầy đủ dữ liệu. Năng lượng mặt "
            "trời chuyển đổi bức xạ thành điện qua hiệu ứng quang điện, còn "
            "tuabin gió biến động năng thành điện cơ học."
        ),
        citations=WIKI[:3],
    )
    answer = build_user_facing_answer(
        findings=[finding],
        evidence=make_evidence(),
        language="vi",
    )

    assert answer.summary, "substantive multi-citation finding must yield claims"
    rendered = render_user_answer(evidence=make_evidence(), answer=answer)
    assert "Chưa có đủ bằng chứng" not in rendered
    assert "hiệu ứng quang điện" in rendered


def test_long_substantive_finding_with_execution_leadin_is_bounded_not_dropped() -> (
    None
):
    body = (
        "Pin quang điện chuyển đổi photon thành điện năng thông qua hiệu ứng "
        "quang điện trong bán dẫn. Tuabin gió chuyển động năng của không khí "
        "thành điện năng qua hộp số và máy phát."
    ) * 8
    finding = StepFinding(
        step_number=2,
        summary=f"Bước 2 hoàn tất: đối chiếu hoàn tất các nguồn. {body}",
        citations=WIKI[:5],
    )
    answer = build_user_facing_answer(
        findings=[finding], evidence=make_evidence(), language="vi"
    )

    assert answer.summary
    assert all(len(claim.text) <= 1_000 for claim in answer.summary)
    assert "Bước" not in answer.summary[0].text
    assert "hoàn tất" not in answer.summary[0].text


def test_unverified_url_marker_drops_sentence_not_whole_finding() -> None:
    finding = StepFinding(
        step_number=1,
        summary=(
            "Công suất gió toàn cầu đạt mức cao theo bài Wind power. Chi tiết "
            "tại https://vi.wikipedia.org/wiki/Wind_power cho năm 2023. Sản "
            "lượng điện mặt trời tăng nhanh nhất trong các nguồn tái tạo."
        ),
        citations=WIKI[:2],
    )
    answer = build_user_facing_answer(
        findings=[finding], evidence=make_evidence(), language="vi"
    )
    rendered = render_user_answer(evidence=make_evidence(), answer=answer)

    assert answer.summary, "marker sentence must not discard the whole finding"
    assert "[unverified url omitted]" not in rendered.casefold()
    assert "Wind_power" not in rendered


def test_citation_membership_still_rejects_unknown_urls() -> None:
    finding = StepFinding(
        step_number=1,
        summary="Năng lượng mặt trời và gió bổ trợ lẫn nhau theo các nguồn.",
        citations=["https://evil.example.com/invented"],
    )
    answer = build_user_facing_answer(
        findings=[finding], evidence=make_evidence(), language="vi"
    )

    assert not answer.summary
    assert not answer.sources


def test_genuinely_empty_evidence_keeps_insufficient_evidence_label() -> None:
    answer = build_user_facing_answer(findings=[], evidence=[], language="vi")
    rendered = render_user_answer(evidence=[], answer=answer)

    assert "Chưa có đủ bằng chứng" in rendered


def test_unextractable_findings_render_extraction_failure_not_insufficiency() -> None:
    finding = StepFinding(
        step_number=1,
        summary="Bước 1 hoàn tất: đã trích xuất đầy đủ và đối chiếu hoàn tất.",
        citations=WIKI[:2],
    )
    answer = build_user_facing_answer(
        findings=[finding], evidence=make_evidence(), language="vi"
    )
    rendered = render_user_answer(evidence=make_evidence(), answer=answer)

    assert not answer.summary
    assert "Chưa có đủ bằng chứng" not in rendered
    assert "Không trích xuất được kết luận" in rendered


# ------------------------------------------------- Track B doubles + synthesis


class FakeResponse:
    def __init__(self, content: object, usage: UsageMetadata) -> None:
        self.content = content
        self.usage_metadata = usage


class ScriptedModel:
    model_name = "glm-test-double"

    def __init__(self, payloads: list[object]) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    def _next(self) -> FakeResponse:
        payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        return FakeResponse(
            payload,
            UsageMetadata(
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
            ),
        )

    def invoke(self, messages: object) -> FakeResponse:
        return self._next()

    async def ainvoke(self, messages: object) -> FakeResponse:
        return self._next()


def synthesis_context() -> AnswerSynthesisContext:
    return AnswerSynthesisContext(
        goal=GOAL,
        language="vi",
        completion_status="complete",
        findings=[
            StepFinding(
                step_number=1,
                summary="Đối chiếu hoàn tất nguồn về điện mặt trời và gió.",
                citations=WIKI[:2],
            )
        ],
        evidence=make_evidence(),
    )


def draft_json(**overrides: object) -> str:
    import json

    payload: dict[str, object] = {
        "language": "vi",
        "summary": [
            {
                "text": "Điện mặt trời và gió khác nhau về nguyên lý.",
                "evidence_indices": [1, 2],
            }
        ],
        "evidence": [],
        "discrepancies": [],
        "limitations": [],
        "comparison_table": None,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


async def classify(payloads: list[object]) -> AnswerSynthesisFormatError:
    synthesizer = LLMAnswerSynthesizer(
        ScriptedModel(payloads), structured_output_mode="prompt_json"
    )
    _scope, token = open_scope()
    try:
        try:
            await synthesizer.synthesize_answer(synthesis_context())
        except AnswerSynthesisFormatError as error:
            return error
        raise AssertionError("expected AnswerSynthesisFormatError")
    finally:
        close_scope(token)


def run_classification(payloads: list[object]) -> AnswerSynthesisFormatError:
    return asyncio.run(classify(payloads))


def test_valid_structured_synthesis_succeeds_without_format_error() -> None:
    synthesizer = LLMAnswerSynthesizer(
        ScriptedModel([draft_json()]), structured_output_mode="prompt_json"
    )
    answer = asyncio.run(synthesizer.synthesize_answer(synthesis_context()))
    assert answer.summary and answer.summary[0].citations == WIKI[:2]


def test_invalid_json_classified_with_per_attempt_identity() -> None:
    error = run_classification(["tôi không trả lời JSON được", "vẫn không phải JSON"])
    assert error.category == "json_invalid"
    assert [item.attempt for item in error.attempt_failures] == [1, 2]
    assert all(item.category == "json_invalid" for item in error.attempt_failures)


def test_schema_bound_violation_classified() -> None:
    six = [{"text": f"claim {i}", "evidence_indices": [1]} for i in range(6)]
    payload = draft_json(summary=six)
    error = run_classification([payload, payload])
    assert error.category == "schema_invalid"
    assert error.reason is None


def test_evidence_index_out_of_range_classified() -> None:
    payload = draft_json(summary=[{"text": "ok", "evidence_indices": [17]}])
    error = run_classification([payload, payload])
    assert error.category == "grounding_invalid"
    assert error.reason == "evidence_index_out_of_range"


def test_language_mismatch_classified() -> None:
    payload = draft_json(language="en")
    error = run_classification([payload, payload])
    assert error.category == "grounding_invalid"
    assert error.reason == "language_mismatch"


def test_url_in_prose_classified() -> None:
    payload = draft_json(
        summary=[
            {
                "text": "xem https://vi.wikipedia.org/wiki/Synth1",
                "evidence_indices": [1],
            }
        ]
    )
    error = run_classification([payload, payload])
    assert error.category == "grounding_invalid"
    assert error.reason == "url_in_prose"


def test_missing_discrepancy_for_known_contradiction_classified() -> None:
    context = synthesis_context().model_copy(
        update={"limitations": ["[contradiction] các nguồn lệch số liệu"]}
    )
    payload = draft_json()

    async def run() -> AnswerSynthesisFormatError:
        synthesizer = LLMAnswerSynthesizer(
            ScriptedModel([payload, payload]), structured_output_mode="prompt_json"
        )
        _scope, token = open_scope()
        try:
            try:
                await synthesizer.synthesize_answer(context)
            except AnswerSynthesisFormatError as error:
                return error
            raise AssertionError("expected AnswerSynthesisFormatError")
        finally:
            close_scope(token)

    error = asyncio.run(run())
    assert error.category == "grounding_invalid"
    assert error.reason == "contradiction_required"


def test_empty_content_not_retried_and_classified_as_empty_content() -> None:
    from mini_deerflow.structured_output import EmptyModelContentError

    model = ScriptedModel([None])
    synthesizer = LLMAnswerSynthesizer(model, structured_output_mode="prompt_json")
    scope, token = open_scope()
    try:
        try:
            asyncio.run(synthesizer.synthesize_answer(synthesis_context()))
            raise AssertionError("expected EmptyModelContentError")
        except EmptyModelContentError:
            pass
        assert model.calls == 1, "empty content must not add retries"
        assert [observation.failed for observation in scope.observations] == [False]
    finally:
        close_scope(token)


def test_provider_error_not_retried_and_kept_distinct() -> None:
    class ExplodingModel(ScriptedModel):
        async def ainvoke(self, messages: object) -> FakeResponse:
            self.calls += 1
            raise RuntimeError("provider exploded")

    model = ExplodingModel([None])
    synthesizer = LLMAnswerSynthesizer(model, structured_output_mode="prompt_json")
    scope, token = open_scope()
    try:
        try:
            asyncio.run(synthesizer.synthesize_answer(synthesis_context()))
            raise AssertionError("expected RuntimeError")
        except RuntimeError as error:
            assert not isinstance(error, AnswerSynthesisFormatError)
        assert model.calls == 1
        assert [observation.failed for observation in scope.observations] == [True]
    finally:
        close_scope(token)


def test_provider_usage_accounting_survives_validation_failure() -> None:
    model = ScriptedModel(["not json", "still not json"])
    synthesizer = LLMAnswerSynthesizer(model, structured_output_mode="prompt_json")
    scope, token = open_scope()
    try:
        try:
            asyncio.run(synthesizer.synthesize_answer(synthesis_context()))
        except AnswerSynthesisFormatError:
            pass
        assert len(scope.observations) == 2
        assert all(
            not observation.failed and observation.usage is not None
            for observation in scope.observations
        ), "provider returned on both attempts; parsing failed afterwards"
    finally:
        close_scope(token)


# --------------------------------------------------------- workflow seam (A3/A4)


class EchoInput(ToolInput):
    text: str


class EchoTool:
    name = "echo"
    description = "Return the supplied text."
    input_model = EchoInput
    timeout_seconds = 5.0
    idempotent = True

    async def run(self, tool_input: EchoInput) -> ToolResult:
        return ToolResult.success(payload={"echo": tool_input.text}, metadata={})


class QueuedSelector:
    def __init__(self, actions: list[object]) -> None:
        self._actions = deque(actions)

    async def select_action(self, context: object) -> object:
        del context
        return self._actions.popleft()


def three_step_plan(goal: str) -> Plan:
    return Plan(
        goal=goal,
        steps=[
            PlanStep(
                step_number=number,
                title=f"Research step {number}",
                objective=f"Collect evidence {number}.",
                success_criteria=f"Evidence {number} collected.",
            )
            for number in (1, 2, 3)
        ],
    )


class RaisingSynthesizer:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def synthesize_answer(self, context: object) -> object:
        del context
        raise self._error


def run_workflow_with_synthesizer(
    error: BaseException,
) -> tuple[dict, list[ExecutionEvent]]:
    tracer = ExecutionTracer(sink=InMemoryTraceSink())
    selector = QueuedSelector(
        [
            CompleteStepAction(
                type="complete_step",
                summary="Đối chiếu năng lượng mặt trời và gió hoàn tất.",
                sources=[],
            )
            for _ in range(3)
        ]
    )
    graph = build_agent_workflow(
        three_step_plan,
        selector,
        ToolRegistry([EchoTool()]),
        answer_synthesizer=RaisingSynthesizer(error),
        tracer=tracer,
    )
    with tracer.run_scope("chat-remediation", "run", turn_id="turn-remediation"):
        state = asyncio.run(
            graph.ainvoke(
                create_initial_state(GOAL),
                config={"recursion_limit": 100},
            )
        )
    return state, _all_events(tracer)


def _all_events(tracer: ExecutionTracer) -> list[ExecutionEvent]:
    sink = tracer._sink
    return list(sink.events)


def _synthesis_fallback_events(events: list[ExecutionEvent]) -> list[ExecutionEvent]:
    return [
        event
        for event in events
        if event.event_type is ExecutionEventType.SYNTHESIS_FALLBACK
    ]


def _format_error(
    category: str, reason: str | None = None
) -> AnswerSynthesisFormatError:
    from mini_deerflow.answer_synthesis import SynthesisAttemptFailure as DomainAttempt

    attempts = (
        DomainAttempt(attempt=1, category=category, reason=reason),
        DomainAttempt(attempt=2, category=category, reason=reason),
    )
    return AnswerSynthesisFormatError(
        "Model returned an invalid answer synthesis after 2 attempts",
        category=category,
        reason=reason,
        attempt_failures=attempts,
    )


def test_workflow_invalid_synthesis_message_and_event() -> None:
    state, events = run_workflow_with_synthesizer(
        _format_error("schema_invalid"),
    )
    final_answer = state["final_answer"]
    assert "dịch vụ mô hình không khả dụng" not in final_answer
    assert "không vượt qua kiểm định" in final_answer
    assert any(
        "Structured answer synthesis was invalid" in error for error in state["errors"]
    )
    fallback_events = _synthesis_fallback_events(events)
    assert len(fallback_events) == 1
    info = fallback_events[0].synthesis_failure
    assert info is not None
    assert info.category == "schema_invalid"
    assert [item.attempt for item in info.attempts] == [1, 2]
    # A4: the node itself still completes; status stays backward compatible.
    assert any(
        event.event_type is ExecutionEventType.NODE_COMPLETED
        and event.node == "synthesize"
        for event in events
    )


def test_workflow_provider_outage_message_and_event() -> None:
    state, events = run_workflow_with_synthesizer(RuntimeError("provider outage"))
    final_answer = state["final_answer"]
    assert "dịch vụ mô hình không khả dụng" in final_answer
    assert "không vượt qua kiểm định" not in final_answer
    fallback_events = _synthesis_fallback_events(events)
    assert len(fallback_events) == 1
    info = fallback_events[0].synthesis_failure
    assert info is not None
    assert info.category == "provider_error"
    assert info.attempts == []


def test_workflow_empty_content_message_and_event() -> None:
    from mini_deerflow.structured_output import EmptyModelContentError

    state, events = run_workflow_with_synthesizer(
        EmptyModelContentError("model response content must be text"),
    )
    final_answer = state["final_answer"]
    assert "không trả về nội dung" in final_answer
    assert "dịch vụ mô hình không khả dụng" not in final_answer
    fallback_events = _synthesis_fallback_events(events)
    assert len(fallback_events) == 1
    info = fallback_events[0].synthesis_failure
    assert info is not None
    assert info.category == "empty_content"


def test_workflow_grounding_reason_code_rendered_in_limitation() -> None:
    state, events = run_workflow_with_synthesizer(
        _format_error("grounding_invalid", "language_mismatch"),
    )
    final_answer = state["final_answer"]
    assert "grounding_invalid/language_mismatch" in final_answer.replace("\\", "")
    assert "dịch vụ mô hình không khả dụng" not in final_answer
    fallback_events = _synthesis_fallback_events(events)
    assert len(fallback_events) == 1
    info = fallback_events[0].synthesis_failure
    assert info is not None
    assert info.reason == "language_mismatch"
    assert any(
        event.event_type is ExecutionEventType.NODE_COMPLETED
        and event.node == "synthesize"
        for event in events
    )


# ------------------------------------------------------ Track B schema safety


def _fallback_event() -> ExecutionEvent:
    return ExecutionEvent.model_validate(
        {
            "event_type": "synthesis_fallback",
            "kind": "synthesis",
            "phase": "outcome",
            "outcome": "fallback",
            "node": "synthesize",
            "run_id": "run-1",
            "thread_id": "chat-1",
            "sequence": 7,
            "synthesis_failure": {
                "category": "grounding_invalid",
                "reason": "language_mismatch",
                "attempts": [
                    {
                        "attempt": 1,
                        "category": "grounding_invalid",
                        "reason": "language_mismatch",
                    },
                    {
                        "attempt": 2,
                        "category": "grounding_invalid",
                        "reason": "language_mismatch",
                    },
                ],
            },
        }
    )


def test_fallback_event_roundtrip_and_closed_schema() -> None:
    import json

    event = _fallback_event()
    reloaded = ExecutionEvent.model_validate_json(event.model_dump_json())
    assert reloaded.synthesis_failure == event.synthesis_failure
    raw = json.loads(event.model_dump_json())
    assert set(raw["synthesis_failure"]) == {"category", "reason", "attempts"}
    with __import__("pytest").raises(Exception):
        ExecutionEvent.model_validate(
            {
                **_fallback_event().model_dump(),
                "synthesis_failure": {
                    "category": "not_a_real_category",
                    "reason": None,
                    "attempts": [],
                },
            }
        )
    with __import__("pytest").raises(Exception):
        ExecutionEvent.model_validate(
            {
                **_fallback_event().model_dump(),
                "synthesis_failure": {
                    "category": "schema_invalid",
                    "reason": None,
                    "attempts": [
                        {"attempt": 1, "category": "schema_invalid", "reason": None},
                        {"attempt": 2, "category": "schema_invalid", "reason": None},
                        {"attempt": 3, "category": "schema_invalid", "reason": None},
                        {"attempt": 4, "category": "schema_invalid", "reason": None},
                        {"attempt": 5, "category": "schema_invalid", "reason": None},
                        {"attempt": 6, "category": "schema_invalid", "reason": None},
                    ],
                },
            }
        )


def test_view_projection_exposes_only_bounded_synthesis_scalars() -> None:
    view = project_trace_event(_fallback_event())
    assert view.synthesis_fallback is True
    assert view.synthesis_category == "grounding_invalid"
    assert view.synthesis_reason == "language_mismatch"
    assert view.synthesis_attempts == 2
    import dataclasses

    payload = {
        field.name: getattr(view, field.name) for field in dataclasses.fields(view)
    }
    synthesis_keys = {key for key in payload if key.startswith("synthesis_")}
    assert synthesis_keys == {
        "synthesis_fallback",
        "synthesis_category",
        "synthesis_reason",
        "synthesis_attempts",
    }


def test_historical_events_project_without_synthesis_fields() -> None:
    legacy = ExecutionEvent.model_validate(
        {
            "event_type": "model_invocation",
            "kind": "model",
            "phase": "outcome",
            "outcome": "succeeded",
            "node": "synthesize",
            "run_id": "run-1",
            "thread_id": "chat-1",
            "sequence": 3,
        }
    )
    view = project_trace_event(legacy)
    assert view.synthesis_fallback is None
    assert view.synthesis_category is None
    assert view.synthesis_reason is None
    assert view.synthesis_attempts is None


def test_fallback_event_never_carries_secret_like_values() -> None:
    event = _fallback_event()
    rendered = event.model_dump_json()
    for canary in ("api_key", "sk-", "Bearer", "password"):
        assert canary not in rendered
