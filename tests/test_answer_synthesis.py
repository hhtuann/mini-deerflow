import asyncio
from collections import deque

import pytest

from mini_deerflow.answer_synthesis import (
    SYNTHESIS_SYSTEM_PROMPT,
    AnswerSynthesisContext,
    AnswerSynthesisDraft,
    DraftClaim,
    DraftComparisonRow,
    DraftComparisonTable,
    LLMAnswerSynthesizer,
    infer_answer_language,
    validate_answer_draft,
)
from mini_deerflow.evidence import (
    EvidenceProvenance,
    EvidenceRecord,
    render_user_answer,
)


def evidence_record(number: int) -> EvidenceRecord:
    return EvidenceRecord(
        url=f"https://example.com/source-{number}",
        source_tool="web_search",
        title=f"Source {number}",
        excerpt=f"Validated evidence for comparison criterion {number}.",
        provenance=EvidenceProvenance(
            tool_name="web_search",
            step_number=number,
            step_tool_call_number=1,
            total_tool_call_number=number,
            observation_index=number,
        ),
    )


def synthesis_context(
    *,
    completion_status: str = "complete",
    limitations: list[str] | None = None,
) -> AnswerSynthesisContext:
    return AnswerSynthesisContext(
        goal="Messi và Ronaldo ai mạnh hơn?",
        language="vi",
        completion_status=completion_status,
        evidence=[evidence_record(1), evidence_record(2)],
        limitations=limitations or [],
    )


def comparison_draft() -> AnswerSynthesisDraft:
    return AnswerSynthesisDraft(
        language="vi",
        summary=[
            DraftClaim(
                text="Không có một người thắng tuyệt đối ở mọi tiêu chí.",
                evidence_indices=[1, 2],
            )
        ],
        evidence=[],
        discrepancies=[],
        limitations=[],
        comparison_table=DraftComparisonTable(
            left_subject="Messi",
            right_subject="Ronaldo",
            rows=[
                DraftComparisonRow(
                    criterion="Kiến tạo",
                    left="Nổi bật trong dữ liệu nguồn một.",
                    right="Thấp hơn trong cùng phạm vi.",
                    assessment="Messi nhỉnh hơn theo nguồn một.",
                    evidence_indices=[1],
                ),
                DraftComparisonRow(
                    criterion="Ghi bàn",
                    left="Có thành tích cao.",
                    right="Nổi bật trong dữ liệu nguồn hai.",
                    assessment="Ronaldo nhỉnh hơn theo nguồn hai.",
                    evidence_indices=[2],
                ),
            ],
        ),
    )


def test_language_inference_prefers_vietnamese_for_vietnamese_goal() -> None:
    assert infer_answer_language("Messi và Ronaldo ai mạnh hơn?") == "vi"
    assert infer_answer_language("Compare Messi and Ronaldo") == "en"


def test_language_inference_handles_accented_names_and_unaccented_vietnamese() -> None:
    assert infer_answer_language("Compare Pelé and Messi") == "en"
    assert infer_answer_language("Messi va Ronaldo ai manh hon?") == "vi"
    assert infer_answer_language("Tim thong tin ve Messi") == "vi"


def test_draft_maps_indices_to_validated_urls_and_renders_table() -> None:
    context = synthesis_context(completion_status="partial")
    answer = validate_answer_draft(comparison_draft(), context)

    assert answer.sources == [
        "https://example.com/source-1",
        "https://example.com/source-2",
    ]
    assert answer.completion_status == "partial"
    assert answer.comparison_table is not None

    markdown = render_user_answer(evidence=context.evidence, answer=answer)

    assert "Câu trả lời một phần" in markdown
    assert "## Bảng so sánh" in markdown
    assert "| Kiến tạo |" in markdown
    assert "[1]" in markdown
    assert "[2]" in markdown


def test_draft_rejects_unknown_evidence_index_and_model_authored_url() -> None:
    context = synthesis_context()
    unknown = comparison_draft().model_copy(
        update={
            "summary": [DraftClaim(text="Kết luận có nguồn.", evidence_indices=[3])]
        }
    )
    with pytest.raises(ValueError, match="outside the context"):
        validate_answer_draft(unknown, context)

    invented_url = comparison_draft().model_copy(
        update={
            "summary": [
                DraftClaim(
                    text="Xem https://invented.example để biết thêm.",
                    evidence_indices=[1],
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="must not contain URLs"):
        validate_answer_draft(invented_url, context)


def test_known_contradiction_must_be_explicit() -> None:
    context = synthesis_context(
        limitations=["[contradiction] Hai nguồn dùng phạm vi thời gian khác nhau."],
    )

    with pytest.raises(ValueError, match="contradictions"):
        validate_answer_draft(comparison_draft(), context)


def test_conflicting_values_keep_separate_citations() -> None:
    context = synthesis_context(
        limitations=["[contradiction] Hai nguồn báo cáo hai giá trị khác nhau."],
    )
    draft = comparison_draft().model_copy(
        update={
            "discrepancies": [
                DraftClaim(text="Nguồn một báo cáo giá trị A.", evidence_indices=[1]),
                DraftClaim(text="Nguồn hai báo cáo giá trị B.", evidence_indices=[2]),
            ]
        }
    )

    answer = validate_answer_draft(draft, context)

    assert [claim.citations for claim in answer.discrepancies] == [
        ["https://example.com/source-1"],
        ["https://example.com/source-2"],
    ]


def test_non_comparison_draft_does_not_create_a_table() -> None:
    context = synthesis_context()
    draft = AnswerSynthesisDraft(
        language="vi",
        summary=[DraftClaim(text="Một kết luận có nguồn.", evidence_indices=[1])],
    )

    answer = validate_answer_draft(draft, context)

    assert answer.comparison_table is None


def test_synthesis_limitations_are_bounded() -> None:
    with pytest.raises(ValueError, match="at most 1000 characters"):
        AnswerSynthesisDraft(language="en", limitations=["x" * 1_001])


def test_synthesis_prompt_declares_exact_json_shape() -> None:
    assert '"language": "vi" or "en"' in SYNTHESIS_SYSTEM_PROMPT
    assert '"comparison_table"' in SYNTHESIS_SYSTEM_PROMPT
    assert "no wrapper key" in SYNTHESIS_SYSTEM_PROMPT


class SequencedRunnable:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[object] = []

    async def ainvoke(self, messages: object) -> object:
        self.calls.append(messages)
        return self.outcomes.popleft()


class FakeModel:
    def __init__(self, runnable: SequencedRunnable) -> None:
        self.runnable = runnable

    def with_structured_output(
        self,
        schema: type[AnswerSynthesisDraft],
        *,
        method: str,
    ) -> SequencedRunnable:
        assert schema is AnswerSynthesisDraft
        assert method == "json_mode"
        return self.runnable


def test_llm_synthesizer_retries_an_invalid_evidence_reference() -> None:
    invalid = comparison_draft().model_copy(
        update={"summary": [DraftClaim(text="Sai nguồn.", evidence_indices=[99])]}
    )
    runnable = SequencedRunnable([invalid, comparison_draft()])
    synthesizer = LLMAnswerSynthesizer(FakeModel(runnable))

    answer = asyncio.run(synthesizer.synthesize_answer(synthesis_context()))

    assert len(runnable.calls) == 2
    assert answer.sources == [
        "https://example.com/source-1",
        "https://example.com/source-2",
    ]
