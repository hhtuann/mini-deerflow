"""Issue12 Task2.3: Slide Agent backend contracts over the real offline runtime.

No production seam is mocked: research turns run the scripted offline backend,
planning goes through the production structured-output seam with the reusable
scripted planner model from the Phase 2 demo, and publication, retrieval, and
HTTP transport use the real PptxArtifactStore and FastAPI application.
"""

import asyncio
import dataclasses
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mini_deerflow.api import create_app
from mini_deerflow.api.dto import JobDTO
from mini_deerflow.context_budget import (
    ContextBudget,
    ProjectedSkillContext,
    ProjectionMetadata,
    render_llm_payload,
)
from mini_deerflow.conversation import ConversationNotFoundError
from mini_deerflow.demo.graph_projection import ExecutionGraphProjection
from mini_deerflow.demo.jobs import DemoJobManager
from mini_deerflow.demo.offline_scenario import OfflineDemoBackend
from mini_deerflow.demo.phase2_slide_agent_offline import (
    ScriptedSlidePlanner,
    build_scripted_plan,
)
from mini_deerflow.demo.service import (
    DemoRuntimeService,
    RunDemoCommand,
    map_demo_error,
)
from mini_deerflow.demo.view_models import ChatSessionView, DemoRunView, EvidenceView
from mini_deerflow.persistence import ThreadNotFoundError
from mini_deerflow.pptx import DeckSpec, render_deck, validate_pptx
from mini_deerflow.pptx.artifacts import PPTX_MIME, ArtifactError, PptxArtifactStore
from mini_deerflow.pptx.schemas import (
    ChartContent,
    ConclusionContent,
    MetricContent,
    TableContent,
)
from mini_deerflow.pptx.slide_agent import (
    MAX_EVIDENCE_ITEMS,
    DeckPlan,
    PresentationRequest,
    SlideAgentConflictError,
    SlideAgentError,
    SlideAgentService,
    SlideAgentValidationError,
    SlideEvidenceItem,
    build_deck_spec,
    build_slide_planning_context,
    plan_deck,
)
from mini_deerflow.skills import SkillContext, SkillsRegistry

TOPIC = "Tác động của trí tuệ nhân tạo đến giáo dục đại học Việt Nam"
AUDIENCE = "sinh viên năm cuối và giảng viên"
SKILL_ID = "slide-creation"
THREAD = "slide-agent-thread"
TURN = "slide-agent-turn"

VI_MARKER = "(số liệu minh họa)"
EN_MARKER = "(illustrative data)"
VI_UNCERTAIN = "Bằng chứng còn hạn chế: nội dung này chưa có nguồn đã xác thực."
EN_UNCERTAIN = "Limited evidence: no validated source supports this content yet."

VALID_BODY = {
    "topic": TOPIC,
    "audience": AUDIENCE,
    "language": "vi",
    "slide_count": 8,
    "theme": "academic",
    "instructions": "Nhấn mạnh bằng chứng và hạn chế.",
}

_STRINGS = {
    "vi": {
        "deck_title": "Hồ sơ tổng quan nghiên cứu",
        "title_t": "Tổng quan nghiên cứu",
        "title_sub": "Bản tóm tắt dành cho người đọc",
        "bullets_t": "Điểm chính về chủ đề",
        "bullets_1": "Điểm thứ nhất về chủ đề",
        "bullets_2": "Điểm thứ hai về bằng chứng",
        "steps_t": "Các bước đọc bộ slide",
        "steps_1": "Bước một đọc tổng quan",
        "steps_2": "Bước hai đọc kết luận",
        "metric_t": "Phạm vi bằng chứng",
        "metric_label": "Tỷ lệ áp dụng",
        "metric_insight": "Con số này chỉ nhằm minh họa cấu trúc.",
        "metric_value_text": "Cao",
        "chart_t": "Xu hướng theo quý",
        "chart_series": "Chuỗi dữ liệu minh họa",
        "chart_caption": "Xu hướng cấu trúc theo quý",
        "cats": ("Quý 1", "Quý 2", "Quý 3"),
        "table_t": "Tổng hợp thành phần",
        "table_h1": "Thành phần",
        "table_h2": "Số lượng",
        "table_cell": "Bài trình chiếu",
        "table_caption": "Thành phần của bộ slide",
        "conclusion_t": "Kết luận và nguồn",
        "conclusion_item": "Kết luận bám theo bằng chứng",
    },
    "en": {
        "deck_title": "Research overview dossier",
        "title_t": "Research overview",
        "title_sub": "A summary prepared for readers",
        "bullets_t": "Key points on the topic",
        "bullets_1": "First point on the topic",
        "bullets_2": "Second point on the evidence",
        "steps_t": "How to read this deck",
        "steps_1": "Step one read the overview",
        "steps_2": "Step two read the conclusion",
        "metric_t": "Evidence coverage",
        "metric_label": "Adoption rate",
        "metric_insight": "This figure only illustrates the structure.",
        "metric_value_text": "High",
        "chart_t": "Quarterly trend",
        "chart_series": "Illustrative data series",
        "chart_caption": "Structural trend by quarter",
        "cats": ("Q1", "Q2", "Q3"),
        "table_t": "Component summary",
        "table_h1": "Component",
        "table_h2": "Count",
        "table_cell": "Presentation deck",
        "table_caption": "Components of the deck",
        "conclusion_t": "Conclusion and sources",
        "conclusion_item": "Conclusion follows the evidence",
    },
}


def vi_request(**overrides) -> PresentationRequest:
    values = {
        "topic": TOPIC,
        "audience": AUDIENCE,
        "language": "vi",
        "slide_count": 8,
        "theme": "light",
        "instructions": "",
    }
    values.update(overrides)
    return PresentationRequest(**values)


def evidence_pair() -> tuple[SlideEvidenceItem, ...]:
    return (
        SlideEvidenceItem(
            key=0,
            canonical_url="https://evidence.example/report",
            title="Báo cáo chính",
            excerpt="Trích dẫn báo cáo đã xác thực.",
            is_cited=True,
        ),
        SlideEvidenceItem(
            key=1,
            canonical_url="https://evidence.example/search",
            title="Kết quả tìm kiếm",
            excerpt="Trích dẫn kết quả tìm kiếm.",
            is_cited=False,
        ),
    )


def _slide(layout, title, payload, *, purpose="Planning narrative.", evidence_keys=()):
    return {
        "layout": layout,
        "title": title,
        "purpose": purpose,
        "evidence_keys": list(evidence_keys),
        "payload": payload,
    }


def base_slides(language="vi", *, conclusion_keys=(0,)):
    strings = _STRINGS[language]
    return [
        _slide("title", strings["title_t"], {"subtitle": strings["title_sub"]}),
        _slide(
            "bullets",
            strings["bullets_t"],
            {"items": [strings["bullets_1"], strings["bullets_2"]]},
            evidence_keys=[0],
        ),
        _slide(
            "steps",
            strings["steps_t"],
            {"items": [strings["steps_1"], strings["steps_2"]]},
        ),
        _slide(
            "table",
            strings["table_t"],
            {
                "headers": [strings["table_h1"], strings["table_h2"]],
                "rows": [[strings["table_cell"], 1]],
                "caption": strings["table_caption"],
            },
        ),
        _slide(
            "conclusion",
            strings["conclusion_t"],
            {
                "items": [strings["conclusion_item"]],
                "source_keys": list(conclusion_keys),
            },
        ),
    ]


def _plan(slides, *, language="vi", deck_title=None) -> DeckPlan:
    return DeckPlan.model_validate(
        {
            "deck_title": deck_title or _STRINGS[language]["deck_title"],
            "language": language,
            "slides": slides,
        }
    )


def build_spec(
    slides,
    *,
    language="vi",
    request=None,
    evidence=None,
    deck_title=None,
) -> DeckSpec:
    return build_deck_spec(
        _plan(slides, language=language, deck_title=deck_title),
        request or vi_request(language=language, slide_count=len(slides)),
        evidence_pair() if evidence is None else evidence,
    )


def slide_services(root: Path):
    """Real offline runtime, artifact store, and scripted planner wiring."""

    runtime = DemoRuntimeService(OfflineDemoBackend(root))
    planner = ScriptedSlidePlanner()
    store = PptxArtifactStore(root, mode="offline")
    service = SlideAgentService(
        runtime,
        store,
        planner_model_factory=lambda: planner,
        structured_output_mode="native",
    )
    return runtime, service, planner, store


def wait_job(client: TestClient, job: dict) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = client.get(f"/api/{job['mode']}/jobs/{job['job_id']}")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] in {"completed", "failed"}:
            return result
        time.sleep(0.01)
    pytest.fail("Job did not finish within 30 seconds")


def assert_no_host_leak(text: str, root: Path) -> None:
    text = text.replace("\\\\", "\\")
    assert str(root) not in text
    assert "Traceback" not in text


class PromptJsonSlidePlanner:
    """Scripted chat model answering through the prompt_json seam."""

    def __init__(self, *, wrap=None, fence=False, extra_field=False, fail_first=0):
        self.calls = 0
        self.messages = None
        self.first_messages = None
        self._wrap = wrap
        self._fence = fence
        self._extra_field = extra_field
        self._fail_first = fail_first

    def invoke(self, messages):
        self.calls += 1
        if self.first_messages is None:
            self.first_messages = messages
        self.messages = messages
        if self.calls <= self._fail_first:
            raise ValidationError.from_exception_data(
                "DeckPlan",
                [{"type": "missing", "loc": ("slides",), "input": {}}],
            )
        payload = json.loads(messages[1][1])
        data = json.loads(build_scripted_plan(payload).model_dump_json())
        if self._extra_field:
            data["surprise"] = True
        if self._wrap:
            data = {self._wrap: data}
        text = json.dumps(data, ensure_ascii=False)
        return f"```json\n{text}\n```" if self._fence else text

    async def ainvoke(self, messages):
        return self.invoke(messages)


class RogueNativeModel:
    """Native-seam model whose structured runnable returns a raw dict."""

    def with_structured_output(self, schema, *, method):
        class _Runnable:
            def invoke(self, messages):
                return {"deck_title": "not a DeckPlan"}

            async def ainvoke(self, messages):
                return self.invoke(messages)

        return _Runnable()

    def invoke(self, messages):
        raise AssertionError("rogue model uses structured output only")

    async def ainvoke(self, messages):
        raise AssertionError("rogue model uses structured output only")


class EvidenceCapturingPlanner(ScriptedSlidePlanner):
    """Scripted planner that also records the projected evidence entries."""

    def __init__(self) -> None:
        super().__init__()
        self.evidence_entries: list[list[dict]] = []

    def _plan_from_messages(self, messages):
        payload = json.loads(messages[1][1])
        self.evidence_entries.append(list(payload.get("evidence") or []))
        return super()._plan_from_messages(messages)


class ReplaySlideSource:
    """SlideSourceService replaying one real ChatSessionView dataclass tree."""

    def __init__(self, runtime: DemoRuntimeService, session: ChatSessionView):
        self._runtime = runtime
        self._session = session

    @property
    def backend(self) -> object:
        return self._runtime.backend

    async def load_conversation(self, thread_id: str) -> ChatSessionView:
        assert thread_id == THREAD
        return self._session


async def replayed_session_with_evidence(
    runtime: DemoRuntimeService,
    evidence: tuple[EvidenceView, ...],
) -> ChatSessionView:
    """Rebuild the real session view with controlled turn-run evidence."""
    session = await runtime.load_conversation(THREAD)
    turns = []
    for item in session.turns:
        if item.turn_id != TURN:
            turns.append(item)
            continue
        assert item.run is not None and item.status == "completed"
        turns.append(
            dataclasses.replace(
                item, run=dataclasses.replace(item.run, evidence=evidence)
            )
        )
    return dataclasses.replace(session, turns=tuple(turns))


# ---------------------------------------------------------------------------
# 1. PresentationRequest boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,valid",
    [
        ({"topic": "x" * 9}, False),
        ({"topic": "x" * 10}, True),
        ({"topic": "x" * 1000}, True),
        ({"topic": "x" * 1001}, False),
        ({"audience": "ab"}, False),
        ({"audience": "abc"}, True),
        ({"audience": "a" * 120}, True),
        ({"audience": "a" * 121}, False),
        ({"language": "fr"}, False),
        ({"slide_count": 4}, False),
        ({"slide_count": 5}, True),
        ({"slide_count": 12}, True),
        ({"slide_count": 13}, False),
        ({"slide_count": "8"}, False),
        ({"slide_count": 7.5}, False),
        ({"slide_count": True}, False),
        ({"theme": "grape"}, False),
        ({"instructions": "x" * 500}, True),
        ({"instructions": "x" * 501}, False),
    ],
)
def test_presentation_request_boundaries(overrides, valid):
    if valid:
        request = vi_request(**overrides)
        for key, value in overrides.items():
            assert getattr(request, key) == value
    else:
        with pytest.raises(ValidationError):
            vi_request(**overrides)


def test_presentation_request_rejects_extra_fields():
    with pytest.raises(ValidationError):
        PresentationRequest(topic=TOPIC, audience=AUDIENCE, language="vi", evil=True)


# ---------------------------------------------------------------------------
# 2. plan_deck through the production structured-output seam
# ---------------------------------------------------------------------------


def test_plan_deck_native_scripted_model_succeeds_and_reports_projection():
    planner = ScriptedSlidePlanner()
    projections: list[ProjectionMetadata] = []
    plan = plan_deck(
        planner,
        vi_request(),
        evidence_pair(),
        active_skill=SkillsRegistry().activate(SKILL_ID),
        structured_output_mode="native",
        on_context_projection=projections.append,
    )
    assert isinstance(plan, DeckPlan)
    assert plan.language == "vi"
    assert len(plan.slides) == 8
    assert len(planner.captures) == 1
    assert len(projections) == 1
    assert isinstance(projections[0], ProjectionMetadata)
    assert projections[0].skill_id == SKILL_ID
    assert projections[0].skill_omitted is False


@pytest.mark.parametrize("wrap", [None, "DeckPlan", "deck_plan"])
@pytest.mark.parametrize("fence", [False, True])
def test_plan_deck_prompt_json_accepts_bare_fenced_and_wrapped(wrap, fence):
    model = PromptJsonSlidePlanner(wrap=wrap, fence=fence)
    plan = plan_deck(model, vi_request(), evidence_pair())
    reference = build_scripted_plan(json.loads(model.messages[1][1]))
    assert plan == reference
    assert plan.language == "vi"
    assert len(plan.slides) == 8


def test_plan_deck_retries_once_after_validation_error():
    planner = ScriptedSlidePlanner(fail_first=1)
    plan = plan_deck(
        planner, vi_request(), evidence_pair(), structured_output_mode="native"
    )
    assert isinstance(plan, DeckPlan)
    assert len(planner.captures) == 1
    model = PromptJsonSlidePlanner(fail_first=1)
    assert isinstance(
        plan_deck(model, vi_request(), evidence_pair()),
        DeckPlan,
    )
    assert model.calls == 2


def test_plan_deck_retry_carries_sanitized_corrective_feedback():
    """Root-cause regression: blind retries waste the bounded second attempt.

    A live provider occasionally returns a schema-invalid DeckPlan; the retry
    must tell the model WHICH fields failed using safe validation facts only
    (locations/messages), never raw model output or input values.
    """
    model = PromptJsonSlidePlanner(fail_first=1)
    assert isinstance(
        plan_deck(model, vi_request(), evidence_pair()),
        DeckPlan,
    )
    assert model.calls == 2
    first_messages = model.first_messages
    assert len(first_messages) == 2
    retry_messages = model.messages
    assert len(retry_messages) == 3
    hint = retry_messages[2]
    assert hint[0] == "human"
    assert "failed validation" in hint[1]
    assert "slides" in hint[1]  # loc of the injected failure
    # No raw model output or provider payloads ride along in the hint.
    assert "{" not in hint[1]
    assert "api" not in hint[1].lower()


def test_plan_deck_exhausted_retries_fail_closed():
    planner = ScriptedSlidePlanner(fail_first=2)
    with pytest.raises(SlideAgentValidationError, match="after 2 attempts"):
        plan_deck(
            planner, vi_request(), evidence_pair(), structured_output_mode="native"
        )


def test_plan_deck_rejects_unknown_plan_fields():
    model = PromptJsonSlidePlanner(extra_field=True)
    with pytest.raises(SlideAgentValidationError, match="after 2 attempts"):
        plan_deck(model, vi_request(), evidence_pair())
    assert model.calls == 2


def test_plan_deck_rejects_non_deckplan_object():
    with pytest.raises(SlideAgentValidationError, match="unexpected object type"):
        plan_deck(
            RogueNativeModel(),
            vi_request(),
            evidence_pair(),
            structured_output_mode="native",
        )


# ---------------------------------------------------------------------------
# 3. build_deck_spec deterministic conversion
# ---------------------------------------------------------------------------


def test_valid_plan_converts_renders_and_validates():
    spec = build_spec(base_slides())
    assert isinstance(spec, DeckSpec)
    assert spec.title == _STRINGS["vi"]["deck_title"]
    assert spec.theme == "light"
    assert len(spec.slides) == 5
    data = render_deck(spec)
    report = validate_pptx(data, spec, min_layouts=4)
    assert report.slide_count == 5


def test_scripted_eight_slide_vi_plan_full_pipeline():
    payload = {
        "presentation": {
            "topic": TOPIC,
            "audience": AUDIENCE,
            "language": "vi",
            "slide_count": 8,
            "theme": "academic",
            "instructions": "Nhấn mạnh bằng chứng.",
        },
        "evidence": [
            {
                "key": item.key,
                "canonical_url": item.canonical_url,
                "title": item.title,
            }
            for item in evidence_pair()
        ],
    }
    plan = build_scripted_plan(payload)
    assert plan.language == "vi" and len(plan.slides) == 8
    request = vi_request(
        slide_count=8, theme="academic", instructions="Nhấn mạnh bằng chứng."
    )
    spec = build_deck_spec(plan, request, evidence_pair())
    assert spec.theme == "academic"
    data = render_deck(spec)
    report = validate_pptx(data, spec, min_layouts=4)
    assert report.slide_count == 8


def test_plan_language_must_match_request():
    plan = _plan(base_slides("en"), language="en")
    with pytest.raises(SlideAgentValidationError, match="language does not match"):
        build_deck_spec(plan, vi_request(slide_count=5), evidence_pair())


def test_plan_slide_count_must_match_request():
    plan = _plan(base_slides())
    with pytest.raises(SlideAgentValidationError, match="slide count does not match"):
        build_deck_spec(plan, vi_request(slide_count=6), evidence_pair())


@pytest.mark.parametrize("where", ["slide_title", "purpose", "payload", "deck_title"])
def test_invented_urls_anywhere_are_rejected(where):
    slides = base_slides()
    deck_title = None
    if where == "slide_title":
        slides[1] = {**slides[1], "title": "Điểm chính www.example.com/vi"}
    elif where == "purpose":
        slides[1] = {**slides[1], "purpose": "Xem thêm tại https://invented.example/x"}
    elif where == "payload":
        slides[0] = {
            **slides[0],
            "payload": {"subtitle": "Nguồn: http://invented.example/a"},
        }
    else:
        deck_title = "Hồ sơ từ www.invented.example"
    with pytest.raises(SlideAgentValidationError, match="must not contain URLs"):
        build_spec(slides, deck_title=deck_title)


def test_out_of_range_evidence_keys_are_rejected():
    slides = base_slides()
    slides[1] = {**slides[1], "evidence_keys": [9]}
    with pytest.raises(
        SlideAgentValidationError, match="not available to the slide planner"
    ):
        build_spec(slides)
    slides = base_slides(conclusion_keys=[9])
    with pytest.raises(
        SlideAgentValidationError, match="not available to the slide planner"
    ):
        build_spec(slides)


@pytest.mark.parametrize(
    "layout,payload",
    [
        ("metric", {"items": ["Một điểm", "Hai điểm"]}),
        ("chart", {"items": ["Một điểm"]}),
        ("bullets", {"subtitle": "Phụ đề bất hợp lệ"}),
        ("title", {"items": ["Một điểm"]}),
    ],
)
def test_payload_must_match_its_layout(layout, payload):
    slides = base_slides()
    slides[1] = {**slides[1], "layout": layout, "payload": payload}
    with pytest.raises(SlideAgentValidationError, match="does not match its layout"):
        build_spec(slides)


@pytest.mark.parametrize("language,marker", [("vi", VI_MARKER), ("en", EN_MARKER)])
def test_numeric_content_is_labelled_illustrative(language, marker):
    strings = _STRINGS[language]
    slides = [
        _slide("title", strings["title_t"], {"subtitle": strings["title_sub"]}),
        _slide(
            "metric",
            strings["metric_t"],
            {
                "value": "75%",
                "label": strings["metric_label"],
                "insight": strings["metric_insight"],
            },
        ),
        _slide(
            "metric",
            strings["metric_t"],
            {
                "value": strings["metric_value_text"],
                "label": strings["metric_label"],
                "insight": strings["metric_insight"],
            },
        ),
        _slide(
            "chart",
            strings["chart_t"],
            {
                "chart_type": "column",
                "categories": list(strings["cats"]),
                "series": [
                    {"name": strings["chart_series"], "values": [1, 2, 3]},
                ],
                "caption": strings["chart_caption"],
            },
        ),
        _slide(
            "table",
            strings["table_t"],
            {
                "headers": [strings["table_h1"], strings["table_h2"]],
                "rows": [[strings["table_cell"], 2]],
                "caption": strings["table_caption"],
            },
        ),
        _slide(
            "table",
            strings["table_t"],
            {
                "headers": [strings["table_h1"], strings["table_h2"]],
                "rows": [[strings["table_cell"], strings["table_h2"]]],
                "caption": strings["table_caption"],
            },
        ),
        _slide(
            "conclusion",
            strings["conclusion_t"],
            {"items": [strings["conclusion_item"]], "source_keys": [0]},
        ),
    ]
    spec = build_spec(
        slides,
        language=language,
        request=vi_request(language=language, slide_count=len(slides)),
    )
    numeric_metric = spec.slides[1].content
    assert isinstance(numeric_metric, MetricContent)
    assert numeric_metric.insight.endswith(marker)
    text_metric = spec.slides[2].content
    assert isinstance(text_metric, MetricContent)
    assert not text_metric.insight.endswith(marker)
    chart = spec.slides[3].content
    assert isinstance(chart, ChartContent)
    assert chart.caption.endswith(marker)
    numeric_table = spec.slides[4].content
    assert isinstance(numeric_table, TableContent)
    assert numeric_table.caption.endswith(marker)
    string_table = spec.slides[5].content
    assert isinstance(string_table, TableContent)
    assert not string_table.caption.endswith(marker)


@pytest.mark.parametrize(
    "value,marked",
    [
        ("42", True),
        ("91%", True),
        ("1.2M", True),
        ("$3.5B", True),
        ("Cao", False),
        ("mười hai phần trăm", False),
    ],
)
def test_metric_digit_marker_regression(value, marked):
    """Any digit in a string metric value marks it; pure prose never does."""
    strings = _STRINGS["vi"]
    slides = base_slides()
    slides[3] = _slide(
        "metric",
        strings["metric_t"],
        {
            "value": value,
            "label": strings["metric_label"],
            "insight": strings["metric_insight"],
        },
    )
    content = build_spec(slides).slides[3].content
    assert isinstance(content, MetricContent)
    assert content.value == value
    assert content.insight.endswith(VI_MARKER) is marked


@pytest.mark.parametrize(
    "rows,marked",
    [
        ((("Bài trình chiếu", "42"),), True),
        ((("Tài liệu tham khảo", "91%"),), True),
        ((("Bài trình chiếu", "42"), ("Tài liệu", "91%")), True),
        ((("Bài trình chiếu", "Thành phần"),), False),
    ],
)
def test_table_string_digit_marker_regression(rows, marked):
    """All-string table cells carrying digits mark the caption; prose does not."""
    strings = _STRINGS["vi"]
    slides = base_slides()
    slides[3] = _slide(
        "table",
        strings["table_t"],
        {
            "headers": [strings["table_h1"], strings["table_h2"]],
            "rows": [list(row) for row in rows],
            "caption": strings["table_caption"],
        },
    )
    content = build_spec(slides).slides[3].content
    assert isinstance(content, TableContent)
    assert content.caption.endswith(VI_MARKER) is marked


def test_conclusion_sources_map_evidence_keys_without_inventing_urls():
    urls = ("https://evidence.example/report", "https://evidence.example/search")
    cases = {
        (0,): (urls[0],),
        (1, 0, 1): (urls[1], urls[0]),
        (0, 0, 0): (urls[0],),
        (): (VI_UNCERTAIN,),
    }
    for keys, expected in cases.items():
        spec = build_spec(base_slides(conclusion_keys=keys))
        content = spec.slides[-1].content
        assert isinstance(content, ConclusionContent)
        assert content.sources == expected
        assert set(content.sources) <= {*urls, VI_UNCERTAIN}


@pytest.mark.parametrize(
    "language,uncertain", [("vi", VI_UNCERTAIN), ("en", EN_UNCERTAIN)]
)
def test_conclusion_uncertainty_line_without_cited_evidence(language, uncertain):
    uncited = tuple(
        item.model_copy(update={"is_cited": False}) for item in evidence_pair()
    )
    spec = build_spec(
        base_slides(language, conclusion_keys=[0]),
        language=language,
        evidence=uncited,
    )
    content = spec.slides[-1].content
    assert isinstance(content, ConclusionContent)
    assert content.sources == (uncertain,)
    empty_slides = base_slides(language, conclusion_keys=[])
    empty_slides[1] = {**empty_slides[1], "evidence_keys": []}
    empty = build_spec(empty_slides, language=language, evidence=())
    assert empty.slides[-1].content.sources == (uncertain,)
    with pytest.raises(SlideAgentValidationError, match="not available"):
        build_spec(
            base_slides(language, conclusion_keys=[0]),
            language=language,
            evidence=(),
        )


@pytest.mark.parametrize("theme", ["light", "dark", "academic"])
def test_request_theme_lands_in_spec(theme):
    spec = build_spec(base_slides(), request=vi_request(slide_count=5, theme=theme))
    assert spec.theme == theme


# ---------------------------------------------------------------------------
# 4. Planning context projection and skill handling
# ---------------------------------------------------------------------------


def test_planning_context_carries_brief_evidence_and_skill():
    skill = SkillsRegistry().activate(SKILL_ID)
    request = vi_request(topic="Chủ đề nghiên cứu " + "chi tiết " * 100)
    assert len(request.topic) > 200
    evidence = tuple(
        SlideEvidenceItem(
            key=index,
            canonical_url=f"https://evidence.example/{index}",
            title=f"Nguồn {index}",
            excerpt=f"Trích dẫn {index}",
            is_cited=index == 0,
        )
        for index in range(MAX_EVIDENCE_ITEMS + 2)
    )
    context = build_slide_planning_context(request, evidence, active_skill=skill)
    assert context.presentation.topic == request.topic[:200]
    assert context.presentation.audience == AUDIENCE
    assert context.presentation.language == "vi"
    assert context.presentation.slide_count == 8
    assert context.presentation.theme == "light"
    assert len(context.evidence) == MAX_EVIDENCE_ITEMS
    assert [item.key for item in context.evidence] == list(range(MAX_EVIDENCE_ITEMS))
    assert isinstance(context.active_skill, ProjectedSkillContext)
    assert context.active_skill.id == SKILL_ID
    assert context.active_skill.instructions == skill.instructions
    metadata = context.context_projection
    assert isinstance(metadata, ProjectionMetadata)
    assert metadata.skill_id == SKILL_ID
    assert metadata.skill_omitted is False
    assert metadata.budget_chars == ContextBudget().max_total_chars
    payload = json.loads(render_llm_payload(context))
    assert payload["presentation"]["slide_count"] == 8
    assert payload["evidence"][0]["canonical_url"] == "https://evidence.example/0"
    assert payload["active_skill"]["id"] == SKILL_ID


def test_small_budget_compacts_deterministically_with_metadata():
    skill = SkillsRegistry().activate(SKILL_ID)
    evidence = tuple(
        SlideEvidenceItem(
            key=index,
            canonical_url=f"https://evidence.example/{index}",
            title=f"Nguồn {index}",
            excerpt="E" * 400,
            is_cited=False,
        )
        for index in range(10)
    )
    budget = ContextBudget(
        max_total_chars=6_000, max_item_chars=3_000, max_excerpt_chars=1_000
    )
    context = build_slide_planning_context(
        vi_request(), evidence, active_skill=skill, context_budget=budget
    )
    metadata = context.context_projection
    assert isinstance(metadata, ProjectionMetadata)
    assert metadata.skill_omitted is True
    assert metadata.omitted_items + metadata.truncated_items >= 1
    assert len(context.evidence) < len(evidence)
    assert len(render_llm_payload(context)) <= budget.max_total_chars
    payload = json.loads(render_llm_payload(context))
    assert payload["evidence"]  # the newest evidence survives compaction
    assert payload["active_skill"]["instructions"] is None


def test_oversized_skill_instructions_are_capped():
    oversized = SkillContext(
        id="oversized-skill",
        fingerprint="a" * 64,
        instructions="x" * 2_500,
    )
    context = build_slide_planning_context(vi_request(), (), active_skill=oversized)
    projected = context.active_skill
    assert isinstance(projected, ProjectedSkillContext)
    assert len(projected.instructions) <= 2_000
    assert "truncated at 2000 of 2500" in projected.instructions
    metadata = context.context_projection
    assert isinstance(metadata, ProjectionMetadata)
    assert metadata.skill_id == "oversized-skill"
    assert metadata.skill_omitted is False
    assert metadata.truncated_items >= 1


# ---------------------------------------------------------------------------
# 5. SlideAgentService against the real offline runtime
# ---------------------------------------------------------------------------


def test_service_generates_from_completed_skill_turn(tmp_path):
    async def scenario():
        runtime, service, planner, store = slide_services(tmp_path)
        await runtime.run(
            RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
        )
        result = await service.generate(THREAD, TURN, vi_request(theme="academic"))
        assert isinstance(result.view, DemoRunView)
        assert result.view.thread_id == THREAD
        assert result.view.active_skill_id == SKILL_ID
        assert result.view.evidence
        assert result.spec.theme == "academic"
        assert isinstance(result.context_metadata, ProjectionMetadata)
        assert result.context_metadata.skill_id == SKILL_ID
        capture = planner.captures[0]
        assert capture["skill_id"] == SKILL_ID
        assert capture["skill_instruction_chars"] > 0
        assert capture["evidence_urls"]
        assert all(url.startswith("https://") for url in capture["evidence_urls"])
        assert capture["language"] == "vi"
        assert capture["slide_count"] == 8
        record, data = await store.retrieve(THREAD, TURN)
        assert record.sha256 == hashlib.sha256(data).hexdigest()
        validate_pptx(data, result.spec, min_layouts=4)

    asyncio.run(scenario())


def test_service_turn_without_skill_plans_without_skill(tmp_path):
    async def scenario():
        runtime, service, planner, _ = slide_services(tmp_path)
        await runtime.run(RunDemoCommand(thread_id=THREAD, turn_id=TURN))
        await service.generate(THREAD, TURN, vi_request())
        capture = planner.captures[-1]
        assert capture["skill_id"] is None
        assert capture["skill_instruction_chars"] == 0
        assert capture["evidence_urls"]

    asyncio.run(scenario())


def test_service_rejects_unknown_running_and_corrupt_turns(tmp_path):
    async def scenario():
        runtime, service, _, _ = slide_services(tmp_path)
        await runtime.run(
            RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
        )
        with pytest.raises(SlideAgentError, match="not found"):
            await service.generate(THREAD, "unknown-turn", vi_request())
        # Corrupt the pinned skill snapshot in durable storage: fail closed.
        with sqlite3.connect(tmp_path / "checkpoints.sqlite") as database:
            database.execute(
                "UPDATE turns SET run_context_json=?", ('{"active_skill": 5}',)
            )
        with pytest.raises(SlideAgentError, match="Pinned skill snapshot"):
            await service.generate(THREAD, TURN, vi_request())
        # A durable running status must block generation even with a checkpoint.
        with sqlite3.connect(tmp_path / "checkpoints.sqlite") as database:
            database.execute("UPDATE turns SET status='running'")
        with pytest.raises(SlideAgentError, match="not completed"):
            await service.generate(THREAD, TURN, vi_request())

    asyncio.run(scenario())


def test_service_conflict_idempotency_and_restart_persistence(tmp_path):
    async def scenario():
        runtime, service, _, store = slide_services(tmp_path)
        await runtime.run(
            RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
        )
        request = vi_request(theme="academic")
        first = await service.generate(THREAD, TURN, request)
        _, data = await store.retrieve(THREAD, TURN)

        conflicting = request.model_copy(update={"slide_count": 6})
        with pytest.raises(SlideAgentConflictError) as error:
            await service.generate(THREAD, TURN, conflicting)
        message = str(error.value)
        assert "different presentation" in message
        assert_no_host_leak(message, tmp_path)

        retry = await service.generate(THREAD, TURN, request)
        assert retry.record == first.record
        assert retry.record.sha256 == first.record.sha256

        fresh_store = PptxArtifactStore(tmp_path, mode="offline")
        record, persisted = await fresh_store.retrieve(THREAD, TURN)
        assert record == first.record
        assert persisted == data
        fresh_service = SlideAgentService(
            DemoRuntimeService(OfflineDemoBackend(tmp_path)),
            fresh_store,
            planner_model_factory=ScriptedSlidePlanner,
            structured_output_mode="native",
        )
        again = await fresh_service.generate(THREAD, TURN, request)
        assert again.record == first.record

    asyncio.run(scenario())


class ConversionRepairPlanner(ScriptedSlidePlanner):
    """First plan violates slide_count; the repair attempt must fix it."""

    def __init__(self) -> None:
        super().__init__()
        self.hint_seen: str | None = None
        self.plan_attempts = 0

    def _plan_from_messages(self, messages):
        self.plan_attempts += 1
        hint = next(
            (
                message[1]
                for message in messages[2:]
                if "previous plan attempt was rejected" in message[1]
            ),
            None,
        )
        if hint is not None:
            self.hint_seen = hint
        plan = super()._plan_from_messages(messages)
        if self.plan_attempts == 1 and hint is None:
            # Deterministic constraint violation (count mismatch) on the
            # first conversion, mirroring the live observed failure class.
            slides = list(plan.slides[:-1])
            return DeckPlan.model_validate(
                {
                    "deck_title": plan.deck_title,
                    "language": plan.language,
                    "slides": slides,
                }
            )
        return plan


class DensityOverflowThenFixPlanner(ScriptedSlidePlanner):
    """First plan is schema-legal but too dense; repair must shorten it."""

    def __init__(self) -> None:
        super().__init__()
        self.hint_seen: str | None = None

    def _plan_from_messages(self, messages):
        hint = next(
            (
                message[1]
                for message in messages[2:]
                if "previous plan attempt was rejected" in message[1]
            ),
            None,
        )
        if hint is not None:
            self.hint_seen = hint
        payload = json.loads(messages[1][1])
        plan = build_scripted_plan(payload)
        if hint is None:
            dense_bullets = DeckPlan.model_validate(
                {
                    "deck_title": plan.deck_title,
                    "language": plan.language,
                    "slides": [
                        plan.slides[0],
                        {
                            "layout": "bullets",
                            "title": plan.slides[1].title,
                            "purpose": "dense",
                            "evidence_keys": [],
                            "payload": {
                                "items": [
                                    ("Năng lượng mặt trời " * 10)[:178]
                                    for _ in range(6)
                                ]
                            },
                        },
                        *plan.slides[2:],
                    ],
                }
            )
            return dense_bullets
        return plan


def test_service_repairs_render_density_overflow_with_bounded_replan(tmp_path):
    """Root-cause regression (live E2E): schema-legal but dense plans died.

    A live model produced plans whose text lengths are inside schema bounds
    yet exceed the renderer's density preflight; the job failed terminally.
    The service now retries once with an explicit shorten-text hint while
    the renderer/validator density contract stays untouched.
    """

    async def scenario():
        runtime, _, _, store = slide_services(tmp_path)
        await runtime.run(
            RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
        )
        planner = DensityOverflowThenFixPlanner()
        service = SlideAgentService(
            runtime,
            store,
            planner_model_factory=lambda: planner,
            structured_output_mode="native",
        )
        result = await service.generate(THREAD, TURN, vi_request())
        # Deterministic trimming fixed density without a second model call.
        assert planner.hint_seen is None
        bullets = next(
            slide for slide in result.spec.slides if slide.content.kind == "bullets"
        )
        assert all(len(item) < 178 for item in bullets.content.items)
        assert len(result.spec.slides) == 8
        record, data = await store.retrieve(THREAD, TURN)
        assert record.sha256 == hashlib.sha256(data).hexdigest()
        validate_pptx(data, result.spec, min_layouts=4)

    asyncio.run(scenario())


def test_service_repairs_conversion_failure_with_bounded_hint_guided_replan(
    tmp_path,
):
    """Root-cause regression: live conversion failures were terminal.

    A live plan that violates deterministic constraints (slide count) used to
    fail the whole presentation job. The service now performs exactly one
    hint-guided re-plan; the hint carries only our safe constant message.
    """

    async def scenario():
        runtime, _, _, store = slide_services(tmp_path)
        await runtime.run(
            RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
        )
        planner = ConversionRepairPlanner()
        service = SlideAgentService(
            runtime,
            store,
            planner_model_factory=lambda: planner,
            structured_output_mode="native",
        )
        result = await service.generate(THREAD, TURN, vi_request())
        assert planner.plan_attempts == 2
        assert planner.hint_seen is not None
        assert "slide count" in planner.hint_seen
        assert len(result.spec.slides) == 8
        record, data = await store.retrieve(THREAD, TURN)
        assert record.sha256 == hashlib.sha256(data).hexdigest()
        validate_pptx(data, result.spec, min_layouts=4)

    asyncio.run(scenario())


def test_service_conversion_repair_is_bounded(tmp_path):
    """Persistent conversion violations fail closed after the repair cap."""

    class AlwaysWrongCountPlanner(ConversionRepairPlanner):
        def _plan_from_messages(self, messages):
            self.plan_attempts += 1
            plan = ScriptedSlidePlanner._plan_from_messages(self, messages)
            slides = list(plan.slides[:-1])
            return DeckPlan.model_validate(
                {
                    "deck_title": plan.deck_title,
                    "language": plan.language,
                    "slides": slides,
                }
            )

    async def scenario():
        runtime, _, _, store = slide_services(tmp_path)
        await runtime.run(
            RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
        )
        planner = AlwaysWrongCountPlanner()
        service = SlideAgentService(
            runtime,
            store,
            planner_model_factory=lambda: planner,
            structured_output_mode="native",
        )
        with pytest.raises(SlideAgentValidationError, match="structural constraints"):
            await service.generate(THREAD, TURN, vi_request())
        assert planner.plan_attempts == 2
        # Fail-closed: nothing was published for the violating plan.
        assert not list(tmp_path.rglob("*.pptx"))

    asyncio.run(scenario())


def test_service_clips_oversized_evidence_and_generation_succeeds(tmp_path):
    """Real long evidence fields are clipped, never fatal to generation."""

    async def scenario():
        runtime, _, _, store = slide_services(tmp_path)
        await runtime.run(
            RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
        )
        session = await runtime.load_conversation(THREAD)
        turn = next(item for item in session.turns if item.turn_id == TURN)
        assert turn.run is not None and turn.run.evidence
        oversized = dataclasses.replace(
            turn.run.evidence[-1],
            title="T" * 200,
            excerpt="E" * 1_200,
            is_cited=True,
        )
        evidence = turn.run.evidence[:-1] + (oversized,)
        replay = await replayed_session_with_evidence(runtime, evidence)
        planner = EvidenceCapturingPlanner()
        service = SlideAgentService(
            ReplaySlideSource(runtime, replay),
            store,
            planner_model_factory=lambda: planner,
            structured_output_mode="native",
        )
        result = await service.generate(THREAD, TURN, vi_request(theme="academic"))
        assert result.spec.theme == "academic"
        assert len(planner.evidence_entries) == 1
        entries = planner.evidence_entries[0]
        entry = entries[-1]
        assert entry["title"] == "T" * 120
        assert entry["excerpt"] == "E" * 400
        assert all(len(item["title"]) <= 120 for item in entries)
        assert all(len(item["excerpt"]) <= 400 for item in entries)
        assert all(len(item["canonical_url"]) <= 300 for item in entries)
        record, data = await store.retrieve(THREAD, TURN)
        assert record.sha256 == hashlib.sha256(data).hexdigest()
        validate_pptx(data, result.spec, min_layouts=4)

    asyncio.run(scenario())


def test_service_retains_newest_evidence_items(tmp_path):
    """Only the last MAX_EVIDENCE_ITEMS items survive, newest last, re-keyed."""

    async def scenario():
        runtime, _, _, store = slide_services(tmp_path)
        await runtime.run(
            RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
        )
        session = await runtime.load_conversation(THREAD)
        turn = next(item for item in session.turns if item.turn_id == TURN)
        assert turn.run is not None and turn.run.evidence
        base = turn.run.evidence[0]
        total = MAX_EVIDENCE_ITEMS + 2
        long_url = "https://evidence.example/retained/" + "u" * 320

        def url_for(index: int) -> str:
            if index == total - 1:
                return long_url
            return f"https://evidence.example/retention/{index}"

        evidence = tuple(
            dataclasses.replace(
                base,
                canonical_url=url_for(index),
                title=f"Retention source {index}",
                excerpt=f"Excerpt number {index} for retention ordering.",
            )
            for index in range(total)
        )
        replay = await replayed_session_with_evidence(runtime, evidence)
        planner = EvidenceCapturingPlanner()
        service = SlideAgentService(
            ReplaySlideSource(runtime, replay),
            store,
            planner_model_factory=lambda: planner,
            structured_output_mode="native",
        )
        await service.generate(THREAD, TURN, vi_request())
        entries = planner.evidence_entries[0]
        assert len(entries) == MAX_EVIDENCE_ITEMS - 1
        assert [item["key"] for item in entries] == list(range(MAX_EVIDENCE_ITEMS - 1))
        # The >300-character URL must be omitted, never truncated to a false URL.
        expected_urls = [url_for(index) for index in range(2, total - 1)]
        assert [item["canonical_url"] for item in entries] == expected_urls
        assert planner.captures[0]["evidence_urls"] == expected_urls

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6. HTTP transport
# ---------------------------------------------------------------------------


def test_api_presentation_lifecycle(tmp_path):
    async def prepare():
        runtime, service, _, store = slide_services(tmp_path)
        await runtime.run(
            RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
        )
        return service, store

    service, store = asyncio.run(prepare())
    url = f"/api/offline/conversations/{THREAD}/turns/{TURN}/presentation"
    with TestClient(
        create_app(tmp_path, slide_services={"offline": service})
    ) as client:
        submitted = client.post(url, json=VALID_BODY)
        assert submitted.status_code == 202, submitted.text
        job = submitted.json()
        assert job["operation"] == "presentation"
        assert job["thread_id"] == THREAD
        assert job["turn_id"] == TURN
        completed = wait_job(client, job)
        assert completed["status"] == "completed", completed["error_message"]
        assert completed["error_message"] is None
        assert completed["view"]["thread_id"] == THREAD
        assert completed["view"]["active_skill_id"] == SKILL_ID

        record, stored = asyncio.run(store.retrieve(THREAD, TURN))
        download = client.get(url)
        assert download.status_code == 200
        assert download.content == stored
        assert download.headers["content-type"] == PPTX_MIME
        assert (
            download.headers["content-disposition"]
            == 'attachment; filename="presentation.pptx"'
        )
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.headers["cache-control"] == "no-store"
        assert download.headers["etag"] == f'"{record.sha256}"'

        retried = wait_job(client, client.post(url, json=VALID_BODY).json())
        assert retried["status"] == "completed"
        assert client.get(url).headers["etag"] == f'"{record.sha256}"'

        conflict = wait_job(
            client, client.post(url, json={**VALID_BODY, "slide_count": 6}).json()
        )
        assert conflict["status"] == "failed"
        assert conflict["view"] is None
        assert "different presentation" in conflict["error_message"]
        assert_no_host_leak(conflict["error_message"], tmp_path)
        assert client.get(url).headers["etag"] == f'"{record.sha256}"'

        operations = [
            item["operation"]
            for item in client.get("/api/offline/jobs").json()
            if item["thread_id"] == THREAD
        ]
        assert operations
        assert set(operations) == {"presentation"}

        report = client.get(
            f"/api/offline/conversations/{THREAD}/turns/{TURN}/artifact"
        )
        assert report.status_code == 200
        assert report.headers["content-type"].startswith("text/markdown")
        assert report.headers["x-content-type-options"] == "nosniff"

        live = f"/api/live/conversations/{THREAD}/turns/{TURN}/presentation"
        assert client.post(live, json=VALID_BODY).status_code == 404
        assert client.get(live).status_code == 404


def test_api_rejects_turn_that_is_not_completed(tmp_path):
    async def prepare():
        interrupted = DemoRuntimeService(
            OfflineDemoBackend(tmp_path, interrupt_after_delegation=True)
        )
        with pytest.raises(RuntimeError, match="deterministic interruption"):
            await interrupted.run(
                RunDemoCommand(thread_id=THREAD, turn_id=TURN, skill_id=SKILL_ID)
            )

    asyncio.run(prepare())
    service = SlideAgentService(
        DemoRuntimeService(OfflineDemoBackend(tmp_path)),
        PptxArtifactStore(tmp_path, mode="offline"),
        planner_model_factory=ScriptedSlidePlanner,
        structured_output_mode="native",
    )
    with TestClient(
        create_app(tmp_path, slide_services={"offline": service})
    ) as client:
        base = f"/api/offline/conversations/{THREAD}/turns"
        response = client.post(
            f"{base}/{TURN}/presentation",
            json=json.loads(vi_request().model_dump_json()),
        )
        assert response.status_code == 409
        assert response.json() == {
            "detail": "Complete or resume the turn before generating slides."
        }
        unknown_turn = client.post(f"{base}/unknown-turn/presentation", json=VALID_BODY)
        assert unknown_turn.status_code == 404
        assert unknown_turn.json() == {"detail": "Turn was not found."}
        assert client.get("/api/offline/jobs").json() == []


def test_api_offline_refuses_without_injected_planner(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        url = f"/api/offline/conversations/{THREAD}/turns/{TURN}/presentation"
        response = client.post(url, json=VALID_BODY)
        assert response.status_code == 503
        assert response.json() == {
            "detail": "Presentation generation is unavailable in this mode."
        }
        assert client.get("/api/offline/jobs").json() == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"audience": AUDIENCE, "language": "vi"},
        {**VALID_BODY, "topic": "ngắn"},
        {**VALID_BODY, "language": "fr"},
        {**VALID_BODY, "slide_count": 13},
        {**VALID_BODY, "slide_count": 4},
        {**VALID_BODY, "slide_count": "8"},
        {**VALID_BODY, "slide_count": True},
        {**VALID_BODY, "theme": "grape"},
        {**VALID_BODY, "instructions": "x" * 501},
        {**VALID_BODY, "evil": True},
    ],
)
def test_api_invalid_bodies_fail_validation_without_side_effects(tmp_path, body):
    service = SlideAgentService(
        DemoRuntimeService(OfflineDemoBackend(tmp_path)),
        PptxArtifactStore(tmp_path, mode="offline"),
        planner_model_factory=ScriptedSlidePlanner,
        structured_output_mode="native",
    )
    with TestClient(
        create_app(tmp_path, slide_services={"offline": service})
    ) as client:
        url = "/api/offline/conversations/some-thread/turns/some-turn/presentation"
        response = client.post(url, json=body)
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid request."}
        assert client.get("/api/offline/jobs").json() == []
        assert client.get("/api/offline/conversations").json() == []


def test_api_unknown_thread_maps_to_conversation_not_found(tmp_path):
    service = SlideAgentService(
        DemoRuntimeService(OfflineDemoBackend(tmp_path)),
        PptxArtifactStore(tmp_path, mode="offline"),
        planner_model_factory=ScriptedSlidePlanner,
        structured_output_mode="native",
    )
    with TestClient(
        create_app(tmp_path, slide_services={"offline": service})
    ) as client:
        url = f"/api/offline/conversations/missing-{THREAD}/turns/{TURN}/presentation"
        response = client.post(url, json=VALID_BODY)
        assert response.status_code == 404
        assert response.json() == {"detail": "Conversation was not found."}
        assert client.get("/api/offline/jobs").json() == []


# ---------------------------------------------------------------------------
# 7. Regression guards
# ---------------------------------------------------------------------------


def test_job_manager_still_rejects_bogus_operations():
    with (
        DemoJobManager() as manager,
        pytest.raises(ValueError, match="operation"),
    ):
        manager.submit("bogus", THREAD, lambda sink: None)


def test_job_dto_operation_literal_accepts_presentation():
    dto = JobDTO(
        job_id="job-1",
        mode="offline",
        thread_id=THREAD,
        turn_id=TURN,
        operation="presentation",
        status="queued",
        execution_graph=ExecutionGraphProjection(),
    )
    assert dto.operation == "presentation"
    with pytest.raises(ValidationError):
        JobDTO(
            job_id="job-2",
            mode="offline",
            thread_id=THREAD,
            operation="bogus",
            status="queued",
            execution_graph=ExecutionGraphProjection(),
        )


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            SlideAgentConflictError("A different presentation already belongs here."),
            "A different presentation already belongs to this turn.",
        ),
        (SlideAgentError("private C:\\host\\path"), "Presentation generation failed."),
        (
            SlideAgentValidationError("plan slide count does not match the request"),
            (
                "Presentation planning failed: plan slide count does not "
                "match the request"
            ),
        ),
        (
            SlideAgentValidationError("bad plan"),
            "Presentation planning failed: bad plan",
        ),
        (
            ArtifactError("disk C:\\host\\path failed"),
            "Presentation generation failed.",
        ),
        (ConversationNotFoundError("conv secret"), "Conversation was not found."),
        (ThreadNotFoundError("thread secret"), "No checkpoint exists for this thread."),
        (TimeoutError(), "Operation timed out."),
    ],
)
def test_map_demo_error_slide_branches_are_safe(error, expected):
    assert map_demo_error(error) == expected


def test_map_demo_error_defaults_never_echo_details():
    message = map_demo_error(RuntimeError("sk-day15-must-not-leak C:\\secret"), "job-1")
    assert message == "Demo operation failed. Reference: job-1."
    assert "sk-day15-must-not-leak" not in message


# Corrective review regressions: cited URL identity and post-compaction keys.


def test_long_cited_url_uses_short_sha256_reference_without_fabricating_url():
    long_url = "https://evidence.example/article?" + "a" * 215
    assert 180 < len(long_url) <= 300
    original = evidence_pair()[0]
    evidence = (original.model_copy(update={"canonical_url": long_url}),)
    spec = build_spec(base_slides(conclusion_keys=(0,)), evidence=evidence)
    conclusion = spec.slides[-1].content
    assert isinstance(conclusion, ConclusionContent)
    source = conclusion.sources[0]
    assert len(source) <= 180
    assert source.startswith("Nguồn #1 SHA256:")
    assert hashlib.sha256(long_url.encode("utf-8")).hexdigest() in source
    assert long_url not in source  # do not present a false truncated URL
    validate_pptx(render_deck(spec), spec, min_layouts=4)


def test_compacted_planner_evidence_keys_remain_stable_and_omitted_keys_fail():
    evidence = tuple(
        SlideEvidenceItem(
            key=index,
            canonical_url=f"https://evidence.example/compacted/{index}",
            title=f"Nguồn số {index}",
            excerpt="E" * 400,
            is_cited=True,
        )
        for index in range(10)
    )
    budget = ContextBudget(
        max_total_chars=6_000, max_item_chars=3_000, max_excerpt_chars=1_000
    )
    presented = []
    request = vi_request()
    plan = plan_deck(
        ScriptedSlidePlanner(),
        request,
        evidence,
        active_skill=SkillsRegistry().activate(SKILL_ID),
        context_budget=budget,
        structured_output_mode="native",
        on_presented_evidence=presented.append,
    )
    assert len(presented) == 1
    allowed = presented[0]
    assert 0 < len(allowed) < len(evidence)
    remaining_keys = {item.key for item in allowed}
    omitted_keys = {item.key for item in evidence} - remaining_keys
    assert omitted_keys
    # The scripted planner now references actual surviving keys, never
    # positions in the shortened evidence list.
    for slide in plan.slides:
        assert set(slide.evidence_keys) <= remaining_keys
    spec = build_deck_spec(plan, request, allowed)
    validate_pptx(render_deck(spec), spec, min_layouts=4)

    bad_key = min(omitted_keys)
    bad_slide = plan.slides[-1].model_copy(
        update={
            "payload": {
                "items": ["Nguồn này đã bị loại khỏi ngữ cảnh model."],
                "source_keys": [bad_key],
            }
        }
    )
    bad_plan = plan.model_copy(update={"slides": (*plan.slides[:-1], bad_slide)})
    with pytest.raises(
        SlideAgentValidationError, match="not available to the slide planner"
    ):
        build_deck_spec(bad_plan, request, allowed)


def test_model_variance_retry_preserves_existing_artifact_and_download(tmp_path):
    class VaryingPlanner(ScriptedSlidePlanner):
        def __init__(self):
            super().__init__()
            self.generated = 0

        def _plan_from_messages(self, messages):
            plan = super()._plan_from_messages(messages)
            self.generated += 1
            if self.generated > 1:
                return plan.model_copy(
                    update={"deck_title": plan.deck_title + " bản khác"}
                )
            return plan

    async def scenario():
        runtime = DemoRuntimeService(OfflineDemoBackend(tmp_path))
        store = PptxArtifactStore(tmp_path, mode="offline")
        planner = VaryingPlanner()
        service = SlideAgentService(
            runtime,
            store,
            planner_model_factory=lambda: planner,
            structured_output_mode="native",
        )
        await runtime.run(RunDemoCommand(thread_id=THREAD, turn_id=TURN))
        request = vi_request()
        first = await service.generate(THREAD, TURN, request)
        _, first_bytes = await store.retrieve(THREAD, TURN)
        with pytest.raises(SlideAgentConflictError, match="different presentation"):
            await service.generate(THREAD, TURN, request)
        assert planner.generated == 2
        assert (await store.retrieve(THREAD, TURN))[1] == first_bytes
        with TestClient(create_app(tmp_path)) as client:
            downloaded = client.get(
                f"/api/offline/conversations/{THREAD}/turns/{TURN}/presentation"
            )
        assert downloaded.status_code == 200
        assert downloaded.content == first_bytes
        assert downloaded.headers["etag"] == f'"{first.record.sha256}"'

    asyncio.run(scenario())
