"""Deterministic VI/EN lexical-retrieval benchmark over synthetic documents.

Credential-free, network-free, real-store benchmark for D3 (issue #49 AC1):
builds a synthetic corpus via the real store + extractor, runs fixed VI/EN
query sets with known relevant chunks, and reports recall@k, MRR, and
citation-match rate (top hit resolves against persisted source). Purely
lexical: a paraphrase query set documents what BM25 cannot do rather than
hiding it.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from mini_deerflow.document_extraction import (
    DEFAULT_EXTRACTION_LIMITS,
    EXTRACTOR_VERSION,
    extract_document,
)
from mini_deerflow.document_retrieval import (
    DEFAULT_RETRIEVAL_LIMITS,
    LexicalRetriever,
    resolve_citation,
)
from mini_deerflow.documents import DocumentLimits, DocumentWorkspaceStore

TXT_MIME = "text/plain"

CORPUS = [
    # (filename, text, tags for relevance)
    (
        "energy.txt",
        "Solar panel efficiency improved twenty percent in 2024.\n",
        "solar",
    ),
    (
        "energy2.txt",
        "Wind turbine output depends on wind speed and blade length.\n",
        "wind",
    ),
    (
        "vi-giao-duc.txt",
        "Trường học ở thành phố Hồ Chí Minh áp dụng chương trình mới.\n",
        "truong-hoc",
    ),
    (
        "vi-nong-nghiep.txt",
        "Nông dân ở Đồng bằng sông Cửu Long trồng lúa ba vụ mỗi năm.\n",
        "nong-nd",
    ),
    (
        "health.txt",
        "Regular exercise reduces cardiovascular disease risk.\n",
        "exercise",
    ),
    (
        "history.txt",
        "The Trần dynasty repelled Mongol invasions three times.\n",
        "tran",
    ),
]

# (query, relevant tag) — lexical or diacritic-folded matches.
QUERIES = [
    ("solar panel efficiency", "solar"),
    ("wind turbine blade", "wind"),
    ("truong hoc", "truong-hoc"),
    ("thành phố hồ chí minh", "truong-hoc"),
    ("nong dan trong lua", "nong-nd"),
    ("Đồng bằng sông Cửu Long", "nong-nd"),
    ("cardiovascular exercise risk", "exercise"),
    ("tran dynasty mongol", "tran"),
]

# Documented limitation probes (expected to be MISSED by pure lexical).
PARAPHRASES = [
    ("renewable energy from sunlight", "solar"),
    ("học đường mới", "truong-hoc"),
]


async def _seed(root: Path) -> tuple[DocumentWorkspaceStore, str, dict[str, str]]:
    import hashlib

    store = DocumentWorkspaceStore(
        root, limits=DocumentLimits(max_document_bytes=2_000_000)
    )
    workspace = (await store.create_workspace("Benchmark")).workspace_id
    tags: dict[str, str] = {}
    for filename, text, tag in CORPUS:
        data = text.encode()
        document = await store.upload_document(
            workspace, filename=filename, content_type=TXT_MIME, data=data
        )
        _, chunks, total = extract_document(TXT_MIME, data, DEFAULT_EXTRACTION_LIMITS)
        now = "2026-11-10T00:00:00+00:00"
        from mini_deerflow.document_extraction import ExtractionRecord

        record = ExtractionRecord(
            workspace_id=workspace,
            document_id=document.document_id,
            version=1,
            source_sha256=hashlib.sha256(data).hexdigest(),
            status="completed",
            error_class=None,
            extractor_version=EXTRACTOR_VERSION,
            chunk_count=len(chunks),
            total_chars=total,
            created_at=now,
            updated_at=now,
        )
        await store.save_extraction(record, chunks)
        tags[document.document_id] = tag
    return store, workspace, tags


def _evaluate(store, workspace: str, tags: dict[str, str]) -> dict[str, object]:
    retriever = LexicalRetriever()
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    citation_matches = 0
    rows: list[dict[str, object]] = []
    for query, tag in QUERIES:
        result = asyncio.run(
            retriever.retrieve(store, workspace, query, None, DEFAULT_RETRIEVAL_LIMITS)
        )
        relevant_rank: int | None = None
        for rank, hit in enumerate(result.hits, start=1):
            if tags.get(hit.document_id) == tag:
                relevant_rank = rank
                break
        recalls.append(1.0 if relevant_rank is not None else 0.0)
        reciprocal_ranks.append(1.0 / relevant_rank if relevant_rank else 0.0)
        citation_ok = False
        if result.hits:
            _, chunk = asyncio.run(
                resolve_citation(
                    store,
                    workspace,
                    result.hits[0].document_id,
                    result.hits[0].chunk_id,
                )
            )
            citation_ok = chunk.chunk_id == result.hits[0].chunk_id
        citation_matches += int(citation_ok)
        rows.append(
            {
                "query": query,
                "hits": len(result.hits),
                "relevant_rank": relevant_rank,
                "citation_match": citation_ok,
            }
        )
    paraphrase_rows = []
    for query, tag in PARAPHRASES:
        result = asyncio.run(
            retriever.retrieve(store, workspace, query, None, DEFAULT_RETRIEVAL_LIMITS)
        )
        hit_tags = {tags.get(hit.document_id) for hit in result.hits}
        paraphrase_rows.append(
            {"query": query, "expected_tag": tag, "retrieved_tags": sorted(hit_tags)}
        )
    return {
        "queries": len(QUERIES),
        "recall_at_k": sum(recalls) / len(recalls),
        "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
        "citation_match_rate": citation_matches / len(QUERIES),
        "rows": rows,
        "paraphrase_probes": paraphrase_rows,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="d3-benchmark-", ignore_cleanup_errors=True
    ) as directory:
        store, workspace, tags = asyncio.run(_seed(Path(directory)))
        report = _evaluate(store, workspace, tags)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
