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
_HTTP_URL_PATTERN = re.compile(
    r"https?://[^\s<>()]+",
    re.IGNORECASE,
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

    @field_validator("citations", mode="before")
    @classmethod
    def canonicalize_citations(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise TypeError("citations must be a list")

        return [canonicalize_url(citation) for citation in value]


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

    without_links = _MARKDOWN_LINK_PATTERN.sub(r"\1", summary)
    without_urls = _HTTP_URL_PATTERN.sub("[unverified URL omitted]", without_links)
    return " ".join(without_urls.split())


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
