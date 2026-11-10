"""Local lexical retrieval over persisted chunks with verifiable citations.

D3 baseline (issue #49): pure-Python BM25 over the ``document_chunks``
rows already persisted by D2 — no FTS5 dependency, no derived index to
desynchronize, no MATCH-injection surface. Retrieval is version-pinned to
the latest version of each active selected document whose extraction is
``completed``; superseded, deleted, unextracted, or other-workspace chunks
are never returned. Citations are ``(document_id, version, chunk_id)``
triples re-validated against the persisted rows on every resolution.
Queries are data, never SQL or matcher syntax.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mini_deerflow.document_extraction import DocumentChunk, SourceLocator

PREVIEW_CHARS = 200
BM25_K1 = 1.5
BM25_B = 0.75

_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


class RetrievalError(RuntimeError):
    """Base retrieval failure; messages are closed prose."""


class RetrievalValidationError(ValueError):
    """Caller input violates the retrieval contract."""


class RetrievalDeadlineExceededError(RetrievalError):
    """Server-side scan budget exhausted; not a client-input error."""

    def __init__(self) -> None:
        super().__init__("Retrieval deadline was exceeded.")


class CitationNotFoundError(RetrievalError):
    """Citation target does not exist for this caller's exact scope."""

    def __init__(self) -> None:
        super().__init__("Citation was not found.")


class CitationStaleError(RetrievalError):
    """Citation refers to a superseded document version."""

    def __init__(self) -> None:
        super().__init__("Citation refers to a stale document version.")


class CitationIntegrityError(RetrievalError):
    """Persisted citation data failed allowlist validation."""

    def __init__(self) -> None:
        super().__init__("Citation data failed verification.")


@dataclass(frozen=True)
class RetrievalLimits:
    """Bounded retrieval budgets; positive integers, finite deadline."""

    max_hits: int = 8
    max_context_chars: int = 12_000
    max_query_chars: int = 400
    deadline_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in ("max_hits", "max_context_chars", "max_query_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, (int, float))
            or not math.isfinite(self.deadline_seconds)
            or self.deadline_seconds < 0
        ):
            raise ValueError("deadline_seconds must be a finite non-negative number.")


DEFAULT_RETRIEVAL_LIMITS = RetrievalLimits(
    max_hits=8,
    max_context_chars=12_000,
    max_query_chars=400,
    deadline_seconds=5.0,
)


def tokenize(text: str) -> tuple[str, ...]:
    """Case- and diacritic-insensitive word tokens (VI base letters)."""

    normalized = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return tuple(_TOKEN_PATTERN.findall(stripped))


class RetrievalHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    version: int = Field(ge=1)
    chunk_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    score: float = Field(ge=0.0)
    preview: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    locator: SourceLocator


class RetrievedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hits: tuple[RetrievalHit, ...]
    total_candidates: int = Field(ge=0)
    truncated_by_budget: bool
    scoped_documents: tuple[str, ...]


class DocumentRetriever(Protocol):
    """Retrieve lexical evidence for one query inside one workspace."""

    async def retrieve(
        self,
        store: Any,
        workspace_id: str,
        query: str,
        document_ids: tuple[str, ...] | None,
        limits: RetrievalLimits,
    ) -> RetrievedEvidence: ...


class _ScoredChunk:
    __slots__ = ("chunk", "document_id", "score", "version")

    def __init__(
        self, document_id: str, version: int, chunk: DocumentChunk, score: float
    ) -> None:
        self.document_id = document_id
        self.version = version
        self.chunk = chunk
        self.score = score


def _bm25_scores(
    candidates: list[tuple[str, int, DocumentChunk]],
    query_tokens: tuple[str, ...],
    deadline: float | None,
) -> list[_ScoredChunk]:
    """Deterministic BM25 ranking; ties broken by (document_id, index)."""

    if deadline is not None:
        start = time.monotonic()

    def check_deadline() -> None:
        if deadline is not None and time.monotonic() - start >= deadline:
            raise RetrievalDeadlineExceededError()

    postings: dict[str, list[int]] = {}
    frequencies: list[dict[str, int]] = []
    lengths: list[int] = []
    for position, (_, _, chunk) in enumerate(candidates):
        check_deadline()
        counts: dict[str, int] = {}
        for token in tokenize(chunk.text):
            counts[token] = counts.get(token, 0) + 1
        frequencies.append(counts)
        lengths.append(sum(counts.values()))
        for token in counts:
            postings.setdefault(token, []).append(position)
    total_chunks = len(candidates)
    if not total_chunks:
        return []
    average_length = sum(lengths) / total_chunks
    query_set = dict.fromkeys(query_tokens)
    scored: list[_ScoredChunk] = []
    for position, (document_id, version, chunk) in enumerate(candidates):
        check_deadline()
        counts = frequencies[position]
        length = lengths[position]
        score = 0.0
        for token in query_set:
            occurrences = counts.get(token)
            if not occurrences:
                continue
            document_frequency = len(postings.get(token, ()))
            inverse = math.log(
                1.0
                + (total_chunks - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            saturation = (
                occurrences
                * (BM25_K1 + 1.0)
                / (
                    occurrences
                    + BM25_K1 * (1.0 - BM25_B + BM25_B * length / average_length)
                )
            )
            score += inverse * saturation
        if score > 0.0:
            scored.append(_ScoredChunk(document_id, version, chunk, score))
    scored.sort(key=lambda item: (-item.score, item.document_id, item.chunk.index))
    return scored


class LexicalRetriever:
    """Scan-and-score BM25 over version-pinned persisted chunks."""

    async def retrieve(
        self,
        store: Any,
        workspace_id: str,
        query: str,
        document_ids: tuple[str, ...] | None,
        limits: RetrievalLimits,
    ) -> RetrievedEvidence:
        if not isinstance(query, str) or not query.strip():
            raise RetrievalValidationError("query must contain 1 to 400 characters")
        if len(query) > limits.max_query_chars:
            raise RetrievalValidationError("query must contain 1 to 400 characters")
        query_tokens = tokenize(query)
        if not query_tokens:
            raise RetrievalValidationError("query contains no searchable words")
        scoped = tuple(document_ids) if document_ids is not None else None
        if scoped is not None and (not scoped or len(set(scoped)) != len(scoped)):
            raise RetrievalValidationError("document scope is invalid")
        candidates = await store.retrievable_chunks(workspace_id, scoped)
        if scoped is not None:
            known = {document_id for document_id, _, _ in candidates}
            unknown = [identifier for identifier in scoped if identifier not in known]
            if unknown:
                # Refuse silent scope shrink: an unknown/deleted/unselected id
                # is a caller bug, not an empty corpus.
                raise RetrievalValidationError("document scope is invalid")
        scored = _bm25_scores(candidates, query_tokens, limits.deadline_seconds)
        hits: list[RetrievalHit] = []
        context_chars = 0
        truncated = False
        for item in scored:
            chunk_chars = item.chunk.char_end - item.chunk.char_start
            if len(hits) >= limits.max_hits:
                truncated = True
                break
            if hits and context_chars + chunk_chars > limits.max_context_chars:
                truncated = True
                break
            hits.append(
                RetrievalHit(
                    document_id=item.document_id,
                    version=item.version,
                    chunk_id=item.chunk.chunk_id,
                    score=round(item.score, 10),
                    preview=item.chunk.text[:PREVIEW_CHARS],
                    char_start=item.chunk.char_start,
                    char_end=item.chunk.char_end,
                    locator=item.chunk.locator,
                )
            )
            context_chars += chunk_chars
        return RetrievedEvidence(
            hits=tuple(hits),
            total_candidates=len(candidates),
            truncated_by_budget=truncated,
            scoped_documents=scoped
            or tuple(dict.fromkeys(document_id for document_id, _, _ in candidates)),
        )


async def resolve_citation(
    store: Any,
    workspace_id: str,
    document_id: str,
    chunk_id: str,
    version: int | None = None,
) -> tuple[Any, DocumentChunk]:
    """Allowlist-validate one citation against persisted rows.

    The chunk must belong to an active document in the *given* workspace,
    its latest version must have a completed extraction, and the persisted
    locator must re-validate. Stale (superseded) versions are a distinct
    closed failure; deleted/cross-workspace/unknown targets are
    indistinguishable not-found.
    """

    from mini_deerflow.documents import DocumentIntegrityError, DocumentNotFoundError

    if not isinstance(chunk_id, str) or not re.fullmatch(r"[0-9a-f]{16}", chunk_id):
        raise CitationNotFoundError() from None
    try:
        document = await store.get_document(workspace_id, document_id)
        latest = document.latest_version
    except DocumentNotFoundError:
        raise CitationNotFoundError() from None
    resolved = latest if version is None else version
    if resolved != latest:
        # Explicit citation of a superseded version.
        raise CitationStaleError()
    try:
        extraction = await store.get_extraction(workspace_id, document_id, resolved)
    except DocumentNotFoundError:
        extraction = None
    chunks: list[DocumentChunk] = []
    if extraction is not None and extraction.status == "completed":
        try:
            chunks = list(await store.list_chunks(workspace_id, document_id, resolved))
        except (DocumentNotFoundError, DocumentIntegrityError, ValueError):
            # Includes malformed persisted locators (pydantic ValueError):
            # allowlist validation failed, not a missing citation.
            raise CitationIntegrityError() from None
    for chunk in chunks:
        if chunk.chunk_id == chunk_id:
            if extraction is None:
                raise CitationIntegrityError() from None
            return extraction, chunk
    # Not citable under the latest completed extraction. If the chunk_id
    # exists under any version of this document, the citation is stale
    # (superseded version, or the latest version has no completed
    # extraction yet); otherwise it is a plain miss.
    try:
        versions = await store.chunk_versions(workspace_id, document_id, chunk_id)
    except Exception:  # noqa: BLE001 - storage failures stay closed
        raise CitationIntegrityError() from None
    if versions:
        raise CitationStaleError()
    raise CitationNotFoundError()


__all__ = [
    "BM25_B",
    "BM25_K1",
    "DEFAULT_RETRIEVAL_LIMITS",
    "PREVIEW_CHARS",
    "CitationIntegrityError",
    "CitationNotFoundError",
    "CitationStaleError",
    "DocumentRetriever",
    "LexicalRetriever",
    "RetrievalDeadlineExceededError",
    "RetrievalError",
    "RetrievalHit",
    "RetrievalLimits",
    "RetrievalValidationError",
    "RetrievedEvidence",
    "resolve_citation",
    "tokenize",
]
