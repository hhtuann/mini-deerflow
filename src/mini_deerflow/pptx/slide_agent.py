"""Model-driven slide planning and turn-owned presentation generation.

The planner reuses the production structured-output seam (``DeckPlan`` typed
schema, bounded attempts) and the shared deterministic context compactor. The
source turn's pinned skill snapshot and a sanitized evidence allowlist are the
only untrusted inputs; slide citations reference evidence by key, so invented
URLs are structurally impossible rather than merely discouraged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, runtime_checkable

from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mini_deerflow.context_budget import (
    ContextBudget,
    ContextBudgetModel,
    ProjectedSkillContext,
    ProjectionMetadata,
    fit_context_to_budget,
    render_llm_payload,
)
from mini_deerflow.pptx.renderer import LayoutOverflowError, render_deck
from mini_deerflow.pptx.schemas import (
    BulletsContent,
    Cell,
    ChartContent,
    ChartSeries,
    Column,
    ComparisonContent,
    ConclusionContent,
    DeckSpec,
    Label,
    MetricContent,
    Number,
    SlideSpec,
    StepsContent,
    TableContent,
    Text,
    Title,
    TitleContent,
)
from mini_deerflow.pptx.validator import PptxValidationError, validate_pptx
from mini_deerflow.skills import SkillContext
from mini_deerflow.structured_output import (
    StructuredChatModel,
    StructuredOutputMode,
    create_structured_output_runnable,
)

if TYPE_CHECKING:
    from mini_deerflow.demo.view_models import DemoRunView
    from mini_deerflow.pptx.artifacts import PptxArtifact

PresentationLanguage = Literal["vi", "en"]
PresentationTheme = Literal["light", "dark", "academic"]
SlideLayoutKind = Literal[
    "title",
    "bullets",
    "comparison",
    "metric",
    "steps",
    "chart",
    "table",
    "conclusion",
]

SLIDE_PLANNER_MAX_ATTEMPTS = 2
SLIDE_PLAN_REPAIR_ATTEMPTS = 2
DENSITY_TRIM_FLOORS = (40, 28, 20, 14, 8)
DENSITY_TRIM_PASSES_PER_FLOOR = 6
MAX_EVIDENCE_ITEMS = 10

_ILLUSTRATIVE_MARKER = {"vi": "(số liệu minh họa)", "en": "(illustrative data)"}
_UNCERTAINTY_SOURCE = {
    "vi": "Bằng chứng còn hạn chế: nội dung này chưa có nguồn đã xác thực.",
    "en": "Limited evidence: no validated source supports this content yet.",
}
_URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)
# Any digit in a structured numeric slot (metric value, table cell) marks the
# slide illustrative: quoted numbers, units, and currencies cannot bypass it.
_DIGIT_PATTERN = re.compile(r"\d")

SLIDE_PLANNER_SYSTEM_PROMPT = """
You are the slide-planning component of a bounded research agent.

Convert the presentation request and its validated evidence into a deck plan.

Request fields you must respect:
- presentation.topic: the subject of the deck.
- presentation.audience: who will read it; adapt depth and wording.
- presentation.language: write EVERY title and content string in this
  language ("vi" Vietnamese or "en" English).
- presentation.slide_count: the plan must contain EXACTLY this many slides.
- presentation.instructions: optional extra user guidance.

Output the DeckPlan JSON with exactly these fields:
{
  "deck_title": "<3-80 characters, in the requested language>",
  "language": "<must equal presentation.language>",
  "slides": [
    {
      "layout": "<title|bullets|comparison|metric|steps|chart|table|conclusion>",
      "title": "<3-80 characters>",
      "purpose": "<<=240 characters, planning narrative only>",
      "evidence_keys": [<0-6 integers indexing the provided evidence>],
      "payload": { "<layout-specific fields as described below>" }
    }
  ]
}

payload by layout (include ONLY the fields for that layout):
- title: {"subtitle": "<<=180 chars>"}
- bullets: {"items": ["<=180 chars", ... 1-6 items]}
- comparison: {"left": {"heading": "<=36>", "items": [1-4 x "<=180>"]},
               "right": {"heading": "<=36>", "items": [1-4 x "<=180>"]}}
- metric: {"value": "<=36>", "label": "<=80>", "insight": "<=180>"}
- steps: {"items": [2-5 x "<=80>"]}
- chart: {"chart_type": "column|bar|line", "categories": [1-8 unique "<=36>"],
          "series": [{"name": "<=36>", "values": [1-8 numbers]}],
          "caption": "<=120; numeric series data is always illustrative>"}
- table: {"headers": [1-5 x "<=48>"], "rows": [1-8 rows of "<=48>" or numbers],
          "caption": "<=120>"}
- conclusion: {"items": [1-3 x "<=180>"], "source_keys": [0-3 evidence indices]}

Planning rules:
1. Use ONLY the provided evidence list for sourcing. Reference evidence by
   its integer key in evidence_keys and source_keys. Never write any URL,
   "www." link, or invented source name anywhere in the plan.
2. Do not fabricate citations, quotations, benchmark metrics, or statistics.
   Numeric chart/table data is illustrative by policy; the system labels it.
   Prefer qualitative statements grounded in evidence excerpts.
3. If evidence is weak, missing, or contradicts a claim, say so explicitly
   in the slide text instead of inventing support.
4. Reflect the topic and audience in every slide; keep the requested
   language everywhere including the deck title.
   Use the explicit evidence item.key values, NOT their positions in the list:
   earlier evidence items may have been omitted to fit the context budget.
5. Produce EXACTLY presentation.slide_count slides with sensible layout
   variety: include at least title, bullets, and conclusion plus at least
   one of comparison/metric/steps/chart/table when count allows.
6. Do not add fields outside this schema. Do not wrap output in code fences.
""".strip()


class SlideAgentError(RuntimeError):
    """Controlled slide-generation failure; never leaks paths or payloads."""


class SlideAgentValidationError(SlideAgentError):
    """The model plan or its conversion violated the presentation contract."""


class SlideAgentConflictError(SlideAgentError):
    """A different immutable deck already belongs to this turn."""


class PresentationRequest(BaseModel):
    """Validated user intent for one generated deck."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(min_length=10, max_length=1000)
    audience: str = Field(min_length=3, max_length=120)
    language: PresentationLanguage
    slide_count: int = Field(default=8, strict=True, ge=5, le=12)
    theme: PresentationTheme = "light"
    instructions: str = Field(default="", max_length=500)


class SlideEvidenceItem(ContextBudgetModel):
    """Bounded, allowlisted evidence entry the planner may reference by key."""

    key: int = Field(ge=0, strict=True)
    canonical_url: str = Field(min_length=1, max_length=300)
    title: str = Field(default="", max_length=120)
    excerpt: str = Field(default="", max_length=400)
    is_cited: bool = False


type PresentedEvidenceObserver = Callable[[tuple[SlideEvidenceItem, ...]], None]


class PresentationBrief(ContextBudgetModel):
    topic: str = Field(max_length=200)
    audience: str = Field(max_length=120)
    language: PresentationLanguage
    slide_count: int = Field(ge=5, le=12, strict=True)
    theme: PresentationTheme
    instructions: str = Field(default="", max_length=500)


class SlidePlanningContext(ContextBudgetModel):
    """Model-facing projection; never raw state, tool output, or the ledger."""

    presentation: PresentationBrief
    evidence: tuple[SlideEvidenceItem, ...] = ()
    active_skill: SkillContext | ProjectedSkillContext | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    context_projection: ProjectionMetadata | None = Field(default=None, exclude=True)


class _PlannedPayload(BaseModel):
    """Model-facing payload base: JSON-shaped input, unknown fields rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannedTitlePayload(_PlannedPayload):
    subtitle: Text


class PlannedBulletsPayload(_PlannedPayload):
    items: tuple[Text, ...] = Field(min_length=1, max_length=6)


class PlannedColumn(_PlannedPayload):
    heading: Label
    items: tuple[Text, ...] = Field(min_length=1, max_length=4)


class PlannedComparisonPayload(_PlannedPayload):
    left: PlannedColumn
    right: PlannedColumn


class PlannedMetricPayload(_PlannedPayload):
    value: Label
    label: Title
    insight: Text


class PlannedStepsPayload(_PlannedPayload):
    items: tuple[Title, ...] = Field(min_length=2, max_length=5)


class PlannedChartPayload(_PlannedPayload):
    chart_type: Literal["column", "bar", "line"] = "column"
    categories: tuple[Label, ...] = Field(min_length=1, max_length=8)
    series: tuple[PlannedChartSeries, ...] = Field(min_length=1, max_length=3)
    caption: str = Field(min_length=1, max_length=120)


class PlannedChartSeries(_PlannedPayload):
    name: Label
    values: tuple[Number, ...] = Field(min_length=1, max_length=8)


class PlannedTablePayload(_PlannedPayload):
    headers: tuple[Cell, ...] = Field(min_length=1, max_length=5)
    rows: tuple[tuple[Cell | Number, ...], ...] = Field(min_length=1, max_length=8)
    caption: str = Field(min_length=1, max_length=120)


class PlannedConclusionPayload(_PlannedPayload):
    items: tuple[Text, ...] = Field(min_length=1, max_length=3)
    source_keys: tuple[Annotated[int, Field(ge=0, strict=True)], ...] = Field(
        default=(), max_length=3
    )


class PlannedSlide(BaseModel):
    """One planned slide; payload shape is checked against the layout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    layout: SlideLayoutKind
    title: Title
    purpose: str = Field(default="", max_length=240)
    evidence_keys: tuple[Annotated[int, Field(ge=0, strict=True)], ...] = Field(
        default=(), max_length=6
    )
    payload: dict[str, object] = Field(default_factory=dict)


class DeckPlan(BaseModel):
    """The typed model output for slide planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deck_title: Title
    language: PresentationLanguage
    slides: tuple[PlannedSlide, ...] = Field(min_length=5, max_length=12)


_PAYLOAD_MODELS: dict[str, type[_PlannedPayload]] = {
    "title": PlannedTitlePayload,
    "bullets": PlannedBulletsPayload,
    "comparison": PlannedComparisonPayload,
    "metric": PlannedMetricPayload,
    "steps": PlannedStepsPayload,
    "chart": PlannedChartPayload,
    "table": PlannedTablePayload,
    "conclusion": PlannedConclusionPayload,
}


def build_slide_planning_context(
    request: PresentationRequest,
    evidence: tuple[SlideEvidenceItem, ...],
    *,
    active_skill: SkillContext | None = None,
    context_budget: ContextBudget | None = None,
) -> SlidePlanningContext:
    """Fit the planning projection under the shared deterministic budget."""

    context = SlidePlanningContext(
        presentation=PresentationBrief(
            topic=request.topic[:200],
            audience=request.audience[:120],
            language=request.language,
            slide_count=request.slide_count,
            theme=request.theme,
            instructions=request.instructions[:500],
        ),
        evidence=evidence[:MAX_EVIDENCE_ITEMS],
        active_skill=active_skill,
    )
    result = fit_context_to_budget(context, context_budget or ContextBudget())
    if not isinstance(result, SlidePlanningContext):
        raise SlideAgentValidationError("slide planning context failed to fit")
    return result


def plan_deck(
    model: StructuredChatModel,
    request: PresentationRequest,
    evidence: tuple[SlideEvidenceItem, ...],
    *,
    active_skill: SkillContext | None = None,
    context_budget: ContextBudget | None = None,
    structured_output_mode: StructuredOutputMode = "prompt_json",
    on_context_projection: Callable[[ProjectionMetadata], None] | None = None,
    on_presented_evidence: PresentedEvidenceObserver | None = None,
    repair_hint: str | None = None,
) -> DeckPlan:
    """Produce a validated DeckPlan through the production model seam.

    ``repair_hint`` carries one safe, constant conversion error from a prior
    plan so a bounded service-level re-plan can correct deterministic
    constraint violations (count/language/payload rules) without widening
    the schema or trusting model-authored retries.
    """

    planning_context = build_slide_planning_context(
        request,
        evidence,
        active_skill=active_skill,
        context_budget=context_budget,
    )
    if on_context_projection is not None and planning_context.context_projection:
        on_context_projection(planning_context.context_projection)
    if on_presented_evidence is not None:
        # These are the exact keys serialized for the model, after compaction.
        on_presented_evidence(planning_context.evidence)

    structured_model = create_structured_output_runnable(
        model,
        DeckPlan,
        method="json_mode",
        mode=structured_output_mode,
    )

    last_error: ValidationError | OutputParserException | None = None
    result: DeckPlan | None = None
    for attempt in range(SLIDE_PLANNER_MAX_ATTEMPTS):
        messages = [
            ("system", SLIDE_PLANNER_SYSTEM_PROMPT),
            ("human", render_llm_payload(planning_context)),
        ]
        if repair_hint and attempt == 0:
            messages.append(
                (
                    "human",
                    (
                        "A previous plan attempt was rejected: "
                        f"{repair_hint}. Follow the requested slide_count and "
                        "language exactly, keep every field within its bound, "
                        "match each payload to its layout, and reference only "
                        "the provided evidence keys."
                    ),
                )
            )
        if attempt > 0 and last_error is not None:
            # Bounded corrective feedback, mirroring the action selector's
            # retry contract: the retry sees only safe validation facts
            # (field locations and message codes), never raw model output.
            messages.append(
                (
                    "human",
                    (
                        "Your previous response failed validation against the "
                        "DeckPlan schema. Failures: "
                        f"{_safe_validation_summary(last_error)}. Return the "
                        "corrected JSON object only."
                    ),
                )
            )
        try:
            candidate = structured_model.invoke(messages)
            if not isinstance(candidate, DeckPlan):
                raise SlideAgentValidationError(
                    "slide planner returned an unexpected object type"
                )
            result = candidate
            break
        except (ValidationError, OutputParserException) as error:
            last_error = error
    if result is None:
        raise SlideAgentValidationError(
            "slide planner failed to return a valid DeckPlan "
            f"after {SLIDE_PLANNER_MAX_ATTEMPTS} attempts"
        ) from last_error
    return result


def _safe_validation_summary(error: ValidationError | OutputParserException) -> str:
    """Field locations and message codes only; inputs/URLs are excluded."""

    if isinstance(error, ValidationError):
        details = error.errors(include_url=False, include_input=False)
        summary = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in details[:8]
        )
        return summary[:2_000] or "schema mismatch"
    return "response was not valid JSON for the schema"


def _trim_text(value: str, floor: int) -> str | None:
    """Shorten one text item at a word boundary; None when untrimmable."""

    target = max(floor, int(len(value) * 0.85))
    if len(value) <= target:
        return None
    window = value[: target + 1]
    cut = window.rfind(" ")
    if cut < floor:
        cut = target
    trimmed = value[:cut].rstrip()
    if trimmed and len(trimmed) < len(value):
        return trimmed
    return None


def _trim_marked(value: str, floor: int) -> str | None:
    """Trim text while preserving a trailing illustrative-data marker."""

    marker = next(
        (
            suffix
            for suffix in (
                f" {_ILLUSTRATIVE_MARKER['vi']}",
                f" {_ILLUSTRATIVE_MARKER['en']}",
            )
            if value.endswith(suffix)
        ),
        None,
    )
    body = value[: -len(marker)] if marker else value
    trimmed = _trim_text(body, floor)
    if trimmed is None:
        return None
    return f"{trimmed}{marker}" if marker else trimmed


def _reduce_spec_density(spec: DeckSpec) -> DeckSpec | None:
    """Deterministically shorten overlong slide text until the deck fits.

    Render overflow with model-authored plans is common: schema bounds are
    wider than the density preflight. Trimming shortens existing words only -
    it never invents content or citations - and stops as soon as the deck
    renders within its layout budget.
    """

    current = spec
    for floor in DENSITY_TRIM_FLOORS:
        for _ in range(DENSITY_TRIM_PASSES_PER_FLOOR):
            trimmed_slides = []
            changed = False
            for slide in current.slides:
                content = slide.content
                title = slide.title
                trimmed_title = _trim_text(title, max(12, floor // 2))
                if trimmed_title is not None:
                    changed = True
                replacements: dict[str, object] = {}
                for field in ("subtitle", "insight"):
                    value = getattr(content, field, None)
                    if isinstance(value, str):
                        trimmed = (
                            _trim_marked(value, floor)
                            if field == "insight"
                            else _trim_text(value, floor)
                        )
                        if trimmed is not None:
                            replacements[field] = trimmed
                            changed = True
                items = getattr(content, "items", None)
                if isinstance(items, tuple) and items:
                    trimmed_items = tuple(
                        _trim_text(value, floor) or value
                        for value in items
                        if isinstance(value, str)
                    )
                    if items != trimmed_items:
                        replacements["items"] = trimmed_items
                        changed = True
                label = getattr(content, "label", None)
                if isinstance(label, str):
                    trimmed = _trim_text(label, max(12, floor // 2))
                    if trimmed is not None:
                        replacements["label"] = trimmed
                        changed = True
                for column in ("left", "right"):
                    member = getattr(content, column, None)
                    if member is None:
                        continue
                    column_updates: dict[str, object] = {}
                    heading = getattr(member, "heading", None)
                    if isinstance(heading, str):
                        trimmed = _trim_text(heading, max(10, floor // 3))
                        if trimmed is not None:
                            column_updates["heading"] = trimmed
                            changed = True
                    column_items = getattr(member, "items", None)
                    if isinstance(column_items, tuple) and column_items:
                        trimmed_items = tuple(
                            _trim_text(value, floor) or value
                            for value in column_items
                            if isinstance(value, str)
                        )
                        if column_items != trimmed_items:
                            column_updates["items"] = trimmed_items
                            changed = True
                    if column_updates:
                        replacements[column] = member.model_copy(update=column_updates)
                table = getattr(content, "rows", None)
                if isinstance(table, tuple) and table:
                    trimmed_rows = []
                    table_changed = False
                    for row in table:
                        trimmed_row = tuple(
                            _trim_text(str(cell), max(8, floor // 2)) or cell
                            if isinstance(cell, str)
                            else cell
                            for cell in row
                        )
                        if row != trimmed_row:
                            table_changed = True
                        trimmed_rows.append(trimmed_row)
                    if table_changed:
                        replacements["rows"] = tuple(trimmed_rows)
                        changed = True
                caption = getattr(content, "caption", None)
                if isinstance(caption, str):
                    trimmed = _trim_marked(caption, floor)
                    if trimmed is not None:
                        replacements["caption"] = trimmed
                        changed = True
                if replacements:
                    content = content.model_copy(update=replacements)
                trimmed_slides.append(
                    slide.model_copy(
                        update={
                            "content": content,
                            "title": trimmed_title or title,
                        }
                    )
                )
            if not changed:
                break
            current = spec.model_copy(update={"slides": tuple(trimmed_slides)})
            try:
                render_deck(current)
                return current
            except LayoutOverflowError:
                continue
    return None


def _require_no_urls(plan: DeckPlan) -> None:
    for slide in plan.slides:
        fields = [slide.title, slide.purpose, json.dumps(slide.payload)]
        if any(_URL_PATTERN.search(value) for value in fields):
            raise SlideAgentValidationError(
                "plan text must not contain URLs; cite evidence by key"
            )
    if _URL_PATTERN.search(plan.deck_title):
        raise SlideAgentValidationError(
            "deck title must not contain URLs; cite evidence by key"
        )


def _evidence_by_key(
    evidence: tuple[SlideEvidenceItem, ...],
) -> dict[int, SlideEvidenceItem]:
    # The compactor may drop oldest records without renumbering the surviving
    # keys. Indexing a compacted tuple by key can misattribute citations.
    entries = {item.key: item for item in evidence}
    if len(entries) != len(evidence):
        raise SlideAgentValidationError("duplicate evidence key")
    return entries


def _source_display(item: SlideEvidenceItem, language: PresentationLanguage) -> str:
    """Display a long source unambiguously without inventing a truncated URL.

    The canonical URL stays intact in the research turn's evidence and in the
    planner payload. PPTX text has a 180-character limit; use its SHA-256 as an
    exact lookup fingerprint when the whole URL cannot fit on the slide.
    """
    if len(item.canonical_url) <= 180:
        return item.canonical_url
    prefix = "Nguồn" if language == "vi" else "Source"
    digest = hashlib.sha256(item.canonical_url.encode("utf-8")).hexdigest()
    title = " ".join(item.title.split())[:70]
    return f"{prefix} #{item.key + 1} SHA256:{digest} {title}".strip()


def _resolve_sources(
    keys: tuple[int, ...],
    evidence: tuple[SlideEvidenceItem, ...],
    language: PresentationLanguage,
) -> tuple[str, ...]:
    if not evidence:
        if keys:
            raise SlideAgentValidationError(
                "evidence key was not available to the slide planner"
            )
        return (_UNCERTAINTY_SOURCE[language],)
    by_key = _evidence_by_key(evidence)
    resolved: list[str] = []
    for key in keys:
        if key not in by_key:
            raise SlideAgentValidationError(
                "evidence key was not available to the slide planner"
            )
        resolved.append(_source_display(by_key[key], language))
    if not resolved:
        return (_UNCERTAINTY_SOURCE[language],)
    # Deduplicate while preserving plan order; sources are capped at three.
    ordered: list[str] = []
    for source in resolved:
        if source not in ordered:
            ordered.append(source)
    return tuple(ordered[:3])


def _marker(language: PresentationLanguage, text: str, limit: int) -> str:
    suffix = f" {_ILLUSTRATIVE_MARKER[language]}"
    if len(text) + len(suffix) > limit:
        text = text[: limit - len(suffix)].rstrip()
    return text + suffix


def _slide_content(
    slide: PlannedSlide,
    evidence: tuple[SlideEvidenceItem, ...],
    language: PresentationLanguage,
    cited_any: bool,
):
    model = _PAYLOAD_MODELS[slide.layout]
    try:
        payload = model.model_validate(slide.payload)
    except ValidationError as error:
        raise SlideAgentValidationError(
            "planned payload does not match its layout"
        ) from error
    allowed_keys = _evidence_by_key(evidence)
    for key in slide.evidence_keys:
        if key not in allowed_keys:
            raise SlideAgentValidationError(
                "evidence key was not available to the slide planner"
            )
    match slide.layout:
        case "title":
            assert isinstance(payload, PlannedTitlePayload)
            return TitleContent(subtitle=payload.subtitle)
        case "bullets":
            assert isinstance(payload, PlannedBulletsPayload)
            return BulletsContent(items=payload.items)
        case "comparison":
            assert isinstance(payload, PlannedComparisonPayload)
            return ComparisonContent(
                left=Column(heading=payload.left.heading, items=payload.left.items),
                right=Column(heading=payload.right.heading, items=payload.right.items),
            )
        case "metric":
            assert isinstance(payload, PlannedMetricPayload)
            insight = payload.insight
            if _DIGIT_PATTERN.search(payload.value):
                insight = _marker(language, insight, 180)
            return MetricContent(
                value=payload.value, label=payload.label, insight=insight
            )
        case "steps":
            assert isinstance(payload, PlannedStepsPayload)
            return StepsContent(items=payload.items)
        case "chart":
            assert isinstance(payload, PlannedChartPayload)
            return ChartContent(
                chart_type=payload.chart_type,
                categories=payload.categories,
                series=tuple(
                    ChartSeries(name=item.name, values=item.values)
                    for item in payload.series
                ),
                caption=_marker(language, payload.caption, 180),
            )
        case "table":
            assert isinstance(payload, PlannedTablePayload)
            caption = payload.caption
            if any(
                _DIGIT_PATTERN.search(str(cell)) for row in payload.rows for cell in row
            ):
                caption = _marker(language, caption, 180)
            return TableContent(
                headers=payload.headers, rows=payload.rows, caption=caption
            )
        case "conclusion":
            assert isinstance(payload, PlannedConclusionPayload)
            if cited_any:
                sources = _resolve_sources(payload.source_keys, evidence, language)
            else:
                sources = (_UNCERTAINTY_SOURCE[language],)
            return ConclusionContent(items=payload.items, sources=sources)
    raise SlideAgentValidationError("unsupported slide layout")


def build_deck_spec(
    plan: DeckPlan,
    request: PresentationRequest,
    evidence: tuple[SlideEvidenceItem, ...],
) -> DeckSpec:
    """Convert a validated plan into a strict production DeckSpec.

    Deterministic and fail-closed: request/plan agreement, URL-free text,
    in-range evidence keys, and exact layout payloads are all enforced here
    before the production schema sees anything.
    """

    if plan.language != request.language:
        raise SlideAgentValidationError("plan language does not match the request")
    if len(plan.slides) != request.slide_count:
        raise SlideAgentValidationError("plan slide count does not match the request")
    _require_no_urls(plan)
    cited_any = any(item.is_cited for item in evidence)
    slides = tuple(
        SlideSpec(
            id=f"slide-{index + 1}",
            title=slide.title,
            content=_slide_content(slide, evidence, plan.language, cited_any),
        )
        for index, slide in enumerate(plan.slides)
    )
    try:
        return DeckSpec.model_validate(
            {
                "title": plan.deck_title,
                "theme": request.theme,
                "slides": [s.model_dump() for s in slides],
            }
        )
    except ValidationError as error:
        raise SlideAgentValidationError("plan failed DeckSpec validation") from error


@dataclass(frozen=True, slots=True)
class SlideGenerationResult:
    """Safe generation outcome: immutable record plus the source turn view."""

    record: PptxArtifact
    view: DemoRunView
    spec: DeckSpec
    context_metadata: ProjectionMetadata | None


@runtime_checkable
class SlideSourceService(Protocol):
    """Structural seam satisfied by ``DemoRuntimeService`` in either mode."""

    @property
    def backend(self) -> object: ...

    async def load_conversation(self, thread_id: str) -> object: ...


class SlideAgentService:
    """Orchestrate planning -> rendering -> validation -> publication."""

    def __init__(
        self,
        source: SlideSourceService,
        store: object,
        *,
        planner_model_factory: Callable[[], StructuredChatModel],
        structured_output_mode: StructuredOutputMode = "prompt_json",
        context_budget: ContextBudget | None = None,
    ) -> None:
        if not callable(planner_model_factory):
            raise TypeError("planner_model_factory must be callable")
        self._source = source
        self._store = store
        self._planner_model_factory = planner_model_factory
        self._structured_output_mode = structured_output_mode
        self._context_budget = context_budget

    async def generate(
        self,
        thread_id: str,
        turn_id: str,
        request: PresentationRequest,
    ) -> SlideGenerationResult:
        """Generate and publish one deck for a completed, persisted turn."""

        from mini_deerflow.pptx.artifacts import (  # local: avoid import cycle
            ArtifactConflictError,
            PptxArtifactStore,
        )

        if not isinstance(self._store, PptxArtifactStore):
            raise SlideAgentError("presentation store is not a PptxArtifactStore")
        session = await self._source.load_conversation(thread_id)
        turn = next((item for item in session.turns if item.turn_id == turn_id), None)
        if turn is None or turn.run is None:
            raise SlideAgentError("Presentation source turn was not found.")
        if turn.status != "completed":
            raise SlideAgentError("Source turn is not completed.")

        turn_record = await self._store.get_turn(thread_id, turn_id)
        active_skill = self._pinned_skill(turn_record.run_context_json)
        # Keep the newest evidence records; clip display-only fields. Never
        # truncate a canonical URL into a potentially false source identity.
        retained = turn.run.evidence[-MAX_EVIDENCE_ITEMS:]
        # Do not truncate canonical URLs: that could invent a different
        # citation. Omit URLs too long for the evidence contract instead.
        evidence = tuple(
            SlideEvidenceItem(
                key=index,
                canonical_url=item.canonical_url,
                title=item.title[:120],
                excerpt=item.excerpt[:400],
                is_cited=item.is_cited,
            )
            for index, item in enumerate(retained)
            if 0 < len(item.canonical_url) <= 300
        )

        metadata: ProjectionMetadata | None = None
        presented_evidence: tuple[SlideEvidenceItem, ...] | None = None

        def observe(projection: ProjectionMetadata) -> None:
            nonlocal metadata
            metadata = projection

        def observe_evidence(projected: tuple[SlideEvidenceItem, ...]) -> None:
            nonlocal presented_evidence
            presented_evidence = projected

        def invoke_planner() -> tuple[DeckSpec, bytes, ProjectionMetadata | None]:
            """Plan -> convert -> render -> validate with one bounded repair.

            A plan that parses and converts can still exceed the renderer's
            density preflight (model-natural text lengths within schema
            bounds). Any deterministic post-plan rejection - conversion,
            layout overflow or structural validation - triggers exactly one
            hint-guided re-plan using our safe constant messages. The
            renderer/validator density contract itself stays untouched.
            """
            model = self._planner_model_factory()
            repair_hint: str | None = None
            for _ in range(SLIDE_PLAN_REPAIR_ATTEMPTS):
                plan = plan_deck(
                    model,
                    request,
                    evidence,
                    active_skill=active_skill,
                    context_budget=self._context_budget,
                    structured_output_mode=self._structured_output_mode,
                    on_context_projection=observe,
                    on_presented_evidence=observe_evidence,
                    repair_hint=repair_hint,
                )
                if presented_evidence is None:
                    raise SlideAgentValidationError(
                        "planner evidence projection missing"
                    )
                try:
                    spec = build_deck_spec(plan, request, presented_evidence)
                    data = render_deck(spec)
                    validate_pptx(data, spec, min_layouts=4)
                    return spec, data, metadata
                except LayoutOverflowError:
                    # Schema-legal but too dense: deterministically shorten
                    # existing text (no new model call, no invented content)
                    # before considering a hint-guided re-plan.
                    reduced = _reduce_spec_density(spec)
                    if reduced is not None:
                        data = render_deck(reduced)
                        validate_pptx(data, reduced, min_layouts=4)
                        return reduced, data, metadata
                    repair_hint = (
                        "content density exceeds layout; shorten every "
                        "title and text item well below their character "
                        "limits"
                    )
                except SlideAgentValidationError as error:
                    # Deterministic constraint violation: one bounded,
                    # hint-guided re-plan (safe constant messages only).
                    repair_hint = str(error)
            raise SlideAgentValidationError(
                "planned deck violated structural constraints "
                f"after {SLIDE_PLAN_REPAIR_ATTEMPTS} attempts"
            )

        try:
            spec, data, metadata = await asyncio.to_thread(invoke_planner)
            record = await self._store.publish(thread_id, turn_id, data, spec)
        except SlideAgentError:
            raise
        except ArtifactConflictError as error:
            raise SlideAgentConflictError(
                "A different presentation already belongs to this turn."
            ) from error
        except (PptxValidationError, LayoutOverflowError) as error:
            raise SlideAgentValidationError(
                "Generated presentation failed validation."
            ) from error
        except ValueError as error:
            raise SlideAgentValidationError(
                "Slide planning or conversion failed validation."
            ) from error
        return SlideGenerationResult(
            record=record, view=turn.run, spec=spec, context_metadata=metadata
        )

    @staticmethod
    def _pinned_skill(run_context_json: str | None) -> SkillContext | None:
        """Fail closed if the pinned snapshot is missing or malformed."""

        import json as _json

        if not run_context_json:
            return None
        try:
            pinned = _json.loads(run_context_json)
            snapshot = pinned.get("active_skill")
            return SkillContext.model_validate(snapshot) if snapshot else None
        except (ValueError, ValidationError) as error:
            raise SlideAgentError("Pinned skill snapshot failed validation.") from error


__all__ = [
    "DeckPlan",
    "PresentationRequest",
    "SlideAgentConflictError",
    "SlideAgentError",
    "SlideAgentService",
    "SlideAgentValidationError",
    "SlideEvidenceItem",
    "SlideGenerationResult",
    "SlidePlanningContext",
    "SlideSourceService",
    "build_deck_spec",
    "build_slide_planning_context",
    "plan_deck",
]
