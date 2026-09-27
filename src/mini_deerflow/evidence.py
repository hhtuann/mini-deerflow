import re
from collections.abc import Sequence
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from mini_deerflow.actions import ToolObservation

MAX_EVIDENCE_RECORDS = 50
MAX_EVIDENCE_EXCERPT_CHARS = 20_000

EvidenceText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_EVIDENCE_EXCERPT_CHARS,
    ),
]

_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)
_MARKDOWN_LINK_PATTERN = re.compile(
    r"\[([^\]]+)\]\(https?://[^)]+\)",
    re.IGNORECASE,
)
_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\s*\[([^\]]*)\]\(https?://[^)]+\)",
    re.IGNORECASE,
)
_HTTP_URL_PATTERN = re.compile(
    r"https?://[^\s<>()]+",
    re.IGNORECASE,
)
_INTERNAL_PREFIX_PATTERN = re.compile(
    r"^(?:step|review)\s+\d+\s*[:\-\u2013\u2014]\s*",
    re.IGNORECASE,
)
_INTERNAL_PROGRESS_PATTERN = re.compile(
    r"^(?:completed|finished)\s+(?:evidence-backed\s+)?research\s+"
    r"step(?:\s+number)?\s+\d+[.!]?$",
    re.IGNORECASE,
)
_INTERNAL_METADATA_PATTERN = re.compile(
    r"\b(?:tool\s+calls?|review\s+cycles?|review\s+cycle\s+\d+|"
    r"replan\s+cycles?|plan\s+step|step\s+\d+|observation\s+\d+|"
    r"provenance|execution\s+(?:trace|report)|budget\s+(?:limit|counter))\b",
    re.IGNORECASE,
)
_INTERNAL_EXECUTION_PATTERN = re.compile(
    r"^\s*(?:(?:b\u01b0\u1edbc|step)\s*\d+\b|(?:branch|nh\u00e1nh)\b|"
    r"(?:fetch|search)\b.*(?:status\s*(?:=\s*)?\d{3}|(?:th\u00e0nh c\u00f4ng|successful|succeeded))|"
    r"(?:failed\s+collection|url\s+(?:verified|verification|omitted)|"
    r"unverified\s+url\s+omitted|observations?|tool\s*(?:calls?|execution)|"
    r"review\s*cycles?|workflow\s*step|agent\s*step|agent\s*trace|"
    r"execution\s*trace|search\s*(?:query|snippet)|provenance)\b|"
    r"ghi\s+ch\u00fa\s+cho\s+b\u01b0\u1edbc\s+sau|"
    r"(?:completed|finished)\s+(?:research\s+)?step\b|"
    r"(?:b\u01b0\u1edbc\s*\d+\s+ho\u00e0n\s+t\u1ea5t|\u0111\u00e3\s+tr\u00edch\s+xu\u1ea5t\s+\u0111\u1ea7y\s+\u0111\u1ee7|"
    r"\u0111\u1ed1i\s+chi\u1ebfu\s+ho\u00e0n\s+t\u1ea5t)\b)",
    re.IGNORECASE,
)
_CONCLUSION_MARKER_PATTERN = re.compile(
    r"(?:^|[.!?]\s+)(?:k\u1ebft\s+lu\u1eadn(?:\s+(?:cho|v\u1ec1)[^:]{0,80})?|"
    r"conclusion(?:\s+for[^:]{0,80})?)\s*:\s*",
    re.IGNORECASE,
)
_DISCREPANCY_PATTERN = re.compile(
    r"(?:m\u00e2u\s+thu\u1eabn|ch\u00eanh\s+l\u1ec7ch|kh\u00e1c\s+bi\u1ec7t|"
    r"kh\u00f4ng\s+ph\u1ea3i\s+m\u00e2u\s+thu\u1eabn|scope|ph\u1ea1m\s+vi)",
    re.IGNORECASE,
)
_ANSWER_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?;])\s+")
_MAX_ANSWER_POINT_CHARS = 240
_RESEARCH_REPORT_SECTIONS = (
    "## Goal",
    "## Findings",
    "## Evidence",
    "## Citations",
    "## Review conclusions",
    "## Gaps and limitations",
    "## Execution",
)


def canonicalize_url(value: str | HttpUrl) -> str:
    """Validate and canonicalize one HTTP(S) URL for identity comparisons."""

    validated = _HTTP_URL_ADAPTER.validate_python(value)
    parsed = urlsplit(str(validated))

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")

    hostname = parsed.hostname

    if hostname is None:
        raise ValueError("URL must include a hostname")

    port = parsed.port
    default_port = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )
    rendered_port = "" if port is None or default_port else f":{port}"
    normalized_hostname = hostname.lower()
    rendered_hostname = (
        f"[{normalized_hostname}]"
        if ":" in normalized_hostname
        else normalized_hostname
    )
    netloc = f"{rendered_hostname}{rendered_port}"

    return urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


class EvidenceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )


class EvidenceProvenance(EvidenceModel):
    tool_name: Literal["web_search", "web_fetch"]
    step_number: int = Field(ge=1, le=7)
    step_tool_call_number: int = Field(ge=1)
    total_tool_call_number: int = Field(ge=1)
    observation_index: int = Field(ge=1)
    delegation_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{0,31}$",
        exclude_if=lambda value: value is None,
    )
    branch_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{0,31}$",
        exclude_if=lambda value: value is None,
    )
    branch_tool_call_number: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_call_numbers(self) -> Self:
        if self.step_tool_call_number > self.total_tool_call_number:
            raise ValueError(
                "step_tool_call_number cannot exceed total_tool_call_number"
            )

        return self


class EvidenceRecord(EvidenceModel):
    """One citable web result derived from a successful tool observation."""

    url: str
    source_tool: Literal["web_search", "web_fetch"]
    title: str | None = Field(default=None, max_length=500)
    excerpt: EvidenceText
    status: Literal["success"] = "success"
    provenance: EvidenceProvenance

    @field_validator("url", mode="before")
    @classmethod
    def canonicalize_record_url(cls, value: object) -> str:
        if not isinstance(value, str | HttpUrl):
            raise TypeError("evidence URL must be a string or HttpUrl")

        return canonicalize_url(value)

    @model_validator(mode="after")
    def validate_tool_provenance(self) -> Self:
        if self.source_tool != self.provenance.tool_name:
            raise ValueError("source_tool must match provenance.tool_name")

        return self

    @property
    def canonical_url(self) -> str:
        return self.url


class StepFinding(EvidenceModel):
    step_number: int = Field(ge=1, le=7)
    summary: str = Field(min_length=1, max_length=4_000)
    citations: list[str] = Field(default_factory=list, max_length=20)
    branch_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{0,31}$",
        exclude_if=lambda value: value is None,
    )

    @field_validator("citations", mode="before")
    @classmethod
    def canonicalize_citations(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise TypeError("citations must be a list")

        return [canonicalize_url(citation) for citation in value]


class AnswerClaim(EvidenceModel):
    """One user-facing claim with only accepted source membership."""

    text: str = Field(min_length=1, max_length=1_000)
    citations: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("citations", mode="before")
    @classmethod
    def canonicalize_claim_citations(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise TypeError("citations must be a list")

        return [canonicalize_url(citation) for citation in value]


class UserFacingAnswer(EvidenceModel):
    """Structured public response, kept separate from the execution trace."""

    summary: list[AnswerClaim] = Field(default_factory=list, max_length=5)
    evidence: list[AnswerClaim] = Field(default_factory=list, max_length=12)
    discrepancies: list[AnswerClaim] = Field(default_factory=list, max_length=6)
    limitations: list[str] = Field(default_factory=list, max_length=6)
    sources: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_RECORDS)


def extract_evidence_records(
    observation: ToolObservation,
) -> list[EvidenceRecord]:
    """Extract only successful, schema-shaped web observations as evidence."""

    if not observation.result.success or not isinstance(
        observation.result.data,
        dict,
    ):
        return []

    if observation.action.tool_name == "web_search":
        provenance = _provenance_for(observation, "web_search")
        raw_results = observation.result.data.get("results")

        if not isinstance(raw_results, list):
            return []

        records: list[EvidenceRecord] = []

        for raw_result in raw_results:
            if not isinstance(raw_result, dict):
                continue

            url = raw_result.get("url")
            title = raw_result.get("title")
            snippet = raw_result.get("snippet")

            if not isinstance(url, str) or not isinstance(title, str):
                continue

            excerpt = snippet if isinstance(snippet, str) and snippet.strip() else title

            try:
                records.append(
                    EvidenceRecord(
                        url=url,
                        source_tool="web_search",
                        title=title,
                        excerpt=excerpt[:MAX_EVIDENCE_EXCERPT_CHARS],
                        provenance=provenance,
                    )
                )
            except (TypeError, ValueError):
                continue

        return records

    if observation.action.tool_name == "web_fetch":
        provenance = _provenance_for(observation, "web_fetch")
        url = observation.result.data.get("url")
        content = observation.result.data.get("content")
        title = observation.result.data.get("title")

        if (
            not isinstance(url, str)
            or not isinstance(content, str)
            or not content.strip()
        ):
            return []

        if title is not None and not isinstance(title, str):
            return []

        try:
            return [
                EvidenceRecord(
                    url=url,
                    source_tool="web_fetch",
                    title=title,
                    excerpt=content[:MAX_EVIDENCE_EXCERPT_CHARS],
                    provenance=provenance,
                )
            ]
        except (TypeError, ValueError):
            return []

    return []


def _provenance_for(
    observation: ToolObservation,
    tool_name: Literal["web_search", "web_fetch"],
) -> EvidenceProvenance:
    return EvidenceProvenance(
        tool_name=tool_name,
        step_number=observation.step_number,
        step_tool_call_number=observation.step_tool_call_number,
        total_tool_call_number=observation.total_tool_call_number,
        observation_index=observation.total_tool_call_number,
        delegation_id=observation.delegation_id,
        branch_id=observation.branch_id,
        branch_tool_call_number=observation.branch_tool_call_number,
    )


def merge_evidence_records(
    current: list[EvidenceRecord],
    additions: list[EvidenceRecord],
) -> list[EvidenceRecord]:
    """Merge bounded evidence by canonical URL, preferring newer observations."""

    merged = list(current)
    positions = {record.canonical_url: index for index, record in enumerate(merged)}

    for record in additions:
        position = positions.get(record.canonical_url)

        if position is None:
            positions[record.canonical_url] = len(merged)
            merged.append(record)
        else:
            merged[position] = record

    return merged[-MAX_EVIDENCE_RECORDS:]


def merge_citation_sources(
    current: list[str],
    additions: list[str],
) -> list[str]:
    """Merge canonical citation URLs without allowing unbounded state growth."""

    merged: list[str] = []

    for source in [*current, *additions]:
        try:
            canonical = canonicalize_url(source)
        except (TypeError, ValueError):
            continue

        if canonical not in merged:
            merged.append(canonical)

    return merged[-MAX_EVIDENCE_RECORDS:]


def validate_citations(
    requested_sources: list[HttpUrl],
    evidence: list[EvidenceRecord],
) -> tuple[list[str], int]:
    """Return evidence-backed canonical citations and a rejected count."""

    citable = {
        record.canonical_url for record in evidence if record.status == "success"
    }
    accepted: list[str] = []
    rejected_count = 0

    for source in requested_sources:
        try:
            canonical = canonicalize_url(source)
        except (TypeError, ValueError):
            rejected_count += 1
            continue

        if canonical not in citable:
            rejected_count += 1
        elif canonical not in accepted:
            accepted.append(canonical)

    return accepted, rejected_count


def sanitize_finding_summary(summary: str) -> str:
    """Remove model-authored URLs; validated citations are rendered separately."""

    without_images = _MARKDOWN_IMAGE_PATTERN.sub(r"\1", summary)
    without_links = _MARKDOWN_LINK_PATTERN.sub(r"\1", without_images)
    without_urls = _HTTP_URL_PATTERN.sub("[unverified URL omitted]", without_links)
    return " ".join(without_urls.split())


def _public_markdown_text(value: str) -> str:
    """Render model-authored prose as Markdown text, never Markdown syntax."""

    sanitized = sanitize_finding_summary(value)
    sanitized = _INTERNAL_PREFIX_PATTERN.sub("", sanitized)
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "#"):
        sanitized = sanitized.replace(character, f"\\{character}")
    return sanitized


def _answer_points(statement: str) -> list[str]:
    """Break a model-authored finding into scan-friendly, complete points.

    The public answer preserves the original sanitized text. This helper only
    introduces line breaks at sentence boundaries or between words when a
    source summary is unusually long.
    """

    sentences = [
        sentence.strip()
        for sentence in _ANSWER_SENTENCE_BOUNDARY.split(statement)
        if sentence.strip()
    ]
    points: list[str] = []

    for sentence in sentences:
        if len(sentence) <= _MAX_ANSWER_POINT_CHARS:
            points.append(sentence)
            continue

        words = sentence.split()
        chunk: list[str] = []
        chunk_length = 0
        for word in words:
            if len(word) > _MAX_ANSWER_POINT_CHARS:
                if chunk:
                    points.append(" ".join(chunk))
                    chunk = []
                    chunk_length = 0
                points.extend(_split_overlong_answer_word(word))
                continue

            next_length = chunk_length + len(word) + (1 if chunk else 0)
            if chunk and next_length > _MAX_ANSWER_POINT_CHARS:
                points.append(" ".join(chunk))
                chunk = [word]
                chunk_length = len(word)
            else:
                chunk.append(word)
                chunk_length = next_length
        if chunk:
            points.append(" ".join(chunk))

    return points


def _split_overlong_answer_word(word: str) -> list[str]:
    """Split an unspaced value without leaving a trailing Markdown escape."""

    parts: list[str] = []
    remaining = word
    while len(remaining) > _MAX_ANSWER_POINT_CHARS:
        end = _MAX_ANSWER_POINT_CHARS
        if remaining[end - 1] == "\\":
            end -= 1
        parts.append(remaining[:end])
        remaining = remaining[end:]
    if remaining:
        parts.append(remaining)
    return parts


def _claim_key(value: str) -> str:
    """Return a stable key for deterministic, conservative claim deduplication."""

    return " ".join(re.findall(r"\w+", value.casefold()))


def _is_internal_execution_text(value: str) -> bool:
    """Recognize progress/debug prose that must remain in the trace only."""

    return bool(
        _INTERNAL_PROGRESS_PATTERN.fullmatch(value)
        or _INTERNAL_EXECUTION_PATTERN.search(value)
        or "[unverified url omitted]" in value.casefold()
    )


def contains_execution_metadata(value: str) -> bool:
    """Detect whether every meaningful legacy-answer point is execution metadata."""

    points = _legacy_answer_points(value)
    return bool(points) and all(_is_internal_execution_text(point) for point in points)


def has_execution_metadata(value: str) -> bool:
    """Detect mixed legacy answers that need trace points removed before display."""

    return any(_is_internal_execution_text(point) for point in _legacy_answer_points(value))


def legacy_public_answer_points(value: str) -> list[str]:
    """Retain only non-trace points from a pre-structured persisted answer."""

    return [
        _public_markdown_text(point)
        for point in _legacy_answer_points(value)
        if not _is_internal_execution_text(point)
    ]


def _legacy_answer_points(value: str) -> list[str]:
    points: list[str] = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped or re.fullmatch(r"#{1,6}\s*[^#]+", stripped):
            continue
        points.extend(_answer_points(sanitize_finding_summary(stripped)))
    return points


def _conclusion_fragments(value: str) -> list[str]:
    """Extract explicit model conclusions without exposing the execution lead-in."""

    matches = list(_CONCLUSION_MARKER_PATTERN.finditer(value))
    if not matches:
        return []

    fragments: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        fragment = value[match.end() : end].strip()
        if fragment:
            fragments.append(fragment)
    return fragments


def _merge_claims(claims: list[AnswerClaim]) -> list[AnswerClaim]:
    """Merge exact or nested claims and retain every accepted citation."""

    merged: list[AnswerClaim] = []
    keys: list[str] = []
    for claim in claims:
        key = _claim_key(claim.text)
        if not key:
            continue
        match_index = next(
            (
                index
                for index, existing_key in enumerate(keys)
                if key == existing_key
                or (
                    min(len(key), len(existing_key)) >= 40
                    and (key in existing_key or existing_key in key)
                )
            ),
            None,
        )
        if match_index is None:
            keys.append(key)
            merged.append(claim)
            continue

        existing = merged[match_index]
        citations = list(dict.fromkeys([*existing.citations, *claim.citations]))
        text = claim.text if len(claim.text) < len(existing.text) else existing.text
        merged[match_index] = AnswerClaim(text=text, citations=citations)
        keys[match_index] = _claim_key(text)
    return merged


def _claims_from_finding(
    finding: StepFinding,
    *,
    evidence_by_url: dict[str, EvidenceRecord],
) -> tuple[list[AnswerClaim], list[AnswerClaim]]:
    """Separate explicit conclusions from supporting public claims."""

    citations = list(
        dict.fromkeys(
            str(citation)
            for citation in finding.citations
            if str(citation) in evidence_by_url
        )
    )
    raw = sanitize_finding_summary(finding.summary)
    raw = _INTERNAL_PREFIX_PATTERN.sub("", raw).strip()
    conclusion_fragments = _conclusion_fragments(raw)
    summary: list[AnswerClaim] = []
    evidence: list[AnswerClaim] = []

    def collect(value: str, target: list[AnswerClaim]) -> None:
        if not citations:
            return
        for point in _answer_points(value):
            if _is_internal_execution_text(point):
                continue
            text = _public_markdown_text(point)
            if text:
                target.append(AnswerClaim(text=text, citations=citations))

    for fragment in conclusion_fragments:
        collect(fragment, summary)
    if not conclusion_fragments:
        collect(raw, evidence)

    return summary, evidence


def build_user_facing_answer(
    *,
    findings: list[StepFinding],
    evidence: list[EvidenceRecord],
    review_notes: Sequence[str] = (),
    has_collection_failures: bool = False,
) -> UserFacingAnswer:
    """Normalize research state into a concise answer and a separate trace-safe view.

    The formatter is deterministic: it never calls the model, never invents a
    source, and only carries citations which already refer to successful web
    evidence. Execution metadata remains available through ``research_report``
    and the demo trace rather than leaking into the main answer.
    """

    evidence_by_url = {
        record.canonical_url: record
        for record in evidence
        if record.status == "success"
    }
    summary: list[AnswerClaim] = []
    supporting: list[AnswerClaim] = []
    for finding in findings:
        extracted_summary, extracted_evidence = _claims_from_finding(
            finding,
            evidence_by_url=evidence_by_url,
        )
        summary.extend(extracted_summary)
        supporting.extend(extracted_evidence)

    summary = _merge_claims(summary)
    supporting = _merge_claims(supporting)
    if not summary and supporting:
        summary, supporting = supporting[:2], supporting[2:]

    summary_keys = {_claim_key(claim.text) for claim in summary}
    supporting = [
        claim for claim in supporting if _claim_key(claim.text) not in summary_keys
    ]
    discrepancies = [
        claim for claim in supporting if _DISCREPANCY_PATTERN.search(claim.text)
    ]
    evidence_claims = [
        claim for claim in supporting if claim not in discrepancies
    ]
    sources = list(
        dict.fromkeys(
            citation
            for claim in [*summary, *evidence_claims, *discrepancies]
            for citation in claim.citations
        )
    )
    if not sources and not summary and not evidence_claims and not discrepancies:
        # A resumed legacy turn may contain only successful evidence plus
        # progress summaries. Keep its collected source list visible without
        # manufacturing a claim from that execution metadata.
        sources = list(
            dict.fromkeys(
                str(citation)
                for finding in findings
                for citation in finding.citations
                if str(citation) in evidence_by_url
            )
        )

    limitations = [
        _public_markdown_text(note)
        for note in review_notes
        if note.strip() and not _is_internal_execution_text(note)
    ]
    limitations = list(dict.fromkeys(limitations))
    if has_collection_failures and not limitations and not summary and not evidence_claims:
        limitations.append(
            "Không thể thu thập đủ bằng chứng công khai để trả lời chắc chắn câu hỏi này."
        )

    return UserFacingAnswer(
        summary=summary[:5],
        evidence=evidence_claims[:12],
        discrepancies=discrepancies[:6],
        limitations=limitations[:6],
        sources=sources,
    )


def _render_claim(claim: AnswerClaim, citation_numbers: dict[str, int]) -> str:
    markers = "".join(
        f"[{citation_numbers[url]}]"
        for url in claim.citations
        if url in citation_numbers
    )
    return f"{claim.text} {markers}".strip()


def render_user_answer(
    *,
    findings: list[StepFinding],
    evidence: list[EvidenceRecord],
    review_notes: Sequence[str] = (),
    has_collection_failures: bool = False,
) -> str:
    """Render the structured public response as backward-compatible Markdown."""

    answer = build_user_facing_answer(
        findings=findings,
        evidence=evidence,
        review_notes=review_notes,
        has_collection_failures=has_collection_failures,
    )
    evidence_by_url = {
        record.canonical_url: record
        for record in evidence
        if record.status == "success"
    }
    citation_numbers = {
        url: index for index, url in enumerate(answer.sources, start=1)
    }
    lines = ["# Kết quả", "", "## Kết luận", ""]

    if answer.summary:
        lines.extend(_render_claim(claim, citation_numbers) for claim in answer.summary)
    else:
        lines.append(
            "Chưa có đủ bằng chứng công khai để đưa ra kết luận trực tiếp cho câu hỏi này."
        )

    if answer.evidence:
        lines.extend(["", "## Bằng chứng & đối chiếu", ""])
        lines.extend(
            f"- {_render_claim(claim, citation_numbers)}"
            for claim in answer.evidence
        )

    if answer.discrepancies:
        lines.extend(["", "## Khác biệt giữa các nguồn", ""])
        lines.extend(
            f"- {_render_claim(claim, citation_numbers)}"
            for claim in answer.discrepancies
        )

    if answer.limitations:
        lines.extend(["", "## Độ tin cậy / hạn chế", ""])
        lines.extend(f"> {limitation}" for limitation in answer.limitations)

    if answer.sources:
        lines.extend(["", "## Sources", ""])
        for url in answer.sources:
            record = evidence_by_url[url]
            title = _public_markdown_text(record.title or "Public source")
            lines.append(f"{citation_numbers[url]}. [{title}]({url})")

    return "\n".join(lines).strip()


def is_research_report(value: object) -> bool:
    """Recognize only this project's deterministic internal report format."""

    return (
        isinstance(value, str)
        and value.startswith("# Research Report\n\n## Goal\n")
        and all(f"\n{section}\n" in value for section in _RESEARCH_REPORT_SECTIONS)
    )


def render_research_report(
    *,
    goal: str,
    findings: list[StepFinding],
    evidence: list[EvidenceRecord],
    errors: list[str],
    total_tool_calls: int,
    successful_tool_calls: int,
    failed_tool_calls: int,
    review_conclusions: Sequence[str] = (),
    review_cycles: int = 0,
    replan_cycles: int = 0,
) -> str:
    """Render a deterministic Markdown artifact from validated state only."""

    citation_records = {
        record.canonical_url: record
        for record in evidence
        if record.status == "success"
    }
    cited_urls = list(
        dict.fromkeys(
            str(citation)
            for finding in findings
            for citation in finding.citations
            if str(citation) in citation_records
        )
    )
    citation_numbers = {url: index for index, url in enumerate(cited_urls, start=1)}

    finding_lines: list[str] = []

    for finding in findings:
        valid_urls = [
            str(citation)
            for citation in finding.citations
            if str(citation) in citation_records
        ]
        markers = " ".join(f"[{citation_numbers[url]}]" for url in valid_urls)
        label = "verified" if valid_urls else "unsupported"
        suffix = f" {markers}" if markers else ""
        finding_lines.append(
            f"- **Step {finding.step_number} ({label}):** "
            f"{sanitize_finding_summary(finding.summary)}{suffix}"
        )

    if not finding_lines:
        finding_lines.append("- No plan step was completed.")

    evidence_lines: list[str] = []

    for record in evidence:
        provenance = record.provenance
        title = sanitize_finding_summary(record.title or "Untitled source")
        excerpt = sanitize_finding_summary(record.excerpt)
        evidence_lines.extend(
            [
                f"- **{title}** — {record.canonical_url}",
                f"  - Excerpt: {excerpt}",
                (
                    "  - Provenance: "
                    f"`{provenance.tool_name}`; step {provenance.step_number}; "
                    f"tool call {provenance.total_tool_call_number}; "
                    f"observation {provenance.observation_index}."
                ),
            ]
        )

    if not evidence_lines:
        evidence_lines.append("- No successful web evidence was collected.")

    citation_lines: list[str] = []

    for url in cited_urls:
        record = citation_records[url]
        provenance = record.provenance
        citation_lines.append(
            f"{citation_numbers[url]}. [{url}]({url}) — "
            f"`{provenance.tool_name}`, step {provenance.step_number}, "
            f"tool call {provenance.total_tool_call_number}, "
            f"observation {provenance.observation_index}."
        )

    if not citation_lines:
        citation_lines.append("- No validated citations were used.")

    review_lines = list(review_conclusions)

    if not review_lines:
        review_lines.append("- No review verdicts were recorded.")

    gap_lines = [f"- {error}" for error in errors]
    gap_lines.extend(
        f"- Step {finding.step_number} has no validated web citation."
        for finding in findings
        if not finding.citations
    )

    if not gap_lines:
        gap_lines.append("- No known evidence gaps were recorded.")

    return "\n".join(
        [
            "# Research Report",
            "",
            "## Goal",
            "",
            goal,
            "",
            "## Findings",
            "",
            *finding_lines,
            "",
            "## Evidence",
            "",
            *evidence_lines,
            "",
            "## Citations",
            "",
            *citation_lines,
            "",
            "## Review conclusions",
            "",
            *review_lines,
            "",
            "## Gaps and limitations",
            "",
            *gap_lines,
            "",
            "## Execution",
            "",
            f"- Tool calls: {total_tool_calls}",
            f"- Successful tool calls: {successful_tool_calls}",
            f"- Failed tool calls: {failed_tool_calls}",
            f"- Review cycles: {review_cycles}",
            f"- Replan cycles: {replan_cycles}",
        ]
    )
